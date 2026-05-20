"use client";

import { useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "";
const BUILD_ID = "next-source-v2";
const MAX_FEED_ITEMS = 32;
const SESSION_STORAGE_KEY = "realtime-voice-webcall-session-id";
const DEFAULT_REALTIME_DISPATCHER_PROMPT = [
  "You are the live voice dispatcher for a Codex control plane.",
  "Role:",
  "- You are not a general assistant.",
  "- Speak like a terse engineering operator.",
  "- Keep replies short and concrete.",
  "",
  "Build:",
  "- This client build is {{BUILD_ID}}.",
  "",
  "Rules:",
  "- Every operational user utterance must go through the `handle_voice_request` tool before you answer.",
  "- If the user says prompt Codex, ask Codex, send this to Codex, use Codex on this, or have Codex do something, treat that as operational and call `handle_voice_request`.",
  "- Do not answer from model memory about instances, routing, task state, tmux, logs, files, repos, tests, services, or machine state.",
  "- Never claim work executed unless backend or tool output confirms it.",
  "- The current machine running this voice server is the `hack` instance.",
  "- If the user asks for hack, this server, or this machine, that means `hack`.",
  "- Registered backend instances:",
  "{{INSTANCE_SUMMARY}}",
  "- Use `handle_voice_request` for instances, routing, dispatch, task status, approvals, blockers, and follow-up answers.",
  "- Use `ping_server` only for explicit backend health checks.",
  "- Use `end_call` only when the user clearly wants to end the call.",
  "- After `handle_voice_request` returns, prefer the tool field `speech` as the spoken reply.",
  "- Keep spoken replies to one or two short sentences unless the user explicitly asks for detail.",
].join("\n");

const DEFAULT_ACCOUNT = {
  id: "",
  isAuthenticated: false,
  name: "",
  email: "",
  phone: "",
  phoneVerified: false,
  github: {
    connected: false,
    username: "",
    connectedAt: "",
    profileUrl: "",
    avatarUrl: "",
    name: "",
  },
  awsConnections: [],
  lastLoginAt: "",
};

const DEFAULT_AUTH_FORM = {
  name: "",
  email: "",
  password: "",
};

const DEFAULT_GITHUB_FORM = {
  username: "",
};

const DEFAULT_AWS_FORM = {
  label: "",
  instanceId: "",
  region: "us-east-1",
  host: "",
};

const DEFAULT_PHONE_STATE = {
  code: "",
  challengeId: "",
  expiresAt: "",
  delivery: "",
  demoCode: "",
};

const INITIAL_COMPOSER =
  "Route a task to the best instance for refining the realtime voice product UI and tightening the account setup flow.";

const SETUP_PANEL_ORDER = ["profile", "phone", "github", "aws", "voice-console"];

const STARTER_REQUESTS = [
  {
    id: "ui-audit",
    label: "UI friction audit",
    description: "Review confusing interactions in the voice shell and suggest focused fixes.",
    prompt:
      "Audit the realtime voice UI for confusing states, weak affordances, and unclear transitions. Recommend specific interaction fixes without changing the backend contract.",
  },
  {
    id: "setup-flow",
    label: "Setup completion plan",
    description: "Tighten the account onboarding path and reduce abandonment between steps.",
    prompt:
      "Improve the account setup flow for the realtime voice product. Focus on logical progression, better step guidance, and fewer repeated actions for profile, phone verification, GitHub, and AWS setup.",
  },
  {
    id: "aws-readiness",
    label: "AWS readiness check",
    description: "Validate whether the current account has enough infrastructure context for dispatch.",
    prompt:
      "Check whether the current workspace has enough AWS context for production-style routing. Highlight missing infrastructure details that could slow down dispatch or follow-up work.",
  },
];

function formatStamp(value = new Date()) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(value);
}

