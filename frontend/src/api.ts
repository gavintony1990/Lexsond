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
  AuthLoginResult,
  AuthDeviceSession,
  AuthSessionState,
  CredentialProfile,
  CredentialProfileCreateInput,
  CredentialVaultStatus,
  CatalogResult,
  ProbeBatch,
  ProbeBatchCreateInput,
  PartnerApplication,
  PartnerApplicationInput,
  EvaluationDataset,
  EvaluationDatasetMetadataInput,
  EvaluationDatasetPatchInput,
  EvaluationDatasetRevision,
  EvaluationRun,
  EvaluationRunCreateInput,
  EvaluationRunEvent,
  EvaluationRunInput,
  EvaluationRunItem,
  EvaluationRunPreview,
  EvaluationScorer,
  EvaluationUploadPreview,
} from "./types";

interface Envelope<T> {
  data: T;
  meta?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: ApiErrorBody["error"]["details"];
  readonly requestId: string | null;

  constructor(body: ApiErrorBody["error"], status = 500) {
    super(body.message);
    this.name = "ApiError";
    this.code = body.code;
    this.details = body.details ?? [];
    this.requestId = body.request_id;
    this.status = status;
  }
}

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(value: string | null): void {
  csrfToken = value;
}

export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");
  const method = (init?.method ?? "GET").toUpperCase();
  if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
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
    if (response.status === 401) unauthorizedHandler?.();
    throw new ApiError(body.error ?? fallback.error, response.status);
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
  authSession: () => request<AuthSessionState>("/auth/session"),
  authCsrf: () => request<{ csrf_token: string; expires_in: number }>("/auth/csrf"),
  login: (email: string, password: string, returnTo: string | null) =>
    request<AuthLoginResult>(
      "/auth/login",
      json({ email, password, return_to: returnTo }),
    ),
  register: (email: string, password: string, displayName: string) =>
    request<{ status: string; message: string }>(
      "/auth/register",
      json({ email, password, display_name: displayName }),
    ),
  verifyEmail: (token: string) =>
    request<{ status: string }>("/auth/verify-email", json({ token })),
  forgotPassword: (email: string) =>
    request<{ status: string; message: string }>("/auth/forgot-password", json({ email })),
  resetPassword: (token: string, newPassword: string) =>
    request<{ status: string }>("/auth/reset-password", json({ token, new_password: newPassword })),
  resendVerification: () =>
    request<{ status: string }>("/auth/resend-verification", { method: "POST" }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ status: string; revoked_sessions: number }>(
      "/auth/change-password",
      json({ current_password: currentPassword, new_password: newPassword }),
    ),
  authSessions: () => request<AuthDeviceSession[]>("/auth/sessions"),
  revokeAuthSession: (id: string) =>
    request<{ status: string; current: boolean }>(`/auth/sessions/${id}`, { method: "DELETE" }),
  logoutAll: () => request<{ status: string; revoked_sessions: number }>("/auth/logout-all", { method: "POST" }),
  logout: () => request<{ status: string }>("/auth/logout", { method: "POST" }),
  bootstrap: () => request<Bootstrap>("/bootstrap"),
  credentialVaultStatus: () => request<CredentialVaultStatus>("/credential-vault/status"),
  credentialProfiles: (includeArchived = false) =>
    request<CredentialProfile[]>(`/credential-profiles?include_archived=${includeArchived}`),
  createCredentialProfile: (value: CredentialProfileCreateInput, idempotencyKey: string) =>
    request<CredentialProfile>("/credential-profiles", {
      ...json(value),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  archiveCredentialProfile: (id: string, version: number) =>
    request<CredentialProfile>(`/credential-profiles/${id}?version=${version}`, { method: "DELETE" }),
  partnerApplications: () => request<PartnerApplication[]>("/partner-applications?limit=100"),
  createPartnerApplication: (value: PartnerApplicationInput, idempotencyKey: string) =>
    request<PartnerApplication>("/partner-applications", { ...json(value), headers: { "Idempotency-Key": idempotencyKey } }),
  submitPartnerApplication: (id: string, version: number) =>
    request<PartnerApplication>(`/partner-applications/${id}/submit?version=${version}`, { method: "POST" }),
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
  catalog: (id: string, apiKey: string | null, credentialProfileId: string | null = null) =>
    request<CatalogResult>(
      `/targets/${id}/catalog`,
      json({ api_key: apiKey || null, credential_profile_id: credentialProfileId }),
    ),
  probeBatches: () => request<ProbeBatch[]>("/probe-batches?limit=100"),
  probeBatch: (id: string) => request<ProbeBatch>(`/probe-batches/${id}`),
  createProbeBatch: (value: ProbeBatchCreateInput, idempotencyKey: string) =>
    request<ProbeBatch>("/probe-batches", {
      ...json(value),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  cancelProbeBatch: (id: string) =>
    request<ProbeBatch>(`/probe-batches/${id}/cancel`, { method: "POST" }),
  evaluationDatasets: (includeArchived = false) =>
    request<EvaluationDataset[]>(`/evaluation-datasets?include_archived=${includeArchived}&limit=100`),
  evaluationDataset: (id: string, includeArchived = false) =>
    request<EvaluationDataset>(`/evaluation-datasets/${id}?include_archived=${includeArchived}`),
  evaluationDatasetRevisions: (id: string) =>
    request<EvaluationDatasetRevision[]>(`/evaluation-datasets/${id}/revisions`),
  evaluationDatasetRevision: (id: string, revision: number) =>
    request<EvaluationDatasetRevision>(`/evaluation-datasets/${id}/revisions/${revision}`),
  validateEvaluationUpload: (file: File, format: "jsonl" | "csv", csvMapping?: Record<string, string> | null) => {
    const body = new FormData();
    body.set("file", file);
    const mapping = csvMapping ? `&csv_mapping=${encodeURIComponent(JSON.stringify(csvMapping))}` : "";
    return request<EvaluationUploadPreview>(`/evaluation-datasets/validate-upload?format=${format}${mapping}`, {
      method: "POST",
      body,
    });
  },
  createEvaluationDataset: (file: File, metadata: EvaluationDatasetMetadataInput) => {
    const body = new FormData();
    body.set("file", file);
    body.set("metadata", new Blob([JSON.stringify(metadata)], { type: "application/json" }));
    return request<EvaluationDataset>("/evaluation-datasets", { method: "POST", body });
  },
  createEvaluationDatasetRevision: (id: string, file: File, format: "jsonl" | "csv", csvMapping?: Record<string, string> | null) => {
    const body = new FormData();
    body.set("file", file);
    const mapping = csvMapping ? `&csv_mapping=${encodeURIComponent(JSON.stringify(csvMapping))}` : "";
    return request<EvaluationDatasetRevision>(`/evaluation-datasets/${id}/revisions?format=${format}${mapping}`, {
      method: "POST",
      body,
    });
  },
  updateEvaluationDataset: (id: string, value: EvaluationDatasetPatchInput) =>
    request<EvaluationDataset>(`/evaluation-datasets/${id}`, {
      method: "PATCH", body: JSON.stringify(value),
    }),
  archiveEvaluationDataset: (id: string) =>
    request<EvaluationDataset>(`/evaluation-datasets/${id}`, { method: "DELETE" }),
  restoreEvaluationDataset: (id: string) =>
    request<EvaluationDataset>(`/evaluation-datasets/${id}/restore`, { method: "POST" }),
  purgeEvaluationDataset: (id: string) =>
    request<void>(`/evaluation-datasets/${id}/purge`, { method: "DELETE" }),
  evaluationScorers: () => request<EvaluationScorer[]>("/evaluation-scorers"),
  previewEvaluationRun: (value: EvaluationRunInput) =>
    request<EvaluationRunPreview>("/evaluation-runs/preview", json(value)),
  createEvaluationRun: (value: EvaluationRunCreateInput, idempotencyKey: string) =>
    request<EvaluationRun>("/evaluation-runs", {
      ...json(value),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  evaluationRuns: (includeArchived = false) =>
    request<EvaluationRun[]>(`/evaluation-runs?include_archived=${includeArchived}&limit=100`),
  evaluationRun: (id: string, includeArchived = false) =>
    request<EvaluationRun>(`/evaluation-runs/${id}?include_archived=${includeArchived}`),
  evaluationRunItems: (id: string) =>
    request<EvaluationRunItem[]>(`/evaluation-runs/${id}/items?limit=2000`),
  cancelEvaluationRun: (id: string) =>
    request<EvaluationRun>(`/evaluation-runs/${id}/cancel`, { method: "POST" }),
  archiveEvaluationRun: (id: string) =>
    request<EvaluationRun>(`/evaluation-runs/${id}`, { method: "DELETE" }),
  restoreEvaluationRun: (id: string) =>
    request<EvaluationRun>(`/evaluation-runs/${id}/restore`, { method: "POST" }),
  purgeEvaluationRun: (id: string) =>
    request<void>(`/evaluation-runs/${id}/purge`, { method: "DELETE" }),
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

export function subscribeToEvaluationRun(
  runId: string,
  onEvent: (event: EvaluationRunEvent) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(`/api/v1/evaluation-runs/${runId}/events`);
  const names = ["evaluation_started", "item_started", "item_finished", "evaluation_finished"];
  const listener = (raw: Event) => {
    const event = raw as MessageEvent<string>;
    onEvent(JSON.parse(event.data) as EvaluationRunEvent);
  };
  names.forEach((name) => source.addEventListener(name, listener));
  source.onerror = () => onError?.();
  return () => source.close();
}

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
