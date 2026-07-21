import type {
  AgentBootstrap,
  AgentEvent,
  AgentMessage,
  AgentReply,
  AgentSession,
  AgentSessionCreateInput,
  ApiErrorBody,
  Bootstrap,
  MonitorIncident,
  MonitorPolicy,
  MonitorPolicyCreateInput,
  MonitorPolicyPatchInput,
  MonitoringOverview,
  RunCreateInput,
  Run,
  RunEvent,
  SuiteCreateInput,
  SuitePatchInput,
  Suite,
  SuiteRevision,
  TargetCreateInput,
  TargetPatchInput,
  Target,
} from "./types";

interface Envelope<T> {
  data: T;
  meta?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly code: string;
  readonly details: ApiErrorBody["error"]["details"];
  readonly requestId: string | null;

  constructor(body: ApiErrorBody["error"]) {
    super(body.message);
    this.name = "ApiError";
    this.code = body.code;
    this.details = body.details ?? [];
    this.requestId = body.request_id;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  const response = await fetch(`/api/v1${path}`, { ...init, headers });
  if (!response.ok) {
    const fallback: ApiErrorBody = {
      error: {
        code: `HTTP_${response.status}`,
        message: `请求失败（HTTP ${response.status}）`,
        details: [],
        request_id: response.headers.get("X-Request-ID"),
      },
    };
    const body = (await response.json().catch(() => fallback)) as ApiErrorBody;
    throw new ApiError(body.error ?? fallback.error);
  }
  if (response.status === 204) return undefined as T;
  const body = (await response.json()) as Envelope<T>;
  return body.data;
}

const json = (value: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(value),
});

