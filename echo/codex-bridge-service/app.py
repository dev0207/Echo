from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PROMPTS_DIR = ROOT / "prompts"
CODEX_BRIDGE_PROMPT_PATH = PROMPTS_DIR / "codex_bridge_system.txt"
BRIDGE_LOG_PATH = DATA_DIR / "bridge-events.jsonl"
DEFAULT_WORKSPACE = Path("/home/ubuntu/hack")
DEFAULT_CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEFAULT_PUBLIC_BASE_URL = os.environ.get("CODEX_BRIDGE_PUBLIC_BASE_URL", "http://127.0.0.1:8800")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "make",
    "needs",
    "of",
    "on",
    "or",
    "our",
    "please",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "this",
    "to",
    "use",
    "was",
    "we",
    "when",
    "with",
    "you",
    "your",
}
KNOWN_TECH_TERMS = {
    "api",
    "asyncio",
    "bash",
    "cli",
    "curl",
    "docker",
    "fastapi",
    "git",
    "http",
    "https",
    "json",
    "postgres",
    "pydantic",
    "python",
    "pytest",
    "redis",
    "shell",
    "sql",
    "typescript",
    "uvicorn",
    "webhook",
}

app = FastAPI(title="Codex Bridge Service", version="0.1.0")


class ExecuteTaskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Natural language work request for Codex.")
    workspace_path: str = Field(default=str(DEFAULT_WORKSPACE))
    timeout_seconds: int = Field(default=1800, ge=30, le=7200)
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    skip_git_repo_check: bool = False
    summary_words: int = Field(default=100, ge=40, le=100)
    public_base_url: str | None = Field(
        default=None,
        description="Base URL Codex can reach to post user-facing messages back to this service.",
    )
    user_webhook_url: str | None = Field(
        default=None,
        description="Optional downstream webhook that receives copied user-facing messages.",
    )
    extra_instructions: str | None = None


class UserMessageRequest(BaseModel):
    task_id: str
    kind: Literal["info", "question", "approval", "warning"] = "info"
    message: str = Field(..., min_length=1)
    expects_response: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskMessage(BaseModel):
    id: str
    task_id: str
    kind: Literal["info", "question", "approval", "warning"]
    message: str
    expects_response: bool
    metadata: dict[str, Any]
    created_at: str
    delivered_to_webhook: bool = False
    webhook_status: str | None = None


class TaskResponseRequest(BaseModel):
    message: str = Field(..., min_length=1)
    in_reply_to: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskResponseRecord(BaseModel):
    id: str
    task_id: str
    message: str
    in_reply_to: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    consumed_at: str | None = None


class TaskRecord(BaseModel):
    task_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    prompt: str
    workspace_path: str
    created_at: str
    completed_at: str | None = None
    codex_command: list[str] = Field(default_factory=list)
    codex_return_code: int | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""
    codex_output: str = ""
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    tech_terms: list[str] = Field(default_factory=list)
    user_webhook_url: str | None = None
    message_count: int = 0
    error: str | None = None


ExecuteTaskRequest.model_rebuild()
UserMessageRequest.model_rebuild()
TaskMessage.model_rebuild()
TaskResponseRequest.model_rebuild()
TaskResponseRecord.model_rebuild()
TaskRecord.model_rebuild()


TASKS: dict[str, TaskRecord] = {}
TASK_MESSAGES: dict[str, list[TaskMessage]] = defaultdict(list)
TASK_RESPONSES: dict[str, list[TaskResponseRecord]] = defaultdict(list)
STORE_LOCK = Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate_for_log(value: Any, max_chars: int = 4000) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return f"{value[:max_chars]}...[truncated {len(value) - max_chars} chars]"
    if isinstance(value, list):
        return [truncate_for_log(item, max_chars=max_chars) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key): truncate_for_log(item, max_chars=max_chars)
            for key, item in list(value.items())[:100]
        }
    return value


