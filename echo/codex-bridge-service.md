# Codex Bridge Service

FastAPI service for dispatching a text task to Codex CLI and receiving user-facing callbacks from Codex.

## Run

```bash
.venv/bin/uvicorn app:app --app-dir codex-bridge-service --host 0.0.0.0 --port 8800
```

Base URL:

```text
http://127.0.0.1:8800
```

## Endpoints

### `GET /health`

Quick health check.

### `POST /api/v1/tasks/execute`

Submits a Codex task in the selected workspace and returns immediately with:

- `task_id`
- `status`
- `poll_url`
- `messages_url`

Example:

```bash
python - <<'PY'
import json
from urllib.request import Request, urlopen

payload = {
    "prompt": "Read the docs folder and summarize the platform architecture.",
    "workspace_path": "/home/ubuntu/hack",
    "public_base_url": "http://127.0.0.1:8800",
    "timeout_seconds": 180
}

req = Request(
    "http://127.0.0.1:8800/api/v1/tasks/execute",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)

print(urlopen(req).read().decode())
PY
```

Important request fields:

- `prompt`: work to do
- `workspace_path`: repo/workspace path
- `public_base_url`: base URL Codex can call back into
- `timeout_seconds`: execution timeout
- `summary_words`: max summary length, up to 100
- `sandbox`: `read-only`, `workspace-write`, or `danger-full-access`
- `user_webhook_url`: optional downstream webhook for forwarded user messages

### `POST /api/v1/user-messages`

Called by Codex when it needs to inform the user, ask a question, or request approval.

Payload:

```json
{
  "task_id": "your-task-id",
  "kind": "info",
  "message": "Working on the task now.",
  "expects_response": false,
  "metadata": {
    "source": "codex"
  }
}
```

Kinds:

- `info`
- `question`
- `approval`
- `warning`

### `GET /api/v1/tasks/{task_id}`

Returns stored task status and output.

Status values:

- `queued`
- `running`
- `succeeded`
- `failed`

When complete, this endpoint includes:

- `summary`
- `keywords`
- `tech_terms`
- `codex_output`
- `raw_stdout`
- `raw_stderr`
- `error`

### `GET /api/v1/tasks/{task_id}/messages`

Returns stored callback messages for that task.

## Live test result

Real test completed on `2026-04-18`:

- request hit `POST /api/v1/tasks/execute`
- service returned `status: succeeded`
- Codex read the docs and produced a technical architecture report
- callback delivery is implemented with Python `urllib.request`, so it does not depend on `curl`

## Current limitation

Callback delivery still depends on the `public_base_url` being reachable from the machine running Codex. If callbacks are missing, verify that the bridge can reach the URL you sent in the execute request and that any reverse tunnel or public proxy is still active.

## Polling example

Submit:

```bash
curl -sS -X POST http://127.0.0.1:8800/api/v1/tasks/execute \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "Read the /home/ubuntu/hack/realtime-voice-webcall folder and tell me what is in it in exactly 50 words.",
    "workspace_path": "/home/ubuntu/hack",
    "public_base_url": "http://127.0.0.1:8800",
    "timeout_seconds": 180
  }'
```

Poll:

```bash
curl -sS http://127.0.0.1:8800/api/v1/tasks/YOUR_TASK_ID
```
