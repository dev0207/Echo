# Voice Codex Platform Plan

## Overview

Build a voice-first control plane for Codex that lets a user speak naturally, routes the request to the correct Codex instance, manages approvals and clarifications, and returns the outcome through voice, chat, or notifications.

The system should support multiple Codex instances, each with its own workspace, permissions, tool access, and operating constraints.

## Hackathon Positioning

This is not another generic coding assistant.

The product is a phone-first operating layer for Codex. A user can call or message the system from a phone, the system resolves the right engineering workspace, dispatches work to Codex, and returns outcomes through voice or chat.

That makes the idea stronger than a simple chatbot wrapper because it combines:

- multimodal input,
- routing and orchestration,
- real engineering execution,
- human-friendly update delivery.

The crisp version of the idea is:

`Call your codebase from your phone and make Codex act on the right workspace.`

## One-Line Pitch

A phone-first interface that lets you call or message Codex, route requests to the right engineering workspace, and get voice or chat updates when the task is done.

## Why This Stands Out

- It solves a real pain for engineers away from a laptop.
- It uses voice naturally instead of forcing a gimmick.
- It treats Codex as an execution worker inside a larger system.
- It has a clean input-to-outcome loop that demos well.
- It maps directly to real use cases such as on-call triage, async debugging, repo Q and A, and delegated coding work.

## Why Now

Coding agents are getting capable enough to perform real software tasks, but most workflows still assume a desktop environment.

Low-latency voice and realtime agent APIs make a spoken control surface practical. Codex provides the execution engine for real development tasks. This product bridges those two shifts.

## Core Goal

Let a user say things like:

- Check why staging deploy failed for billing.
- Fix the login timeout issue in the app repo and open a PR.
- Call me when tests finish.

And have the system:

1. understand the request,
2. route it to the right Codex instance,
3. enforce approval and safety policy,
4. execute asynchronously when needed,
5. report back clearly.

## Product Principles

- Voice is the intake and summary interface, not the full technical surface.
- The middle layer is the control plane, not a transcript forwarder.
- Codex instances are isolated workers.
- Risk classification happens before execution.
- Long-running tasks are async by default.
- Spoken output must be concise and safe.
- Every execution path must be observable and auditable.
- Route to a workspace abstraction first, not raw EC2 infrastructure.
- The product should feel designed around engineering workflows, not stitched together from APIs.

## Naming and Framing

The strongest framing is not that users can talk to servers.

The stronger framing is that engineering infrastructure becomes contactable.

Useful product language:

- Your infrastructure becomes contactable.
- Codex becomes callable.
- Manage engineering tasks from your phone, hands-free.
- Talk to your codebase.

## Layered Architecture

### Layer 1: Voice Layer

This layer handles direct interaction with the user.

Responsibilities:

- inbound and outbound calls,
- app voice sessions,
- speech-to-text,
- text-to-speech,
- interruption handling,
- confirmation collection,
- callback delivery,
- channel-aware response formatting.

Inputs:

- live audio,
- text,
- button confirmations,
- callback preferences.

Outputs:

- transcript segments,
- user confirmations,
- spoken summaries,
- callback events.

This layer should not make execution decisions.

### Layer 2: Processing / Orchestration Layer

This is the control plane of the system.

Responsibilities:

- transcript normalization,
- intent classification,
- project and instance resolution,
- policy and permission checks,
- prompt compilation,
- job lifecycle management,
- clarification handling,
- event consumption,
- result normalization,
- callback planning.

This layer converts messy human speech into safe, structured work.

### Layer 3: Codex Instance Layer

This is the execution plane.

Responsibilities:

- receive structured work payloads,
- execute in a specific workspace,
- use allowed tools,
- respect per-instance policy,
- emit structured lifecycle events,
- produce technical results and artifacts.

This layer should not own telephony, approval UX, or user identity logic.

## Expanded Runtime View

For implementation, the system should be described as 6 runtime subsystems.

### 1. Input Layer

Phone call, chat, voice note, or async request.

### 2. Understanding Layer