def append_bridge_log(event: str, **payload: Any) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = {
        "created_at": utc_now(),
        "event": event,
        **{key: truncate_for_log(value) for key, value in payload.items()},
    }
    with BRIDGE_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def load_prompt_template(path: Path, fallback: str) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return content.strip() or fallback


def get_public_base_url(req: ExecuteTaskRequest, request: FastAPIRequest) -> str:
    if req.public_base_url:
        return req.public_base_url.rstrip("/")
    if DEFAULT_PUBLIC_BASE_URL:
        return DEFAULT_PUBLIC_BASE_URL.rstrip("/")
    return str(request.base_url).rstrip("/")


def build_codex_prompt(req: ExecuteTaskRequest, task_id: str, callback_url: str, response_url: str) -> str:
    inline_callbacks_enabled = os.environ.get("CODEX_BRIDGE_INLINE_CALLBACKS", "0").strip() == "1"
    extra_instructions_text = (req.extra_instructions or "").strip()
    direct_chat_mode = "Interaction mode: direct_chat" in extra_instructions_text
    transport_instructions = (
        f"""
Bridge transport notes:
- The callback URL ({callback_url}) and response URL ({response_url}) are bridge plumbing, not part of the user task.
- Do not send network callbacks or poll for user responses from inside Codex unless the task explicitly says to test bridge transport.
- If you need progress updates, include them in the final report instead.
- If you need user input or approval, state the exact question or approval request in the final report.
- Never reinterpret a request like "send hi to Codex" as "POST hi to the callback endpoint".
""".strip()
        if not inline_callbacks_enabled
        else f"""
Bridge callback rules:
- Send progress, blockers, questions, and approvals to {callback_url}.
- Use Python, not curl.
- Keep callback messages short and factual.
- Use kind=info for progress, kind=approval for approval requests, kind=warning for blockers, kind=question when a user answer is required.

Callback example:
python3 -c "import json, urllib.request; payload={{'task_id':'{task_id}','kind':'question','message':'<short message>','expects_response':True,'metadata':{{'source':'codex'}}}}; req=urllib.request.Request('{callback_url}', data=json.dumps(payload).encode('utf-8'), headers={{'Content-Type':'application/json'}}, method='POST'); print(urllib.request.urlopen(req, timeout=10).read().decode('utf-8'))"

User response polling:
- If you need the user's answer, poll {response_url}.
- Continue when response.message is present.
- If timed_out is true, report that blocker in the final output.

Polling example:
python3 - <<'PY'
import json
import time
import urllib.request

url = "{response_url}"
response = None
deadline = time.time() + 900
while time.time() < deadline and response is None:
    data = json.loads(urllib.request.urlopen(url, timeout=10).read().decode("utf-8"))
    response = data.get("response")
    if response is None:
        time.sleep(5)

print(json.dumps(response or {{"message": "", "timed_out": True}}))
PY
""".strip()
    )
    fallback_template = """
You are Codex running behind a FastAPI bridge for engineering work.

Role:
- Operate as a developer, not a chat assistant.
- Be brief, concrete, and execution-focused.
- Be quick. Prefer a single-shot answer.
- The upstream request is authoritative. Execute it directly.
- Do not invent a different task unless safety or a hard blocker requires it.
- There is no room for exploratory back-and-forth, open questions, or optional queries.
- Do not ask the user follow-up questions unless a true hard blocker makes execution impossible.
- If something is missing, make the best reasonable assumption and continue.
- If a hard blocker exists, report it directly in the final output instead of trying to start a conversation.

Task metadata:
- Task ID: {{TASK_ID}}
- Workspace path: {{WORKSPACE_PATH}}

Primary request:
{{PRIMARY_REQUEST}}

{{TRANSPORT_INSTRUCTIONS}}

Final output:
- Return a short technical report with sections: Outcome, Files Changed, Commands Run, Validation, Risks, Next Steps.
- Include the real result or blocker. Do not claim success if the work did not actually finish.

Additional instructions:
{{EXTRA_INSTRUCTIONS}}
""".strip()
    direct_chat_template = """
You are Codex replying directly to a user's literal message through a bridge.

Role:
- Reply directly to the user's message.
- Be brief, natural, and truthful.
- Do not reinterpret the message as an engineering task, debugging task, or bridge operation.
- Do not talk about callback plumbing unless the user explicitly asked.

Task metadata:
- Task ID: {{TASK_ID}}
- Workspace path: {{WORKSPACE_PATH}}

User message:
{{PRIMARY_REQUEST}}

{{TRANSPORT_INSTRUCTIONS}}

Final output:
- Return only your direct reply to the user.

Additional instructions:
{{EXTRA_INSTRUCTIONS}}
""".strip()
    template = (
        direct_chat_template
        if direct_chat_mode
        else load_prompt_template(CODEX_BRIDGE_PROMPT_PATH, fallback_template)
    )
    replacements = {
        "{{TASK_ID}}": task_id,
        "{{WORKSPACE_PATH}}": req.workspace_path,
        "{{PRIMARY_REQUEST}}": req.prompt.strip(),
        "{{CALLBACK_URL}}": callback_url,
        "{{RESPONSE_URL}}": response_url,
        "{{TRANSPORT_INSTRUCTIONS}}": transport_instructions,
        "{{EXTRA_INSTRUCTIONS}}": (req.extra_instructions or "None.").strip(),
    }
    prompt = template
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    append_bridge_log(
        "prompt.built",
        task_id=task_id,
        workspace_path=req.workspace_path,
        prompt_path=str(CODEX_BRIDGE_PROMPT_PATH),
        primary_request=req.prompt,
        extra_instructions=req.extra_instructions or "",
        callback_url=callback_url,
        response_url=response_url,
        rendered_prompt=prompt,
    )
    return prompt


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_./:-]{2,}", text)


