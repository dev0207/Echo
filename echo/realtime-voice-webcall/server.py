#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import base64
import re
from datetime import datetime, timezone
from html import escape as xml_escape
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from account_service import AccountService

ROOT = Path(__file__).resolve().parent
EXPORTED_FRONTEND_DIR = ROOT / "out"
DATA_DIR = ROOT / "data"
PROMPTS_DIR = ROOT / "prompts"
STATE_PATH = DATA_DIR / "session-store.json"
AUTH_STATE_PATH = DATA_DIR / "account-store.json"
ROUTING_AUDIT_PATH = DATA_DIR / "routing-decisions.jsonl"
WEBCALL_EVENT_LOG_PATH = DATA_DIR / "webcall-events.jsonl"
INSTANCE_REGISTRY_PATH = ROOT.parent / "instance-registry.json"
SERVER_MAPPING_PATH = ROOT.parent / "docs" / "server-mapping.md"
CODEX_BRIDGE_PATH = ROOT.parent / "codex-bridge-service.md"
ORCHESTRATOR_PROMPT_PATH = PROMPTS_DIR / "orchestrator_system.txt"
VOICE_AGENT_PROMPT_PATH = PROMPTS_DIR / "voice_agent_system.txt"
OPENAI_API_BASE = "https://api.openai.com/v1"
WEBCALL_PUBLIC_BASE_URL = os.environ.get("WEBCALL_PUBLIC_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
TWILIO_CONTINUE_PROMPT = (
    "You can say another request, ask for task status, or say hang up."
)
BRIDGE_MESSAGE_MIRROR: dict[str, list[dict]] = {}
BRIDGE_STORE_LOCK = Lock()
PERSISTED_STATE_LOCK = Lock()
INSTANCE_HEALTH_LOCK = Lock()
INSTANCE_HEALTH_STATE: dict[str, dict] = {}
INSTANCE_HEALTH_INTERVAL_SECONDS = 5
DEFAULT_INSTANCE_REGISTRY = [
    {
        "instance_id": "hack",
        "host_alias": "hack",
        "label": "Hack Server",
        "role": "orchestration_and_backend",
        "summary": "Central control plane for orchestration, backend logic, API handling, and Codex dispatch preparation.",
        "workspace_path": "/home/ubuntu/hack",
        "bridge_base_url": "http://127.0.0.1:8800",
        "bridge_execute_endpoint": "/api/v1/tasks/execute",
    },
    {
        "instance_id": "STT-A10",
        "host_alias": "STT-A10",
        "label": "STT-A10",
        "role": "speech_to_text",
        "summary": "Speech-to-text and diarization worker for incoming audio processing.",
        "workspace_path": "",
        "bridge_base_url": "",
        "bridge_execute_endpoint": "",
    },
    {
        "instance_id": "TTS-H100",
        "host_alias": "TTS-H100",
        "label": "TTS-H100",
        "role": "text_to_speech",
        "summary": "Text-to-speech worker for voice generation and audio synthesis.",
        "workspace_path": "",
        "bridge_base_url": "",
        "bridge_execute_endpoint": "",
    },
]


def load_instance_registry(default_registry: list[dict]) -> list[dict]:
    if not INSTANCE_REGISTRY_PATH.exists():
        return default_registry
    try:
        payload = json.loads(INSTANCE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_registry
    if not isinstance(payload, list):
        return default_registry

    normalized: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if not item.get("instance_id"):
            continue
        normalized.append(item)
    return normalized or default_registry


INSTANCE_REGISTRY = load_instance_registry(DEFAULT_INSTANCE_REGISTRY)
INSTANCE_REGISTRY_MTIME_NS: int | None = None


def refresh_instance_registry_if_needed(force: bool = False) -> list[dict]:
    global INSTANCE_REGISTRY, INSTANCE_REGISTRY_MTIME_NS

    try:
        mtime_ns = INSTANCE_REGISTRY_PATH.stat().st_mtime_ns
    except OSError:
        if force:
            INSTANCE_REGISTRY = load_instance_registry(DEFAULT_INSTANCE_REGISTRY)
            INSTANCE_REGISTRY_MTIME_NS = None
        return INSTANCE_REGISTRY

    if not force and INSTANCE_REGISTRY_MTIME_NS == mtime_ns:
        return INSTANCE_REGISTRY

    INSTANCE_REGISTRY = load_instance_registry(DEFAULT_INSTANCE_REGISTRY)
    INSTANCE_REGISTRY_MTIME_NS = mtime_ns
    return INSTANCE_REGISTRY


refresh_instance_registry_if_needed(force=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_whitespace(value: str) -> str:
    return " ".join(str(value or "").split())


def truncate_for_log(value: Any, max_chars: int = 4000) -> Any:
    if isinstance(value, str):
        compacted = value if "\n" in value else compact_whitespace(value)
        if len(compacted) <= max_chars:
            return compacted
        return f"{compacted[:max_chars]}...[truncated {len(compacted) - max_chars} chars]"
    if isinstance(value, list):
        return [truncate_for_log(item, max_chars=max_chars) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key): truncate_for_log(item, max_chars=max_chars)
            for key, item in list(value.items())[:100]
        }
    return value


def append_webcall_event(event: str, **payload: Any) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = {
        "created_at": utc_now(),
        "event": event,
        **{key: truncate_for_log(value) for key, value in payload.items()},
    }
    with WEBCALL_EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def load_prompt_template(path: Path, fallback: str) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return content.strip() or fallback


def render_prompt_template(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def parse_codex_jsonl_events(raw_stdout: str) -> list[dict]:
    events: list[dict] = []
    for line in str(raw_stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def extract_codex_output_from_raw_stdout(raw_stdout: str) -> str:
    text_chunks: list[str] = []
    for event in parse_codex_jsonl_events(raw_stdout):
        event_type = str(event.get("type", "")).strip()
        if event_type == "agent_message_delta" and event.get("delta"):
            text_chunks.append(str(event["delta"]))
            continue
        if event_type != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            text_chunks.append(str(item["text"]))
            continue
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                text_chunks.append(str(content["text"]))
    return compact_whitespace(" ".join(text_chunks))


def normalize_bridge_task_payload(bridge_payload: dict) -> dict:
    payload = dict(bridge_payload or {})
    raw_stdout = str(payload.get("raw_stdout", "") or "")
    codex_output = compact_whitespace(payload.get("codex_output", ""))
    if not codex_output and raw_stdout:
        codex_output = extract_codex_output_from_raw_stdout(raw_stdout)
    if codex_output:
        payload["codex_output"] = codex_output

    summary = str(payload.get("summary", "") or "").strip()
    summary_lc = summary.lower()
    fallback_summary = (
        not summary
        or "codex returned no final message" in summary_lc
        or "no final report was captured" in summary_lc
    )
    status = str(payload.get("status", "")).strip().lower()
    prompt = compact_whitespace(payload.get("prompt", ""))
    downstream_failure = extract_downstream_failure_reason(codex_output)
    if status == "succeeded" and codex_output and downstream_failure:
        if prompt:
            payload["summary"] = (
                "Task completed, but the downstream Codex execution failed. "
                f"Request: {prompt}. Failure reason: {downstream_failure}. "
                f"Raw Codex output: {codex_output}"
            )
        else:
            payload["summary"] = (
                "Task completed, but the downstream Codex execution failed. "
                f"Failure reason: {downstream_failure}. Raw Codex output: {codex_output}"
            )
        return payload
    if codex_output and fallback_summary:
        if status == "succeeded":
            outcome = "Task completed successfully."
        elif status == "failed":
            outcome = "Task did not complete successfully."
        else:
            outcome = f"Task status is {status or 'unknown'}."
        if prompt:
            payload["summary"] = f"{outcome} Request: {prompt}. Result: {codex_output}"
        else:
            payload["summary"] = f"{outcome} Result: {codex_output}"
    return payload


def extract_downstream_failure_reason(text: str) -> str:
    normalized = compact_whitespace(text)
    if not normalized:
        return ""

    lowered = normalized.lower()
    failure_markers = (
        "final status: failed",
        "finished with status `failed`",
        "finished with status failed",
        '"status":"failed"',
        '"status": "failed"',
        "status `failed`",
        "status failed",
        "task then ran and finished with status `failed`",
        "task then ran and finished with status failed",
    )
    if not any(marker in lowered for marker in failure_markers):
        return ""

    for pattern in (
        r"Failure reason:\s*(.+?)(?:\*\*|Next Steps|Risks|Validation|Commands Run|Files Changed|$)",
        r"Failure reason:\s*(.+)$",
    ):
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return compact_whitespace(match.group(1))
    return "The downstream Codex execution failed."


def normalize_account_context(account: dict | None) -> dict:
    if not account:
        return {}

    aws_connections = account.get("awsConnections") or []
    normalized_aws = []
    for item in aws_connections:
        if not isinstance(item, dict):
            continue
        normalized_aws.append(
            {
                "id": str(item.get("id", "")).strip(),
                "label": str(item.get("label", "")).strip(),
                "instance_id": str(item.get("instanceId", "")).strip(),
                "region": str(item.get("region", "")).strip(),
                "host": str(item.get("host", "")).strip(),
                "verified": bool(item.get("verified")),
                "verification_reason": str(item.get("verificationReason", "")).strip(),
            }
        )

    github = account.get("github") or {}
    return {
        "account_id": str(account.get("id", "")).strip(),
        "account_name": str(account.get("name", "")).strip(),
        "account_email": str(account.get("email", "")).strip(),
        "phone_verified": bool(account.get("phoneVerified")),
        "github_username": str(github.get("username", "")).strip(),
        "connected_aws_instances": [item["instance_id"] for item in normalized_aws if item["instance_id"]],
        "aws_connections": normalized_aws,
    }


def merge_request_context(raw_context: Any, cookie_header: str | None) -> dict:
    context = raw_context.copy() if isinstance(raw_context, dict) else {}
    account_context = normalize_account_context(ACCOUNT_SERVICE.current_user(cookie_header))

    for key, value in account_context.items():
        if key == "aws_connections":
            existing = context.get(key)
            if isinstance(existing, list) and existing:
                continue
            context[key] = value
            continue

        if key == "connected_aws_instances":
            existing = context.get(key)
            if isinstance(existing, list) and existing:
                continue
            if isinstance(existing, str) and existing.strip():
                continue
            context[key] = value
            continue

        existing = context.get(key)
        if existing in (None, "", [], False):
            context[key] = value

    return context


def merge_phone_request_context(raw_context: Any, phone: str | None) -> dict:
    context = raw_context.copy() if isinstance(raw_context, dict) else {}
    account_context = normalize_account_context(ACCOUNT_SERVICE.user_by_phone(phone or ""))
    for key, value in account_context.items():
        existing = context.get(key)
        if key in {"aws_connections", "connected_aws_instances"} and existing:
            continue
        if existing in (None, "", [], False):
            context[key] = value
    return context


def default_state() -> dict:
    return {"sessions": {}, "tasks": {}}


def load_persisted_state() -> dict:
    if not STATE_PATH.exists():
        return default_state()
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()
    if not isinstance(payload, dict):
        return default_state()
    payload.setdefault("sessions", {})
    payload.setdefault("tasks", {})
    return payload


PERSISTED_STATE = load_persisted_state()
ACCOUNT_SERVICE = AccountService(AUTH_STATE_PATH)


def save_persisted_state_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(PERSISTED_STATE, indent=2, sort_keys=True), encoding="utf-8")


def session_record(session_id: str) -> dict:
    now = utc_now()
    return {
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
        "caller_phone": "",
        "last_twilio_call_sid": "",
        "active_task_id": "",
        "last_task_id": "",
        "last_user_request": "",
        "pending_twilio_mode": "",
        "pending_twilio_prompt": "",
        "pending_twilio_task_id": "",
        "pending_twilio_message_id": "",
        "pending_routing": None,
        "tasks": [],
    }


def task_record(task_id: str, session_id: str = "") -> dict:
    now = utc_now()
    return {
        "task_id": task_id,
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
        "request_text": "",
        "target_instance_id": "",
        "status": "queued",
        "routing": {},
        "dispatch": {},
        "latest_message": None,
        "messages": [],
        "notify_phone": "",
        "notified_message_ids": [],
        "twilio_calls": [],
        "summary": "",
        "caller_summary": "",
        "codex_output": "",
        "raw_stdout": "",
        "raw_stderr": "",
        "completed_at": "",
        "error": "",
    }


def ensure_session_locked(session_id: str) -> dict:
    sessions = PERSISTED_STATE.setdefault("sessions", {})
    if session_id not in sessions:
        sessions[session_id] = session_record(session_id)
    return sessions[session_id]


def ensure_task_locked(task_id: str, session_id: str = "") -> dict:
    tasks = PERSISTED_STATE.setdefault("tasks", {})
    if task_id not in tasks:
        tasks[task_id] = task_record(task_id, session_id=session_id)
    if session_id and not tasks[task_id].get("session_id"):
        tasks[task_id]["session_id"] = session_id
    return tasks[task_id]


def build_caller_summary(task: dict) -> str:
    status = str(task.get("status", "")).strip() or "unknown"
    latest_message = task.get("latest_message") or {}
    summary = str(task.get("summary", "")).strip()
    error = str(task.get("error", "")).strip()
    request_text = compact_whitespace(task.get("request_text", ""))
    codex_output = compact_whitespace(task.get("codex_output", ""))
    if not codex_output:
        codex_output = extract_codex_output_from_raw_stdout(task.get("raw_stdout", ""))
    downstream_failure = extract_downstream_failure_reason(codex_output)

    if status in {"queued", "running"}:
        if latest_message.get("message"):
            return f"Task is {status}. Latest update: {latest_message['message']}"
        return f"Task is {status}."
    if status == "succeeded":
        if codex_output:
            if downstream_failure:
                if request_text:
                    return (
                        "Task is done, but the downstream Codex execution failed. "
                        f"Request: {request_text}. Failure reason: {downstream_failure} "
                        f"Raw Codex output: {codex_output}"
                    )
                return (
                    "Task is done, but the downstream Codex execution failed. "
                    f"Failure reason: {downstream_failure} Raw Codex output: {codex_output}"
                )
            if request_text:
                return (
                    "Task is done. "
                    f"Task completed successfully. Request: {request_text}. Result: {codex_output}"
                )
            return f"Task is done. Result: {codex_output}"
        return f"Task is done. {summary}" if summary else "Task is done."
    if status == "failed":
        if error:
            if codex_output:
                return f"Task failed. {error} Result: {codex_output}"
            return f"Task failed. {error}"
        if latest_message.get("message"):
            return f"Task failed. Latest update: {latest_message['message']}"
        return "Task failed."
    return "Task status is currently unknown."


def update_task_summary_locked(task: dict) -> None:
    task["caller_summary"] = build_caller_summary(task)
    task["updated_at"] = utc_now()


def register_dispatch_state(session_id: str, request_text: str, routing: dict, dispatch_payload: dict) -> None:
    task = dispatch_payload.get("task") or {}
    task_id = str(task.get("task_id", "")).strip()
    if not task_id:
        return

    with PERSISTED_STATE_LOCK:
        session = ensure_session_locked(session_id)
        record = ensure_task_locked(task_id, session_id=session_id)
        record["request_text"] = request_text
        record["target_instance_id"] = routing.get("target_instance_id", "")
        record["routing"] = routing
        record["dispatch"] = dispatch_payload
        record["status"] = task.get("status", "queued")
        if session.get("caller_phone"):
            record["notify_phone"] = session.get("caller_phone", "")
        update_task_summary_locked(record)

        if task_id not in session["tasks"]:
            session["tasks"].append(task_id)
        session["active_task_id"] = task_id
        session["last_task_id"] = task_id
        session["last_user_request"] = request_text
        session["updated_at"] = utc_now()
        save_persisted_state_locked()


def append_task_message(task_id: str, message: dict) -> None:
    with PERSISTED_STATE_LOCK:
        existing = PERSISTED_STATE.setdefault("tasks", {}).get(task_id, {})
        record = ensure_task_locked(task_id, session_id=existing.get("session_id", ""))
        message_id = str(message.get("id", "")).strip()
        known_ids = {str(item.get("id", "")) for item in record["messages"]}
        if not message_id or message_id not in known_ids:
            record["messages"].append(message)
        record["latest_message"] = message
        update_task_summary_locked(record)

        session_id = record.get("session_id", "")
        if session_id:
            session = ensure_session_locked(session_id)
            session["last_task_id"] = task_id
            session["updated_at"] = utc_now()
        save_persisted_state_locked()


def sync_task_from_bridge_payload(task_id: str, bridge_payload: dict) -> dict:
    bridge_payload = normalize_bridge_task_payload(bridge_payload)
    with PERSISTED_STATE_LOCK:
        existing = PERSISTED_STATE.setdefault("tasks", {}).get(task_id, {})
        record = ensure_task_locked(task_id, session_id=existing.get("session_id", ""))
        record["status"] = bridge_payload.get("status", record.get("status", "queued"))
        record["summary"] = bridge_payload.get("summary", record.get("summary", ""))
        record["codex_output"] = bridge_payload.get("codex_output", record.get("codex_output", ""))
        record["raw_stdout"] = bridge_payload.get("raw_stdout", record.get("raw_stdout", ""))
        record["raw_stderr"] = bridge_payload.get("raw_stderr", record.get("raw_stderr", ""))
        record["completed_at"] = bridge_payload.get("completed_at", record.get("completed_at", ""))
        record["error"] = bridge_payload.get("error", record.get("error", ""))
        update_task_summary_locked(record)

        session_id = record.get("session_id", "")
        if session_id:
            session = ensure_session_locked(session_id)
            session["last_task_id"] = task_id
            if record["status"] in {"queued", "running"}:
                session["active_task_id"] = task_id
            elif session.get("active_task_id") == task_id:
                session["active_task_id"] = ""
            session["updated_at"] = utc_now()

        save_persisted_state_locked()
        return json.loads(json.dumps(record))


def append_routing_audit(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False)
    with ROUTING_AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def get_session_snapshot(session_id: str) -> dict:
    with PERSISTED_STATE_LOCK:
        session = PERSISTED_STATE.setdefault("sessions", {}).get(session_id)
        if session is None:
            return {
                "session_id": session_id,
                "created_at": "",
                "updated_at": "",
                "active_task_id": "",
                "last_task_id": "",
                "last_user_request": "",
                "tasks": [],
                "last_task": None,
            }
        task_id = session.get("last_task_id", "")
        last_task = None
        if task_id:
            last_task = PERSISTED_STATE.setdefault("tasks", {}).get(task_id)
        return {
            **json.loads(json.dumps(session)),
            "last_task": json.loads(json.dumps(last_task)) if last_task else None,
        }


def get_task_record_snapshot(task_id: str) -> dict | None:
    with PERSISTED_STATE_LOCK:
        task = PERSISTED_STATE.setdefault("tasks", {}).get(task_id)
        if task is None:
            return None
        return json.loads(json.dumps(task))


def bridge_task_payload_from_snapshot(task_id: str) -> dict | None:
    stored = get_task_record_snapshot(task_id)
    if not stored:
        return None
    payload = {
        "task_id": task_id,
        "status": stored.get("status", "unknown"),
        "summary": stored.get("summary", ""),
        "caller_summary": stored.get("caller_summary", ""),
        "codex_output": stored.get("codex_output", ""),
        "raw_stdout": stored.get("raw_stdout", ""),
        "raw_stderr": stored.get("raw_stderr", ""),
        "completed_at": stored.get("completed_at", ""),
        "error": stored.get("error", ""),
        "request_text": stored.get("request_text", ""),
        "target_instance_id": stored.get("target_instance_id", ""),
        "session_id": stored.get("session_id", ""),
        "latest_message": stored.get("latest_message"),
        "source": "local-task-cache",
    }
    payload = normalize_bridge_task_payload(payload)
    payload["caller_summary"] = build_caller_summary(
        {
            **stored,
            "summary": payload.get("summary", stored.get("summary", "")),
            "codex_output": payload.get("codex_output", stored.get("codex_output", "")),
            "raw_stdout": payload.get("raw_stdout", stored.get("raw_stdout", "")),
            "raw_stderr": payload.get("raw_stderr", stored.get("raw_stderr", "")),
        }
    )
    return payload


def bridge_task_messages_payload_from_snapshot(task_id: str) -> dict | None:
    stored = get_task_record_snapshot(task_id)
    if not stored:
        return None
    messages = list(stored.get("messages", []))
    return {
        "task_id": task_id,
        "messages": messages,
        "mirrored_messages": [],
        "mirrored_count": 0,
        "source": "local-task-cache",
    }


def normalize_phone_number(value: str) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit() or ch == "+")
    if digits.startswith("00"):
        digits = f"+{digits[2:]}"
    if digits and not digits.startswith("+"):
        digits = f"+{digits}"
    return digits


def read_form_request(handler: SimpleHTTPRequestHandler) -> dict[str, str]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        return {}
    raw_body = handler.rfile.read(content_length).decode("utf-8", errors="replace")
    parsed = parse_qs(raw_body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def xml_response(handler: SimpleHTTPRequestHandler, status: int, xml_body: str) -> None:
    body = xml_body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/xml; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def twiml_response(inner_xml: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner_xml}</Response>'


def twiml_say(message: str, voice: str = "alice") -> str:
    return f'<Say voice="{xml_escape(voice)}">{xml_escape(message)}</Say>'


def twiml_hangup() -> str:
    return "<Hangup/>"


def twiml_gather(prompt: str, action_url: str, num_digits: int = 1) -> str:
    return (
        f'<Gather input="speech dtmf" action="{xml_escape(action_url)}" method="POST" '
        f'numDigits="{num_digits}" speechTimeout="auto" timeout="5">'
        f"{twiml_say(prompt)}"
        "</Gather>"
    )


def twilio_inbound_action_url() -> str:
    return f"{WEBCALL_PUBLIC_BASE_URL}/twilio/voice/inbound"


def twilio_public_line_payload() -> dict:
    config = twilio_voice_config()
    inbound_url = twilio_inbound_action_url()
    payload = {
        "configured": config is not None,
        "public_base_url": WEBCALL_PUBLIC_BASE_URL,
        "inbound_voice_webhook_url": inbound_url,
        "inbound_voice_method": "POST",
        "outbound_voice_trigger_url": f"{WEBCALL_PUBLIC_BASE_URL}/twilio/voice/outbound/trigger",
        "same_agent_as_web": True,
    }
    if config is not None:
        account_sid, _auth_token, from_number = config
        payload["account_sid"] = account_sid
        payload["from_number"] = from_number
    return payload


def extract_literal_codex_message(text: str) -> str:
    normalized = compact_whitespace(text).strip()
    if not normalized:
        return ""

    patterns = [
        r"^(?:send|tell|ask|message|prompt)\s+['\"]?(?P<message>.+?)['\"]?\s+(?:to|for)\s+(?:the\s+)?codex\b[.!?]*$",
        r"^(?:send|tell|ask|message|prompt)\s+(?:the\s+)?codex\b(?:\s+to)?\s*[:,-]?\s*['\"]?(?P<message>.+?)['\"]?[.!?]*$",
    ]
    for pattern in patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        message = compact_whitespace(match.group("message")).strip(" '\"")
        if message:
            return message
    return ""


def looks_like_hangup_request(text: str) -> bool:
    normalized = _normalized_text(text)
    return normalized in {
        "bye",
        "cancel",
        "disconnect",
        "end",
        "end call",
        "goodbye",
        "hang up",
        "hangup",
        "stop",
    }


def twilio_followup_twiml(message: str) -> str:
    prompt = " ".join(part for part in [message.strip(), TWILIO_CONTINUE_PROMPT] if part).strip()
    return twiml_response(
        twiml_gather(prompt, twilio_inbound_action_url())
        + twiml_say("I did not hear anything else. Goodbye.")
        + twiml_hangup()
    )


def update_session_caller(session_id: str, phone: str, call_sid: str = "") -> None:
    normalized_phone = normalize_phone_number(phone)
    with PERSISTED_STATE_LOCK:
        session = ensure_session_locked(session_id)
        if normalized_phone:
            session["caller_phone"] = normalized_phone
        if call_sid:
            session["last_twilio_call_sid"] = call_sid
        session["updated_at"] = utc_now()
        save_persisted_state_locked()


def set_session_pending_twilio(
    session_id: str,
    *,
    mode: str,
    prompt: str,
    task_id: str = "",
    message_id: str = "",
    routing: dict | None = None,
) -> None:
    with PERSISTED_STATE_LOCK:
        session = ensure_session_locked(session_id)
        session["pending_twilio_mode"] = mode
        session["pending_twilio_prompt"] = prompt
        session["pending_twilio_task_id"] = task_id
        session["pending_twilio_message_id"] = message_id
        session["pending_routing"] = routing
        session["updated_at"] = utc_now()
        save_persisted_state_locked()


def clear_session_pending_twilio(session_id: str) -> None:
    with PERSISTED_STATE_LOCK:
        session = ensure_session_locked(session_id)
        session["pending_twilio_mode"] = ""
        session["pending_twilio_prompt"] = ""
        session["pending_twilio_task_id"] = ""
        session["pending_twilio_message_id"] = ""
        session["pending_routing"] = None
        session["updated_at"] = utc_now()
        save_persisted_state_locked()


def task_message_by_id(task_id: str, message_id: str) -> dict | None:
    record = get_task_record_snapshot(task_id)
    if not record:
        return None
    for message in record.get("messages", []):
        if str(message.get("id", "")).strip() == message_id:
            return message
    return None


def store_task_phone(task_id: str, phone: str) -> None:
    normalized_phone = normalize_phone_number(phone)
    if not normalized_phone:
        return
    with PERSISTED_STATE_LOCK:
        existing = PERSISTED_STATE.setdefault("tasks", {}).get(task_id, {})
        record = ensure_task_locked(task_id, session_id=existing.get("session_id", ""))
        record["notify_phone"] = normalized_phone
        update_task_summary_locked(record)
        save_persisted_state_locked()


def append_task_call(task_id: str, call_payload: dict) -> None:
    with PERSISTED_STATE_LOCK:
        existing = PERSISTED_STATE.setdefault("tasks", {}).get(task_id, {})
        record = ensure_task_locked(task_id, session_id=existing.get("session_id", ""))
        record.setdefault("twilio_calls", []).append(call_payload)
        record["updated_at"] = utc_now()
        save_persisted_state_locked()


def mark_message_notified(task_id: str, message_id: str) -> bool:
    if not message_id:
        return False
    with PERSISTED_STATE_LOCK:
        existing = PERSISTED_STATE.setdefault("tasks", {}).get(task_id, {})
        record = ensure_task_locked(task_id, session_id=existing.get("session_id", ""))
        notified = record.setdefault("notified_message_ids", [])
        if message_id in notified:
            return False
        notified.append(message_id)
        record["updated_at"] = utc_now()
        save_persisted_state_locked()
        return True


def probe_instance_health(instance: dict) -> dict:
    bridge_base_url = str(instance.get("bridge_base_url", "")).strip().rstrip("/")
    now = utc_now()
    if not bridge_base_url:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": "bridge_base_url_missing",
            "checked_at": now,
            "health": None,
        }

    health_url = f"{bridge_base_url}/health"
    try:
        payload = http_get_json(health_url)
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": bool(payload.get("ok")),
            "reachable": True,
            "reason": "ok" if payload.get("ok") else "health_not_ok",
            "checked_at": now,
            "health": payload,
        }
    except HTTPError as exc:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": f"http_error:{exc.code}",
            "checked_at": now,
            "health": None,
        }
    except URLError as exc:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": f"url_error:{exc.reason}",
            "checked_at": now,
            "health": None,
        }
    except Exception as exc:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": f"error:{type(exc).__name__}",
            "checked_at": now,
            "health": None,
        }