Intent extraction, project resolution, action classification, and response shaping.

### 3. Policy Layer

Identity, permissions, confirmation gates, allowed tools, and environment targeting.

### 4. Execution Layer

Codex operating in the selected workspace and tool context.

### 5. Result Normalization Layer

Converts raw Codex output into:

- short spoken summary,
- detailed chat log,
- follow-up action options.

### 6. Delivery Layer

In-app chat, outbound call, push, SMS, or WhatsApp-style summary.

## Runtime Subsystems

Even though the product is described in 3 layers, the runtime should be split into 6 subsystems.

1. Session Gateway
   Accepts calls, chat, and voice sessions.

2. Transcript and Turn Manager
   Maintains turns, partial transcripts, interruptions, and session context.

3. Orchestrator
   Resolves intent, instance, policy, and dispatch.

4. Policy Engine
   Computes risk class, approval requirement, and allowed actions.

5. Instance Adapter
   Converts a job into the format expected by the target Codex instance.

6. Result and Notification Manager
   Turns raw instance output into voice summaries, notifications, and next-step prompts.

## Workspace-First Routing

The router should not think in terms of raw EC2 selection first.

It should resolve requests through a workspace abstraction:

- repo or project,
- environment,
- allowed action class,
- execution target.

This keeps the system from sounding infra-fragile and makes the architecture more robust.

The selected workspace may map to an EC2-backed Codex runtime internally, but that infrastructure detail should stay behind the abstraction boundary.

## Main Control Flow

Canonical flow:

`User -> Voice Session -> Orchestrator -> Instance Adapter -> Codex Instance -> Event/Webhook -> Orchestrator -> Voice/Notification`

This separation keeps voice, policy, and execution concerns clean.

## Product Loop

The core product loop is:

`user speaks or types on phone -> system identifies the right workspace -> system normalizes the request -> system sends the task to Codex -> Codex returns output -> system normalizes the result for people -> system replies by chat or calls back with the update`

This loop is the main hackathon story and should be visible in the demo.

## User Interaction Modes

### 1. Inspect

Read-only tasks.

Examples:

- Why did staging deploy fail?
- What changed in the last PR?
- Summarize the error in production logs.

Default behavior:

- auto-run,
- no risky actions,
- fast spoken summary,
- full technical result in text.

### 2. Execute

Action-taking tasks.

Examples:

- Fix the failing test and open a PR.
- Update the config in staging.
- Add the missing endpoint and run tests.

Default behavior:

- classify risk,
- require approval where needed,
- run async if work is non-trivial,
- return progress and completion events.

### 3. Watch

Monitor or notify workflows.

Examples:

- Call me when tests pass.
- Notify me when the task is blocked.
- Tell me when the PR is ready.

Default behavior:

- bind to a job or condition,
- wait for event trigger,
- notify through user-selected channel.

## Data Model

### Job

Every request becomes a job.

Suggested fields:

- `job_id`
- `session_id`
- `user_id`
- `source_channel`
- `raw_transcript`
- `normalized_intent`
- `target_instance_id`
- `workspace_id`
- `risk_level`
- `approval_state`
- `compiled_instruction`
- `status`
- `result_summary`
- `artifact_links`
- `callback_policy`
- `created_at`
- `updated_at`

### InstanceConfig

Every Codex environment should be declared explicitly.

Suggested fields:

- `instance_id`
- `label`
- `workspace_path`
- `repo`
- `environment`
- `branch_strategy`
- `allowed_tools`
- `permission_profile`
- `routing_aliases`
- `capabilities`
- `event_webhook`
- `owner_team`
- `active`

### Session

Tracks user interaction state.

Suggested fields:

- `session_id`
- `user_id`
- `channel`
- `current_state`
- `active_job_id`
- `transcript_buffer`
- `last_user_turn`
- `last_system_turn`
- `callback_preference`

## Intent Resolution Model

The orchestration layer should resolve three things in order.

### 1. Intent Resolver

Classify the request as one of:

- `ask`
- `act`
- `watch`
- `follow_up`
- `approval_response`
- `cancel`