function formatDetailedStamp(value) {
  if (!value) {
    return "Not yet";
  }

  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function stringifyValue(value) {
  if (typeof value === "string") {
    return value;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

function withApiDefaults(options = {}) {
  return {
    credentials: "include",
    ...options,
  };
}

function getStableSessionId() {
  if (typeof window === "undefined") {
    return `session-${Date.now()}`;
  }

  const existing = window.localStorage.getItem(SESSION_STORAGE_KEY);
  if (existing) {
    return existing;
  }

  const created =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `session-${Date.now()}`;
  window.localStorage.setItem(SESSION_STORAGE_KEY, created);
  return created;
}

function normalizeAccount(payload) {
  return {
    ...DEFAULT_ACCOUNT,
    ...(payload || {}),
    github: {
      ...DEFAULT_ACCOUNT.github,
      ...((payload || {}).github || {}),
    },
    awsConnections: Array.isArray((payload || {}).awsConnections)
      ? payload.awsConnections
      : [],
  };
}

function normalizeIntentText(value = "") {
  return value.trim().toLowerCase().replace(/\s+/g, " ");
}

function buildAccountContext(account) {
  return {
    account_email: account.email,
    account_name: account.name,
    github_username: account.github.username,
    phone_verified: account.phoneVerified,
    connected_aws_instances: account.awsConnections.map((item) => item.instanceId).filter(Boolean),
    aws_connections: account.awsConnections.map((item) => ({
      id: item.id || "",
      label: item.label || "",
      instance_id: item.instanceId || "",
      region: item.region || "",
      host: item.host || "",
      verified: Boolean(item.verified),
      verification_reason: item.verificationReason || "",
    })),
  };
}

function looksLikeOperationalVoiceRequest(value = "") {
  const text = normalizeIntentText(value);
  if (!text) {
    return false;
  }

  const patterns = [
    "codex",
    "dispatch",
    "route",
    "run",
    "execute",
    "start",
    "prompt",
    "ask",
    "inspect",
    "check",
    "fix",
    "debug",
    "repo",
    "repository",
    "workspace",
    "task",
    "status",
    "progress",
    "instance",
    "instances",
    "hack",
    "a10",
    "h100",
    "stt",
    "tts",
  ];

  return patterns.some((pattern) => text.includes(pattern));
}

function buildSetupTasks(account) {
  return [
    {
      id: "profile",
      step: "01",
      title: "Complete workspace profile",
      description: "Add your name and contact email so routed work is tied to an operator.",
      complete: Boolean(account.name && account.email),
      impact: "Required",
      actionLabel: "Save profile",
      helper: "Identity comes first so every later action is tied to an operator.",
    },
    {
      id: "phone",
      step: "02",
      title: "Verify recovery phone",
      description: "Use SMS or demo verification before identity-sensitive actions.",
      complete: Boolean(account.phoneVerified),
      impact: "Security",
      actionLabel: "Verify phone",
      helper: "Operators missed this when it was buried inside profile editing.",
    },
    {
      id: "github",
      step: "03",
      title: "Connect GitHub",
      description: "Attach repository identity so backend tasks include source context.",
      complete: Boolean(account.github?.connected),
      impact: "Context",
      actionLabel: "Link GitHub",
      helper: "Repository identity reduces clarification when dispatch reaches Codex.",
    },
    {
      id: "aws",
      step: "04",
      title: "Attach at least one AWS instance",
      description: "Save infrastructure targets so routed work can reference production hosts.",
      complete: account.awsConnections.length > 0,
      impact: "Infra",
      actionLabel: "Add AWS target",
      helper: "Infrastructure context makes routing and approvals more accurate.",
    },
  ];
}

function getRecommendedSetupStep(tasks) {
  return tasks.find((task) => !task.complete) || null;
}

function Panel({ eyebrow, title, detail, actions, className = "", panelId, panelRef, children }) {
  return (
    <section id={panelId} ref={panelRef} tabIndex={-1} className={`panel ${className}`.trim()}>
      <div className="panel-head">
        <div>
          {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
          <h2>{title}</h2>
        </div>
        {actions ? <div className="panel-actions">{actions}</div> : null}
      </div>
      {detail ? <p className="panel-detail">{detail}</p> : null}
      {children}
    </section>
  );
}

function Feed({ items, emptyLabel, renderItem }) {
  if (!items.length) {
    return (
      <div className="empty-state">
        <p>{emptyLabel}</p>
      </div>
    );
  }

  return <div className="feed">{items.map(renderItem)}</div>;
}

export default function WebcallExperience({ realtimeDispatcherPrompt = "" }) {
  const [hydrated, setHydrated] = useState(false);
  const [authMode, setAuthMode] = useState("signup");
  const [account, setAccount] = useState(DEFAULT_ACCOUNT);
  const [authForm, setAuthForm] = useState(DEFAULT_AUTH_FORM);
  const [githubForm, setGithubForm] = useState(DEFAULT_GITHUB_FORM);
  const [awsForm, setAwsForm] = useState(DEFAULT_AWS_FORM);
  const [composer, setComposer] = useState(INITIAL_COMPOSER);
  const [instances, setInstances] = useState([]);
  const [health, setHealth] = useState(null);
  const [sessionSnapshot, setSessionSnapshot] = useState(null);
  const [statusMode, setStatusMode] = useState("idle");
  const [statusLabel, setStatusLabel] = useState("Idle");
  const [events, setEvents] = useState([]);
  const [transcriptEntries, setTranscriptEntries] = useState([]);
  const [toolResults, setToolResults] = useState([]);
  const [dispatchCards, setDispatchCards] = useState([]);
  const [busyAction, setBusyAction] = useState("");
  const [demoCredentials, setDemoCredentials] = useState(null);
  const [phoneState, setPhoneState] = useState(DEFAULT_PHONE_STATE);
  const [profileDraft, setProfileDraft] = useState({
    name: "",
    email: "",
    phone: "",
  });
  const [activeSetupStep, setActiveSetupStep] = useState("profile");

  const pcRef = useRef(null);
  const dcRef = useRef(null);
  const localStreamRef = useRef(null);
  const remoteAudioRef = useRef(null);
  const handledToolCallsRef = useRef(new Set());
  const completedTranscriptItemsRef = useRef(new Set());
  const processedOperationalTranscriptItemsRef = useRef(new Set());
  const seenTaskMessageIdsRef = useRef(new Set());
  const announcedUpdateKeysRef = useRef(new Set());
  const taskWatcherRef = useRef(null);
  const activeTaskIdRef = useRef("");
  const lastTaskStatusSignatureRef = useRef("");
  const sessionIdRef = useRef(getStableSessionId());
  const realtimeSessionConfiguredRef = useRef(false);
  const lastSessionUpdateAtRef = useRef(0);
  const lastToolCallAtRef = useRef(0);
  const setupSectionRefs = useRef({});
  const didInitializeSetupStepRef = useRef(false);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        await Promise.all([fetchHealth(), fetchInstances(), fetchAuthSession(), fetchSessionState()]);
      } catch (error) {
        if (!cancelled) {
          addToolResult("Bootstrap warning", String(error), "warning");
        }
      } finally {
        if (!cancelled) {
          setHydrated(true);
        }
      }
    }

    bootstrap();

    return () => {
      cancelled = true;
      endCall(true);
    };
  }, []);

  useEffect(() => {
    if (!account.isAuthenticated) {
      didInitializeSetupStepRef.current = false;
      setActiveSetupStep("profile");
      return;
    }

    if (!didInitializeSetupStepRef.current || !SETUP_PANEL_ORDER.includes(activeSetupStep)) {
      didInitializeSetupStepRef.current = true;
      setActiveSetupStep(getRecommendedSetupStep(buildSetupTasks(account))?.id || "voice-console");
    }
  }, [
    account.isAuthenticated,
    account.name,
    account.email,
    account.phoneVerified,
    account.github.connected,
    account.awsConnections.length,
    activeSetupStep,
  ]);

  function prependEntry(setter, entry) {
    setter((current) => [entry, ...current].slice(0, MAX_FEED_ITEMS));
  }

  function addEvent(payload) {
    prependEntry(setEvents, {
      id:
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`,
      stamp: formatStamp(),
      body: stringifyValue(payload),
    });
  }

  function addTranscript(speaker, body) {
    prependEntry(setTranscriptEntries, {
      id:
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`,
      stamp: formatStamp(),
      speaker,
      body,
    });
  }

  function addToolResult(title, body, tone = "default") {
    prependEntry(setToolResults, {
      id:
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`,
      stamp: formatStamp(),
      title,
      body: stringifyValue(body),
      tone,
    });
  }

  function addDispatchCard(title, detail, meta = []) {
    prependEntry(setDispatchCards, {
      id:
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`,
      stamp: formatStamp(),
      title,
      detail,
      meta,
    });
  }

  function syncAccount(nextAccount) {
    const normalized = normalizeAccount(nextAccount);
    setAccount(normalized);
    setProfileDraft({
      name: normalized.name || "",
      email: normalized.email || "",
      phone: normalized.phone || "",
    });
    setAuthForm((current) => ({
      ...current,
      email: normalized.email || current.email || "",
      password: "",
    }));
    setGithubForm({
      username: normalized.github?.username || "",
    });
    return normalized;
  }

  function setSetupSectionRef(stepId, node) {
    if (!stepId) {
      return;
    }

    if (node) {
      setupSectionRefs.current[stepId] = node;
      return;
    }

    delete setupSectionRefs.current[stepId];
  }

  function focusSetupStep(stepId, options = {}) {
    if (!stepId) {
      return;
    }

    setActiveSetupStep(stepId);

    if (typeof window === "undefined") {
      return;
    }

    window.setTimeout(() => {
      const node = setupSectionRefs.current[stepId];
      if (!node) {
        return;
      }

      node.scrollIntoView({
        behavior: options.instant ? "auto" : "smooth",
        block: "start",
      });

      if (options.focus === false) {
        return;
      }

      const target = node.querySelector("input, textarea, button");
      if (target && typeof target.focus === "function") {
        target.focus({ preventScroll: true });
      }
    }, options.delay ?? 40);
  }

  function focusNextSetupStep(accountSnapshot, fallbackStep = "voice-console") {
    const nextTask = getRecommendedSetupStep(buildSetupTasks(accountSnapshot));
    focusSetupStep(nextTask?.id || fallbackStep);
  }

  function useStarterPrompt(prompt) {
    setComposer(prompt);
    focusSetupStep("voice-console", { focus: false, delay: 0 });
  }

  function renderRealtimeDispatcherPrompt() {
    const template = (realtimeDispatcherPrompt || DEFAULT_REALTIME_DISPATCHER_PROMPT).trim();
    return template
      .replaceAll("{{BUILD_ID}}", BUILD_ID)
      .replaceAll("{{INSTANCE_SUMMARY}}", formatInstanceSummary());
  }

  function debugRequestPreview(body) {
    if (!body) {
      return "";
    }
    if (typeof body === "string") {
      return body.length > 4000 ? `${body.slice(0, 4000)}...[truncated]` : body;
    }
    return stringifyValue(body);
  }

  async function debugFrontendEvent(event, payload = {}) {
    const body = JSON.stringify({
      event,
      build_id: BUILD_ID,
      session_id: sessionIdRef.current,
      stamp: new Date().toISOString(),
      ...payload,
    });

    try {
      if (typeof navigator !== "undefined" && navigator.sendBeacon && body.length < 60000) {
        const blob = new Blob([body], { type: "application/json" });
        navigator.sendBeacon(apiUrl("/debug/frontend-event"), blob);
        return;
      }

      await fetch(
        apiUrl("/debug/frontend-event"),
        withApiDefaults({
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body,
          keepalive: true,
        }),
      );
    } catch {}
  }

  async function requestJson(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const requestPreview = debugRequestPreview(options.body);

    if (path !== "/debug/frontend-event") {
      console.debug("[webcall] request", { path, method, requestPreview });
      void debugFrontendEvent("request.start", {
        path,
        method,
        request_preview: requestPreview,
      });
    }

    let response;
    try {
      response = await fetch(apiUrl(path), withApiDefaults(options));
    } catch (error) {
      if (path !== "/debug/frontend-event") {
        console.warn("[webcall] network error", { path, method, error: String(error) });
        void debugFrontendEvent("request.network_error", {
          path,
          method,
          request_preview: requestPreview,
          error: String(error),
        });
      }
      throw error;
    }

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (path !== "/debug/frontend-event") {
        console.warn("[webcall] request failed", { path, method, status: response.status, payload });
        void debugFrontendEvent("request.error", {
          path,
          method,
          status: response.status,
          request_preview: requestPreview,
          response_payload: payload,
        });
      }
      throw new Error(payload.error || "Request failed.");
    }
    if (path !== "/debug/frontend-event") {
      console.debug("[webcall] response", { path, method, status: response.status, payload });
      void debugFrontendEvent("request.done", {
        path,
        method,
        status: response.status,
        response_payload: payload,
      });
    }
    return payload;
  }

  async function fetchHealth() {
    const payload = await requestJson("/health");
    setHealth(payload);
    return payload;
  }

  async function fetchInstances() {
    const payload = await requestJson("/instances");
    setInstances(Array.isArray(payload.instances) ? payload.instances : []);
    return payload.instances || [];
  }

  async function fetchSessionState() {
    const payload = await requestJson(`/session/${encodeURIComponent(sessionIdRef.current)}`);
    const session = payload.session || null;
    setSessionSnapshot(session);
    activeTaskIdRef.current = session?.active_task_id || "";
    return payload;
  }

  async function fetchTaskStatus(taskId) {
    return requestJson(`/bridge/tasks/${encodeURIComponent(taskId)}`);
  }

  async function fetchTaskMessages(taskId) {
    return requestJson(`/bridge/tasks/${encodeURIComponent(taskId)}/messages`);
  }

  async function fetchTaskResponses(taskId) {
    return requestJson(`/bridge/tasks/${encodeURIComponent(taskId)}/responses`);
  }

  async function submitTaskResponse(taskId, message, inReplyTo = null) {
    return requestJson(`/bridge/tasks/${encodeURIComponent(taskId)}/responses`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        message,
        in_reply_to: inReplyTo,
        metadata: {
          source: "voice-webcall",
          session_id: sessionIdRef.current,
        },
      }),
    });
  }

  async function fetchAuthSession() {
    const payload = await requestJson("/auth/session");
    setDemoCredentials(payload.demoCredentials || null);
    if (payload.account) {
      syncAccount(payload.account);
    } else {
      syncAccount(DEFAULT_ACCOUNT);
    }
    return payload;
  }

  async function fetchToken() {
    const response = await fetch(
      apiUrl("/token"),
      withApiDefaults({
      method: "POST",
      }),
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || "Failed to create realtime token.");
    }
    return payload;
  }

  async function requestOrchestration(transcript, context = {}) {
    return requestJson("/orchestrate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        transcript,
        session_id: sessionIdRef.current,
        source_channel: "realtime_voice",
        context: {
          ...buildAccountContext(account),
          ...(context || {}),
        },
      }),
    });
  }

  async function requestDispatch(requestText) {
    return requestJson("/dispatch", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        transcript: requestText,
        session_id: sessionIdRef.current,
        source_channel: "dashboard",
        context: buildAccountContext(account),
      }),
    });
  }

  async function requestSharedVoiceAgent(requestText) {
    return requestJson("/agent/respond", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        user_input: requestText,
        session_id: sessionIdRef.current,
        source_channel: "realtime_voice",
        context: {
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          language: navigator.language,
          build_id: BUILD_ID,
          ...buildAccountContext(account),
        },
      }),
    });
  }

  async function handleOperationalTranscriptFallback(itemId, transcript) {
    const normalized = normalizeIntentText(transcript);
    if (!normalized || !looksLikeOperationalVoiceRequest(normalized)) {
      return;
    }
    if (processedOperationalTranscriptItemsRef.current.has(itemId)) {
      return;
    }
    processedOperationalTranscriptItemsRef.current.add(itemId);

    const scheduledAt = Date.now();
    window.setTimeout(async () => {
      if (lastToolCallAtRef.current >= scheduledAt) {
        return;
      }

      try {
        const payload = await requestSharedVoiceAgent(transcript);
        const routing = payload.routing || {};
        const structuredOutput = routing.structured_output || {};
        const taskId = payload.task_id || payload.dispatch?.task?.task_id || "";

        addToolResult("Voice fallback decision", {
          speech: payload.speech,
          decision: payload.decision,
          task_id: taskId || null,
          awaiting_user_input: payload.awaiting_user_input || false,
        });

        if (routing.target_instance_id || structuredOutput.dispatch_title) {
          addDispatchCard(
            structuredOutput.dispatch_title || "Voice task prepared",
            payload.speech || routing.voice_summary || routing.reasoning_summary || "Voice fallback handled the request.",
            [
              `Instance ${routing.target_instance_id || "unknown"}`,
              taskId ? `Task ${taskId}` : "No task yet",
            ]
          );
        }

        if (taskId && taskId !== "pending") {
          activeTaskIdRef.current = taskId;
          await fetchSessionState().catch(() => null);
          await pollTaskState(taskId).catch(() => null);
          startTaskWatcher();
        }

        if (payload.speech) {
          queueAssistantUpdate(payload.speech);
        }
      } catch (error) {
        addToolResult("Voice fallback error", String(error), "warning");
      }
    }, 1500);
  }

  function setRealtimeStatus(mode, label) {
    setStatusMode(mode);
    setStatusLabel(label);
  }

  function getKnownTaskId(explicitTaskId = "") {
    return (
      explicitTaskId ||
      activeTaskIdRef.current ||
      sessionSnapshot?.active_task_id ||
      ""
    );
  }

  function formatInstanceSummary(list = instances) {
    if (!list.length) {
      return "No backend instances loaded yet.";
    }

    return list
      .map((instance) => {
        const workspace = instance.workspace_path ? ` Workspace: ${instance.workspace_path}.` : "";
        const runtime = instance.runtime || {};
        const status = runtime.live ? "live" : "not live";
        const reason = runtime.reason ? ` Status detail: ${runtime.reason}.` : "";
        return `${instance.instance_id}: ${instance.summary}.${workspace} Runtime: ${status}.${reason}`;
      })
      .join("\n");
  }

  function buildRoutingVoiceBrief(routing = {}) {
    const structuredOutput = routing.structured_output || {};
    const target = routing.target_instance_id || "unknown";
    const dispatchTitle = structuredOutput.dispatch_title || "Untitled dispatch";
    const approval = routing.approval_required ? "Approval is required." : "No approval is required.";
    const clarification = routing.clarification_required
      ? `Clarification needed: ${routing.clarification_question || "missing detail."}`
      : "No clarification is required.";
    const prompt = structuredOutput.codex_prompt || "No Codex prompt was generated.";

    return `${dispatchTitle}. Route this to ${target}. ${approval} ${clarification} Codex prompt: ${prompt}`;
  }

  function sendEvent(payload) {
    if (!dcRef.current || dcRef.current.readyState !== "open") {
      throw new Error("Realtime data channel is not open.");
    }

    dcRef.current.send(JSON.stringify(payload));
    addEvent({ direction: "client", payload });
  }

  function buildRealtimeSessionConfig() {
    return {
      instructions: buildVoiceAgentInstructions(),
      tools: registerTools(),
      tool_choice: "required",
    };
  }

  function refreshRealtimeSession(reason = "") {
    if (!dcRef.current || dcRef.current.readyState !== "open") {
      return false;
    }

    sendEvent({
      type: "session.update",
      session: buildRealtimeSessionConfig(),
    });
    realtimeSessionConfiguredRef.current = false;
    lastSessionUpdateAtRef.current = Date.now();
    if (reason) {
      addToolResult("Realtime session update", `Reasserted session policy: ${reason}`);
    }
    return true;
  }

  function createResponse(reason = "") {
    const now = Date.now();
    const needsRefresh =
      !realtimeSessionConfiguredRef.current || now - lastSessionUpdateAtRef.current > 15000;

    if (needsRefresh) {
      refreshRealtimeSession(reason || "response.create");
    }
    sendEvent({ type: "response.create" });
  }

  function queueAssistantUpdate(text) {
    if (!text || !dcRef.current || dcRef.current.readyState !== "open") {
      return;
    }

    addTranscript("System", text);
    sendEvent({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [
          {
            type: "input_text",
            text: `System task update. This is not a new user request. Briefly tell the caller: ${text}`,
          },
        ],
      },
    });
    createResponse("system task update");
  }

  function rememberAnnouncement(key) {
    if (!key) {
      return false;
    }
    if (announcedUpdateKeysRef.current.has(key)) {
      return false;
    }
    announcedUpdateKeysRef.current.add(key);
    return true;
  }

  function processTaskStatusUpdate(payload) {
    const taskId = payload.task_id || activeTaskIdRef.current;
    if (!taskId) {
      return;
    }

    const signature = [
      taskId,
      payload.status || "",
      payload.completed_at || "",
      payload.summary || "",
      payload.error || "",
    ].join("|");
    if (signature === lastTaskStatusSignatureRef.current) {
      return;
    }
    lastTaskStatusSignatureRef.current = signature;

    const callerSummary = payload.caller_summary || payload.summary || `Task is ${payload.status}.`;
    addToolResult(`Task update: ${taskId}`, {
      status: payload.status,
      caller_summary: callerSummary,
      completed_at: payload.completed_at || null,
      error: payload.error || null,
    });

    if (payload.status === "queued" || payload.status === "running") {
      if (rememberAnnouncement(`status:${signature}`)) {
        queueAssistantUpdate(callerSummary);
      }
      return;
    }

    if (payload.status === "succeeded" || payload.status === "failed") {
      stopTaskWatcher();
      if (activeTaskIdRef.current === taskId) {
        activeTaskIdRef.current = "";
      }
    }

    if ((payload.status === "succeeded" || payload.status === "failed") && rememberAnnouncement(`final:${signature}`)) {
      queueAssistantUpdate(callerSummary);
    }
  }

  function processTaskMessagesUpdate(payload) {
    const combined = [...(payload.messages || []), ...(payload.mirrored_messages || [])];
    for (const message of combined) {
      const messageId = message.id || `${message.kind}:${message.message}`;
      if (seenTaskMessageIdsRef.current.has(messageId)) {
        continue;
      }
      seenTaskMessageIdsRef.current.add(messageId);
      addToolResult(`Task message: ${message.kind || "info"}`, message.message || "");

      const shouldAnnounce =
        message.expects_response || message.kind === "warning" || message.kind === "approval";
      if (shouldAnnounce && rememberAnnouncement(`message:${messageId}`)) {
        queueAssistantUpdate(message.message || "There is a new task update.");
      }
    }
  }

  async function pollTaskState(explicitTaskId = "") {
    const taskId = getKnownTaskId(explicitTaskId);
    if (!taskId) {
      return;
    }

    const [taskStatus, taskMessages, nextSessionState] = await Promise.all([
      fetchTaskStatus(taskId),
      fetchTaskMessages(taskId),
      fetchSessionState().catch(() => null),
    ]);

    if (taskStatus.task_id) {
      activeTaskIdRef.current = taskStatus.task_id;
    }
    if (nextSessionState?.session) {
      setSessionSnapshot(nextSessionState.session);
    }

    processTaskStatusUpdate(taskStatus);
    processTaskMessagesUpdate(taskMessages);
  }

  function startTaskWatcher() {
    if (taskWatcherRef.current) {
      window.clearInterval(taskWatcherRef.current);
    }
    taskWatcherRef.current = window.setInterval(() => {
      pollTaskState().catch((error) => {
        addToolResult("Task watcher error", String(error), "warning");
      });
    }, 6000);
  }

  function stopTaskWatcher() {
    if (taskWatcherRef.current) {
      window.clearInterval(taskWatcherRef.current);
      taskWatcherRef.current = null;
    }
  }

  function buildVoiceAgentInstructions() {
    return renderRealtimeDispatcherPrompt();
  }

  function registerTools() {
    return [
      {
        type: "function",
        name: "handle_voice_request",
        description:
          "Send every Codex control-plane request to the backend shared voice agent and speak back its exact result.",
        parameters: {
          type: "object",
          properties: {
            request_text: {
              type: "string",
              description: "The user's spoken request.",
            },
          },
          required: ["request_text"],
          additionalProperties: false,
        },
      },
      {
        type: "function",
        name: "ping_server",
        description: "Checks whether the local backend is healthy.",
        parameters: {
          type: "object",
          properties: {},
          additionalProperties: false,
        },
      },
      {
        type: "function",
        name: "end_call",
        description: "Ends the active voice call after confirming with the user request.",
        parameters: {
          type: "object",
          properties: {
            reason: {
              type: "string",
              description: "Short reason for ending the call.",
            },
          },
          required: ["reason"],
          additionalProperties: false,
        },
      },
    ];
  }

  async function runTool(name, args) {
    switch (name) {
      case "handle_voice_request": {
        const payload = await requestSharedVoiceAgent(args.request_text);
        const routing = payload.routing || {};
        const structuredOutput = routing.structured_output || {};
        const taskId = payload.task_id || payload.dispatch?.task?.task_id || "";
        if (taskId && taskId !== "pending") {
          activeTaskIdRef.current = taskId;
          await fetchSessionState().catch(() => null);
          await pollTaskState(taskId).catch(() => null);
          startTaskWatcher();
        }
        addToolResult("Shared voice agent", {
          speech: payload.speech,
          decision: payload.decision,
          task_id: taskId || null,
          awaiting_user_input: payload.awaiting_user_input || false,
          routing,
        });
        if (routing.target_instance_id || structuredOutput.dispatch_title) {
          addDispatchCard(
            structuredOutput.dispatch_title || "Voice task prepared",
            payload.speech || routing.voice_summary || routing.reasoning_summary || "Voice agent handled the request.",
            [
              `Instance ${routing.target_instance_id || "unknown"}`,
              taskId ? `Task ${taskId}` : "No task yet",
            ]
          );
        }
        return {
          ok: payload.ok,
          speech: payload.speech,
          decision: payload.decision,
          awaiting_user_input: payload.awaiting_user_input || false,
          task_id: taskId === "pending" ? null : taskId,
          routing,
          voice_brief: payload.speech,
          raw_response: payload,
        };
      }
      case "ping_server": {
        const payload = await fetchHealth();
        addToolResult("Server health", `Healthy: ${payload.ok ? "yes" : "no"}`);
        return payload;
      }
      case "end_call":
        addToolResult("Call ended by tool", args.reason || "No reason provided.");
        endCall();
        return { ended: true, reason: args.reason || null };
      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  }

  async function handleToolCall(item) {
    if (handledToolCallsRef.current.has(item.call_id)) {
      return;
    }
    handledToolCallsRef.current.add(item.call_id);
    lastToolCallAtRef.current = Date.now();

    let parsedArguments = {};
    try {
      parsedArguments = item.arguments ? JSON.parse(item.arguments) : {};
    } catch (error) {
      parsedArguments = {
        parse_error: String(error),
        raw: item.arguments,
      };
    }

    addToolResult(`Tool call: ${item.name}`, parsedArguments);

    try {
      const result = await runTool(item.name, parsedArguments);
      sendEvent({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: item.call_id,
          output: JSON.stringify(result),
        },
      });
      createResponse(`tool output: ${item.name}`);
    } catch (error) {
      sendEvent({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: item.call_id,
          output: JSON.stringify({ ok: false, error: String(error) }),
        },
      });
      createResponse(`tool error: ${item.name}`);
      addToolResult("Tool handler error", String(error), "warning");
    }
  }

  function handleRealtimeEvent(event) {
    addEvent({ direction: "server", payload: event });

    if (event.type === "session.created") {
      setRealtimeStatus("connecting", "Configuring");
      refreshRealtimeSession("session.created");
      return;
    }

    if (event.type === "session.updated") {
      realtimeSessionConfiguredRef.current = true;
      setRealtimeStatus("live", "Live");
      addToolResult("Realtime session ready", "Session instructions and tools acknowledged.");
      return;
    }

    if (event.type === "response.output_audio_transcript.done" && event.transcript) {
      addTranscript("Assistant", event.transcript);
      return;
    }

    if (
      event.type === "conversation.item.input_audio_transcription.completed" &&
      event.transcript
    ) {
      addTranscript("You", event.transcript);
      handleOperationalTranscriptFallback(
        event.item_id || event.item?.id || event.event_id || event.transcript,
        event.transcript
      ).catch((error) => {
        addToolResult("Voice fallback error", String(error), "warning");
      });
      return;
    }

    if (event.type === "response.output_item.done" && event.item?.type === "function_call") {
      handleToolCall(event.item).catch((error) => {
        addToolResult("Tool handler error", String(error), "warning");
      });
      return;
    }

    if (
      event.type === "conversation.item.done" &&
      event.item?.type === "message" &&
      event.item?.role === "assistant"
    ) {
      if (completedTranscriptItemsRef.current.has(event.item.id)) {
        return;
      }

      const transcript = (event.item.content || [])
        .filter((part) => part.type === "audio" || part.type === "text")
        .map((part) => part.transcript || part.text || "")
        .join(" ")
        .trim();

      if (transcript) {
        completedTranscriptItemsRef.current.add(event.item.id);
        addTranscript("Assistant", transcript);
      }
      return;
    }

    if (event.type === "response.done") {
      for (const item of event.response?.output || []) {
        if (item.type === "function_call") {
          handleToolCall(item).catch((error) => {
            addToolResult("Tool handler error", String(error), "warning");
          });
        }
      }
    }
  }

  async function startCall() {
    setBusyAction("call");
    setRealtimeStatus("connecting", "Connecting");

    try {
      await fetchInstances();
      await fetchSessionState().catch(() => null);
      const tokenData = await fetchToken();
      const ephemeralKey = tokenData.value;
      if (!ephemeralKey) {
        throw new Error("Token response did not include a client secret.");
      }

      const pc = new RTCPeerConnection();
      pcRef.current = pc;

      pc.ontrack = (event) => {
        if (remoteAudioRef.current) {
          remoteAudioRef.current.srcObject = event.streams[0];
        }
      };

      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      localStreamRef.current = stream;
      for (const track of stream.getTracks()) {
        pc.addTrack(track, stream);
      }

      const dc = pc.createDataChannel("oai-events");
      dcRef.current = dc;

      dc.addEventListener("open", () => {
        setRealtimeStatus("connecting", "Configuring");
      });
      dc.addEventListener("message", (event) => {
        handleRealtimeEvent(JSON.parse(event.data));
      });
      dc.addEventListener("close", () => {
        setRealtimeStatus("idle", "Idle");
      });

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      const sdpResponse = await fetch("https://api.openai.com/v1/realtime/calls", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${ephemeralKey}`,
          "Content-Type": "application/sdp",
        },
        body: offer.sdp,
      });

      if (!sdpResponse.ok) {
        throw new Error(`Realtime SDP exchange failed: ${await sdpResponse.text()}`);
      }

      await pc.setRemoteDescription({
        type: "answer",
        sdp: await sdpResponse.text(),
      });

      addToolResult("Call connected", "Realtime voice session is live.");
      if (getKnownTaskId()) {
        await pollTaskState().catch(() => null);
        if (getKnownTaskId()) {
          startTaskWatcher();
        }
      }
    } catch (error) {
      addToolResult("Connection error", String(error), "warning");
      endCall();
    } finally {
      setBusyAction("");
    }
  }

  function endCall(silent = false) {
    stopTaskWatcher();

    if (dcRef.current) {
      try {
        dcRef.current.close();
      } catch {}
    }

    if (pcRef.current) {
      try {
        pcRef.current.close();
      } catch {}
    }

    if (localStreamRef.current) {
      for (const track of localStreamRef.current.getTracks()) {
        track.stop();
      }
    }

    if (remoteAudioRef.current) {
      remoteAudioRef.current.srcObject = null;
    }

    pcRef.current = null;
    dcRef.current = null;
    localStreamRef.current = null;
    handledToolCallsRef.current = new Set();
    completedTranscriptItemsRef.current = new Set();
    processedOperationalTranscriptItemsRef.current = new Set();
    seenTaskMessageIdsRef.current = new Set();
    announcedUpdateKeysRef.current = new Set();
    lastTaskStatusSignatureRef.current = "";
    realtimeSessionConfiguredRef.current = false;
    lastSessionUpdateAtRef.current = 0;
    lastToolCallAtRef.current = 0;
    setRealtimeStatus("idle", "Idle");

    if (!silent) {
      addEvent({ direction: "local", payload: { type: "call.closed" } });
    }
  }

  function updateProfileField(field, value) {
    setProfileDraft((current) => ({
      ...current,
      [field]: value,
    }));
    if (field === "phone") {
      setPhoneState(DEFAULT_PHONE_STATE);
    }
  }

  async function saveProfile(event) {
    event.preventDefault();
    setBusyAction("profile");
    try {
      const payload = await requestJson("/auth/profile", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: profileDraft.name.trim(),
          email: profileDraft.email.trim(),
          phone: profileDraft.phone.trim(),
        }),
      });
      const nextAccount = syncAccount(payload.account);
      addToolResult("Profile updated", payload.account);
      focusNextSetupStep(nextAccount);
    } catch (error) {
      addToolResult("Profile update failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function submitAuth(event) {
    event.preventDefault();
    setBusyAction("auth");
    try {
      const payload = await requestJson(authMode === "signup" ? "/auth/signup" : "/auth/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: authForm.name.trim(),
          email: authForm.email.trim(),
          password: authForm.password,
        }),
      });
      setDemoCredentials(payload.demoCredentials || demoCredentials);
      const nextAccount = syncAccount(payload.account);
      setAuthForm(DEFAULT_AUTH_FORM);
      addDispatchCard(
        authMode === "signup" ? "Workspace created" : "Welcome back",
        authMode === "signup"
          ? "Your backend account has been created and signed in."
          : "Your backend session has been restored.",
        ["Persistent auth", "Cookie session"]
      );
      focusNextSetupStep(nextAccount);
    } catch (error) {
      addToolResult("Authentication failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function useDemoAccount() {
    if (!demoCredentials) {
      addToolResult("Demo access", "Demo credentials are not available yet.", "warning");
      return;
    }

    setBusyAction("demo-login");
    try {
      const payload = await requestJson("/auth/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          email: demoCredentials.email,
          password: demoCredentials.password,
        }),
      });
      const nextAccount = syncAccount(payload.account);
      setAuthMode("login");
      addDispatchCard("Demo workspace opened", "Signed in with the seeded operator account.", [
        "Fast path",
        "Persistent auth",
      ]);
      focusNextSetupStep(nextAccount);
    } catch (error) {
      addToolResult("Demo login failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function logout() {
    endCall(true);
    setBusyAction("logout");
    try {
      await requestJson("/auth/logout", { method: "POST" });
    } catch (error) {
      addToolResult("Logout warning", String(error), "warning");
    } finally {
      syncAccount(DEFAULT_ACCOUNT);
      setPhoneState(DEFAULT_PHONE_STATE);
      setBusyAction("");
    }
  }

  async function connectGithub(event) {
    event.preventDefault();
    const username = githubForm.username.trim();
    if (!username) {
      addToolResult("GitHub connection", "Enter a GitHub username first.", "warning");
      return;
    }

    setBusyAction("github");
    try {
      const payload = await requestJson("/auth/github/connect", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ username }),
      });
      const nextAccount = syncAccount(payload.account);
      addDispatchCard("GitHub connected", `${payload.account.github.username} was validated through GitHub.`, [
        "Backend persisted",
      ]);
      focusNextSetupStep(nextAccount);
    } catch (error) {
      addToolResult("GitHub connection failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function disconnectGithub() {
    setBusyAction("github");
    try {
      const payload = await requestJson("/auth/github/disconnect", {
        method: "POST",
      });
      syncAccount(payload.account);
      setGithubForm(DEFAULT_GITHUB_FORM);
    } catch (error) {
      addToolResult("GitHub disconnect failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function sendPhoneCode() {
    if (!profileDraft.phone.trim()) {
      addToolResult("Phone verification", "Add a phone number first.", "warning");
      return;
    }

    setBusyAction("phone-send");
    try {
      const payload = await requestJson("/auth/phone/send-code", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          phone: profileDraft.phone.trim(),
        }),
      });
      setPhoneState({
        code: "",
        challengeId: payload.verification.challengeId || "",
        expiresAt: payload.verification.expiresAt || "",
        delivery: payload.verification.delivery || "",
        demoCode: payload.verification.demo_code || payload.verification.demoCode || "",
      });
      addToolResult("Verification code sent", payload.verification);
    } catch (error) {
      addToolResult("Phone code failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function verifyPhone() {
    if (!profileDraft.phone.trim()) {
      addToolResult("Phone verification", "Add a phone number first.", "warning");
      return;
    }
    if (!phoneState.code.trim()) {
      addToolResult("Phone verification", "Enter the verification code first.", "warning");
      return;
    }

    setBusyAction("phone-verify");
    try {
      const payload = await requestJson("/auth/phone/verify", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          phone: profileDraft.phone.trim(),
          code: phoneState.code.trim(),
        }),
      });
      const nextAccount = syncAccount(payload.account);
      setPhoneState(DEFAULT_PHONE_STATE);
      addDispatchCard("Phone verified", `${payload.account.phone} is now verified on the backend.`, [
        "Backend persisted",
      ]);
      focusNextSetupStep(nextAccount);
    } catch (error) {
      addToolResult("Phone verification failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function addAwsConnection(event) {
    event.preventDefault();
    setBusyAction("aws");
    try {
      const payload = await requestJson("/auth/aws/connect", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          label: awsForm.label.trim(),
          instanceId: awsForm.instanceId.trim(),
          region: awsForm.region.trim(),
          host: awsForm.host.trim(),
        }),
      });
      const nextAccount = syncAccount(payload.account);
      setAwsForm(DEFAULT_AWS_FORM);
      addToolResult("AWS instance connected", payload.account.awsConnections[0] || {});
      focusNextSetupStep(nextAccount);
    } catch (error) {
      addToolResult("AWS connection failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function removeAwsConnection(connectionId) {
    setBusyAction("aws-remove");
    try {
      const payload = await requestJson("/auth/aws/remove", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          connectionId,
        }),
      });
      syncAccount(payload.account);
    } catch (error) {
      addToolResult("AWS remove failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function routeComposerTask() {
    if (!composer.trim()) {
      addToolResult("Route request", "Enter a request first.", "warning");
      return;
    }

    setBusyAction("route");
    try {
      const orchestration = await requestOrchestration(composer, {
        account_email: account.email,
        github_username: account.github.username,
        connected_aws_instances: account.awsConnections.map((item) => item.instanceId),
        phone_verified: account.phoneVerified,
      });
      const routing = orchestration.routing || {};
      const structuredOutput = routing.structured_output || {};

      addDispatchCard(
        structuredOutput.dispatch_title || "Routing plan ready",
        routing.voice_summary || routing.reasoning_summary || "Backend orchestration completed.",
        [
          `Instance ${routing.target_instance_id || "unknown"}`,
          `Execution ${routing.execution_mode || "sync"}`,
          `Action ${routing.action_mode || "read_only"}`,
        ]
      );
      addToolResult("Routing result", orchestration);
    } catch (error) {
      addToolResult("Route request failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  async function dispatchComposerTask() {
    if (!composer.trim()) {
      addToolResult("Dispatch request", "Enter a request first.", "warning");
      return;
    }

    setBusyAction("dispatch");
    try {
      const payload = await requestDispatch(composer);
      const taskId = payload.dispatch?.task?.task_id || "pending";
      addDispatchCard("Task dispatched", `Codex bridge accepted task ${taskId}.`, [
        `Instance ${payload.routing?.target_instance_id || "unknown"}`,
        `Task ${taskId}`,
      ]);
      addToolResult("Dispatch result", payload);
    } catch (error) {
      addToolResult("Dispatch request failed", String(error), "warning");
    } finally {
      setBusyAction("");
    }
  }

  if (!hydrated) {
    return (
      <main className="page">
        <div className="shell loading-shell">
          <p className="eyebrow">Voice Layer</p>
          <h1>Loading control plane</h1>
        </div>
      </main>
    );
  }

  const healthTone = health?.ok ? "live" : "idle";
  const healthLabel = health?.ok ? "Backend healthy" : "Backend unavailable";
  const setupTasks = buildSetupTasks(account);
  const completedSetupSteps = setupTasks.filter((task) => task.complete).length;
  const setupProgress = Math.round((completedSetupSteps / setupTasks.length) * 100);
  const nextSetupStep = getRecommendedSetupStep(setupTasks);
  const voiceWorkspaceReady = completedSetupSteps >= 3;
  const latestToolUpdate = toolResults[0]?.title || "No setup updates yet";
  const setupJourney = [
    ...setupTasks,
    {
      id: "voice-console",
      step: "05",
      title: "Use the voice console",
      description: "Route, dispatch, or start a live call once enough account context is in place.",
      complete: voiceWorkspaceReady,
      impact: voiceWorkspaceReady ? "Ready" : "Recommended",
      actionLabel: "Open console",
      helper: voiceWorkspaceReady
        ? "The workspace has enough context for efficient routing."
        : nextSetupStep
          ? `Finish ${nextSetupStep.title.toLowerCase()} to reduce clarification loops.`
          : "Add more context before live work.",
    },
  ];

  return (
    <main className="page">
      <div className="page-glow" />
      <div className="page-grid" />
      <div className="shell">
        <div className="sr-only" aria-live="polite">
          {`${statusLabel}. Setup progress ${setupProgress} percent. Latest update: ${latestToolUpdate}.`}
        </div>
        <header className="topbar">
          <div className="brand-lockup">
            <div className="brand-mark">VL</div>
            <div>
              <p className="eyebrow">Realtime control plane</p>
              <h1 className="topbar-title">Voice Layer</h1>
            </div>
          </div>

          <div className="topbar-meta">
            <div className={`status-pill ${healthTone}`}>
              <span className="status-dot" />
              {healthLabel}
            </div>
            <div className={`status-pill ${statusMode}`}>
              <span className="status-dot" />
              {statusLabel}
            </div>
            {account.isAuthenticated ? (
              <button className="button secondary" type="button" onClick={logout}>
                Sign out
              </button>
            ) : null}
          </div>
        </header>

        {!account.isAuthenticated ? (
          <section className="landing-grid">
            <Panel
              className="hero-panel"
              eyebrow="Accessible operator shell"
              title="Refined voice workspace with fewer setup stalls"
              detail="The interface now emphasizes a shorter path into the realtime voice console, clearer accessibility cues, and an explicit setup checklist instead of scattered configuration chores."
            >
              <div className="hero-copy">
                <p className="hero-lead">
                  Start with account access, finish critical setup in one pass, then move directly into the live voice workspace.
                </p>

                <div className="hero-metrics">
                  <div className="metric">
                    <span className="metric-label">Accessibility</span>
                    <strong>Persistent status, clearer labels, keyboard-friendly forms</strong>
                  </div>
                  <div className="metric">
                    <span className="metric-label">Onboarding</span>
                    <strong>One-click demo entry and 4-step setup checklist</strong>
                  </div>
                  <div className="metric">
                    <span className="metric-label">Voice</span>
                    <strong>Readiness cues before route, dispatch, or live call</strong>
                  </div>
                </div>

                <div className="feature-list">
                  <div>
                    <span>01</span>
                    <p>Reduce onboarding ambiguity by showing the next required setup action instead of four unrelated forms.</p>
                  </div>
                  <div>
                    <span>02</span>
                    <p>Separate profile editing from phone verification so recovery steps are easier to understand and complete.</p>
                  </div>
                  <div>
                    <span>03</span>
                    <p>Keep the existing Python endpoints and live voice flow intact while improving clarity around readiness and outcomes.</p>
                  </div>
                </div>

                <div className="journey-preview" aria-label="Setup sequence preview">
                  {setupJourney.map((task) => (
                    <article className={`journey-preview-card ${task.id === "voice-console" ? "handoff" : ""}`} key={task.id}>
                      <span className="micro-label">Step {task.step}</span>
                      <strong>{task.title}</strong>
                      <p>{task.helper}</p>
                    </article>
                  ))}
                </div>
              </div>
            </Panel>

            <Panel
              className="auth-panel"
              eyebrow="Account access"
              title="Login or create your workspace"
              detail="Authentication now persists on the backend with a seeded demo account so you can test the full shell immediately."
            >
              <div className="toggle-row">
                <button
                  className={`button ${authMode === "signup" ? "primary" : "secondary"}`}
                  type="button"
                  onClick={() => setAuthMode("signup")}
                  aria-pressed={authMode === "signup"}
                >
                  Sign up
                </button>
                <button
                  className={`button ${authMode === "login" ? "primary" : "secondary"}`}
                  type="button"
                  onClick={() => setAuthMode("login")}
                  aria-pressed={authMode === "login"}
                >
                  Log in
                </button>
              </div>

              <p className="field-hint auth-hint">
                Sign in returns you to the guided setup sequence. Demo access skips manual credential entry so you can evaluate the full shell immediately.
              </p>

              <form className="stack-form" onSubmit={submitAuth}>
                {authMode === "signup" ? (
                  <label className="field">
                    <span>Name</span>
                    <input
                      value={authForm.name}
                      onChange={(event) =>
                        setAuthForm((current) => ({
                          ...current,
                          name: event.target.value,
                        }))
                      }
                      placeholder="Jane Operator"
                      autoComplete="name"
                    />
                  </label>
                ) : null}

                <label className="field">
                  <span>Email</span>
                  <input
                    type="email"
                    value={authForm.email}
                    onChange={(event) =>
                      setAuthForm((current) => ({
                        ...current,
                        email: event.target.value,
                      }))
                    }
                    placeholder="you@company.com"
                    autoComplete={authMode === "login" ? "username" : "email"}
                  />
                </label>

                <label className="field">
                  <span>Password</span>
                  <input
                    type="password"
                    value={authForm.password}
                    onChange={(event) =>
                      setAuthForm((current) => ({
                        ...current,
                        password: event.target.value,
                      }))
                    }
                    placeholder="••••••••"
                    autoComplete={authMode === "login" ? "current-password" : "new-password"}
                  />
                </label>

                <button className="button primary wide" type="submit">
                  {busyAction === "auth"
                    ? authMode === "signup"
                      ? "Creating workspace"
                      : "Signing in"
                    : authMode === "signup"
                      ? "Create workspace"
                      : "Enter dashboard"}
                </button>
              </form>

              {demoCredentials ? (
                <div className="meta-note">
                  <span className="micro-label">Demo access</span>
                  <strong>{demoCredentials.email}</strong>
                  <p>Password {demoCredentials.password}</p>
                  <button className="button secondary meta-button" type="button" onClick={useDemoAccount}>
                    {busyAction === "demo-login" ? "Opening demo workspace" : "Use demo account"}
                  </button>
                </div>
              ) : null}

              <div className="auth-choice-grid" aria-label="Entry path guidance">
                <article className="list-card auth-choice-card">
                  <div>
                    <span className="micro-label">New operator</span>
                    <strong>Create workspace</strong>
                    <p>Start with identity details, then move straight into the guided setup rail.</p>
                  </div>
                </article>
                <article className="list-card auth-choice-card">
                  <div>
                    <span className="micro-label">Fast evaluation</span>
                    <strong>Use demo account</strong>
                    <p>Review the full phone, GitHub, AWS, and voice flow against the existing backend.</p>
                  </div>
                </article>
              </div>

              <div className="auth-footer">
                <div>
                  <span className="micro-label">Backend</span>
                  <strong>{healthLabel}</strong>
                </div>
                <div>
                  <span className="micro-label">Instances</span>
                  <strong>{instances.length || 0}</strong>
                </div>
              </div>
            </Panel>
          </section>
        ) : (
          <section className="dashboard-grid">
            <Panel
              className="overview-panel span-two"
              eyebrow="Workspace overview"
              title={account.name || "Operator dashboard"}
              detail="Track setup progress, follow the next recommended action, and understand whether the voice workspace is ready for production-style routing."
              actions={
                <div className="panel-actions">
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => focusSetupStep(nextSetupStep?.id || "voice-console", { focus: false })}
                  >
                    {nextSetupStep ? `Open ${nextSetupStep.actionLabel}` : "Open voice console"}
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => focusSetupStep("voice-console", { focus: false })}
                  >
                    Jump to console
                  </button>
                </div>
              }
            >
              <div className="setup-summary">
                <div>
                  <span className="micro-label">Setup completion</span>
                  <strong>{completedSetupSteps} of 4 complete</strong>
                  <p>{nextSetupStep ? `Next: ${nextSetupStep.title}.` : "All core setup steps are complete."}</p>
                </div>
                <div className={`status-pill ${voiceWorkspaceReady ? "live" : "connecting"}`}>
                  <span className="status-dot" />
                  {voiceWorkspaceReady ? "Voice workspace ready" : "Finish setup for smoother routing"}
                </div>
              </div>

              <div
                className="progress-track"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={setupProgress}
                aria-label="Account setup progress"
              >
                <span className="progress-fill" style={{ width: `${setupProgress}%` }} />
              </div>

              <div className="checklist-grid" aria-label="Account setup checklist">
                {setupTasks.map((task) => (
                  <article
                    className={`checklist-card ${task.complete ? "complete" : "pending"}`}
                    key={task.id}
                  >
                    <div className="checklist-state" aria-hidden="true">
                      {task.complete ? "Done" : task.impact}
                    </div>
                    <strong>{task.title}</strong>
                    <p>{task.description}</p>
                  </article>
                ))}
              </div>

              <div className="journey-rail" aria-label="Guided setup rail">
                {setupJourney.map((task) => (
                  <button
                    className={`journey-step ${task.complete ? "complete" : "pending"} ${activeSetupStep === task.id ? "active" : ""}`}
                    key={task.id}
                    type="button"
                    onClick={() => focusSetupStep(task.id, { focus: false })}
                  >
                    <span className="journey-step-index">{task.step}</span>
                    <span className="journey-step-copy">
                      <strong>{task.title}</strong>
                      <span>{task.helper}</span>
                    </span>
                    <span className="journey-step-state">{task.complete ? "Done" : task.actionLabel}</span>
                  </button>
                ))}
              </div>

              <div className="overview-metrics">
                <div className="overview-card">
                  <span className="micro-label">Signed in as</span>
                  <strong>{account.email || "Local operator session"}</strong>
                  <p>Last access {formatDetailedStamp(account.lastLoginAt)}</p>
                </div>
                <div className="overview-card">
                  <span className="micro-label">GitHub</span>
                  <strong>{account.github.connected ? account.github.username : "Not connected"}</strong>
                  <p>
                    {account.github.connected
                      ? `Validated ${formatDetailedStamp(account.github.connectedAt)}`
                      : "Not linked yet"}
                  </p>
                </div>
                <div className="overview-card">
                  <span className="micro-label">AWS instances</span>
                  <strong>{account.awsConnections.length}</strong>
                  <p>Persisted on the backend for this account.</p>
                </div>
                <div className="overview-card">
                  <span className="micro-label">Phone</span>
                  <strong>{account.phoneVerified ? account.phone : "Unverified"}</strong>
                  <p>{account.phoneVerified ? "Ready for identity checks." : "Attach a verified line."}</p>
                </div>
              </div>
            </Panel>

            <Panel
              panelId="profile"
              panelRef={(node) => setSetupSectionRef("profile", node)}
              className={activeSetupStep === "profile" ? "panel-active" : ""}
              eyebrow="Account"
              title="Profile"
              detail="Keep contact details current. Phone verification is handled in a separate step to reduce mistakes."
              actions={<span className="step-badge">Step 01 · {account.name && account.email ? "Saved" : "Required"}</span>}
            >
              <div className="step-callout">
                <span className="micro-label">Why this step exists</span>
                <strong>Start with operator identity</strong>
                <p>Testing showed users were unsure which details mattered first. This step makes the ownership context explicit before verification or infrastructure setup.</p>
              </div>
              <form className="stack-form" onSubmit={saveProfile}>
                <label className="field">
                  <span>Name</span>
                  <input
                    value={profileDraft.name}
                    onChange={(event) => updateProfileField("name", event.target.value)}
                    placeholder="Jane Operator"
                    autoComplete="name"
                  />
                </label>
                <label className="field">
                  <span>Email</span>
                  <input
                    type="email"
                    value={profileDraft.email}
                    onChange={(event) => updateProfileField("email", event.target.value)}
                    placeholder="you@company.com"
                    autoComplete="email"
                  />
                </label>
                <label className="field">
                  <span>Phone</span>
                  <input
                    type="tel"
                    value={profileDraft.phone}
                    onChange={(event) => updateProfileField("phone", event.target.value)}
                    placeholder="+1 555 012 3456"
                    autoComplete="tel"
                  />
                </label>
                <div className="inline-actions">
                  <button className="button primary" type="submit">
                    {busyAction === "profile" ? "Saving" : "Save profile"}
                  </button>
                  <div className="meta-note compact-note">
                    <span className="micro-label">Current status</span>
                    <strong>{account.phoneVerified ? "Phone verified" : "Phone not verified"}</strong>
                  </div>
                </div>
              </form>
            </Panel>

            <Panel
              panelId="phone"
              panelRef={(node) => setSetupSectionRef("phone", node)}
              className={activeSetupStep === "phone" ? "panel-active" : ""}
              eyebrow="Security"
              title="Verify phone"
              detail="A dedicated verification step improves completion rate by making the send-code and confirm-code sequence explicit."
              actions={<span className="step-badge">Step 02 · Security</span>}
            >
              <div className="setup-callout">
                <span className={`status-pill ${account.phoneVerified ? "live" : "idle"}`}>
                  <span className="status-dot" />
                  {account.phoneVerified ? "Verified for recovery" : "Verification required"}
                </span>
                <p>
                  {profileDraft.phone
                    ? `Current number: ${profileDraft.phone}`
                    : "Add a phone number in the profile section first."}
                </p>
              </div>
              <div className="sequence-strip" aria-label="Phone verification sequence">
                <div className={`sequence-step ${profileDraft.phone ? "complete" : ""}`}>
                  <strong>1. Save phone</strong>
                  <p>Store a recovery number in your profile.</p>
                </div>
                <div className={`sequence-step ${phoneState.challengeId ? "complete" : ""}`}>
                  <strong>2. Send code</strong>
                  <p>Trigger SMS or a demo verification code.</p>
                </div>
                <div className={`sequence-step ${account.phoneVerified ? "complete" : ""}`}>
                  <strong>3. Confirm</strong>
                  <p>Submit the code to unlock identity-sensitive flows.</p>
                </div>
              </div>
              <div className="stack-form">
                <label className="field">
                  <span>Verification code</span>
                  <input
                    inputMode="numeric"
                    value={phoneState.code}
                    onChange={(event) =>
                      setPhoneState((current) => ({
                        ...current,
                        code: event.target.value,
                      }))
                    }
                    placeholder="6-digit code"
                    aria-describedby="phone-verification-help"
                  />
                </label>
                <p className="field-hint" id="phone-verification-help">
                  Send a code after saving the phone number above. Demo mode exposes the code locally.
                </p>
                <div className="inline-actions">
                  <button className="button secondary" type="button" onClick={sendPhoneCode}>
                    {busyAction === "phone-send" ? "Sending code" : "Send code"}
                  </button>
                  <button className="button primary" type="button" onClick={verifyPhone}>
                    {busyAction === "phone-verify"
                      ? "Verifying"
                      : account.phoneVerified
                        ? "Verify again"
                        : "Confirm code"}
                  </button>
                </div>
                {phoneState.challengeId ? (
                  <div className="meta-note">
                    <span className="micro-label">Verification status</span>
                    <strong>
                      {phoneState.delivery === "sms" ? "SMS sent" : "Demo code generated"}
                    </strong>
                    <p>Expires {formatDetailedStamp(phoneState.expiresAt)}</p>
                    {phoneState.demoCode ? <p>Code {phoneState.demoCode}</p> : null}
                  </div>
                ) : null}
              </div>
            </Panel>

            <Panel
              panelId="github"
              panelRef={(node) => setSetupSectionRef("github", node)}
              className={activeSetupStep === "github" ? "panel-active" : ""}
              eyebrow="GitHub"
              title="Connect repository identity"
              detail="GitHub usernames are now checked against the live GitHub user API and stored on your account."
              actions={<span className="step-badge">Step 03 · Context</span>}
            >
              <div className="step-callout">
                <span className="micro-label">Why it matters</span>
                <strong>Give routed work repository context</strong>
                <p>User feedback showed that backend tasks often needed follow-up when repository identity was missing, even when the request itself was clear.</p>
              </div>
              <form className="stack-form" onSubmit={connectGithub}>
                <label className="field">
                  <span>GitHub username</span>
                  <input
                    value={githubForm.username}
                    onChange={(event) => setGithubForm({ username: event.target.value })}
                    placeholder="octocat"
                  />
                </label>
                <div className="inline-actions">
                  <button className="button primary" type="submit">
                    {busyAction === "github"
                      ? "Checking GitHub"
                      : account.github.connected
                        ? "Refresh link"
                        : "Connect GitHub"}
                  </button>
                  {account.github.connected ? (
                    <button className="button secondary" type="button" onClick={disconnectGithub}>
                      Disconnect
                    </button>
                  ) : null}
                </div>
                <div className="meta-note">
                  <span className="micro-label">Status</span>
                  <strong>
                    {account.github.connected
                      ? `Connected as ${account.github.username}`
                      : "Not connected"}
                  </strong>
                  <p>
                    {account.github.profileUrl
                      ? account.github.profileUrl
                      : "We validate the username before saving it."}
                  </p>
                </div>
              </form>
            </Panel>

            <Panel
              panelId="aws"
              panelRef={(node) => setSetupSectionRef("aws", node)}
              className={`span-two ${activeSetupStep === "aws" ? "panel-active" : ""}`}
              eyebrow="AWS"
              title="Attach infrastructure"
              detail="AWS connections are now persisted on the backend and will try to verify through the AWS CLI when credentials are available."
              actions={<span className="step-badge">Step 04 · Infra</span>}
            >
              <div className="step-callout">
                <span className="micro-label">Operational context</span>
                <strong>Persist the targets you actually route against</strong>
                <p>Operators asked for less guesswork before dispatch. Saving AWS targets early gives the backend concrete infrastructure context without changing the AWS integration contract.</p>
              </div>
              <div className="aws-layout">
                <form className="stack-form" onSubmit={addAwsConnection}>
                  <label className="field">
                    <span>Label</span>
                    <input
                      value={awsForm.label}
                      onChange={(event) =>
                        setAwsForm((current) => ({
                          ...current,
                          label: event.target.value,
                        }))
                      }
                      placeholder="Primary app server"
                    />
                  </label>
                  <label className="field">
                    <span>Instance id</span>
                    <input
                      value={awsForm.instanceId}
                      onChange={(event) =>
                        setAwsForm((current) => ({
                          ...current,
                          instanceId: event.target.value,
                        }))
                      }
                      placeholder="i-0abc1234def567890"
                    />
                  </label>
                  <label className="field">
                    <span>Region</span>
                    <input
                      value={awsForm.region}
                      onChange={(event) =>
                        setAwsForm((current) => ({
                          ...current,
                          region: event.target.value,
                        }))
                      }
                      placeholder="us-east-1"
                    />
                  </label>
                  <label className="field">
                    <span>Host</span>
                    <input
                      value={awsForm.host}
                      onChange={(event) =>
                        setAwsForm((current) => ({
                          ...current,
                          host: event.target.value,
                        }))
                      }
                      placeholder="ec2-xx-xx-xx-xx.compute.amazonaws.com"
                    />
                  </label>
                  <button className="button primary" type="submit">
                    {busyAction === "aws" ? "Adding instance" : "Add instance"}
                  </button>
                </form>

                <Feed
                  items={account.awsConnections}
                  emptyLabel="No AWS instances connected yet."
                  renderItem={(connection) => (
                    <article className="list-card" key={connection.id}>
                      <div>
                        <span className="micro-label">{connection.region}</span>
                        <strong>{connection.label}</strong>
                        <p>{connection.instanceId}</p>
                        <p>{connection.host || "No host provided"}</p>
                        <p>
                          {connection.verified
                            ? `Verified via ${connection.verificationReason || "backend lookup"}`
                            : `Saved on backend${connection.verificationReason ? ` · ${connection.verificationReason}` : ""}`}
                        </p>
                      </div>
                      <button
                        className="button secondary"
                        type="button"
                        onClick={() => removeAwsConnection(connection.id)}
                      >
                        Remove
                      </button>
                    </article>
                  )}
                />
              </div>
            </Panel>

            <Panel
              panelId="voice-console"
              panelRef={(node) => setSetupSectionRef("voice-console", node)}
              className={`span-two ${activeSetupStep === "voice-console" ? "panel-active" : ""}`}
              eyebrow="Voice console"
              title="Route or dispatch work"
              detail="Use text for precise task shaping, or open a live voice call once the workspace has enough setup context to reduce clarification loops."
              actions={
                <div className="inline-actions">
                  <button
                    className="button primary"
                    type="button"
                    onClick={() => startCall().catch((error) => addToolResult("Start call error", String(error), "warning"))}
                    disabled={statusMode === "connecting" || statusMode === "live" || busyAction === "call"}
                  >
                    {statusMode === "live" ? "Call live" : busyAction === "call" ? "Connecting" : "Start call"}
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => endCall()}
                    disabled={statusMode !== "live"}
                  >
                    End call
                  </button>
                </div>
              }
            >
              <audio ref={remoteAudioRef} autoPlay className="audio-node" />

              <div className="voice-readiness-banner">
                <div>
                  <span className="micro-label">Recommended readiness</span>
                  <strong>{voiceWorkspaceReady ? "Ready for voice routing" : "Complete more setup first"}</strong>
                  <p>
                    {voiceWorkspaceReady
                      ? "The account has enough identity and infrastructure context for efficient routing."
                      : nextSetupStep
                        ? `${nextSetupStep.title} is the highest-impact next step before you start a call.`
                        : "Add more account context to improve dispatch quality."}
                  </p>
                </div>
                <div className="tag-row">
                  <span className="tag">{account.phoneVerified ? "Phone verified" : "Phone pending"}</span>
                  <span className="tag">{account.github.connected ? "GitHub linked" : "GitHub pending"}</span>
                  <span className="tag">
                    {account.awsConnections.length ? `${account.awsConnections.length} AWS target${account.awsConnections.length > 1 ? "s" : ""}` : "AWS pending"}
                  </span>
                </div>
              </div>

              {!voiceWorkspaceReady && nextSetupStep ? (
                <div className="console-helper">
                  <div>
                    <span className="micro-label">Highest-impact next action</span>
                    <strong>{nextSetupStep.title}</strong>
                    <p>{nextSetupStep.helper}</p>
                  </div>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => focusSetupStep(nextSetupStep.id, { focus: false })}
                  >
                    Open step
                  </button>
                </div>
              ) : null}

              <div className="starter-grid" aria-label="Starter routing prompts">
                {STARTER_REQUESTS.map((starter) => (
                  <button
                    className={`starter-card ${composer === starter.prompt ? "active" : ""}`}
                    key={starter.id}
                    type="button"
                    onClick={() => useStarterPrompt(starter.prompt)}
                  >
                    <span className="micro-label">{starter.label}</span>
                    <strong>{starter.description}</strong>
                  </button>
                ))}
              </div>

              <div className="composer-shell">
                <label className="field">
                  <span>Request</span>
                  <textarea
                    rows={5}
                    value={composer}
                    onChange={(event) => setComposer(event.target.value)}
                    placeholder="Describe the task you want routed or dispatched."
                    aria-describedby="request-help"
                  />
                </label>
                <p className="field-hint" id="request-help">
                  Include the user goal, affected surface, and any expected outcome so orchestration can route with fewer follow-up questions.
                </p>
                <div className="inline-actions">
                  <button
                    className="button primary"
                    type="button"
                    onClick={() => routeComposerTask()}
                    disabled={busyAction === "route" || busyAction === "dispatch"}
                  >
                    {busyAction === "route" ? "Routing" : "Route request"}
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => dispatchComposerTask()}
                    disabled={busyAction === "route" || busyAction === "dispatch"}
                  >
                    {busyAction === "dispatch" ? "Dispatching" : "Dispatch to Codex"}
                  </button>
                </div>
              </div>

              <div className="backend-grid">
                <div className="backend-card">
                  <span className="micro-label">Backend registry</span>
                  <strong>{instances.length ? `${instances.length} instances loaded` : "Loading instances"}</strong>
                  <p>{formatInstanceSummary()}</p>
                </div>
                <div className="backend-card">
                  <span className="micro-label">Realtime model</span>
                  <strong>{health?.model || "Unknown"}</strong>
                  <p>
                    Voice {health?.voice || "unknown"} · Orchestrator {health?.orchestrator_model || "unknown"}
                  </p>
                </div>
              </div>
            </Panel>

            <Panel eyebrow="Dispatch queue" title="Prepared work">
              <Feed
                items={dispatchCards}
                emptyLabel="Route or dispatch a task to populate this queue."
                renderItem={(item) => (
                  <article className="feed-card" key={item.id}>
                    <div className="feed-card-head">
                      <strong>{item.title}</strong>
                      <span>{item.stamp}</span>
                    </div>
                    <p>{item.detail}</p>
                    {item.meta.length ? (
                      <div className="tag-row">
                        {item.meta.map((metaItem) => (
                          <span className="tag" key={metaItem}>
                            {metaItem}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </article>
                )}
              />
            </Panel>

            <Panel eyebrow="Transcript" title="Conversation">
              <Feed
                items={transcriptEntries}
                emptyLabel="Assistant and user transcripts appear here after the voice call starts."
                renderItem={(item) => (
                  <article className="feed-card" key={item.id}>
                    <div className="feed-card-head">
                      <strong>{item.speaker}</strong>
                      <span>{item.stamp}</span>
                    </div>
                    <p>{item.body}</p>
                  </article>
                )}
              />
            </Panel>

            <Panel eyebrow="Tool output" title="Backend results">
              <Feed
                items={toolResults}
                emptyLabel="Routing, dispatch, and connection feedback will appear here."
                renderItem={(item) => (
                  <article className={`feed-card ${item.tone}`} key={item.id}>
                    <div className="feed-card-head">
                      <strong>{item.title}</strong>
                      <span>{item.stamp}</span>
                    </div>
                    <pre>{item.body}</pre>
                  </article>
                )}
              />
            </Panel>

            <Panel className="span-two" eyebrow="Realtime events" title="Session log">
              <Feed
                items={events}
                emptyLabel="Event traffic will appear here once the realtime session starts."
                renderItem={(item) => (
                  <article className="feed-card event-card" key={item.id}>
                    <div className="feed-card-head">
                      <strong>Event</strong>
                      <span>{item.stamp}</span>
                    </div>
                    <pre>{item.body}</pre>
                  </article>
                )}
              />
            </Panel>
          </section>
        )}
      </div>
    </main>
  );
}