def refresh_instance_health() -> None:
    refresh_instance_registry_if_needed()
    snapshot: dict[str, dict] = {}
    for instance in INSTANCE_REGISTRY:
        instance_id = str(instance.get("instance_id", "")).strip()
        if not instance_id:
            continue
        snapshot[instance_id] = probe_instance_health(instance)
    with INSTANCE_HEALTH_LOCK:
        INSTANCE_HEALTH_STATE.clear()
        INSTANCE_HEALTH_STATE.update(snapshot)


def health_monitor_loop() -> None:
    while True:
        refresh_instance_health()
        time.sleep(INSTANCE_HEALTH_INTERVAL_SECONDS)


def get_instance_health_snapshot() -> dict[str, dict]:
    with INSTANCE_HEALTH_LOCK:
        if not INSTANCE_HEALTH_STATE:
            return {}
        return json.loads(json.dumps(INSTANCE_HEALTH_STATE))


def instance_with_health(instance: dict) -> dict:
    health_map = get_instance_health_snapshot()
    instance_id = str(instance.get("instance_id", "")).strip()
    payload = json.loads(json.dumps(instance))
    payload["runtime"] = health_map.get(
        instance_id,
        {
            "instance_id": instance_id,
            "live": False,
            "reachable": False,
            "reason": "health_unknown",
            "checked_at": "",
            "health": None,
        },
    )
    return payload


def instance_ids() -> list[str]:
    refresh_instance_registry_if_needed()
    return [instance["instance_id"] for instance in INSTANCE_REGISTRY]


def instance_registry_summary() -> str:
    refresh_instance_registry_if_needed()
    health_map = get_instance_health_snapshot()
    return "\n".join(
        (
            f"- {instance['instance_id']}: {instance['summary']} "
            f"(bridge: {instance.get('bridge_base_url') or 'not configured'}; "
            f"runtime: {'live' if health_map.get(instance['instance_id'], {}).get('live') else 'not live'}; "
            f"status_reason: {health_map.get(instance['instance_id'], {}).get('reason', 'unknown')})"
        )
        for instance in INSTANCE_REGISTRY
    )


def get_instance_config(instance_id: str) -> dict | None:
    refresh_instance_registry_if_needed()
    for instance in INSTANCE_REGISTRY:
        if instance["instance_id"] == instance_id:
            return instance
    return None


def get_instance(instance_id: str) -> dict | None:
    refresh_instance_registry_if_needed()
    for instance in INSTANCE_REGISTRY:
        if instance["instance_id"] == instance_id:
            return instance
    return None


def frontend_dir() -> Path:
    exported_index = EXPORTED_FRONTEND_DIR / "index.html"
    if exported_index.exists():
        return EXPORTED_FRONTEND_DIR
    return EXPORTED_FRONTEND_DIR


