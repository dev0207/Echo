# Account Setup Flow

## Goal

Reduce time-to-first-successful-route by making the account journey linear, visible, and easy to resume.

## Flow

### 1. Account access

- `Sign up` creates a backend account and lands the user in guided setup.
- `Log in` restores the cookie-backed session and resumes guided setup.
- `Use demo account` logs in without manual credential entry for test sessions.

### 2. Guided setup sequence

1. `Profile`
   Save name, email, and optional phone number.
   Outcome: operator identity exists for routed work.
2. `Verify phone`
   Send verification code, then confirm it.
   Outcome: recovery and identity-sensitive actions are backed by a verified number.
3. `Connect GitHub`
   Validate a GitHub username and persist repository identity.
   Outcome: backend tasks have stronger source context.
4. `Attach AWS`
   Save at least one AWS target.
   Outcome: dispatch and infra follow-up can reference concrete hosts or instance IDs.
5. `Voice console`
   Route, dispatch, or start a live call with the best available context.

## State Rules

- The overview progress bar is based on the four account setup tasks.
- The guided rail shows all steps plus the final voice-console handoff.
- After a successful step completion, the UI scrolls to the next incomplete step.
- If all four setup tasks are complete, the next target becomes the voice console.
- Voice readiness is considered good at 3 of 4 setup steps complete, but the console remains available even before that threshold.

## Interaction Notes

- Clicking a guided rail item focuses the corresponding panel.
- The current panel receives a stronger visual treatment so users know where they are in the flow.
- Phone verification exposes the internal sub-sequence explicitly:
  1. Save phone
  2. Send code
  3. Confirm code
- Starter prompts in the voice console reduce blank-state friction during evaluation and operator testing.

## Operational Notes

- The flow does not alter the existing AWS persistence model.
- The flow does not require any backend schema or API changes.
- The flow remains compatible with the current local Python server and exported Next.js frontend.
