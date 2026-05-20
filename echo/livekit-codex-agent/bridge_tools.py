from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
HACK_ROOT = ROOT.parent
ENV_PATH = HACK_ROOT / ".env"
INSTANCE_REGISTRY_PATH = HACK_ROOT / "instance-registry.json"
DEFAULT_TIMEOUT = 45


load_dotenv(ENV_PATH, override=False)


@dataclass
class SessionState:
    last_task_id: str = ""
    last_instance_id: str = "hack"
    task_instance_map: dict[str, str] = field(default_factory=dict)
    job_ctx: Any | None = None
    agent_session: Any | None = None
    outbound_trunk_id: str = ""


def compact_whitespace(value: str) -> str:
    return " ".join(str(value or "").split())


def load_instance_registry() -> list[dict[str, Any]]:
    try:
        payload = json.loads(INSTANCE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict) and item.get("instance_id"):
            normalized.append(item)
    return normalized


def get_instance(instance_id: str) -> dict[str, Any] | None:
    wanted = str(instance_id or "").strip()
    if not wanted:
        return None
    for instance in load_instance_registry():
        if str(instance.get("instance_id", "")).strip() == wanted:
            return instance
    return None


def instance_registry_summary() -> str:
    lines: list[str] = []
    for instance in load_instance_registry():
        lines.append(
            "- {instance_id}: {summary} (ip: {ssh_host}; bridge: {bridge_base_url}; workspace: {workspace_path})".format(
                instance_id=instance.get("instance_id", ""),
                summary=instance.get("summary", "No summary available."),
                ssh_host=instance.get("ssh_host", "unknown"),
                bridge_base_url=instance.get("bridge_base_url", "not configured"),
                workspace_path=instance.get("workspace_path", "not configured"),
            )
        )
    return "\n".join(lines) if lines else "- No registered instances were found."


def _json_request(url: str, payload: dict[str, Any] | None = None, method: str = "POST") -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
        raw = response.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _get_json(url: str) -> dict[str, Any]:
    request = Request(url, method="GET")
    with urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
        raw = response.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


async def http_json_request(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "POST",
) -> dict[str, Any]:
    return await asyncio.to_thread(_json_request, url, payload, method)


async def http_get_json(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_get_json, url)


def classify_action_mode(request_text: str) -> str:
    lowered = request_text.lower()
    read_only_overrides = (
        "do not modify",
        "don't modify",
        "do not change",
        "don't change",
        "read-only",
        "read only",
        "without modifying",
        "no file changes",
        "no changes",
    )
    if any(term in lowered for term in read_only_overrides):
        return "read_only"

    write_terms = (
        "fix",
        "implement",
        "create",
        "update",
        "modify",
        "change",
        "write",
        "edit",
        "patch",
        "refactor",
        "add",
        "remove",
        "delete",
    )
    return "write" if any(term in lowered for term in write_terms) else "read_only"


def infer_target_instance(request_text: str, explicit_instance_id: str = "") -> str:
    explicit = str(explicit_instance_id or "").strip()
    if explicit:
        return explicit

    lowered = request_text.lower()
    if any(term in lowered for term in ("stt", "speech to text", "transcribe", "transcription", "diarization", "diarize")):
        return "STT-A10"
    if any(term in lowered for term in ("tts", "text to speech", "speech synthesis", "voice generation", "synthesize voice")):
        return "TTS-H100"
    return "hack"


def build_bridge_execute_payload(request_text: str, target_instance_id: str) -> dict[str, Any]:
    instance = get_instance(target_instance_id)
    if instance is None:
        raise ValueError(f"Unknown instance: {target_instance_id}")

    bridge_base_url = str(instance.get("bridge_base_url", "")).strip().rstrip("/")
    bridge_execute_endpoint = str(instance.get("bridge_execute_endpoint", "")).strip()
    workspace_path = str(instance.get("workspace_path", "")).strip()

    if not bridge_base_url or not bridge_execute_endpoint:
        raise ValueError(f"Instance {target_instance_id} does not have a bridge endpoint configured.")
    if not workspace_path:
        raise ValueError(f"Instance {target_instance_id} does not have a workspace path configured.")

    action_mode = classify_action_mode(request_text)
    sandbox = "workspace-write" if action_mode == "write" else "read-only"
    timeout_seconds = 1800 if action_mode == "write" else 600

    extra_instructions = "\n".join(
        [
            "Caller channel: LiveKit voice agent.",
            "Operate like a concise developer with server access.",
            "Do the work directly; do not turn this into a chatty conversation.",
            "If blocked, state the blocker plainly in the final report.",
        ]
    )

    return {
        "target_instance_id": target_instance_id,
        "bridge_url": f"{bridge_base_url}{bridge_execute_endpoint}",
        "execute_payload": {
            "prompt": request_text.strip(),
            "workspace_path": workspace_path,
            "timeout_seconds": timeout_seconds,
            "summary_words": 100,
            "sandbox": sandbox,
            "public_base_url": bridge_base_url,
            "extra_instructions": extra_instructions,
        },
    }


def resolve_task_id(state: SessionState, task_id: str = "") -> str:
    candidate = str(task_id or "").strip()
    if candidate:
        return candidate
    if state.last_task_id:
        return state.last_task_id
    raise ValueError("No task_id was provided and there is no active task in this session yet.")


def resolve_task_instance_id(state: SessionState, task_id: str = "") -> str:
    resolved_task_id = resolve_task_id(state, task_id)
    return state.task_instance_map.get(resolved_task_id, state.last_instance_id or "hack")