def frontend_source_label() -> str:
    exported_index = EXPORTED_FRONTEND_DIR / "index.html"
    if exported_index.exists():
        return "next-export"
    return "missing"


def frontend_export_ready() -> bool:
    return (EXPORTED_FRONTEND_DIR / "index.html").exists()


def allowed_web_origins() -> set[str]:
    raw = os.environ.get("WEBCALL_ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def origin_is_allowed(origin: str | None) -> bool:
    if not origin:
        return False

    normalized = origin.strip().rstrip("/")
    if not normalized:
        return False

    if normalized in allowed_web_origins():
        return True

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    if parsed.netloc in {"127.0.0.1:3000", "localhost:3000", "127.0.0.1:8765", "localhost:8765"}:
        return True

    if parsed.scheme == "https" and parsed.netloc.endswith(".vercel.app"):
        return True

    return False


ORCHESTRATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["ask", "act", "watch", "follow_up", "approval_response", "cancel"],
        },
        "target_instance_id": {
            "type": "string",
            "enum": instance_ids(),
        },
        "target_host_alias": {"type": "string"},
        "routing_confidence": {"type": "number"},
        "risk_level": {"type": "string", "enum": ["R0", "R1", "R2", "R3", "R4"]},
        "execution_mode": {"type": "string", "enum": ["sync", "async"]},
        "action_mode": {"type": "string", "enum": ["read_only", "write"]},
        "approval_required": {"type": "boolean"},
        "clarification_required": {"type": "boolean"},
        "clarification_question": {"type": "string"},
        "user_goal": {"type": "string"},
        "workspace_id": {"type": "string"},
        "workspace_path": {"type": "string"},
        "repo": {"type": "string"},
        "environment": {"type": "string"},
        "reasoning_summary": {"type": "string"},
        "structured_output": {
            "type": "object",
            "properties": {
                "dispatch_title": {"type": "string"},
                "codex_prompt": {"type": "string"},
                "expected_artifacts": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "callback_policy": {"type": "string"},
            },
            "required": [
                "dispatch_title",
                "codex_prompt",
                "expected_artifacts",
                "constraints",
                "callback_policy",
            ],
            "additionalProperties": False,
        },
        "voice_summary": {"type": "string"},
    },
    "required": [
        "intent",
        "target_instance_id",
        "target_host_alias",
        "routing_confidence",
        "risk_level",
        "execution_mode",
        "action_mode",
        "approval_required",
        "clarification_required",
        "clarification_question",
        "user_goal",
        "workspace_id",
        "workspace_path",
        "repo",
        "environment",
        "reasoning_summary",
        "structured_output",
        "voice_summary",
    ],
    "additionalProperties": False,
}

VOICE_AGENT_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["list_instances", "route", "dispatch", "status", "messages", "respond", "none"],
        },
        "request_text": {"type": "string"},
        "task_id": {"type": "string"},
        "response_text": {"type": "string"},
        "in_reply_to": {"type": "string"},
        "project_hint": {"type": "string"},
    },
    "required": [
        "operation",
        "request_text",
        "task_id",
        "response_text",
        "in_reply_to",
        "project_hint",
    ],
    "additionalProperties": False,
}


def build_voice_tool_agent_system_prompt() -> str:
    fallback_template = """
You are the server-side request router between realtime voice and the Codex bridge layer.

Role:
- You are a terse developer/operator, not a chat assistant.
- Your job is to understand the transcribed request, call the right tools, and then produce a short spoken answer.
- Prefer tools over guessing.
- Never claim work happened unless a tool result confirms it.

Behavior:
- For instance availability or runtime health, use tools.
- For machine, repo, tmux, logs, files, tests, services, GPU, process state, or runtime inspection, dispatch real work through the bridge.
- If the user is asking about an existing task, get task status or messages.
- If the user is answering a pending task question or approval, use the task response tool.
- Keep the final user-facing reply short, concrete, and operational.
- If there is an explicit instance in the request or session context, preserve it.
- Do not ask the user to SSH manually when tools can inspect the target.

Registered instances:
{{INSTANCE_REGISTRY_SUMMARY}}
""".strip()
    prompt = render_prompt_template(
        load_prompt_template(PROMPTS_DIR / "voice_tool_agent_system.txt", fallback_template),
        {"{{INSTANCE_REGISTRY_SUMMARY}}": instance_registry_summary()},
    )
    append_webcall_event(
        "voice_tool_agent.prompt_template",
        prompt_path=str(PROMPTS_DIR / "voice_tool_agent_system.txt"),
        system_prompt=prompt,
    )
    return prompt


def _session_effective_task_id(session_id: str, preferred_task_id: str = "") -> str:
    preferred_task_id = str(preferred_task_id or "").strip()
    if preferred_task_id:
        if get_task_record_snapshot(preferred_task_id):
            return preferred_task_id
        if re.fullmatch(r"[0-9a-fA-F-]{32,64}", preferred_task_id):
            return preferred_task_id
    session = get_session_snapshot(session_id)
    return str(session.get("active_task_id", "")).strip() or str(session.get("last_task_id", "")).strip()


def _tool_instances_payload(only_live: bool = False) -> dict:
    instances = [instance_with_health(instance) for instance in INSTANCE_REGISTRY]
    if only_live:
        instances = [item for item in instances if item.get("runtime", {}).get("live")]
    return {
        "instances": instances,
        "count": len(instances),
        "live_instance_ids": [item["instance_id"] for item in instances if item.get("runtime", {}).get("live")],
    }


def build_tool_backed_voice_tool_agent_speech(state: dict[str, Any]) -> str:
    decision = state.get("decision") or {}
    operation = str(decision.get("operation", "")).strip()
    routing = state.get("routing") or {}
    task = state.get("task") or {}
    dispatch = state.get("dispatch") or {}
    dispatch_task = dispatch.get("task") or {}
    instances = state.get("instances") or []
    task_id = str(state.get("task_id", "")).strip()

    if operation == "list_instances":
        if not instances:
            return "You have no configured instances."
        names = [str(item.get("instance_id", "")).strip() for item in instances if str(item.get("instance_id", "")).strip()]
        if not names:
            return "You have no configured instances."
        return f"You have {len(names)} configured instances: {', '.join(names)}."

    if operation == "dispatch":
        request_text = str(decision.get("request_text", "")).strip()
        direct_message = extract_literal_codex_message(request_text)
        target = str(routing.get("target_instance_id", "")).strip() or "the selected instance"
        status_source = task or dispatch_task
        status = str(status_source.get("status", "")).strip().lower()
        codex_output = compact_whitespace(task.get("codex_output", ""))
        caller_summary = str(task.get("caller_summary", "")).strip()

        if direct_message and status == "succeeded" and codex_output:
            return codex_output
        if status == "succeeded" and caller_summary:
            return caller_summary
        if status:
            return f"I sent the request to Codex on {target}. The task is currently {status}."
        if task_id:
            return f"I sent the request to Codex on {target}."
        return "I prepared the route, but no Codex task was created."

    if operation == "status":
        if not task_id:
            return "There is no active task yet."
        direct_message = extract_literal_codex_message(str(task.get("request_text", "")).strip())
        codex_output = compact_whitespace(task.get("codex_output", ""))
        if direct_message and str(task.get("status", "")).strip().lower() == "succeeded" and codex_output:
            return codex_output
        return str(task.get("caller_summary", "")).strip() or "I checked the task status."

    if operation == "messages":
        if not task_id:
            return "There is no active task with messages yet."
        return latest_task_message_summary(task_id) or "I checked the task messages."

    if operation == "respond":
        return "I sent your answer back to Codex."

    if operation == "route":
        return str(routing.get("voice_summary", "")).strip() or "I prepared the route."

    return ""


def run_voice_tool_agent(
    *,
    session_id: str,
    channel: str,
    user_input: str,
    context: dict | None = None,
) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")

    session = get_session_snapshot(session_id)
    effective_context = context or {}
    explicit_target = (
        str(effective_context.get("target_instance_id", "")).strip()
        or infer_requested_instance_id(user_input)
    )
    waiting_task_id, waiting_message = active_task_waiting_for_response(session_id)
    state: dict[str, Any] = {
        "decision": {
            "operation": "none",
            "request_text": user_input.strip(),
            "task_id": "",
            "response_text": "",
            "in_reply_to": "",
            "project_hint": explicit_target,
        },
        "speech": "",
        "awaiting_user_input": False,
        "pending_mode": "",
        "task_id": "",
        "routing": None,
        "dispatch": None,
        "dispatched_task_id": "",
        "task": None,
        "messages": None,
        "instances": None,
        "tool_results": [],
    }

    def tool_handler(name: str, args: dict[str, Any]) -> dict[str, Any]:
        append_webcall_event(
            "voice_tool_agent.tool_call",
            session_id=session_id,
            channel=channel,
            name=name,
            args=args,
        )
        def soft_tool_error(kind: str, message: str, *, task_id: str = "") -> dict[str, Any]:
            payload = {
                "ok": False,
                "error": kind,
                "message": message,
            }
            if task_id:
                payload["task_id"] = task_id
            append_webcall_event(
                "voice_tool_agent.tool_soft_error",
                session_id=session_id,
                channel=channel,
                name=name,
                args=args,
                payload=payload,
            )
            return payload

        if name == "list_instances":
            payload = _tool_instances_payload(bool(args.get("only_live")))
            if state["decision"].get("operation") in {"", "none"}:
                state["decision"] = {
                    "operation": "list_instances",
                    "request_text": user_input.strip(),
                    "task_id": "",
                    "response_text": "",
                    "in_reply_to": "",
                    "project_hint": explicit_target,
                }
            state["instances"] = payload["instances"]
            return payload

        if name == "check_bridge_health":
            health = get_instance_health_snapshot()
            instance_id = str(args.get("instance_id", "")).strip()
            if instance_id:
                payload = {"instance_id": instance_id, "health": health.get(instance_id)}
            else:
                payload = {"health": health}
            if state["decision"].get("operation") in {"", "none"}:
                state["decision"] = {
                    "operation": "list_instances",
                    "request_text": user_input.strip(),
                    "task_id": "",
                    "response_text": "",
                    "in_reply_to": "",
                    "project_hint": explicit_target,
                }
            return payload

        if name in {"route_codex_request", "dispatch_codex_request"}:
            request_text = str(args.get("request_text", "")).strip() or user_input.strip()
            target_instance_id = str(args.get("target_instance_id", "")).strip() or explicit_target
            orchestration = run_orchestration(
                {
                    "transcript": request_text,
                    "session_id": session_id,
                    "source_channel": channel,
                    "context": {
                        **effective_context,
                        "project_hint": target_instance_id,
                        "target_instance_id": target_instance_id,
                    },
                }
            )
            routing = apply_explicit_target_to_routing(orchestration.get("routing") or {}, target_instance_id)
            state["routing"] = routing
            state["decision"] = {
                "operation": "dispatch" if name == "dispatch_codex_request" else "route",
                "request_text": request_text,
                "task_id": "",
                "response_text": "",
                "in_reply_to": "",
                "project_hint": target_instance_id,
            }
            if name == "route_codex_request":
                return {"routing": routing}

            dispatch_result = dispatch_via_bridge(routing, session_id)
            register_dispatch_state(session_id, request_text, routing, dispatch_result)
            task = dispatch_result.get("task") or {}
            task_id = str(task.get("task_id", "")).strip()
            state["dispatch"] = dispatch_result
            state["dispatched_task_id"] = task_id
            state["task_id"] = task_id
            state["decision"]["task_id"] = task_id
            return {
                "routing": routing,
                "dispatch": dispatch_result,
                "task_id": task_id,
            }

        if name == "get_task_status":
            task_id = _session_effective_task_id(session_id, str(args.get("task_id", "")).strip())
            if not task_id:
                payload = soft_tool_error("no_active_task", "No active task is available for this session.")
            else:
                try:
                    payload = get_bridge_task(task_id)
                except HTTPError as exc:
                    payload = soft_tool_error(
                        f"http_{exc.code}",
                        f"Task lookup failed with HTTP {exc.code}.",
                        task_id=task_id,
                    )
                except URLError as exc:
                    payload = soft_tool_error(
                        "bridge_unreachable",
                        f"Bridge lookup failed: {exc.reason}.",
                        task_id=task_id,
                    )
                except ValueError as exc:
                    payload = soft_tool_error("bad_task_reference", str(exc), task_id=task_id)
            if not (
                state["decision"].get("operation") == "dispatch"
                and task_id
                and task_id == state.get("dispatched_task_id")
            ):
                state["decision"] = {
                    "operation": "status",
                    "request_text": user_input.strip(),
                    "task_id": task_id,
                    "response_text": "",
                    "in_reply_to": "",
                    "project_hint": explicit_target,
                }
            state["task_id"] = task_id
            state["task"] = payload
            return payload

        if name == "get_task_messages":
            task_id = _session_effective_task_id(session_id, str(args.get("task_id", "")).strip())
            if not task_id:
                payload = {
                    **soft_tool_error("no_active_task", "No active task is available for this session."),
                    "messages": [],
                }
            else:
                try:
                    payload = get_bridge_task_messages(task_id)
                except HTTPError as exc:
                    payload = {
                        **soft_tool_error(
                            f"http_{exc.code}",
                            f"Task messages lookup failed with HTTP {exc.code}.",
                            task_id=task_id,
                        ),
                        "messages": [],
                    }
                except URLError as exc:
                    payload = {
                        **soft_tool_error(
                            "bridge_unreachable",
                            f"Task messages lookup failed: {exc.reason}.",
                            task_id=task_id,
                        ),
                        "messages": [],
                    }
                except ValueError as exc:
                    payload = {
                        **soft_tool_error("bad_task_reference", str(exc), task_id=task_id),
                        "messages": [],
                    }
            if not (
                state["decision"].get("operation") == "dispatch"
                and task_id
                and task_id == state.get("dispatched_task_id")
            ):
                state["decision"] = {
                    "operation": "messages",
                    "request_text": user_input.strip(),
                    "task_id": task_id,
                    "response_text": "",
                    "in_reply_to": "",
                    "project_hint": explicit_target,
                }
            state["task_id"] = task_id
            state["messages"] = payload
            latest = (get_task_record_snapshot(task_id) or {}).get("latest_message") or {}
            if task_message_requires_response(latest):
                state["awaiting_user_input"] = True
                state["pending_mode"] = "respond_to_task"
            return payload

        if name == "respond_to_task":
            task_id = _session_effective_task_id(session_id, str(args.get("task_id", "")).strip())
            message = str(args.get("message", "")).strip()
            in_reply_to = str(args.get("in_reply_to", "")).strip()
            if not task_id:
                payload = soft_tool_error("no_active_task", "No active task is available for this session.")
            elif not message:
                payload = soft_tool_error("empty_response", "A response message is required.", task_id=task_id)
            else:
                if not in_reply_to:
                    _, waiting = active_task_waiting_for_response(session_id)
                    if waiting:
                        in_reply_to = str(waiting.get("id", "")).strip()
                try:
                    payload = submit_bridge_task_response(
                        task_id,
                        {
                            "message": message,
                            "in_reply_to": in_reply_to or None,
                            "metadata": {"source": f"{channel}_voice_tool_agent"},
                        },
                    )
                except HTTPError as exc:
                    payload = soft_tool_error(
                        f"http_{exc.code}",
                        f"Task response submit failed with HTTP {exc.code}.",
                        task_id=task_id,
                    )
                except URLError as exc:
                    payload = soft_tool_error(
                        "bridge_unreachable",
                        f"Task response submit failed: {exc.reason}.",
                        task_id=task_id,
                    )
                except ValueError as exc:
                    payload = soft_tool_error("bad_task_reference", str(exc), task_id=task_id)
            state["decision"] = {
                "operation": "respond",
                "request_text": "",
                "task_id": task_id,
                "response_text": message,
                "in_reply_to": in_reply_to,
                "project_hint": explicit_target,
            }
            state["task_id"] = task_id
            state["awaiting_user_input"] = False
            state["pending_mode"] = ""
            return payload

        return {"error": f"unknown_tool:{name}"}

    system_prompt = build_voice_tool_agent_system_prompt()
    user_payload = {
        "session_id": session_id,
        "channel": channel,
        "user_input": user_input,
        "context": effective_context,
        "explicit_target": explicit_target,
        "session": session,
        "waiting_task_id": waiting_task_id,
        "waiting_message": waiting_message,
        "instances": [instance_with_health(instance) for instance in INSTANCE_REGISTRY],
    }
    request_payload = {
        "model": get_voice_tool_model(),
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=True)},
        ],
        "tools": VOICE_TOOL_AGENT_TOOLS,
    }

    append_webcall_event(
        "voice_tool_agent.start",
        session_id=session_id,
        channel=channel,
        model=get_voice_tool_model(),
        user_payload=user_payload,
    )

    previous_response_id: str | None = None
    next_input: list[dict[str, Any]] | None = None
    final_text = ""
    for _ in range(8):
        payload = dict(request_payload)
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
            payload["input"] = next_input or []
        raw_response = openai_json_request("/responses", payload, api_key)
        response_payload = json.loads(raw_response.decode("utf-8"))
        previous_response_id = str(response_payload.get("id", "")).strip() or previous_response_id
        function_calls = extract_function_calls(response_payload)
        append_webcall_event(
            "voice_tool_agent.response",
            session_id=session_id,
            channel=channel,
            response=response_payload,
            function_call_count=len(function_calls),
        )
        if not function_calls:
            final_text = str(extract_response_text(response_payload) or "").strip()
            break

        next_input = []
        for tool_call in function_calls:
            try:
                args = json.loads(tool_call["arguments"])
            except json.JSONDecodeError:
                args = {}
            try:
                result = tool_handler(tool_call["name"], args)
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": "tool_exception",
                    "message": f"{type(exc).__name__}: {exc}",
                    "tool_name": tool_call["name"],
                }
                append_webcall_event(
                    "voice_tool_agent.tool_exception",
                    session_id=session_id,
                    channel=channel,
                    name=tool_call["name"],
                    args=args,
                    error=result["message"],
                )
            state["tool_results"].append(
                {
                    "name": tool_call["name"],
                    "args": args,
                    "result": result,
                }
            )
            next_input.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call["call_id"],
                    "output": json.dumps(result, ensure_ascii=True),
                }
            )

    normalized_input = normalize_instance_aliases(user_input.strip())
    if not state.get("tool_results"):
        if looks_like_instance_inventory_query(normalized_input) or looks_like_live_instance_query(normalized_input):
            payload = _tool_instances_payload(only_live=looks_like_live_instance_query(normalized_input))
            state["instances"] = payload["instances"]
            state["decision"] = {
                "operation": "list_instances",
                "request_text": user_input.strip(),
                "task_id": "",
                "response_text": "",
                "in_reply_to": "",
                "project_hint": explicit_target,
            }
        elif looks_like_bridge_health_query(normalized_input) or (
            mentions_registered_instance(normalized_input) and looks_like_concrete_inspection_request(normalized_input)
        ):
            raise RuntimeError("Voice tool agent returned no tool calls for a tool-backed request.")

    speech = build_tool_backed_voice_tool_agent_speech(state)
    if not speech:
        speech = final_text.strip()
    if speech and not state.get("dispatch"):
        normalized_speech = _normalized_text(speech)
        misleading_phrases = {
            "i started the task",
            "i started a task",
            "i sent it to codex",
            "i sent this to codex",
            "i dispatched it",
            "requested to codex",
        }
        if any(phrase in normalized_speech for phrase in misleading_phrases):
            speech = ""
    if not speech:
        speech = "I handled the request."

    result = {
        "ok": True,
        "mode": "voice_tool_agent",
        "decision": state["decision"],
        "speech": speech,
        "awaiting_user_input": bool(state.get("awaiting_user_input")),
        "pending_mode": str(state.get("pending_mode", "")).strip(),
        "task_id": str(state.get("task_id", "")).strip(),
        "tool_results": state.get("tool_results", []),
    }
    if state.get("routing") is not None:
        result["routing"] = state["routing"]
    if state.get("dispatch") is not None:
        result["dispatch"] = state["dispatch"]
    if state.get("task") is not None:
        result["task"] = state["task"]
    if state.get("messages") is not None:
        result["messages"] = state["messages"]
    if state.get("instances") is not None:
        result["instances"] = state["instances"]

    append_webcall_event(
        "voice_tool_agent.final",
        session_id=session_id,
        channel=channel,
        result=result,
    )
    return result

