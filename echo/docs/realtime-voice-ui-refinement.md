# Realtime Voice UI Refinement

## Outcome

The realtime voice webcall now uses a guided setup model instead of a flat dashboard. The updated interface keeps the existing Next.js to Python contract intact while making progression through account setup clearer and making the voice console easier to enter with enough context.

## Implemented Design Spec

### 1. Entry and authentication

- Keep sign up, log in, and demo login on the same screen.
- Add a visible setup sequence preview so first-time users understand the path before they authenticate.
- Add entry-path guidance for new operators versus fast demo evaluation.
- Preserve backend cookie auth and the existing demo account behavior.

### 2. Authenticated workspace progression

- Keep the 4 core setup tasks: profile, phone verification, GitHub, AWS.
- Add a guided setup rail with explicit step numbers, current focus state, completion state, and direct jump actions.
- Automatically move the user to the next recommended setup panel after a successful profile save, phone verification, GitHub connect, AWS attach, or auth completion.
- Highlight the currently focused panel so the dashboard reads as a sequence instead of a loose collection of forms.

### 3. Step-specific interaction refinements

- Profile remains the identity anchor and now explains why it comes first.
- Phone verification now exposes the sequence visually as:
  1. Save phone
  2. Send code
  3. Confirm
- GitHub and AWS panels now include short rationale callouts tied to dispatch quality and infrastructure accuracy.
- Keyboard focus styling is explicit for buttons, setup rail items, inputs, and starter cards.

### 4. Voice console handoff

- Treat voice readiness as guidance rather than a hard gate.
- Show the highest-impact missing step inside the console when readiness is incomplete.
- Add starter routing prompts for common operator tasks so the composer is easier to use during testing and demos.
- Keep route, dispatch, and live call functionality unchanged.

## Interaction Model

### Landing state

- User chooses `Sign up`, `Log in`, or `Use demo account`.
- The setup preview communicates the full path before account creation.

### Authenticated state

- The overview panel shows progress, the next recommended action, and the full guided rail.
- Clicking a rail item scrolls to the matching panel.
- Completing a step advances the user to the next incomplete step automatically.
- The voice console remains available at all times, but incomplete readiness keeps the next missing step visible.

## AWS and Backend Compatibility

- No backend routes changed.
- Existing endpoints remain the source of truth: `/auth/*`, `/instances`, `/token`, `/orchestrate`, `/dispatch`.
- AWS connections still persist through the current backend service and continue using the existing verification behavior.
- No new service boundary or AWS integration mechanism was introduced.

## Acceptance Criteria

- A restored authenticated session lands on the next incomplete setup step instead of a generic dashboard start.
- Users can move between setup steps from a single rail without losing the current backend-backed forms.
- Phone verification exposes the send and confirm sequence explicitly.
- The voice console can direct users back to the missing setup step without blocking routing entirely.