### 2. Context Resolver

Infer:

- project,
- repo,
- environment,
- related prior job,
- user shorthand mappings.

### 3. Execution Resolver

Decide:

- target instance,
- sync or async,
- read-only or write,
- approval required or not,
- whether clarification is required.

## Prompt Compilation

Never send raw transcript directly to a Codex instance.

The orchestrator should compile a structured execution packet with:

- user goal,
- resolved project and workspace,
- constraints,
- approval boundaries,
- desired artifacts,
- expected completion behavior,
- blocking behavior,
- callback rules.

Example structure:

- Goal: fix login timeout issue in billing app.
- Workspace: billing-service.
- Constraints: no production changes, create feature branch, run targeted tests only.
- Output: concise summary, changed files, test result, PR link if created.
- If blocked: emit `needs_input`.
- If action exceeds policy: emit `needs_permission`.

This is the main reliability boundary in the system.

## Risk and Approval Model

### Risk Classes

- `R0`: read-only analysis
- `R1`: reversible local write
- `R2`: branch and PR changes
- `R3`: environment or infra mutation
- `R4`: production or destructive action

### Default Approval Rules

- `R0`: auto-run
- `R1`: auto-run if user profile permits
- `R2`: explicit user confirmation required
- `R3`: explicit confirmation plus policy gate
- `R4`: blocked or multi-party approval

### Safety Envelope

For the first version, keep the trust model simple and explicit.

- read-only by default,
- write actions require confirmation,
- production actions are blocked or approval-gated,
- all actions are logged.

This makes the system easier to explain and easier to trust.

### Voice Confirmation Rule

Avoid loose approvals like plain `yes`.

Use deterministic confirmations.

Example:

System prompt:

`Confirm opening a pull request in billing-service staging by saying: confirm PR in billing-service.`

This reduces accidental or ambiguous approvals.

## Session State Machine

A formal state machine is required.

Suggested states:

- `listening`
- `transcribing`
- `clarifying`
- `awaiting_confirmation`
- `dispatching`
- `running`
- `awaiting_instance_input`
- `awaiting_user_input`
- `callback_pending`
- `completed`
- `failed`
- `cancelled`

This state model is critical because most engineering work is not completed in a single call.

## Event Contract

Codex instances should emit structured events.

Required event types:

- `job.accepted`
- `job.progress`
- `job.needs_input`
- `job.needs_permission`
- `job.completed`
- `job.failed`
- `job.cancelled`

Required event fields:

- `job_id`
- `instance_id`
- `timestamp`
- `status`
- `summary`
- `artifacts`
- `user_action_required`
- `retryable`

The orchestrator consumes these events and decides how to surface them to the user.

## Output Normalization

Raw Codex output must be transformed into three forms.

### 1. Spoken Summary

For voice callbacks or live response.

Constraints:

- 1 to 3 short sentences,
- no secrets,
- no large diffs,
- action-oriented language only.

### 2. Technical Summary

For chat or app view.

Should include:

- what was done,
- changed files,
- tests run,
- result,
- links to PR or artifacts,
- follow-up needed.

### 3. Action Prompt

For blocked or approval-required states.

Examples:

- I need confirmation before opening the PR.
- I need the target repo because two billing repos match.
- I am blocked because test credentials are missing.

## Multi-Instance Configuration Strategy

The system must support multiple Codex instances with different responsibilities.

Recommended configuration dimensions:

- repo or workspace,
- environment,
- team ownership,
- allowed actions,
- tool access,
- deployment permissions,
- branch strategy,
- webhook destination.

Recommended starting model:

- one instance per repo or workspace,
- environment-specific policy on top,
- avoid one instance per environment initially unless truly needed.

## Memory Strategy

### Session Memory

Short-lived context tied to an active interaction.

Examples:

- that repo,
- same task,
- open a PR too,
- call me when done.

### Operational Memory

Persistent user and platform knowledge.

Examples:

- user aliases for projects,
- preferred callback channel,
- recently used instances,
- team-specific routing rules,
- known repo and environment mappings.

