# Realtime Voice Webcall

Minimal black-and-white Next.js frontend for the voice-layer control plane, still backed by the same Python server for Realtime tokens, orchestration, and Codex dispatch.

## What changed

- `app/` and `components/` now hold a proper Next.js frontend instead of a single static HTML page
- the UI now includes login, signup, GitHub connect, AWS instance tracking, phone attachment, and a cleaner control-plane dashboard
- the dashboard now uses a guided setup rail that moves users through profile, phone verification, GitHub, AWS, and then into the voice console
- auth now persists on the backend with a seeded demo account and cookie-backed sessions
- GitHub connect validates usernames against the public GitHub user API
- phone verification is backend-driven and returns a demo code unless Twilio credentials are configured
- AWS connections persist on the backend and attempt verification via the AWS CLI when it is available/configured
- the realtime voice console still talks to the existing Python endpoints: `/token`, `/instances`, `/orchestrate`, `/dispatch`
- `server.py` serves `out/` only when a real Next export exists
- Twilio voice now runs through the same shared backend agent as the web console, including dispatch, status, clarifications, approvals, and task-response turns

## Key files

- `server.py` - serves the exported frontend and handles backend token/orchestration/dispatch routes
- `app/page.js` - root Next.js page
- `components/webcall-experience.js` - dashboard UI, auth shell, integrations, and realtime client logic
- `app/globals.css` - black-and-white product styling
- `../docs/server-mapping.md` - routing context for orchestration

## Run

1. Copy `.env.example` to `.env`
2. Set `OPENAI_API_KEY`
3. Install Node.js 20+ and npm if they are not already available
4. Install frontend dependencies:

```bash
npm install
```

5. Build the exported frontend:

```bash
npm run build
```

6. Start the Python server:

```bash
python3 server.py
```

7. If you exported the frontend locally, open `http://127.0.0.1:8765`

## Optional dev setup

- Set `NEXT_PUBLIC_API_BASE=http://127.0.0.1:8765` if you want to run `next dev` against the Python backend
- Keep `NEXT_PUBLIC_API_BASE` empty when serving the exported site from the same Python server
- If you are using Vercel for the frontend, the Python server is backend-only and will not serve a fallback UI

## Vercel UI deployment

- Deploy the Next.js app to Vercel as a static site
- In Vercel, set `NEXT_PUBLIC_API_BASE` to the public HTTPS URL of the Python backend
- On the Python backend, set `WEBCALL_ALLOWED_ORIGINS` to the Vercel site origin
- If the Vercel UI needs backend login/session cookies, also set `AUTH_COOKIE_SAMESITE=None` and `AUTH_COOKIE_SECURE=true`

## Twilio public line

- Set `WEBCALL_PUBLIC_BASE_URL` to the public HTTPS URL of this Python backend
- Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER`
- Point the Twilio voice webhook for the public phone number to `POST {WEBCALL_PUBLIC_BASE_URL}/twilio/voice/inbound`
- The backend exposes `GET /twilio/public-line` with the current webhook configuration and public-line metadata
- Inbound callers now use the same shared agent flow as the web console, and the call stays open for follow-up requests instead of ending after one turn
- If a verified account phone matches the incoming caller number, the Twilio path reuses that operator context for routing

## Demo account

- Email: `demo@voicelayer.local`
- Password: `demo123`

Override these with `DEMO_ACCOUNT_NAME`, `DEMO_ACCOUNT_EMAIL`, and `DEMO_ACCOUNT_PASSWORD` in `.env`.

## Current limitations

- GitHub connect validates a username and stores the profile, but it is not full OAuth yet
- AWS attach is persistent backend state and can verify through the AWS CLI, but it is not a full IAM/OAuth flow
- SMS delivery uses Twilio only when the related environment variables are configured; otherwise the backend exposes a demo verification code for testing