def extract_keywords(prompt: str, output: str, limit: int = 8) -> list[str]:
    counter: Counter[str] = Counter()
    for token in tokenize(f"{prompt} {output}"):
        normalized = token.strip(".,:;()[]{}<>").lower()
        if normalized in STOPWORDS or normalized.isdigit():
            continue
        if len(normalized) < 4:
            continue
        counter[normalized] += 1
    return [token for token, _ in counter.most_common(limit)]


def extract_tech_terms(prompt: str, output: str, limit: int = 8) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in tokenize(f"{prompt} {output}"):
        cleaned = raw.strip(".,:;()[]{}<>")
        lowered = cleaned.lower()
        looks_technical = (
            lowered in KNOWN_TECH_TERMS
            or "/" in cleaned
            or "." in cleaned
            or "-" in cleaned
            or cleaned.isupper()
            or any(ch.isdigit() for ch in cleaned)
        )
        if looks_technical and lowered not in seen:
            found.append(cleaned)
            seen.add(lowered)
        if len(found) >= limit:
            break
    return found


def compress_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(",.;:") + "."


def build_summary(prompt: str, output: str, success: bool, max_words: int) -> str:
    cleaned_output = compress_whitespace(output)
    if not cleaned_output:
        cleaned_output = (
            "Codex process exited successfully, but no final report was captured."
            if success
            else "Codex returned no final message."
        )

    outcome = (
        "completed successfully"
        if success and compress_whitespace(output)
        else "finished without a captured final report"
        if success
        else "did not complete successfully"
    )

    summary = (
        f"Task {outcome}. Request: {compress_whitespace(prompt)}. "
        f"Result: {cleaned_output}"
    )
    return trim_words(summary, max_words)