export const api = {
  bootstrap: () => request<Bootstrap>("/bootstrap"),
  agentBootstrap: () => request<AgentBootstrap>("/agent/bootstrap"),
  agentSessions: (includeArchived = false) =>
    request<AgentSession[]>(`/agent/sessions?include_archived=${includeArchived}&limit=100`),
  agentSession: (id: string, includeArchived = false) =>
    request<AgentSession>(`/agent/sessions/${id}?include_archived=${includeArchived}`),
  createAgentSession: (value: AgentSessionCreateInput) =>
    request<AgentSession>("/agent/sessions", json(value)),
  archiveAgentSession: (id: string) =>
    request<AgentSession>(`/agent/sessions/${id}`, { method: "DELETE" }),
  restoreAgentSession: (id: string) =>
    request<AgentSession>(`/agent/sessions/${id}/restore`, { method: "POST" }),
  purgeAgentSession: (id: string) =>
    request<void>(`/agent/sessions/${id}/purge`, { method: "DELETE" }),
  agentMessages: (id: string) =>
    request<AgentMessage[]>(`/agent/sessions/${id}/messages?limit=100`),
  agentEvents: (id: string, afterSequence = 0) =>
    request<AgentEvent[]>(`/agent/sessions/${id}/events?after_sequence=${afterSequence}`),
  sendAgentMessage: (
    id: string,
    content: string,
    apiKey: string | null,
    timeoutSeconds = 45,
  ) =>
    request<AgentReply>(`/agent/sessions/${id}/messages`,
      json({ content, api_key: apiKey || null, timeout_seconds: timeoutSeconds })),
  targets: (includeArchived = false) =>
    request<Target[]>(`/targets?include_archived=${includeArchived}`),
  target: (id: string, includeArchived = false) =>
    request<Target>(`/targets/${id}?include_archived=${includeArchived}`),
  createTarget: (value: TargetCreateInput) => request<Target>("/targets", json(value)),
  updateTarget: (id: string, value: TargetPatchInput) =>
    request<Target>(`/targets/${id}`, { method: "PATCH", body: JSON.stringify(value) }),
  archiveTarget: (id: string) => request<Target>(`/targets/${id}`, { method: "DELETE" }),
  restoreTarget: (id: string) => request<Target>(`/targets/${id}/restore`, { method: "POST" }),
  purgeTarget: (id: string) => request<void>(`/targets/${id}/purge`, { method: "DELETE" }),
  catalog: (id: string, apiKey: string | null) =>
    request<{ models: Array<{ id: string; probe_types: string[] }>; model_count: number }>(
      `/targets/${id}/catalog`,
      json({ api_key: apiKey || null }),
    ),
  suites: (includeArchived = false) =>
    request<Suite[]>(`/suites?include_archived=${includeArchived}`),
  suite: (id: string, includeArchived = false) =>
    request<Suite>(`/suites/${id}?include_archived=${includeArchived}`),
  suiteRevisions: (id: string) => request<SuiteRevision[]>(`/suites/${id}/revisions`),
  createSuite: (value: SuiteCreateInput) => request<Suite>("/suites", json(value)),
  updateSuite: (id: string, value: SuitePatchInput) =>
    request<Suite>(`/suites/${id}`, { method: "PATCH", body: JSON.stringify(value) }),
  archiveSuite: (id: string) => request<Suite>(`/suites/${id}`, { method: "DELETE" }),
  restoreSuite: (id: string) => request<Suite>(`/suites/${id}/restore`, { method: "POST" }),
  purgeSuite: (id: string) => request<void>(`/suites/${id}/purge`, { method: "DELETE" }),
  monitorPolicies: (includeArchived = false) =>
    request<MonitorPolicy[]>(`/monitor-policies?include_archived=${includeArchived}`),
  monitorPolicy: (id: string, includeArchived = false) =>
    request<MonitorPolicy>(`/monitor-policies/${id}?include_archived=${includeArchived}`),
  createMonitorPolicy: (value: MonitorPolicyCreateInput) =>
    request<MonitorPolicy>("/monitor-policies", json(value)),
  updateMonitorPolicy: (id: string, value: MonitorPolicyPatchInput) =>
    request<MonitorPolicy>(`/monitor-policies/${id}`, {
      method: "PATCH",
      body: JSON.stringify(value),
    }),
  runMonitorPolicyNow: (id: string) =>
    request<MonitorPolicy>(`/monitor-policies/${id}/run-now`, { method: "POST" }),
  archiveMonitorPolicy: (id: string) =>
    request<MonitorPolicy>(`/monitor-policies/${id}`, { method: "DELETE" }),
  restoreMonitorPolicy: (id: string) =>
    request<MonitorPolicy>(`/monitor-policies/${id}/restore`, { method: "POST" }),
  purgeMonitorPolicy: (id: string) =>
    request<void>(`/monitor-policies/${id}/purge`, { method: "DELETE" }),
  monitoringOverview: (window: "90m" | "24h" | "7d" | "30d") =>
    request<MonitoringOverview>(`/monitoring/overview?window=${window}`),
  monitorIncidents: (limit = 100) =>
    request<MonitorIncident[]>(`/monitoring/incidents?limit=${limit}`),
  runs: (includeArchived = false) =>
    request<Run[]>(`/runs?include_archived=${includeArchived}&limit=100`),
  run: (id: string, includeArchived = false) =>
    request<Run>(`/runs/${id}?include_archived=${includeArchived}`),
  createRun: (value: RunCreateInput, idempotencyKey: string) =>
    request<Run>("/runs", {
      ...json(value),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  cancelRun: (id: string) => request<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  archiveRun: (id: string) => request<Run>(`/runs/${id}`, { method: "DELETE" }),
  restoreRun: (id: string) => request<Run>(`/runs/${id}/restore`, { method: "POST" }),
  purgeRun: (id: string) => request<void>(`/runs/${id}/purge`, { method: "DELETE" }),
};

export function subscribeToRun(
  runId: string,
  onEvent: (event: RunEvent) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(`/api/v1/runs/${runId}/events`);
  const names = [
    "run_started",
    "step_started",
    "step_completed",
    "run_completed",
    "run_failed",
    "run_cancel_requested",
    "run_cancelled",
    "legacy_run_imported",
    "temporal_workflow_started",
    "temporal_activity_started",
    "temporal_activity_attempt_failed",
    "temporal_activity_completed",
    "temporal_workflow_succeeded",
    "temporal_workflow_failed",
    "temporal_workflow_rejected",
    "temporal_workflow_cancelled",
  ];
  const listener = (raw: Event) => {
    const event = raw as MessageEvent<string>;
    onEvent(JSON.parse(event.data) as RunEvent);
  };
  names.forEach((name) => source.addEventListener(name, listener));
  source.onerror = () => onError?.();
  return () => source.close();
}