VOICE_TOOL_AGENT_TOOLS = [
    {
        "type": "function",
        "name": "list_instances",
        "description": "List registered instances and their live status.",
        "parameters": {
            "type": "object",
            "properties": {
                "only_live": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "check_bridge_health",
        "description": "Check whether one instance bridge or all instance bridges are reachable.",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "enum": instance_ids()},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "route_codex_request",
        "description": "Create a routing/dispatch plan for a user request without starting work.",
        "parameters": {
            "type": "object",
            "properties": {
                "request_text": {"type": "string"},
                "target_instance_id": {"type": "string", "enum": instance_ids()},
            },
            "required": ["request_text"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "dispatch_codex_request",
        "description": "Route and dispatch a Codex task to the right bridge instance.",
        "parameters": {
            "type": "object",
            "properties": {
                "request_text": {"type": "string"},
                "target_instance_id": {"type": "string", "enum": instance_ids()},
            },
            "required": ["request_text"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_task_status",
        "description": "Get the current status and final output for a task. If task_id is omitted, use the current session task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_task_messages",
        "description": "Get the latest messages for a task. If task_id is omitted, use the current session task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "respond_to_task",
        "description": "Answer a pending Codex question or approval for the current task or a specified task.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "task_id": {"type": "string"},
                "in_reply_to": {"type": "string"},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
]


def load_dotenv(dotenv_path: Path, *, override: bool = False) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if override or key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env", override=True)
WEBCALL_PUBLIC_BASE_URL = os.environ.get("WEBCALL_PUBLIC_BASE_URL", WEBCALL_PUBLIC_BASE_URL).rstrip("/")


def get_orchestrator_model() -> str:
    return os.environ.get("OPENAI_ORCHESTRATOR_MODEL", "gpt-4o-mini")


def get_voice_tool_model() -> str:
    return os.environ.get("OPENAI_VOICE_TOOL_MODEL", get_orchestrator_model())


def json_response(
    handler: SimpleHTTPRequestHandler,
    status: int,
    payload: dict,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def read_json_request(handler: SimpleHTTPRequestHandler) -> dict:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        return {}
    raw_body = handler.rfile.read(content_length)
    if not raw_body:
        return {}
    return json.loads(raw_body.decode("utf-8"))


def load_server_mapping() -> str:
    if not SERVER_MAPPING_PATH.exists():
        return "Server mapping document is unavailable."
    return SERVER_MAPPING_PATH.read_text(encoding="utf-8")


def load_codex_bridge_service() -> str:
    if not CODEX_BRIDGE_PATH.exists():
        return "Codex bridge service document is unavailable."
    return CODEX_BRIDGE_PATH.read_text(encoding="utf-8")


def openai_json_request(path: str, payload: dict, api_key: str) -> bytes:
    append_webcall_event(
        "openai.request",
        path=path,
        model=payload.get("model", ""),
        input=payload.get("input"),
        text_format=payload.get("text"),
    )
    request = Request(
        f"{OPENAI_API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        raw = response.read()
    append_webcall_event(
        "openai.response",
        path=path,
        model=payload.get("model", ""),
        response_bytes=len(raw),
        response_text=raw.decode("utf-8", errors="replace"),
    )
    return raw


def http_json_request(url: str, payload: dict | None = None, method: str = "POST") -> dict:
    append_webcall_event(
        "http_json_request.start",
        url=url,
        method=method,
        payload=payload or {},
    )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=45) as response:
        raw = response.read()
        status = getattr(response, "status", "")
    if not raw:
        append_webcall_event(
            "http_json_request.done",
            url=url,
            method=method,
            status=status,
            response={},
        )
        return {}
    decoded = json.loads(raw.decode("utf-8"))
    append_webcall_event(
        "http_json_request.done",
        url=url,
        method=method,
        status=status,
        response=decoded,
    )
    return decoded


def http_get_json(url: str) -> dict:
    append_webcall_event("http_get_json.start", url=url)
    request = Request(url, method="GET")
    with urlopen(request, timeout=45) as response:
        raw = response.read()
        status = getattr(response, "status", "")
    if not raw:
        append_webcall_event("http_get_json.done", url=url, status=status, response={})
        return {}
    decoded = json.loads(raw.decode("utf-8"))
    append_webcall_event("http_get_json.done", url=url, status=status, response=decoded)
    return decoded


def twilio_voice_config() -> tuple[str, str, str] | None:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not account_sid or not auth_token or not from_number:
        return None
    return account_sid, auth_token, from_number


def twilio_base_url(account_sid: str) -> str:
    return f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"


def twilio_call_status_callback_url(task_id: str, session_id: str, message_id: str = "") -> str:
    query = urlencode(
        {
            "task_id": task_id,
            "session_id": session_id,
            "message_id": message_id,
        }
    )
    return f"{WEBCALL_PUBLIC_BASE_URL}/twilio/voice/status?{query}"


def twilio_user_phone_for_status_callback(form: dict[str, str]) -> str:
    to_number = normalize_phone_number(form.get("To", ""))
    from_number = normalize_phone_number(form.get("From", ""))
    config = twilio_voice_config()
    twilio_number = normalize_phone_number(config[2]) if config else ""

    if twilio_number:
        if to_number == twilio_number and from_number:
            return from_number
        if from_number == twilio_number and to_number:
            return to_number
    return from_number or to_number


def create_twilio_call(to_number: str, twiml_url: str, status_callback_url: str) -> dict:
    config = twilio_voice_config()
    if config is None:
        raise RuntimeError("Twilio voice is not configured.")
    account_sid, auth_token, from_number = config
    auth_header = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    body = urlencode(
        {
            "To": normalize_phone_number(to_number),
            "From": from_number,
            "Url": twiml_url,
            "Method": "GET",
            "StatusCallback": status_callback_url,
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": "initiated ringing answered completed",
        }
    ).encode("utf-8")
    request = Request(
        f"{twilio_base_url(account_sid)}/Calls.json",
        data=body,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "voice-layer-control-plane/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_response_text(response_payload: dict) -> str | None:
    output_text = response_payload.get("output_text")
    if output_text:
        return output_text

    for item in response_payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    return None


def extract_function_calls(response_payload: dict) -> list[dict]:
    calls: list[dict] = []
    for item in response_payload.get("output", []):
        if str(item.get("type", "")).strip() != "function_call":
            continue
        calls.append(
            {
                "id": str(item.get("id", "")).strip(),
                "call_id": str(item.get("call_id", "")).strip(),
                "name": str(item.get("name", "")).strip(),
                "arguments": str(item.get("arguments", "")).strip() or "{}",
            }
        )
    return calls


def build_orchestrator_messages(request_payload: dict) -> list[dict]:
    transcript = str(request_payload.get("transcript", "")).strip()
    session_id = str(request_payload.get("session_id", "")).strip() or "voice-webcall"
    channel = str(request_payload.get("source_channel", "")).strip() or "realtime_voice"
    prior_context = request_payload.get("context") or {}
    server_mapping = load_server_mapping()
    codex_bridge_service = load_codex_bridge_service()
    fallback_template = """
You are the orchestration layer for a voice-first Codex control plane.
Role:
- Act like a terse developer router, not a general assistant.
- Convert the user's engineering request into structured routing data only.
- Return output that is ready for bridge dispatch.

Structured instance registry:
{{INSTANCE_REGISTRY_SUMMARY}}

Infrastructure notes:
{{SERVER_MAPPING}}

Codex bridge contract:
{{CODEX_BRIDGE_SERVICE}}

Routing rules:
- The current server is the hack instance. If the user asks for hack, this server, or this machine, route to hack.
- hack, STT-A10, and TTS-H100 can all execute Codex work through their configured bridge services.
- Prefer live instances when multiple targets fit.
- Reflect live/runtime status from the registry when the user asks what is up right now.
- Route speech transcription or diarization work to STT-A10.
- Route speech synthesis or voice generation work to TTS-H100.
- Route orchestration, backend, API, repo, debugging, and ambiguous backend work to hack.
- The `codex_prompt` is the execution body. Do not include voice UX policy or bridge callback implementation details.
- If target is hack and workspace is not specified, use `/home/ubuntu/hack`.
- Fill `workspace_path` whenever it can be inferred.
- For actionable work, keep `constraints`, `expected_artifacts`, and `callback_policy` concrete.
- Do not ask for clarification when the task is reasonably dispatchable.
- Set `clarification_required=true` only when one missing fact blocks dispatch.
- Keep `voice_summary` short.
- Do not chat. Compile a dispatch plan.
""".strip()
    system_prompt = render_prompt_template(
        load_prompt_template(ORCHESTRATOR_PROMPT_PATH, fallback_template),
        {
            "{{INSTANCE_REGISTRY_SUMMARY}}": instance_registry_summary(),
            "{{SERVER_MAPPING}}": server_mapping,
            "{{CODEX_BRIDGE_SERVICE}}": codex_bridge_service,
        },
    )

    user_prompt = json.dumps(
        {
            "session_id": session_id,
            "source_channel": channel,
            "request_text": transcript,
            "context": prior_context,
        },
        ensure_ascii=True,
    )
    append_webcall_event(
        "orchestrator.prompt",
        session_id=session_id,
        channel=channel,
        transcript=transcript,
        prompt_path=str(ORCHESTRATOR_PROMPT_PATH),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def build_voice_agent_messages(
    *,
    session_id: str,
    channel: str,
    user_input: str,
    context: dict | None = None,
) -> list[dict]:
    session = get_session_snapshot(session_id)
    active_task = session.get("last_task") or {}
    waiting_task_id, waiting_message = active_task_waiting_for_response(session_id)
    fallback_template = """
You are the shared voice-agent policy for a Codex control plane.

Role:
- You are not a chat assistant.
- Be terse and operational.
- Choose exactly one backend operation from the schema.
- Never pretend you executed work yourself.

Registered instances:
{{INSTANCE_REGISTRY_SUMMARY}}

Available operations:
- list_instances
- route
- dispatch
- status
- messages
- respond
- none

Decision rules:
- Prefer `respond` when a pending task is waiting for a user answer or approval and the new utterance sounds like the answer.
- Prefer `list_instances` when the user asks what instances exist or which are live/up/running/reachable.
- Prefer `dispatch` for concrete machine, repo, process, tmux, log, file, test, or runtime inspection requests.
- Questions like "what tmux sessions are on A10" are still `dispatch`.
- Prefer `dispatch` over `route` when the user says run, do it, check, inspect, fix, execute, prompt Codex, or ask Codex.
- Prefer `status` or `messages` when the user asks about existing work.
- Use `task_id` only when the user names a task or session context already has one.
- Keep `request_text` normalized and ready for the next backend step.
- Keep `response_text` only for `respond`.
- Do not send the user to manual SSH when a registered bridge can inspect the target.
""".strip()
    system_prompt = render_prompt_template(
        load_prompt_template(VOICE_AGENT_PROMPT_PATH, fallback_template),
        {"{{INSTANCE_REGISTRY_SUMMARY}}": instance_registry_summary()},
    )
    user_payload = {
        "session_id": session_id,
        "channel": channel,
        "user_input": user_input,
        "context": context or {},
        "session": session,
        "active_task": active_task,
        "waiting_task_id": waiting_task_id,
        "waiting_message": waiting_message,
        "instances": [instance_with_health(instance) for instance in INSTANCE_REGISTRY],
    }
    append_webcall_event(
        "voice_agent.prompt",
        session_id=session_id,
        channel=channel,
        prompt_path=str(VOICE_AGENT_PROMPT_PATH),
        system_prompt=system_prompt,
        user_payload=user_payload,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=True)},
    ]


def run_orchestration(request_payload: dict) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")

    transcript = str(request_payload.get("transcript", "")).strip()
    if not transcript:
        raise ValueError("transcript is required.")

    payload = {
        "model": get_orchestrator_model(),
        "input": build_orchestrator_messages(request_payload),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "codex_dispatch_packet",
                "strict": True,
                "schema": ORCHESTRATION_RESPONSE_SCHEMA,
            }
        },
    }
    raw_response = openai_json_request("/responses", payload, api_key)
    response_payload = json.loads(raw_response.decode("utf-8"))
    output_text = extract_response_text(response_payload)
    if not output_text:
        raise RuntimeError("OpenAI orchestration response did not include structured output text.")

    try:
        orchestrated = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse orchestration payload: {exc}") from exc

    context = request_payload.get("context") or {}
    explicit_target = str(context.get("target_instance_id", "")).strip() or infer_requested_instance_id(transcript)
    if explicit_target and explicit_target in instance_ids():
        orchestrated["target_instance_id"] = explicit_target

    instance_config = get_instance_config(orchestrated.get("target_instance_id", ""))
    if instance_config:
        orchestrated["target_host_alias"] = (
            orchestrated.get("target_host_alias") or instance_config["host_alias"]
        )
        orchestrated["workspace_path"] = (
            orchestrated.get("workspace_path") or instance_config.get("workspace_path", "")
        )
        if not orchestrated.get("workspace_id"):
            orchestrated["workspace_id"] = instance_config["instance_id"]

    structured_output = orchestrated.get("structured_output", {})
    if explicit_target and instance_config:
        orchestrated["reasoning_summary"] = f"User explicitly requested work on {explicit_target}."
        constraints = [
            item
            for item in (structured_output.get("constraints") or [])
            if "execute this on" not in str(item).lower()
        ]
        structured_output["constraints"] = [f"Execute this on {explicit_target}.", *constraints]

    if orchestrated.get("target_instance_id") == "hack":
        if not orchestrated.get("workspace_path"):
            orchestrated["workspace_path"] = "/home/ubuntu/hack"
        if not orchestrated.get("workspace_id"):
            orchestrated["workspace_id"] = "hack"
        if not structured_output.get("callback_policy"):
            structured_output["callback_policy"] = (
                "Use the Codex bridge service to dispatch asynchronously and notify the user on completion or if blocked."
            )
        constraints = structured_output.get("constraints") or []
        if not constraints:
            constraints = [
                "Target workspace_path /home/ubuntu/hack unless the user specifies a different repo.",
                "Prepare output suitable for POST /api/v1/tasks/execute on the Codex bridge service.",
                "If blocked, ask one concrete follow-up question or emit an approval request.",
            ]
        structured_output["constraints"] = constraints
        if not structured_output.get("expected_artifacts"):
            structured_output["expected_artifacts"] = [
                "Codex-ready execution prompt",
                "Target workspace path",
                "Summary of routing decision",
            ]
    orchestrated["structured_output"] = structured_output
    append_webcall_event(
        "orchestration.result",
        session_id=request_payload.get("session_id", ""),
        transcript=transcript,
        routing=orchestrated,
    )

    return {
        "ok": True,
        "model": get_orchestrator_model(),
        "routing": orchestrated,
        "source": "openai-responses-json-schema",
    }


def run_voice_agent_decision(
    *,
    session_id: str,
    channel: str,
    user_input: str,
    context: dict | None = None,
) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")
    payload = {
        "model": get_orchestrator_model(),
        "input": build_voice_agent_messages(
            session_id=session_id,
            channel=channel,
            user_input=user_input,
            context=context or {},
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "voice_agent_decision",
                "strict": True,
                "schema": VOICE_AGENT_DECISION_SCHEMA,
            }
        },
    }
    raw_response = openai_json_request("/responses", payload, api_key)
    response_payload = json.loads(raw_response.decode("utf-8"))
    output_text = extract_response_text(response_payload)
    if not output_text:
        raise RuntimeError("OpenAI voice agent response did not include structured output text.")
    try:
        decision = json.loads(output_text)
        append_webcall_event(
            "voice_agent.decision",
            session_id=session_id,
            channel=channel,
            user_input=user_input,
            decision=decision,
        )
        return decision
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse voice agent decision: {exc}") from exc


def bridge_callback_url() -> str:
    return f"{WEBCALL_PUBLIC_BASE_URL}/bridge/user-messages"


def build_bridge_execute_payload(routing: dict) -> dict:
    target_instance_id = str(routing.get("target_instance_id", "")).strip()
    instance = get_instance(target_instance_id)
    if instance is None:
        raise ValueError(f"Unknown target instance: {target_instance_id}")

    bridge_base_url = str(instance.get("bridge_base_url", "")).strip()
    bridge_execute_endpoint = str(instance.get("bridge_execute_endpoint", "")).strip()
    if not bridge_base_url or not bridge_execute_endpoint:
        raise ValueError(f"Instance {target_instance_id} does not support Codex bridge execution.")

    structured_output = routing.get("structured_output") or {}
    configured_workspace_path = str(instance.get("workspace_path", "")).strip()
    routed_workspace_path = str(routing.get("workspace_path", "")).strip()
    if target_instance_id == "hack":
        workspace_path = routed_workspace_path or configured_workspace_path
    else:
        # Remote worker instances should execute in the workspace configured in the registry,
        # not a model-invented path from orchestration output.
        workspace_path = configured_workspace_path or routed_workspace_path
    if not workspace_path:
        raise ValueError(f"Instance {target_instance_id} does not have a workspace path.")

    risk_level = str(routing.get("risk_level", "R0")).strip() or "R0"
    action_mode = str(routing.get("action_mode", "read_only")).strip() or "read_only"
    execution_mode = str(routing.get("execution_mode", "sync")).strip() or "sync"
    callback_policy = str(structured_output.get("callback_policy", "")).strip()

    sandbox = "read-only"
    if action_mode == "write":
        sandbox = "workspace-write"
    if risk_level in {"R3", "R4"}:
        sandbox = "danger-full-access"

    timeout_seconds = 180 if execution_mode == "sync" else 1800
    if callback_policy and "tests" in callback_policy.lower():
        timeout_seconds = max(timeout_seconds, 900)

    codex_prompt = str(structured_output.get("codex_prompt", "")).strip() or str(routing.get("user_goal", "")).strip()
    direct_codex_message = extract_literal_codex_message(codex_prompt)

    extra_instructions = []
    constraints = structured_output.get("constraints") or []
    expected_artifacts = structured_output.get("expected_artifacts") or []
    if direct_codex_message:
        codex_prompt = direct_codex_message
        sandbox = "read-only"
        timeout_seconds = min(timeout_seconds, 120)
        extra_instructions.extend(
            [
                "Interaction mode: direct_chat",
                "Reply directly to the user's literal message as Codex.",
                "Do not reinterpret the message as an engineering task or bridge operation.",
            ]
        )
    if constraints:
        extra_instructions.append("Constraints:")
        extra_instructions.extend(f"- {item}" for item in constraints)
    if expected_artifacts:
        extra_instructions.append("Expected artifacts:")
        extra_instructions.extend(f"- {item}" for item in expected_artifacts)
    if callback_policy:
        extra_instructions.append(f"Callback policy: {callback_policy}")

    return {
        "bridge_url": f"{bridge_base_url.rstrip('/')}{bridge_execute_endpoint}",
        "execute_payload": {
            "prompt": codex_prompt,
            "workspace_path": workspace_path,
            "public_base_url": bridge_base_url.rstrip("/"),
            "timeout_seconds": timeout_seconds,
            "summary_words": 100,
            "sandbox": sandbox,
            "user_webhook_url": bridge_callback_url(),
            "extra_instructions": "\n".join(extra_instructions).strip() or None,
        },
    }


def dispatch_via_bridge(routing: dict, session_id: str) -> dict:
    bridge_request = build_bridge_execute_payload(routing)
    append_webcall_event(
        "bridge.dispatch.start",
        session_id=session_id,
        target_instance_id=routing.get("target_instance_id", ""),
        bridge_url=bridge_request["bridge_url"],
        execute_payload=bridge_request["execute_payload"],
    )
    response = http_json_request(
        bridge_request["bridge_url"],
        bridge_request["execute_payload"],
        method="POST",
    )

    task_id = str(response.get("task_id", "")).strip()
    if task_id:
        dispatch_message = {
            "id": f"{task_id}:dispatch",
            "task_id": task_id,
            "kind": "info",
            "message": f"Task dispatched from session {session_id}.",
            "expects_response": False,
            "metadata": {
                "source": "webcall-server",
                "target_instance_id": routing.get("target_instance_id"),
            },
        }
        with BRIDGE_STORE_LOCK:
            BRIDGE_MESSAGE_MIRROR.setdefault(task_id, [])
            BRIDGE_MESSAGE_MIRROR[task_id].append(dispatch_message)
        append_task_message(task_id, dispatch_message)
    append_webcall_event(
        "bridge.dispatch.done",
        session_id=session_id,
        target_instance_id=routing.get("target_instance_id", ""),
        response=response,
    )

    return {
        "ok": True,
        "task": response,
        "bridge_request": {
            "target_instance_id": routing.get("target_instance_id"),
            "workspace_path": bridge_request["execute_payload"]["workspace_path"],
            "sandbox": bridge_request["execute_payload"]["sandbox"],
            "timeout_seconds": bridge_request["execute_payload"]["timeout_seconds"],
            "user_webhook_url": bridge_request["execute_payload"]["user_webhook_url"],
        },
    }


def get_bridge_base_url_for_task(task_id: str, fallback_instance_id: str = "hack") -> str:
    task_record = get_task_record_snapshot(task_id)
    instance_id = fallback_instance_id
    if task_record and task_record.get("target_instance_id"):
        instance_id = str(task_record["target_instance_id"]).strip() or fallback_instance_id
    instance = get_instance(instance_id)
    if instance is None or not instance.get("bridge_base_url"):
        raise ValueError(f"Bridge base URL is not configured for instance {instance_id}.")
    return str(instance["bridge_base_url"]).rstrip("/")


def get_bridge_task(task_id: str) -> dict:
    bridge_base_url = get_bridge_base_url_for_task(task_id)
    try:
        append_webcall_event("bridge.task.fetch.start", task_id=task_id, bridge_base_url=bridge_base_url)
        bridge_payload = normalize_bridge_task_payload(
            http_get_json(f"{bridge_base_url}/api/v1/tasks/{task_id}")
        )
    except HTTPError as exc:
        if exc.code == 404:
            cached = bridge_task_payload_from_snapshot(task_id)
            if cached is not None:
                append_webcall_event("bridge.task.fetch.cached", task_id=task_id, payload=cached)
                return cached
        raise
    stored = sync_task_from_bridge_payload(task_id, bridge_payload)
    bridge_payload["caller_summary"] = stored.get("caller_summary", "")
    bridge_payload["request_text"] = stored.get("request_text", "")
    bridge_payload["target_instance_id"] = stored.get("target_instance_id", "")
    bridge_payload["session_id"] = stored.get("session_id", "")
    bridge_payload["latest_message"] = stored.get("latest_message")
    append_webcall_event("bridge.task.fetch.done", task_id=task_id, payload=bridge_payload)
    return bridge_payload


def get_bridge_task_messages(task_id: str) -> dict:
    bridge_base_url = get_bridge_base_url_for_task(task_id)
    try:
        append_webcall_event("bridge.messages.fetch.start", task_id=task_id, bridge_base_url=bridge_base_url)
        bridge_payload = http_get_json(f"{bridge_base_url}/api/v1/tasks/{task_id}/messages")
    except HTTPError as exc:
        if exc.code == 404:
            cached = bridge_task_messages_payload_from_snapshot(task_id)
            if cached is not None:
                append_webcall_event("bridge.messages.fetch.cached", task_id=task_id, payload=cached)
                return cached
        raise
    with BRIDGE_STORE_LOCK:
        mirrored = list(BRIDGE_MESSAGE_MIRROR.get(task_id, []))
    for message in bridge_payload.get("messages", []):
        append_task_message(task_id, message)
    for message in mirrored:
        append_task_message(task_id, message)
    bridge_payload["mirrored_messages"] = mirrored
    bridge_payload["mirrored_count"] = len(mirrored)
    append_webcall_event("bridge.messages.fetch.done", task_id=task_id, payload=bridge_payload)
    return bridge_payload


def get_bridge_task_responses(task_id: str) -> dict:
    bridge_base_url = get_bridge_base_url_for_task(task_id)
    return http_get_json(f"{bridge_base_url}/api/v1/tasks/{task_id}/responses")


def submit_bridge_task_response(task_id: str, payload: dict) -> dict:
    bridge_base_url = get_bridge_base_url_for_task(task_id)
    append_webcall_event(
        "bridge.response.submit.start",
        task_id=task_id,
        bridge_base_url=bridge_base_url,
        payload=payload,
    )
    result = http_json_request(
        f"{bridge_base_url}/api/v1/tasks/{task_id}/responses",
        payload,
        method="POST",
    )
    append_webcall_event("bridge.response.submit.done", task_id=task_id, response=result)
    return result


def task_message_requires_response(message: dict | None) -> bool:
    payload = message or {}
    return bool(payload.get("expects_response")) or str(payload.get("kind", "")).strip() == "approval"


def normalize_task_reply(user_input: str, waiting_message: dict | None = None) -> str:
    text = user_input.strip()
    normalized = _normalized_text(text)
    if str((waiting_message or {}).get("kind", "")).strip() != "approval":
        return text

    if normalized in {"1", "yes", "confirm", "approved", "approve", "pass", "go ahead", "proceed"}:
        return "approve"
    if normalized in {"0", "no", "cancel", "reject", "deny", "stop"}:
        return "cancel"
    return text


def active_task_waiting_for_response(session_id: str) -> tuple[str, dict] | tuple[str, None]:
    session = get_session_snapshot(session_id)
    task_id = str(session.get("active_task_id", "")).strip() or str(session.get("last_task_id", "")).strip()
    if not task_id:
        return "", None
    task = get_task_record_snapshot(task_id)
    if not task:
        return "", None
    latest = task.get("latest_message") or {}
    if task_message_requires_response(latest):
        return task_id, latest
    for message in reversed(task.get("messages", [])):
        if task_message_requires_response(message):
            return task_id, message
    return task_id, None


def outbound_twiml_url(task_id: str, message_id: str = "") -> str:
    query = urlencode({"task_id": task_id, "message_id": message_id})
    return f"{WEBCALL_PUBLIC_BASE_URL}/twilio/voice/outbound?{query}"


def maybe_trigger_twilio_callback(task_id: str, message: dict) -> dict | None:
    message_id = str(message.get("id", "")).strip()
    if not task_id or not message_id:
        return None

    expects_response = bool(message.get("expects_response"))
    kind = str(message.get("kind", "")).strip()
    metadata = message.get("metadata") or {}
    event_type = str(metadata.get("event", "")).strip()
    should_call = expects_response or kind in {"approval", "warning"} or event_type in {
        "task.completed",
        "task.failed",
    }
    if not should_call:
        return None

    task = get_task_record_snapshot(task_id)
    if not task:
        return None
    session_id = str(task.get("session_id", "")).strip()
    session = get_session_snapshot(session_id) if session_id else {}
    to_number = normalize_phone_number(task.get("notify_phone") or session.get("caller_phone") or "")
    if not to_number:
        return None
    if not mark_message_notified(task_id, message_id):
        return None

    call = create_twilio_call(
        to_number,
        outbound_twiml_url(task_id, message_id),
        twilio_call_status_callback_url(task_id, session_id, message_id),
    )
    call_record = {
        "direction": "outbound-api",
        "sid": call.get("sid", ""),
        "to": to_number,
        "message_id": message_id,
        "task_id": task_id,
        "created_at": utc_now(),
        "status": call.get("status", ""),
    }
    append_task_call(task_id, call_record)
    return call_record


def twilio_initial_prompt(session_id: str) -> str:
    task_id, waiting_message = active_task_waiting_for_response(session_id)
    if task_id and waiting_message:
        message = str(waiting_message.get("message", "")).strip() or "I need your answer before I continue."
        return f"{message} After the tone, say your answer or press a key."
    session = get_session_snapshot(session_id)
    last_request = str(session.get("last_user_request", "")).strip()
    if last_request:
        return f"You are back in your Codex workspace. Say a new request, or continue from: {last_request}"
    return "Welcome to Codex voice control. After the tone, tell me what you want me to do."


def process_twilio_user_input(session_id: str, call_sid: str, from_number: str, user_input: str) -> str:
    update_session_caller(session_id, from_number, call_sid)
    session = get_session_snapshot(session_id)
    mode = str(session.get("pending_twilio_mode", "")).strip()
    input_text = user_input.strip()

    if looks_like_hangup_request(input_text):
        clear_session_pending_twilio(session_id)
        return twiml_response(twiml_say("Okay. Goodbye.") + twiml_hangup())

    if mode == "clarify":
        prior_request = str(session.get("last_user_request", "")).strip()
        prompt = str(session.get("pending_twilio_prompt", "")).strip()
        combined = input_text
        if prior_request:
            combined = f"Original request: {prior_request}\nClarification response: {input_text}"
        elif prompt:
            combined = f"Clarification prompt: {prompt}\nUser response: {input_text}"
        clear_session_pending_twilio(session_id)
        input_text = combined
    if mode == "dispatch_approval":
        normalized = input_text.lower()
        input_text = "confirm" if normalized in {"1", "yes", "confirm", "approved", "approve", "pass", "go ahead", "proceed"} else "cancel"

    response = shared_agent_response(
        session_id=session_id,
        channel="twilio_voice",
        user_input=input_text,
        context=merge_phone_request_context(
            {
                "caller_phone": normalize_phone_number(from_number),
                "call_sid": call_sid,
            },
            from_number,
        ),
    )
    task_id = str(response.get("task_id", "")).strip()
    if task_id:
        store_task_phone(task_id, from_number)
    if response.get("awaiting_user_input"):
        prompt = str(response.get("speech", "")).strip() or "I need your input."
        pending_mode = str(response.get("pending_mode", "")).strip()
        set_session_pending_twilio(
            session_id,
            mode=pending_mode,
            prompt=prompt,
            task_id=task_id,
            message_id=str(response.get("in_reply_to", "")).strip(),
            routing=response.get("routing") if pending_mode == "dispatch_approval" else None,
        )
        action_url = f"{WEBCALL_PUBLIC_BASE_URL}/twilio/voice/inbound"
        return twiml_response(
            twiml_gather(prompt, action_url) + twiml_say("I did not hear a response.") + twiml_hangup()
        )
    clear_session_pending_twilio(session_id)
    return twilio_followup_twiml(str(response.get("speech", "")).strip() or "Done.")


def twilio_outbound_message(task_id: str, message_id: str = "") -> tuple[str, bool]:
    task = get_task_record_snapshot(task_id) or {}
    message = task_message_by_id(task_id, message_id) if message_id else None
    if message is None:
        message = task.get("latest_message") or {}

    message_text = str(message.get("message", "")).strip()
    expects_response = bool(message.get("expects_response"))
    if message_text:
        return message_text, expects_response
    caller_summary = str(task.get("caller_summary", "")).strip()
    if caller_summary:
        return caller_summary, False
    return "Your Codex task has an update.", False


def latest_task_message_summary(task_id: str) -> str:
    task = get_task_record_snapshot(task_id) or {}
    latest = task.get("latest_message") or {}
    if latest.get("message"):
        return str(latest["message"]).strip()
    messages = task.get("messages", [])
    if messages:
        return str(messages[-1].get("message", "")).strip()
    return str(task.get("caller_summary", "")).strip() or "There are no task updates yet."


def _normalized_text(value: str) -> str:
    return " ".join(str(value).lower().split())


def _compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalized_text(value))


def normalize_instance_aliases(value: str) -> str:
    normalized = " ".join(str(value).split())
    replacements = [
        (r"\bairtel\b", "A10"),
        (r"\bstd a10\b", "STT-A10"),
        (r"\bst d a10\b", "STT-A10"),
        (r"\ba ten\b", "A10"),
        (r"\ba-?10\b", "A10"),
        (r"\btts[- ]?800\b", "TTS-H100"),
        (r"\bech hundred\b", "H100"),
        (r"\bedge hundred\b", "H100"),
        (r"\bage hundred\b", "H100"),
        (r"\bh hundred\b", "H100"),
        (r"\bh ?100\b", "H100"),
    ]
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized


def apply_explicit_target_to_routing(routing: dict, explicit_target: str) -> dict:
    if not explicit_target or explicit_target not in instance_ids():
        return routing

    payload = json.loads(json.dumps(routing))
    payload["target_instance_id"] = explicit_target

    instance_config = get_instance_config(explicit_target)
    if not instance_config:
        return payload

    payload["target_host_alias"] = instance_config["host_alias"]
    payload["workspace_path"] = payload.get("workspace_path") or instance_config.get("workspace_path", "")
    if not payload.get("workspace_id"):
        payload["workspace_id"] = instance_config["instance_id"]

    structured_output = payload.get("structured_output") or {}
    constraints = [
        item
        for item in (structured_output.get("constraints") or [])
        if "execute this on" not in str(item).lower()
    ]
    structured_output["constraints"] = [f"Execute this on {explicit_target}.", *constraints]
    payload["structured_output"] = structured_output
    payload["reasoning_summary"] = f"User explicitly requested work on {explicit_target}."
    return payload


def infer_requested_instance_id(user_input: str) -> str:
    text = _normalized_text(normalize_instance_aliases(user_input))
    if "stt-a10" in text or re.search(r"\ba10\b", text) or "speech-to-text" in text:
        return "STT-A10"
    if "tts-h100" in text or re.search(r"\bh100\b", text) or "text-to-speech" in text:
        return "TTS-H100"
    if "hack" in text or "current server" in text or "this machine" in text:
        return "hack"
    return ""


def registered_instance_aliases() -> set[str]:
    refresh_instance_registry_if_needed()
    aliases = {"hack", "stt-a10", "a10", "tts-h100", "h100", "stt", "tts"}
    for instance in INSTANCE_REGISTRY:
        for raw in (
            instance.get("instance_id", ""),
            instance.get("host_alias", ""),
            instance.get("label", ""),
            instance.get("role", ""),
        ):
            text = _normalized_text(str(raw))
            compact = _compact_text(str(raw))
            if text:
                aliases.add(text)
                aliases.update(part for part in re.split(r"[^a-z0-9]+", text) if part)
            if compact:
                aliases.add(compact)
    return aliases


def looks_like_live_instance_query(user_input: str) -> bool:
    text = _normalized_text(normalize_instance_aliases(user_input))
    if "instance" not in text and "server" not in text and "worker" not in text:
        return False
    return any(term in text for term in [" live", " running", " up", " available", " reachable", " online"])


def looks_like_instance_inventory_query(user_input: str) -> bool:
    text = _normalized_text(normalize_instance_aliases(user_input))
    if "instance" not in text and "server" not in text and "worker" not in text:
        return False
    inventory_terms = [
        "how many",
        "which",
        "what",
        "do i have",
        "configured",
        "list",
        "show",
    ]
    return any(term in text for term in inventory_terms)


def looks_like_bridge_health_query(user_input: str) -> bool:
    text = _normalized_text(normalize_instance_aliases(user_input))
    bridge_terms = ["bridge", "api", "health", "healthy", "active", "down", "up", "reachable", "responding"]
    return "codex" in text and any(term in text for term in bridge_terms)


def mentions_registered_instance(user_input: str) -> bool:
    normalized_input = normalize_instance_aliases(user_input)
    text = _normalized_text(normalized_input)
    compact = _compact_text(normalized_input)
    for alias in registered_instance_aliases():
        if alias and alias in text:
            return True
        compact_alias = _compact_text(alias)
        if compact_alias and compact_alias in compact:
            return True
    return False


def looks_like_concrete_inspection_request(user_input: str) -> bool:
    text = _normalized_text(normalize_instance_aliases(user_input))
    action_terms = [
        "check",
        "inspect",
        "read",
        "show",
        "tell me",
        "what is",
        "what are",
        "running",
        "list",
        "find",
        "look up",
        "verify",
        "debug",
        "see",
    ]
    target_terms = [
        "tmux",
        "session",
        "sessions",
        "log",
        "logs",
        "process",
        "processes",
        "service",
        "services",
        "file",
        "files",
        "repo",
        "workspace",
        "test",
        "tests",
        "runtime",
        "status",
        "port",
        "ports",
    ]
    return any(term in text for term in action_terms) and any(term in text for term in target_terms)


def shared_agent_response(
    *,
    session_id: str,
    channel: str,
    user_input: str,
    context: dict | None = None,
) -> dict:
    def audit(result_payload: dict, *, normalized_text: str, explicit_target_value: str, effective_target_value: str = "") -> None:
        try:
            routing = result_payload.get("routing") or {}
            decision_payload = result_payload.get("decision") or {}
            audit_payload = {
                "created_at": utc_now(),
                "session_id": session_id,
                "channel": channel,
                "user_input": user_input,
                "normalized_input": normalized_text,
                "explicit_target": explicit_target_value,
                "effective_explicit_target": effective_target_value,
                "decision_operation": decision_payload.get("operation", ""),
                "decision_request_text": decision_payload.get("request_text", ""),
                "decision_task_id": decision_payload.get("task_id", ""),
                "routing_target_instance_id": routing.get("target_instance_id", ""),
                "routing_dispatch_title": ((routing.get("structured_output") or {}).get("dispatch_title", "")),
                "result_task_id": result_payload.get("task_id", ""),
                "speech": result_payload.get("speech", ""),
            }
            append_routing_audit(audit_payload)
            append_webcall_event("shared_agent.audit", **audit_payload, result_payload=result_payload)
        except Exception:
            pass

    append_webcall_event(
        "shared_agent.start",
        session_id=session_id,
        channel=channel,
        user_input=user_input,
        context=context or {},
    )

    session = get_session_snapshot(session_id)
    pending_mode = str(session.get("pending_twilio_mode", "")).strip()
    pending_routing = session.get("pending_routing") or {}
    pending_task_id = str(session.get("pending_twilio_task_id", "")).strip()
    pending_message_id = str(session.get("pending_twilio_message_id", "")).strip()
    waiting_task_id, waiting_message = active_task_waiting_for_response(session_id)

    if pending_mode == "dispatch_approval" and pending_routing:
        normalized = user_input.strip().lower()
        approved = normalized in {"1", "yes", "confirm", "approved", "approve"}
        clear_session_pending_twilio(session_id)
        if approved:
            dispatch_result = dispatch_via_bridge(pending_routing, session_id)
            register_dispatch_state(session_id, user_input, pending_routing, dispatch_result)
            task = dispatch_result.get("task") or {}
            return {
                "ok": True,
                "decision": {
                    "operation": "dispatch",
                    "request_text": user_input,
                    "task_id": str(task.get("task_id", "")).strip(),
                    "response_text": "",
                    "in_reply_to": "",
                    "project_hint": "",
                },
                "speech": "Approved. I started the task and will keep you updated.",
                "awaiting_user_input": False,
                "pending_mode": "",
                "task_id": str(task.get("task_id", "")).strip(),
                "routing": pending_routing,
                "dispatch": dispatch_result,
            }
        return {
            "ok": True,
            "decision": {
                "operation": "dispatch",
                "request_text": user_input,
                "task_id": "",
                "response_text": "",
                "in_reply_to": "",
                "project_hint": "",
            },
            "speech": "Okay, I did not dispatch that task.",
            "awaiting_user_input": False,
            "pending_mode": "",
            "task_id": "",
        }

    if pending_mode == "respond_to_task" and pending_task_id:
        clear_session_pending_twilio(session_id)
        payload = submit_bridge_task_response(
            pending_task_id,
            {
                "message": normalize_task_reply(user_input, waiting_message),
                "in_reply_to": pending_message_id or None,
                "metadata": {"source": f"{channel}_shared_agent"},
            },
        )
        return {
            "ok": True,
            "decision": {
                "operation": "respond",
                "request_text": "",
                "task_id": pending_task_id,
                "response_text": normalize_task_reply(user_input, waiting_message),
                "in_reply_to": pending_message_id,
                "project_hint": "",
            },
            "speech": "I sent your answer back to Codex.",
            "awaiting_user_input": False,
            "pending_mode": "",
            "task_id": pending_task_id,
            "response": payload,
        }

    normalized_pending_reply = _normalized_text(user_input)
    if waiting_task_id and waiting_message and normalized_pending_reply in {
        "1",
        "yes",
        "confirm",
        "approved",
        "approve",
        "pass",
        "go ahead",
        "proceed",
        "0",
        "no",
        "cancel",
        "reject",
        "deny",
        "stop",
    }:
        reply_text = normalize_task_reply(user_input, waiting_message)
        payload = submit_bridge_task_response(
            waiting_task_id,
            {
                "message": reply_text,
                "in_reply_to": str((waiting_message or {}).get("id", "")).strip() or None,
                "metadata": {"source": f"{channel}_shared_agent"},
            },
        )
        return {
            "ok": True,
            "decision": {
                "operation": "respond",
                "request_text": "",
                "task_id": waiting_task_id,
                "response_text": reply_text,
                "in_reply_to": str((waiting_message or {}).get("id", "")).strip(),
                "project_hint": "",
            },
            "speech": "I sent your approval back to Codex.",
            "awaiting_user_input": False,
            "pending_mode": "",
            "task_id": waiting_task_id,
            "response": payload,
        }

    if pending_mode == "clarify":
        prior_request = str(session.get("last_user_request", "")).strip()
        prompt = str(session.get("pending_twilio_prompt", "")).strip()
        combined = user_input.strip()
        if prior_request:
            combined = f"Original request: {prior_request}\nClarification response: {combined}"
        elif prompt:
            combined = f"Clarification prompt: {prompt}\nUser response: {combined}"
        clear_session_pending_twilio(session_id)
        user_input = combined

    normalized_input = normalize_instance_aliases(user_input.strip())
    context_target = str((context or {}).get("target_instance_id", "")).strip()
    if context_target not in instance_ids():
        context_target = ""
    explicit_target = infer_requested_instance_id(normalized_input) or context_target
    if looks_like_bridge_health_query(normalized_input):
        health_map = get_instance_health_snapshot()
        hack_health = health_map.get("hack") or {}
        hack_live = bool(hack_health.get("live"))
        speech = (
            "The hack Codex bridge service is up and reachable."
            if hack_live
            else "The hack Codex bridge service appears down or unreachable."
        )
        payload = {
            "ok": True,
            "decision": {
                "operation": "list_instances",
                "request_text": normalized_input,
                "task_id": "",
                "response_text": "",
                "in_reply_to": "",
                "project_hint": "",
            },
            "speech": speech,
            "awaiting_user_input": False,
            "pending_mode": "",
            "task_id": "",
            "instances": [instance_with_health(instance) for instance in INSTANCE_REGISTRY],
        }
        audit(payload, normalized_text=normalized_input, explicit_target_value=explicit_target)
        return payload

    if looks_like_live_instance_query(normalized_input):
        live_instances = [instance_with_health(instance) for instance in INSTANCE_REGISTRY]
        live_ids = [item["instance_id"] for item in live_instances if item.get("runtime", {}).get("live")]
        speech = (
            f"Live instances: {', '.join(live_ids)}."
            if live_ids
            else "No registered instances are live right now."
        )
        payload = {
            "ok": True,
            "decision": {
                "operation": "list_instances",
                "request_text": normalized_input,
                "task_id": "",
                "response_text": "",
                "in_reply_to": "",
                "project_hint": "",
            },
            "speech": speech,
            "awaiting_user_input": False,
            "pending_mode": "",
            "task_id": "",
            "instances": live_instances,
        }
        audit(payload, normalized_text=normalized_input, explicit_target_value=explicit_target)
        return payload

    if os.environ.get("OPENAI_VOICE_TOOL_AGENT", "1").strip() != "0":
        try:
            payload = run_voice_tool_agent(
                session_id=session_id,
                channel=channel,
                user_input=user_input,
                context=context or {},
            )
            effective_target = (
                str(((payload.get("routing") or {}).get("target_instance_id", ""))).strip()
                or explicit_target
            )
            audit(payload, normalized_text=normalized_input, explicit_target_value=explicit_target, effective_target_value=effective_target)
            return payload
        except Exception as exc:
            append_webcall_event(
                "voice_tool_agent.error",
                session_id=session_id,
                channel=channel,
                user_input=user_input,
                error=f"{type(exc).__name__}: {exc}",
            )

    if mentions_registered_instance(normalized_input) and looks_like_concrete_inspection_request(normalized_input):
        decision = {
            "operation": "dispatch",
            "request_text": normalized_input,
            "task_id": "",
            "response_text": "",
            "in_reply_to": "",
            "project_hint": explicit_target,
        }
    else:
        decision = run_voice_agent_decision(
            session_id=session_id,
            channel=channel,
            user_input=user_input,
            context=context or {},
        )
    operation = str(decision.get("operation", "none")).strip() or "none"
    request_text = str(decision.get("request_text", "")).strip() or user_input.strip()
    task_id = str(decision.get("task_id", "")).strip()
    response_text = str(decision.get("response_text", "")).strip() or user_input.strip()
    in_reply_to = str(decision.get("in_reply_to", "")).strip()
    project_hint = str(decision.get("project_hint", "")).strip()

    session = get_session_snapshot(session_id)
    fallback_task_id = str(session.get("active_task_id", "")).strip() or str(session.get("last_task_id", "")).strip()
    effective_task_id = task_id or fallback_task_id

    result = {
        "ok": True,
        "decision": decision,
        "speech": "",
        "awaiting_user_input": False,
        "pending_mode": "",
        "task_id": effective_task_id,
    }

    if operation == "list_instances":
        live_instances = [instance_with_health(instance) for instance in INSTANCE_REGISTRY]
        summaries = [f"{item['instance_id']} handles {item['summary']}" for item in live_instances]
        result["speech"] = ". ".join(summaries[:3]) + "."
        result["instances"] = live_instances
        audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
        return result

    if operation == "status":
        if not effective_task_id:
            result["speech"] = "There is no active task yet."
            audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
            return result
        payload = get_bridge_task(effective_task_id)
        result["task"] = payload
        result["task_id"] = effective_task_id
        result["speech"] = str(payload.get("caller_summary", "")).strip() or "I checked the task status."
        audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
        return result

    if operation == "messages":
        if not effective_task_id:
            result["speech"] = "There is no active task with messages yet."
            audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
            return result
        payload = get_bridge_task_messages(effective_task_id)
        result["messages"] = payload
        result["task_id"] = effective_task_id
        result["speech"] = latest_task_message_summary(effective_task_id)
        latest = (get_task_record_snapshot(effective_task_id) or {}).get("latest_message") or {}
        if task_message_requires_response(latest):
            result["awaiting_user_input"] = True
            result["pending_mode"] = "respond_to_task"
            result["in_reply_to"] = latest.get("id", "")
            set_session_pending_twilio(
                session_id,
                mode="respond_to_task",
                prompt=result["speech"],
                task_id=effective_task_id,
                message_id=str(latest.get("id", "")).strip(),
            )
        audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
        return result

    if operation == "respond":
        if not effective_task_id:
            result["speech"] = "There is no active task waiting for a response."
            audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
            return result
        reply_target = in_reply_to
        if not reply_target:
            _, waiting_message = active_task_waiting_for_response(session_id)
            if waiting_message:
                reply_target = str(waiting_message.get("id", "")).strip()
        payload = submit_bridge_task_response(
            effective_task_id,
            {
                "message": response_text,
                "in_reply_to": reply_target or None,
                "metadata": {
                    "source": f"{channel}_shared_agent",
                },
            },
        )
        result["response"] = payload
        result["task_id"] = effective_task_id
        result["speech"] = "I sent your answer back to Codex."
        audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
        return result

    if operation in {"route", "dispatch", "none"}:
        if not request_text:
            result["speech"] = "I need a clearer request."
            audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
            return result
        effective_explicit_target = (
            explicit_target
            or project_hint
            or infer_requested_instance_id(request_text)
            or context_target
        )
        orchestration = run_orchestration(
            {
                "transcript": request_text,
                "session_id": session_id,
                "source_channel": channel,
                "context": {
                    **(context or {}),
                    "project_hint": project_hint,
                    "target_instance_id": effective_explicit_target,
                },
            }
        )
        routing = apply_explicit_target_to_routing(orchestration.get("routing") or {}, effective_explicit_target)
        result["routing"] = routing
        result["speech"] = str(routing.get("voice_summary", "")).strip() or "I routed the request."
        if routing.get("clarification_required"):
            result["speech"] = str(routing.get("clarification_question", "")).strip() or result["speech"]
            result["awaiting_user_input"] = True
            result["pending_mode"] = "clarify"
            set_session_pending_twilio(
                session_id,
                mode="clarify",
                prompt=result["speech"],
            )
            audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target, effective_target_value=effective_explicit_target)
            return result
        if routing.get("approval_required"):
            result["speech"] = (
                f"{result['speech']} This action needs approval. Say confirm or press 1 to approve."
            ).strip()
            result["awaiting_user_input"] = True
            result["pending_mode"] = "dispatch_approval"
            set_session_pending_twilio(
                session_id,
                mode="dispatch_approval",
                prompt=result["speech"],
                routing=routing,
            )
            audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target, effective_target_value=effective_explicit_target)
            return result
        if operation == "route":
            audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target, effective_target_value=effective_explicit_target)
            return result

        dispatch_result = dispatch_via_bridge(routing, session_id)
        register_dispatch_state(session_id, request_text, routing, dispatch_result)
        result["dispatch"] = dispatch_result
        task = dispatch_result.get("task") or {}
        result["task_id"] = str(task.get("task_id", "")).strip()
        clear_session_pending_twilio(session_id)
        result["speech"] = f"{result['speech']} I started the task and will keep you updated.".strip()
        audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target, effective_target_value=effective_explicit_target)
        return result

    result["speech"] = "I did not understand the next action to take."
    audit(result, normalized_text=normalized_input, explicit_target_value=explicit_target)
    return result


class RealtimeHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(frontend_dir()), **kwargs)

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin_is_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin.strip().rstrip("/"))
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}

        if route == "/auth/session":
            account = ACCOUNT_SERVICE.current_user(self.headers.get("Cookie"))
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "authenticated": bool(account),
                    "account": account,
                    "demoCredentials": ACCOUNT_SERVICE.demo_credentials(),
                    "storagePath": str(AUTH_STATE_PATH),
                },
            )
            return
        if route == "/health":
            refresh_instance_registry_if_needed()
            health_map = get_instance_health_snapshot()
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "provider": "openai-realtime-webrtc",
                    "model": os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime"),
                    "voice": os.environ.get("OPENAI_REALTIME_VOICE", "verse"),
                    "orchestrator_model": get_orchestrator_model(),
                    "instances": INSTANCE_REGISTRY,
                    "instance_health": health_map,
                    "server_mapping_loaded": SERVER_MAPPING_PATH.exists(),
                    "frontend_source": frontend_source_label(),
                    "twilio_public_line": twilio_public_line_payload(),
                },
            )
            return
        if route == "/twilio/public-line":
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "twilio": twilio_public_line_payload(),
                },
            )
            return
        if route == "/instances":
            refresh_instance_registry_if_needed()
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "instances": [instance_with_health(instance) for instance in INSTANCE_REGISTRY],
                    "bridge_callback_url": bridge_callback_url(),
                    "health_refresh_seconds": INSTANCE_HEALTH_INTERVAL_SECONDS,
                },
            )
            return
        if route.startswith("/session/"):
            session_id = route[len("/session/") :].strip("/")
            if not session_id:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "session_id is required."})
                return
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "session": get_session_snapshot(session_id),
                    "storage_path": str(STATE_PATH),
                },
            )
            return
        if route == "/twilio/voice/inbound":
            session_id = f"twilio:{normalize_phone_number(query.get('from', '')) or query.get('call_sid', 'unknown')}"
            prompt = twilio_initial_prompt(session_id)
            xml_response(
                self,
                HTTPStatus.OK,
                twiml_response(
                    twiml_gather(prompt, twilio_inbound_action_url())
                    + twiml_say("I did not hear a response. Goodbye.")
                    + twiml_hangup()
                ),
            )
            return
        if route == "/twilio/voice/outbound":
            task_id = str(query.get("task_id", "")).strip()
            if not task_id:
                xml_response(self, HTTPStatus.BAD_REQUEST, twiml_response(twiml_say("Missing task id.") + twiml_hangup()))
                return
            message_id = str(query.get("message_id", "")).strip()
            prompt, expects_response = twilio_outbound_message(task_id, message_id)
            if expects_response:
                action_url = (
                    f"{WEBCALL_PUBLIC_BASE_URL}/twilio/voice/respond?"
                    + urlencode({"task_id": task_id, "message_id": message_id})
                )
                xml_response(
                    self,
                    HTTPStatus.OK,
                    twiml_response(
                        twiml_gather(prompt, action_url)
                        + twiml_say("I did not get a response. You can call back anytime.")
                        + twiml_hangup()
                    ),
                )
                return
            xml_response(self, HTTPStatus.OK, twiml_response(twiml_say(prompt) + twiml_hangup()))
            return
        if route.startswith("/bridge/tasks/") and route.endswith("/messages"):
            task_id = route[len("/bridge/tasks/") : -len("/messages")].strip("/")
            if not task_id:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "task_id is required."})
                return
            try:
                payload = get_bridge_task_messages(task_id)
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to fetch bridge task messages.", "details": details},
                )
                return
            except URLError as exc:
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": f"Failed to reach bridge task messages endpoint: {exc}"},
                )
                return
            json_response(self, HTTPStatus.OK, payload)
            return
        if route.startswith("/bridge/tasks/") and route.endswith("/responses"):
            task_id = route[len("/bridge/tasks/") : -len("/responses")].strip("/")
            if not task_id:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "task_id is required."})
                return
            try:
                payload = get_bridge_task_responses(task_id)
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to fetch bridge task responses.", "details": details},
                )
                return
            except URLError as exc:
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": f"Failed to reach bridge task responses endpoint: {exc}"},
                )
                return
            json_response(self, HTTPStatus.OK, payload)
            return
        if route.startswith("/bridge/tasks/"):
            task_id = route[len("/bridge/tasks/") :].strip("/")
            if not task_id:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "task_id is required."})
                return
            try:
                payload = get_bridge_task(task_id)
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to fetch bridge task.", "details": details},
                )
                return
            except URLError as exc:
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": f"Failed to reach bridge task endpoint: {exc}"},
                )
                return
            json_response(self, HTTPStatus.OK, payload)
            return

        if not frontend_export_ready():
            json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {
                    "error": "Frontend export is not present on this server.",
                    "frontend_source": frontend_source_label(),
                    "expected_index": str(EXPORTED_FRONTEND_DIR / "index.html"),
                },
            )
            return
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}

        if route == "/auth/signup":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                account, token = ACCOUNT_SERVICE.signup(
                    str(request_payload.get("name", "")),
                    str(request_payload.get("email", "")),
                    str(request_payload.get("password", "")),
                )
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "account": account, "demoCredentials": ACCOUNT_SERVICE.demo_credentials()},
                headers={"Set-Cookie": ACCOUNT_SERVICE.cookie_header(token)},
            )
            return

        if route == "/auth/login":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                account, token = ACCOUNT_SERVICE.login(
                    str(request_payload.get("email", "")),
                    str(request_payload.get("password", "")),
                )
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "account": account, "demoCredentials": ACCOUNT_SERVICE.demo_credentials()},
                headers={"Set-Cookie": ACCOUNT_SERVICE.cookie_header(token)},
            )
            return

        if route == "/auth/logout":
            ACCOUNT_SERVICE.logout(self.headers.get("Cookie"))
            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "loggedOut": True},
                headers={"Set-Cookie": ACCOUNT_SERVICE.clear_cookie_header()},
            )
            return

        if route == "/auth/profile":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                account = ACCOUNT_SERVICE.update_profile(
                    self.headers.get("Cookie"),
                    str(request_payload.get("name", "")),
                    str(request_payload.get("email", "")),
                    str(request_payload.get("phone", "")),
                )
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            json_response(self, HTTPStatus.OK, {"ok": True, "account": account})
            return

        if route == "/auth/github/connect":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                account = ACCOUNT_SERVICE.connect_github(
                    self.headers.get("Cookie"),
                    str(request_payload.get("username", "")),
                )
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to reach GitHub.", "details": details},
                )
                return
            except URLError as exc:
                json_response(self, HTTPStatus.BAD_GATEWAY, {"error": f"Failed to reach GitHub: {exc}"})
                return
            json_response(self, HTTPStatus.OK, {"ok": True, "account": account})
            return

        if route == "/auth/github/disconnect":
            try:
                account = ACCOUNT_SERVICE.disconnect_github(self.headers.get("Cookie"))
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            json_response(self, HTTPStatus.OK, {"ok": True, "account": account})
            return

        if route == "/auth/phone/send-code":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                delivery = ACCOUNT_SERVICE.send_phone_code(
                    self.headers.get("Cookie"),
                    str(request_payload.get("phone", "")),
                )
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to send SMS verification.", "details": details},
                )
                return
            except URLError as exc:
                json_response(self, HTTPStatus.BAD_GATEWAY, {"error": f"Failed to send SMS verification: {exc}"})
                return
            json_response(self, HTTPStatus.OK, {"ok": True, "verification": delivery})
            return

        if route == "/auth/phone/verify":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                account = ACCOUNT_SERVICE.verify_phone_code(
                    self.headers.get("Cookie"),
                    str(request_payload.get("phone", "")),
                    str(request_payload.get("code", "")),
                )
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            json_response(self, HTTPStatus.OK, {"ok": True, "account": account})
            return

        if route == "/auth/aws/connect":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                account = ACCOUNT_SERVICE.add_aws_connection(
                    self.headers.get("Cookie"),
                    str(request_payload.get("label", "")),
                    str(request_payload.get("instanceId", "")),
                    str(request_payload.get("region", "")),
                    str(request_payload.get("host", "")),
                )
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            json_response(self, HTTPStatus.OK, {"ok": True, "account": account})
            return

        if route == "/auth/aws/remove":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            try:
                account = ACCOUNT_SERVICE.remove_aws_connection(
                    self.headers.get("Cookie"),
                    str(request_payload.get("connectionId", "")),
                )
            except PermissionError as exc:
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
                return
            json_response(self, HTTPStatus.OK, {"ok": True, "account": account})
            return

        if route == "/token":
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "OPENAI_API_KEY is not configured on the server."},
                )
                return

            payload = {
                "session": {
                    "type": "realtime",
                    "model": os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime"),
                    "audio": {
                        "input": {
                            "transcription": {
                                "model": os.environ.get(
                                    "OPENAI_REALTIME_TRANSCRIPTION_MODEL",
                                    "gpt-4o-mini-transcribe",
                                )
                            }
                        },
                        "output": {
                            "voice": os.environ.get("OPENAI_REALTIME_VOICE", "verse"),
                        }
                    },
                }
            }

            try:
                body = openai_json_request("/realtime/client_secrets", payload, api_key)
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                print(f"OpenAI token request failed: {exc.code} {details}", file=sys.stderr)
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "error": "Failed to create realtime client secret.",
                        "details": details,
                    },
                )
                return
            except URLError as exc:
                print(f"OpenAI token request failed: {exc}", file=sys.stderr)
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to reach OpenAI for realtime client secret."},
                )
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/twilio/voice/inbound":
            form = read_form_request(self)
            call_sid = str(form.get("CallSid", "")).strip()
            from_number = normalize_phone_number(form.get("From", ""))
            session_id = f"twilio:{from_number or call_sid or 'unknown'}"
            user_input = str(form.get("SpeechResult", "")).strip() or str(form.get("Digits", "")).strip()
            if not user_input:
                prompt = twilio_initial_prompt(session_id)
                xml_response(
                    self,
                    HTTPStatus.OK,
                    twiml_response(
                        twiml_gather(prompt, twilio_inbound_action_url())
                        + twiml_say("I still did not hear a response. Goodbye.")
                        + twiml_hangup()
                    ),
                )
                return
            try:
                xml = process_twilio_user_input(session_id, call_sid, from_number, user_input)
            except Exception as exc:
                xml = twiml_response(
                    twiml_say(f"I hit an error while processing that request: {exc}") + twiml_hangup()
                )
            xml_response(self, HTTPStatus.OK, xml)
            return

        if route == "/twilio/voice/respond":
            form = read_form_request(self)
            task_id = str(query.get("task_id", "")).strip()
            message_id = str(query.get("message_id", "")).strip()
            user_input = str(form.get("SpeechResult", "")).strip() or str(form.get("Digits", "")).strip()
            if not task_id or not user_input:
                xml_response(
                    self,
                    HTTPStatus.OK,
                    twiml_response(twiml_say("I could not capture your response.") + twiml_hangup()),
                )
                return
            try:
                submit_bridge_task_response(
                    task_id,
                    {
                        "message": user_input,
                        "in_reply_to": message_id or None,
                        "metadata": {
                            "source": "twilio_outbound_call",
                            "call_sid": str(form.get("CallSid", "")).strip(),
                        },
                    },
                )
                xml = twiml_response(twiml_say("Thanks. I sent your answer back to Codex.") + twiml_hangup())
            except Exception as exc:
                xml = twiml_response(twiml_say(f"I could not submit your response: {exc}") + twiml_hangup())
            xml_response(self, HTTPStatus.OK, xml)
            return

        if route == "/twilio/voice/status":
            form = read_form_request(self)
            task_id = str(query.get("task_id", "")).strip()
            session_id = str(query.get("session_id", "")).strip()
            message_id = str(query.get("message_id", "")).strip()
            status_message = {
                "id": f"{form.get('CallSid', '')}:{form.get('CallStatus', '')}",
                "task_id": task_id,
                "kind": "info",
                "message": f"Twilio call {form.get('CallStatus', 'unknown')} for {form.get('To', '')}.",
                "expects_response": False,
                "metadata": {
                    "source": "twilio_status",
                    "call_sid": form.get("CallSid", ""),
                    "call_status": form.get("CallStatus", ""),
                    "call_duration": form.get("CallDuration", ""),
                    "direction": form.get("Direction", ""),
                    "message_id": message_id,
                },
            }
            if task_id:
                append_task_message(task_id, status_message)
                append_task_call(
                    task_id,
                    {
                        "direction": form.get("Direction", ""),
                        "sid": form.get("CallSid", ""),
                        "status": form.get("CallStatus", ""),
                        "to": form.get("To", ""),
                        "from": form.get("From", ""),
                        "message_id": message_id,
                        "updated_at": utc_now(),
                    },
                )
            if session_id:
                update_session_caller(
                    session_id,
                    twilio_user_phone_for_status_callback(form),
                    form.get("CallSid", ""),
                )
            xml_response(self, HTTPStatus.OK, twiml_response(""))
            return

        if route == "/twilio/voice/outbound/trigger":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            task_id = str(request_payload.get("task_id", "")).strip()
            message_id = str(request_payload.get("message_id", "")).strip()
            to_number = normalize_phone_number(request_payload.get("to_number", ""))
            if not task_id or not to_number:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "task_id and to_number are required."})
                return
            task = get_task_record_snapshot(task_id) or {}
            session_id = str(task.get("session_id", "")).strip()
            try:
                call = create_twilio_call(
                    to_number,
                    outbound_twiml_url(task_id, message_id),
                    twilio_call_status_callback_url(task_id, session_id, message_id),
                )
            except Exception as exc:
                json_response(self, HTTPStatus.BAD_GATEWAY, {"error": f"Failed to create Twilio call: {exc}"})
                return
            append_task_call(
                task_id,
                {
                    "direction": "outbound-api",
                    "sid": call.get("sid", ""),
                    "status": call.get("status", ""),
                    "to": to_number,
                    "message_id": message_id,
                    "created_at": utc_now(),
                },
            )
            json_response(self, HTTPStatus.OK, {"ok": True, "call": call})
            return

        if route == "/agent/respond":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            session_id = str(request_payload.get("session_id", "")).strip() or "voice-webcall"
            user_input = str(request_payload.get("user_input", "")).strip()
            append_webcall_event(
                "http.agent.respond.request",
                session_id=session_id,
                route=route,
                user_input=user_input,
                payload=request_payload,
            )
            if not user_input:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "user_input is required."})
                return
            try:
                result = shared_agent_response(
                    session_id=session_id,
                    channel=str(request_payload.get("source_channel", "")).strip() or "realtime_voice",
                    user_input=user_input,
                    context=merge_request_context(request_payload.get("context"), self.headers.get("Cookie")),
                )
            except Exception as exc:
                append_webcall_event(
                    "http.agent.respond.error",
                    session_id=session_id,
                    route=route,
                    error=f"{type(exc).__name__}: {exc}",
                    payload=request_payload,
                )
                json_response(self, HTTPStatus.BAD_GATEWAY, {"error": f"Shared voice agent failed: {exc}"})
                return
            append_webcall_event(
                "http.agent.respond.response",
                session_id=session_id,
                route=route,
                response=result,
            )
            json_response(self, HTTPStatus.OK, result)
            return

        if route == "/orchestrate":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return

            request_payload["context"] = merge_request_context(
                request_payload.get("context"),
                self.headers.get("Cookie"),
            )
            append_webcall_event("http.orchestrate.request", route=route, payload=request_payload)

            try:
                result = run_orchestration(request_payload)
            except ValueError as exc:
                append_webcall_event("http.orchestrate.error", route=route, error=str(exc), payload=request_payload)
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                print(f"OpenAI orchestration request failed: {exc.code} {details}", file=sys.stderr)
                append_webcall_event(
                    "http.orchestrate.http_error",
                    route=route,
                    status_code=exc.code,
                    details=details,
                    payload=request_payload,
                )
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "error": "Failed to create orchestration plan.",
                        "details": details,
                    },
                )
                return
            except URLError as exc:
                print(f"OpenAI orchestration request failed: {exc}", file=sys.stderr)
                append_webcall_event("http.orchestrate.url_error", route=route, error=str(exc), payload=request_payload)
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to reach OpenAI for orchestration plan."},
                )
                return
            except RuntimeError as exc:
                append_webcall_event("http.orchestrate.runtime_error", route=route, error=str(exc), payload=request_payload)
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return

            append_webcall_event("http.orchestrate.response", route=route, response=result)
            json_response(self, HTTPStatus.OK, result)
            return

        if route == "/dispatch":
            try:
                request_payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return

            session_id = str(request_payload.get("session_id", "")).strip() or "voice-webcall"
            request_payload["context"] = merge_request_context(
                request_payload.get("context"),
                self.headers.get("Cookie"),
            )
            append_webcall_event("http.dispatch.request", route=route, payload=request_payload)
            routing = request_payload.get("routing")
            if not isinstance(routing, dict):
                transcript = str(request_payload.get("transcript", "")).strip()
                if not transcript:
                    json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": "routing or transcript is required."},
                    )
                    return
                try:
                    routing = run_orchestration(request_payload)["routing"]
                except Exception as exc:
                    append_webcall_event(
                        "http.dispatch.routing_error",
                        route=route,
                        error=f"{type(exc).__name__}: {exc}",
                        payload=request_payload,
                    )
                    json_response(
                        self,
                        HTTPStatus.BAD_GATEWAY,
                        {"error": f"Failed to build routing before dispatch: {exc}"},
                    )
                    return

            try:
                if routing.get("clarification_required"):
                    json_response(
                        self,
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "routing": routing,
                            "dispatch": {
                                "ok": False,
                                "dispatchable": False,
                                "reason": "clarification_required",
                            },
                        },
                    )
                    return
                if routing.get("approval_required"):
                    json_response(
                        self,
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "routing": routing,
                            "dispatch": {
                                "ok": False,
                                "dispatchable": False,
                                "reason": "approval_required",
                            },
                        },
                    )
                    return

                dispatch_result = dispatch_via_bridge(routing, session_id)
                register_dispatch_state(
                    session_id,
                    str(request_payload.get("transcript", "")).strip(),
                    routing,
                    dispatch_result,
                )
            except ValueError as exc:
                append_webcall_event(
                    "http.dispatch.value_error",
                    route=route,
                    error=str(exc),
                    routing=routing,
                    payload=request_payload,
                )
                json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "routing": routing,
                        "dispatch": {
                            "ok": False,
                            "dispatchable": False,
                            "reason": str(exc),
                        },
                    },
                )
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                append_webcall_event(
                    "http.dispatch.http_error",
                    route=route,
                    status_code=exc.code,
                    details=details,
                    routing=routing,
                    payload=request_payload,
                )
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Bridge execute request failed.", "details": details, "routing": routing},
                )
                return
            except URLError as exc:
                append_webcall_event(
                    "http.dispatch.url_error",
                    route=route,
                    error=str(exc),
                    routing=routing,
                    payload=request_payload,
                )
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": f"Failed to reach Codex bridge: {exc}", "routing": routing},
                )
                return

            json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "routing": routing,
                    "dispatch": dispatch_result,
                },
            )
            append_webcall_event(
                "http.dispatch.response",
                route=route,
                routing=routing,
                dispatch=dispatch_result,
            )
            return

        if route == "/bridge/user-messages":
            try:
                message = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return

            task_id = str(message.get("task_id", "")).strip()
            if not task_id:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "task_id is required."})
                return

            append_webcall_event("http.bridge.user_message", route=route, task_id=task_id, message=message)
            with BRIDGE_STORE_LOCK:
                BRIDGE_MESSAGE_MIRROR.setdefault(task_id, []).append(message)
            append_task_message(task_id, message)

            callback_call = None
            try:
                callback_call = maybe_trigger_twilio_callback(task_id, message)
            except Exception:
                callback_call = None

            json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "stored": True, "task_id": task_id, "twilio_call": callback_call},
            )
            return

        if route == "/debug/frontend-event":
            try:
                payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return
            append_webcall_event("frontend.client_event", route=route, payload=payload)
            json_response(self, HTTPStatus.OK, {"ok": True})
            return

        if route.startswith("/bridge/tasks/") and route.endswith("/responses"):
            task_id = route[len("/bridge/tasks/") : -len("/responses")].strip("/")
            if not task_id:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "task_id is required."})
                return
            try:
                payload = read_json_request(self)
            except json.JSONDecodeError:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return

            try:
                result = submit_bridge_task_response(task_id, payload)
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Failed to submit bridge task response.", "details": details},
                )
                return
            except URLError as exc:
                json_response(
                    self,
                    HTTPStatus.BAD_GATEWAY,
                    {"error": f"Failed to reach bridge task response endpoint: {exc}"},
                )
                return

            json_response(self, HTTPStatus.OK, result)
            return

        json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})


def main() -> None:
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8765"))
    refresh_instance_registry_if_needed(force=True)
    refresh_instance_health()
    monitor = Thread(target=health_monitor_loop, daemon=True)
    monitor.start()
    server = ThreadingHTTPServer((host, port), RealtimeHandler)
    print(f"Serving realtime voice webcall at http://{host}:{port} from {frontend_dir()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