def parse_jsonl_events(raw_stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


TEXT_VALUE_KEYS = {
    "content",
    "delta",
    "message",
    "output_text",
    "response",
    "summary",
    "text",
}
TEXT_CONTAINER_KEYS = {
    "content",
    "contents",
    "item",
    "items",
    "message",
    "messages",
    "output",
    "outputs",
    "part",
    "parts",
    "payload",
    "response",
    "result",
}


def collect_text_fragments(payload: Any, *, parent_key: str = "") -> list[str]:
    fragments: list[str] = []

    if isinstance(payload, str):
        cleaned = payload.strip()
        if cleaned and parent_key in TEXT_VALUE_KEYS:
            fragments.append(cleaned)
        return fragments

    if isinstance(payload, list):
        for item in payload:
            fragments.extend(collect_text_fragments(item, parent_key=parent_key))
        return fragments

    if not isinstance(payload, dict):
        return fragments

    for key, value in payload.items():
        lowered = str(key).strip().lower()
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and lowered in TEXT_VALUE_KEYS:
                fragments.append(cleaned)
            continue
        if isinstance(value, (dict, list)) and lowered in TEXT_CONTAINER_KEYS:
            fragments.extend(collect_text_fragments(value, parent_key=lowered))

    return fragments


def dedupe_full_messages(chunks: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        normalized = compress_whitespace(chunk)
        if not normalized or normalized in seen:
            continue
        deduped.append(chunk)
        seen.add(normalized)
    return deduped


def read_fallback_output(fallback_path: Path) -> str:
    if not fallback_path.exists():
        return ""

    content = fallback_path.read_text(encoding="utf-8").strip()
    if not content:
        return ""

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content

    fragments = dedupe_full_messages(collect_text_fragments(parsed))
    if fragments:
        return compress_whitespace(" ".join(fragments))
    if isinstance(parsed, str):
        return compress_whitespace(parsed)
    return content


def extract_agent_output(raw_stdout: str, fallback_path: Path) -> str:
    fallback_output = read_fallback_output(fallback_path)
    if fallback_output:
        return fallback_output

    events = parse_jsonl_events(raw_stdout)
    final_chunks: list[str] = []
    delta_chunks: list[str] = []
    for event in events:
        event_type = str(event.get("type", "")).strip().lower()
        fragments = dedupe_full_messages(
            collect_text_fragments(
                {
                    "text": event.get("text"),
                    "delta": event.get("delta"),
                    "message": event.get("message"),
                    "content": event.get("content"),
                    "item": event.get("item"),
                    "output": event.get("output"),
                    "result": event.get("result"),
                    "response": event.get("response"),
                    "payload": event.get("payload"),
                }
            )
        )
        if not fragments:
            continue
        if "delta" in event_type or event.get("delta") is not None:
            delta_chunks.extend(fragments)
        else:
            final_chunks.extend(fragments)

    chunks = dedupe_full_messages(final_chunks) or delta_chunks
    return compress_whitespace(" ".join(chunks))


def build_codex_command(req: ExecuteTaskRequest, prompt: str, output_path: Path) -> list[str]:
    command = [
        DEFAULT_CODEX_BIN,
        "exec",
        "--json",
        "--full-auto",
        "--ephemeral",
        "--sandbox",
        req.sandbox,
        "-C",
        req.workspace_path,
        "-o",
        str(output_path),
        prompt,
    ]
    if req.skip_git_repo_check:
        command.insert(2, "--skip-git-repo-check")
    return command


async def deliver_to_webhook(webhook_url: str, message: TaskMessage) -> str:
    payload = message.model_dump()

    def _send() -> str:
        request = Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return f"{response.status} {response.reason}"

    try:
        status = await asyncio.to_thread(_send)
        append_bridge_log(
            "webhook.delivered",
            task_id=message.task_id,
            message_id=message.id,
            webhook_url=webhook_url,
            status=status,
        )
        return status
    except HTTPError as exc:
        append_bridge_log(
            "webhook.http_error",
            task_id=message.task_id,
            message_id=message.id,
            webhook_url=webhook_url,
            status_code=exc.code,
        )
        return f"http_error:{exc.code}"
    except URLError as exc:
        append_bridge_log(
            "webhook.url_error",
            task_id=message.task_id,
            message_id=message.id,
            webhook_url=webhook_url,
            reason=str(exc.reason),
        )
        return f"url_error:{exc.reason}"
    except Exception as exc:  # pragma: no cover
        append_bridge_log(
            "webhook.exception",
            task_id=message.task_id,
            message_id=message.id,
            webhook_url=webhook_url,
            error=f"{type(exc).__name__}: {exc}",
        )
        return f"error:{type(exc).__name__}"


async def emit_task_message(
    task_id: str,
    *,
    kind: Literal["info", "question", "approval", "warning"],
    message: str,
    expects_response: bool = False,
    metadata: dict[str, Any] | None = None,
) -> TaskMessage:
    task = get_task_or_404(task_id)
    payload = TaskMessage(
        id=str(uuid4()),
        task_id=task_id,
        kind=kind,
        message=message.strip(),
        expects_response=expects_response,
        metadata=metadata or {},
        created_at=utc_now(),
    )
    webhook_status: str | None = None
    delivered = False
    if task.user_webhook_url:
        webhook_status = await deliver_to_webhook(task.user_webhook_url, payload)
        delivered = webhook_status.startswith("2")
    if webhook_status:
        payload.webhook_status = webhook_status
        payload.delivered_to_webhook = delivered
    with STORE_LOCK:
        TASK_MESSAGES[task_id].append(payload)
        current = TASKS[task_id]
        TASKS[task_id] = current.model_copy(update={"message_count": len(TASK_MESSAGES[task_id])})
    append_bridge_log(
        "task.message",
        task_id=task_id,
        kind=kind,
        expects_response=expects_response,
        metadata=metadata or {},
        message=message,
        webhook_status=webhook_status,
    )
    return payload


def ensure_workspace_exists(workspace_path: str) -> None:
    path = Path(workspace_path)
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Workspace does not exist: {workspace_path}")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Workspace is not a directory: {workspace_path}")


def store_task(task: TaskRecord) -> None:
    with STORE_LOCK:
        TASKS[task.task_id] = task


def get_task_or_404(task_id: str) -> TaskRecord:
    with STORE_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown task_id: {task_id}")
    return task


async def run_codex_task(task_id: str, req: ExecuteTaskRequest, codex_prompt: str) -> None:
    current = get_task_or_404(task_id)
    current = current.model_copy(update={"status": "running"})
    store_task(current)

    with tempfile.NamedTemporaryFile(prefix=f"codex-last-message-{task_id}-", delete=False) as handle:
        output_path = Path(handle.name)

    command = build_codex_command(req, codex_prompt, output_path)
    current = current.model_copy(update={"codex_command": command})
    store_task(current)
    append_bridge_log(
        "task.started",
        task_id=task_id,
        workspace_path=req.workspace_path,
        timeout_seconds=req.timeout_seconds,
        sandbox=req.sandbox,
        command=command,
        output_path=str(output_path),
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=req.workspace_path,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=req.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            append_bridge_log(
                "task.timeout",
                task_id=task_id,
                timeout_seconds=req.timeout_seconds,
                command=command,
            )
            current = get_task_or_404(task_id).model_copy(
                update={
                    "status": "failed",
                    "completed_at": utc_now(),
                    "error": f"Codex timed out after {req.timeout_seconds}s",
                }
            )
            store_task(current)
            await emit_task_message(
                task_id,
                kind="warning",
                message=f"Task timed out after {req.timeout_seconds} seconds.",
                metadata={"source": "bridge", "event": "task.failed"},
            )
            return

        raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
        raw_stderr = stderr_bytes.decode("utf-8", errors="replace")
        codex_output = extract_agent_output(raw_stdout, output_path)
        success = process.returncode == 0
        resolved_output = codex_output or (compress_whitespace(raw_stderr) if not success else "")
        summary = build_summary(req.prompt, resolved_output, success, req.summary_words)
        keywords = extract_keywords(req.prompt, resolved_output)
        tech_terms = extract_tech_terms(req.prompt, resolved_output)
        append_bridge_log(
            "task.completed",
            task_id=task_id,
            success=success,
            return_code=process.returncode,
            stdout_len=len(raw_stdout),
            stderr_len=len(raw_stderr),
            extracted_output=codex_output,
            resolved_output=resolved_output,
            summary=summary,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )

        current = get_task_or_404(task_id).model_copy(
            update={
                "status": "succeeded" if success else "failed",
                "completed_at": utc_now(),
                "codex_return_code": process.returncode,
                "raw_stdout": raw_stdout,
                "raw_stderr": raw_stderr,
                "codex_output": resolved_output,
                "summary": summary,
                "keywords": keywords,
                "tech_terms": tech_terms,
                "message_count": len(TASK_MESSAGES[task_id]),
                "error": None if success else (raw_stderr.strip() or "Codex exited with a non-zero status."),
            }
        )
        store_task(current)
        await emit_task_message(
            task_id,
            kind="info" if success else "warning",
            message=summary,
            metadata={"source": "bridge", "event": "task.completed" if success else "task.failed"},
        )
    except Exception as exc:
        append_bridge_log(
            "task.exception",
            task_id=task_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        current = get_task_or_404(task_id).model_copy(
            update={
                "status": "failed",
                "completed_at": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        store_task(current)
        await emit_task_message(
            task_id,
            kind="warning",
            message=f"Task failed due to {type(exc).__name__}: {exc}",
            metadata={"source": "bridge", "event": "task.failed"},
        )
    finally:
        output_path.unlink(missing_ok=True)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "codex-bridge-service",
        "codex_bin": DEFAULT_CODEX_BIN,
        "default_workspace": str(DEFAULT_WORKSPACE),
    }


@app.post("/api/v1/tasks/execute")
async def execute_task(req: ExecuteTaskRequest, request: FastAPIRequest) -> dict[str, Any]:
    ensure_workspace_exists(req.workspace_path)

    task_id = str(uuid4())
    public_base_url = get_public_base_url(req, request)
    callback_url = f"{public_base_url}/api/v1/user-messages"
    response_url = f"{public_base_url}/api/v1/tasks/{task_id}/responses/next"
    append_bridge_log(
        "task.execute.request",
        task_id=task_id,
        client=str(request.client) if request.client else "",
        workspace_path=req.workspace_path,
        timeout_seconds=req.timeout_seconds,
        sandbox=req.sandbox,
        public_base_url=public_base_url,
        user_webhook_url=req.user_webhook_url or "",
        prompt=req.prompt,
        extra_instructions=req.extra_instructions or "",
    )
    codex_prompt = build_codex_prompt(req, task_id, callback_url, response_url)

    record = TaskRecord(
        task_id=task_id,
        status="queued",
        prompt=req.prompt,
        workspace_path=req.workspace_path,
        created_at=utc_now(),
        user_webhook_url=req.user_webhook_url,
    )
    store_task(record)
    asyncio.create_task(run_codex_task(task_id, req, codex_prompt))
    append_bridge_log(
        "task.execute.accepted",
        task_id=task_id,
        poll_url=f"{str(request.base_url).rstrip('/')}/api/v1/tasks/{task_id}",
    )

    return {
        "task_id": task_id,
        "status": "queued",
        "poll_url": f"{str(request.base_url).rstrip('/')}/api/v1/tasks/{task_id}",
        "messages_url": f"{str(request.base_url).rstrip('/')}/api/v1/tasks/{task_id}/messages",
        "responses_url": f"{str(request.base_url).rstrip('/')}/api/v1/tasks/{task_id}/responses",
    }


@app.post("/api/v1/user-messages")
async def receive_user_message(payload: UserMessageRequest) -> dict[str, Any]:
    with STORE_LOCK:
        task = TASKS.get(payload.task_id)

    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown task_id: {payload.task_id}")

    message = TaskMessage(
        id=str(uuid4()),
        task_id=payload.task_id,
        kind=payload.kind,
        message=payload.message.strip(),
        expects_response=payload.expects_response,
        metadata=payload.metadata,
        created_at=utc_now(),
    )

    webhook_status: str | None = None
    delivered = False
    if task.user_webhook_url:
        webhook_status = await deliver_to_webhook(task.user_webhook_url, message)
        delivered = webhook_status.startswith("2")

    if webhook_status:
        message.webhook_status = webhook_status
        message.delivered_to_webhook = delivered

    with STORE_LOCK:
        TASK_MESSAGES[payload.task_id].append(message)
        current = TASKS[payload.task_id]
        TASKS[payload.task_id] = current.model_copy(
            update={"message_count": len(TASK_MESSAGES[payload.task_id])}
        )
    append_bridge_log(
        "task.user_message.received",
        task_id=payload.task_id,
        kind=payload.kind,
        expects_response=payload.expects_response,
        metadata=payload.metadata,
        message=payload.message,
        webhook_status=webhook_status,
    )

    return {"ok": True, "message": message.model_dump()}


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = get_task_or_404(task_id)
    return task.model_dump()


@app.get("/api/v1/tasks/{task_id}/messages")
def get_task_messages(task_id: str) -> dict[str, Any]:
    with STORE_LOCK:
        task = TASKS.get(task_id)
        messages = list(TASK_MESSAGES.get(task_id, []))
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown task_id: {task_id}")
    return {
        "task_id": task_id,
        "count": len(messages),
        "messages": [message.model_dump() for message in messages],
    }


@app.post("/api/v1/tasks/{task_id}/responses")
def submit_task_response(task_id: str, payload: TaskResponseRequest) -> dict[str, Any]:
    _ = get_task_or_404(task_id)
    response = TaskResponseRecord(
        id=str(uuid4()),
        task_id=task_id,
        message=payload.message.strip(),
        in_reply_to=payload.in_reply_to,
        metadata=payload.metadata,
        created_at=utc_now(),
    )
    with STORE_LOCK:
        TASK_RESPONSES[task_id].append(response)
    append_bridge_log(
        "task.response.submitted",
        task_id=task_id,
        response_id=response.id,
        in_reply_to=payload.in_reply_to or "",
        metadata=payload.metadata,
        message=payload.message,
    )
    return {"ok": True, "response": response.model_dump()}


@app.get("/api/v1/tasks/{task_id}/responses")
def get_task_responses(task_id: str) -> dict[str, Any]:
    _ = get_task_or_404(task_id)
    with STORE_LOCK:
        responses = list(TASK_RESPONSES.get(task_id, []))
    return {
        "task_id": task_id,
        "count": len(responses),
        "responses": [response.model_dump() for response in responses],
    }


@app.get("/api/v1/tasks/{task_id}/responses/next")
def get_next_task_response(task_id: str) -> dict[str, Any]:
    _ = get_task_or_404(task_id)
    with STORE_LOCK:
        responses = TASK_RESPONSES.get(task_id, [])
        for index, response in enumerate(responses):
            if response.consumed_at is None:
                consumed = response.model_copy(update={"consumed_at": utc_now()})
                responses[index] = consumed
                append_bridge_log(
                    "task.response.consumed",
                    task_id=task_id,
                    response_id=consumed.id,
                    in_reply_to=consumed.in_reply_to or "",
                )
                return {"ok": True, "response": consumed.model_dump()}
    append_bridge_log("task.response.empty", task_id=task_id)
    return {"ok": True, "response": None}
