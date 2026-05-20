# Codex Bridge Service

FastAPI worker service that turns a structured request into a real `codex exec` run inside a target workspace, then exposes lifecycle state and user-facing messages.

## Why this service exists

The voice/orchestration layer should not execute coding tasks directly.

This bridge provides a clean execution boundary:

- accepts safe, structured task requests,
- executes Codex in a known workspace and sandbox,
- normalizes raw output into concise status + summary,
- stores task messages and optional user responses,
- forwards updates to webhooks when needed.

## Architecture

```mermaid
flowchart LR
  O[Orchestrator or LiveKit agent] --> E[POST /api/v1/tasks/execute]
  E --> X[Run codex exec in workspace]
  X --> P[Parse stdout or fallback output]
  P --> S[Store task state and summary]
  X --> M[POST /api/v1/user-messages]
  S --> Q[GET /api/v1/tasks/{task_id}]
  M --> N[GET /api/v1/tasks/{task_id}/messages]
  O --> R[POST /api/v1/tasks/{task_id}/responses]
  X --> R2[GET /api/v1/tasks/{task_id}/responses/next]
```

## Key Features

- Async task execution with in-memory task lifecycle:
  `queued -> running -> succeeded/failed`.
- Sandboxing per request:
  `read-only`, `workspace-write`, `danger-full-access`.
- Output normalization:
  extracts final report text, summary, keywords, and technical terms.
- Message channel:
  stores Codex progress/questions/approvals and optional downstream webhook delivery.
- Response channel:
  orchestration layer can answer pending Codex questions through a task response API.
- JSONL audit log under `data/bridge-events.jsonl`.

## API Endpoints

### Health

- `GET /health`

### Task lifecycle

- `POST /api/v1/tasks/execute`
- `GET /api/v1/tasks/{task_id}`

### User-facing task messages

- `POST /api/v1/user-messages`
- `GET /api/v1/tasks/{task_id}/messages`

### User responses to task questions/approvals

- `POST /api/v1/tasks/{task_id}/responses`
- `GET /api/v1/tasks/{task_id}/responses`
- `GET /api/v1/tasks/{task_id}/responses/next`

## Install

```bash
cd /home/ubuntu/hack
python3 -m venv .venv
.venv/bin/pip install -r codex-bridge-service/requirements.txt
```

## Run

```bash
cd /home/ubuntu/hack
.venv/bin/uvicorn codex-bridge-service.app:app --host 0.0.0.0 --port 8800
```

Quick check:

```bash
curl -sS http://127.0.0.1:8800/health
```

## Environment

Minimal vars:

- `CODEX_BIN` default: `codex`
- `CODEX_BRIDGE_PUBLIC_BASE_URL` default: `http://127.0.0.1:8800`
- `CODEX_BRIDGE_INLINE_CALLBACKS` default: `0`

Example:

```env
CODEX_BIN=codex
CODEX_BRIDGE_PUBLIC_BASE_URL=http://127.0.0.1:8800
CODEX_BRIDGE_INLINE_CALLBACKS=0
```

## Execute Example

```bash
curl -sS -X POST http://127.0.0.1:8800/api/v1/tasks/execute \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "Inspect docs and summarize architecture in 6 bullets.",
    "workspace_path": "/home/ubuntu/hack",
    "timeout_seconds": 900,
    "sandbox": "read-only",
    "public_base_url": "http://127.0.0.1:8800"
  }'
```

Typical response:

```json
{
  "task_id": "<uuid>",
  "status": "queued",
  "poll_url": "http://127.0.0.1:8800/api/v1/tasks/<uuid>",
  "messages_url": "http://127.0.0.1:8800/api/v1/tasks/<uuid>/messages",
  "responses_url": "http://127.0.0.1:8800/api/v1/tasks/<uuid>/responses"
}
```

## Deploying on Remote Workers

Each worker machine should have:

- Codex CLI installed and authenticated.
- access to its target workspace path.
- this bridge service running on `:8800`.

Then update orchestrator routing via `instance-registry.json` on the control-plane host.

## Public Repo Safety

- Do not commit real `instance-registry.json`.
- Do not commit runtime logs in `data/`.
- Do commit only templates (`instance-registry.example.json`, `.env.example`).