async def probe_instance_health(instance: dict[str, Any]) -> dict[str, Any]:
    bridge_base_url = str(instance.get("bridge_base_url", "")).strip().rstrip("/")
    if not bridge_base_url:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": "bridge_base_url_missing",
            "health": None,
        }
    try:
        payload = await http_get_json(f"{bridge_base_url}/health")
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": bool(payload.get("ok")),
            "reachable": True,
            "reason": "ok" if payload.get("ok") else "health_not_ok",
            "health": payload,
        }
    except HTTPError as exc:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": f"http_error:{exc.code}",
            "health": None,
        }
    except URLError as exc:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": f"url_error:{exc.reason}",
            "health": None,
        }
    except Exception as exc:
        return {
            "instance_id": instance.get("instance_id", ""),
            "live": False,
            "reachable": False,
            "reason": f"error:{type(exc).__name__}",
            "health": None,
        }


async def list_instances(*, only_live: bool = False) -> dict[str, Any]:
    payloads: list[dict[str, Any]] = []
    for instance in load_instance_registry():
        enriched = dict(instance)
        enriched["runtime"] = await probe_instance_health(instance)
        payloads.append(enriched)
    if only_live:
        payloads = [item for item in payloads if item.get("runtime", {}).get("live")]
    return {
        "count": len(payloads),
        "instances": payloads,
    }


async def check_bridge_health(instance_id: str = "") -> dict[str, Any]:
    requested = str(instance_id or "").strip()
    if requested:
        instance = get_instance(requested)
        if instance is None:
            raise ValueError(f"Unknown instance: {requested}")
        return {
            "instance_id": requested,
            "health": await probe_instance_health(instance),
        }
    health_map: dict[str, Any] = {}
    for instance in load_instance_registry():
        health_map[str(instance.get("instance_id", ""))] = await probe_instance_health(instance)
    return {"health": health_map}


async def route_request(request_text: str, target_instance_id: str = "") -> dict[str, Any]:
    resolved = infer_target_instance(request_text, explicit_instance_id=target_instance_id)
    instance = get_instance(resolved)
    if instance is None:
        raise ValueError(f"Unable to resolve instance for request: {request_text}")
    return {
        "target_instance_id": resolved,
        "ssh_host": instance.get("ssh_host", ""),
        "bridge_base_url": instance.get("bridge_base_url", ""),
        "workspace_path": instance.get("workspace_path", ""),
        "action_mode": classify_action_mode(request_text),
        "reason": (
            "Matched explicit instance."
            if target_instance_id
            else "Heuristic routing selected the best-fit instance for the request."
        ),
    }


async def dispatch_codex_request(
    *,
    state: SessionState,
    request_text: str,
    target_instance_id: str = "",
) -> dict[str, Any]:
    route = await route_request(request_text, target_instance_id=target_instance_id)
    bridge_request = build_bridge_execute_payload(request_text, route["target_instance_id"])
    response = await http_json_request(
        bridge_request["bridge_url"],
        bridge_request["execute_payload"],
        method="POST",
    )
    task_id = str(response.get("task_id", "")).strip()
    if task_id:
        state.last_task_id = task_id
        state.task_instance_map[task_id] = route["target_instance_id"]
    state.last_instance_id = route["target_instance_id"]
    return {
        "ok": True,
        "target_instance_id": route["target_instance_id"],
        "ssh_host": route["ssh_host"],
        "workspace_path": bridge_request["execute_payload"]["workspace_path"],
        "sandbox": bridge_request["execute_payload"]["sandbox"],
        "timeout_seconds": bridge_request["execute_payload"]["timeout_seconds"],
        "task_id": task_id,
        "poll_url": response.get("poll_url", ""),
        "messages_url": response.get("messages_url", ""),
        "responses_url": response.get("responses_url", ""),
    }


async def get_task_status(*, state: SessionState, task_id: str = "") -> dict[str, Any]:
    resolved_task_id = resolve_task_id(state, task_id)
    instance = get_instance(resolve_task_instance_id(state, resolved_task_id)) or get_instance("hack")
    if instance is None:
        raise ValueError("Could not resolve the bridge instance for this session.")
    bridge_base_url = str(instance.get("bridge_base_url", "")).strip().rstrip("/")
    payload = await http_get_json(f"{bridge_base_url}/api/v1/tasks/{resolved_task_id}")
    state.last_task_id = resolved_task_id
    return payload


async def get_task_messages(*, state: SessionState, task_id: str = "") -> dict[str, Any]:
    resolved_task_id = resolve_task_id(state, task_id)
    instance = get_instance(resolve_task_instance_id(state, resolved_task_id)) or get_instance("hack")
    if instance is None:
        raise ValueError("Could not resolve the bridge instance for this session.")
    bridge_base_url = str(instance.get("bridge_base_url", "")).strip().rstrip("/")
    payload = await http_get_json(f"{bridge_base_url}/api/v1/tasks/{resolved_task_id}/messages")
    state.last_task_id = resolved_task_id
    return payload


async def respond_to_task(
    *,
    state: SessionState,
    message: str,
    task_id: str = "",
    in_reply_to: str = "",
) -> dict[str, Any]:
    resolved_task_id = resolve_task_id(state, task_id)
    instance = get_instance(resolve_task_instance_id(state, resolved_task_id)) or get_instance("hack")
    if instance is None:
        raise ValueError("Could not resolve the bridge instance for this session.")
    bridge_base_url = str(instance.get("bridge_base_url", "")).strip().rstrip("/")
    payload = await http_json_request(
        f"{bridge_base_url}/api/v1/tasks/{resolved_task_id}/responses",
        {
            "message": message.strip(),
            "in_reply_to": in_reply_to or None,
            "metadata": {"source": "livekit-codex-agent"},
        },
        method="POST",
    )
    state.last_task_id = resolved_task_id
    return payload


def format_tool_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