This memory layer is required for robust routing and low-friction voice usage.

## Failure Modes and Design Responses

### Ambiguous Routing

Problem:

- user says fix the auth issue
- there are multiple possible repos or services

Response:

- ask a single targeted clarification,
- prefer recent session context,
- expose confidence score internally.

### Long-Running Tasks

Problem:

- coding tasks often exceed live session length

Response:

- dispatch async,
- track progress by job,
- callback or notify on completion.

### Permission Escalation

Problem:

- instance discovers a riskier action mid-task

Response:

- emit `job.needs_permission`,
- orchestrator pauses the task,
- user approves through controlled prompt.

### Noisy Voice Input

Problem:

- speech transcription is imperfect

Response:

- normalize transcript,
- ask compact clarifiers,
- use deterministic approval phrase.

### Dropped Call

Problem:

- live session ends before work completes

Response:

- preserve session and job state,
- use callback policy,
- continue async if allowed.

## Security and Safety Requirements

Minimum controls:

- caller identity verification,
- per-user authorization,
- risk classification before execution,
- signed webhooks,
- immutable audit trail,
- rate limiting,
- idempotent job submission,
- cancellation support,
- redaction of secrets in spoken summaries,
- replay protection for approvals.

## Core Use Cases

- on-call debugging,
- repo Q and A from phone,
- delegated coding tasks,
- PR and test status callbacks,
- async engineering summaries.

## MVP Scope

### Goal

Deliver a narrow but complete loop.

### Included

- one voice entry point,
- one text fallback,
- 2 to 5 Codex instances,
- inspect and execute modes,
- structured approvals,
- async callbacks,
- completion summaries,
- webhook-based event ingestion.

### Excluded Initially

- autonomous production deploys,
- complex multi-party approvals,
- multi-instance collaborative execution,
- broad proactive automation,
- fully open-ended voice sessions,
- Sora in the main demo path.

## Suggested MVP Demo

User says:

`Check why staging deploy failed for billing.`

System should:

1. transcribe and normalize intent,
2. resolve the billing instance,
3. run an inspect task,
4. return a concise spoken summary,
5. optionally let the user say `fix it and open a PR`,
6. collect approval,
7. dispatch async,
8. call back on completion.

This is the minimum full-system proof.

## Strong Demo Flows

### Demo 1: Inbound Voice

User says:

`Call my Codex. Check why the staging deploy failed for repo X.`

System behavior:

1. identify the project and workspace,
2. run read-only inspection,
3. summarize the failure,
4. propose the next action.

Example callback:

`The deploy failed because REDIS_URL is missing in staging. I prepared a fix and a PR draft. Do you want me to apply it?`

### Demo 2: Outbound Callback

User sends:

`Refactor auth middleware and tell me when tests pass.`

System behavior:

1. dispatch async,
2. track progress,
3. place an outbound update when done.

Example callback:

`The auth middleware refactor is complete. Tests passed. I created branch codex/auth-refactor and opened a PR.`

### Demo 3: Mobile-First Summary

User says:

`Summarize what changed in the past 24 hours on onboarding-service.`

System behavior:

1. inspect commits, PRs, and issues,
2. produce a spoken digest,
3. attach a richer chat summary.

## Hackathon Story

The strongest hackathon story is:

- engineers are often away from a laptop,
- engineering agents are becoming capable,
- voice makes access immediate,
- orchestration makes that access safe and useful.

The differentiator is not `chat with code`.

The differentiator is telephony-grade orchestration for coding agents.

## Judge-Facing Value

Judges will likely respond to these strengths:

- clear pain point,
- visible end-to-end loop,
- multimodal interaction that feels natural,
- credible use of Codex as a real worker,
- strong demoability,
- safety envelope that shows restraint.

## Clarity Risk and Mitigation

The main risk is sounding too broad.

Bad framing:

`A voice interface for all coding infrastructure.`

Better framing:

`Call your codebase from your phone and make Codex act on the right workspace.`

Mitigation tactics:

- stay focused on one narrow demo path,
- keep the product language concrete,
- keep EC2 as implementation detail,
- emphasize workspace routing and outcome delivery.

## Team Split

Team composition:

- 1 product manager,
- 2 full-stack developers.

### Product Manager

Owns:

- user journeys,
- approval and policy rules,
- routing language and naming model,
- callback UX,
- acceptance criteria,
- metrics and MVP scope.

Main outputs:

- top user flows,
- approval matrix,
- edge-case decision log,
- test transcripts,
- completion quality criteria.

### Developer 1: Voice + Orchestrator Owner

Owns:

- session gateway,
- transcript handling,
- state machine,
- intent parsing,
- job orchestration,
- approval flow,
- callback logic,
- spoken and text summary generation.

### Developer 2: Instance Runtime Owner

Owns:

- instance registry,
- dispatch adapter,
- Codex integration contract,
- structured event emission,
- artifact packaging,
- permission metadata,
- local testing harness.

### Shared Early Contract Work

All three must align on:

- `Job` schema,
- `InstanceConfig` schema,
- event contract,
- approval payload,
- summary format,
- MVP cutline.

## Build Plan

### Phase 0: Design Lock

- finalize user journeys,
- define instance model,
- define risk classes,
- define event schema,
- define summary contract.

### Phase 1: Control Plane Skeleton

- build session model,
- build job model,
- implement orchestration endpoints,
- implement fake transcript input,
- implement fake instance output.

### Phase 2: Execution Plane Integration

- add instance registry,
- add dispatch adapter,
- add structured lifecycle events,
- add artifact model,
- connect real Codex runtime.

### Phase 3: Approval and Callback Flows

- implement confirmation flow,
- implement callback scheduling,
- implement blocked-state handling,
- implement async completion loop.

### Phase 4: Quality and Safety

- add audit logs,
- add retries and idempotency,
- add redaction,
- add observability,
- test dropped call and ambiguous routing cases.

## Suggested Milestones

### Milestone 1

User can submit a read-only request and hear a spoken result.

### Milestone 2

User can approve a write action and receive async completion.

### Milestone 3

System can handle blocked jobs, clarifications, and callback retries.

### Milestone 4

System supports multiple named instances with stable routing.

## Observability

Track at minimum:

- routing accuracy,
- clarification rate,
- approval rejection rate,
- approval ambiguity rate,
- completion rate,
- callback latency,
- instance failure rate,
- average job duration,
- number of blocked jobs.

## Roadmap Extensions

After the core Codex loop works, the platform can expand from `talk to Codex` into `talk to your AI workers`.

Possible workers:

- Codex for code tasks,
- image generation for UI assets,
- support or incident summarization agents,
- Sora-like video generation for release and stakeholder outputs.

The key is to keep these extensions use-case based rather than generic.

Good roadmap examples:

- create a release demo video from the new feature branch,
- turn a PR summary into a launch clip,
- generate a product walkthrough for a shipped UI,
- create a visual incident summary for stakeholders.

This should remain roadmap material until the Codex loop is fully working.

## Open Questions

These should be resolved before implementation begins in earnest.

- What is the initial voice channel: phone, app, or both?
- What is the first supported Codex runtime model?
- What exact actions are allowed in MVP?
- What approval classes are permitted by default?
- What is the fallback path when voice confirmation fails?
- How are user-to-project aliases seeded?
- What is the first artifact surface: app UI, SMS, email, or chat?

## Recommended First Decision Set

To keep the build tight, the team should decide these immediately:

1. single primary input channel,
2. single event schema,
3. single instance config format,
4. single confirmation style,
5. single callback mechanism,
6. single MVP demo path.

## Final Recommendation

Treat the system as a control plane over isolated Codex workers.

- Voice layer handles intake and summary.
- Processing layer owns control, policy, and state.
- Instance layer owns execution.

Keep the initial story narrow:

`Today you can talk to Codex from your phone. Later you can talk to all your AI workers.`

This separation is what makes the system configurable, safe, and scalable across multiple Codex instances.