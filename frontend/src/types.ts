import type { components } from "./generated/api-schema";

export type TargetCreateInput = components["schemas"]["TargetCreate"];
export type TargetPatchInput = components["schemas"]["TargetPatch"];
export type SuiteCreateInput = Omit<
  components["schemas"]["SuiteCreate"],
  "document"
> & { document: SuiteDocument };
export type SuitePatchInput = Omit<
  components["schemas"]["SuitePatch"],
  "document"
> & { document?: SuiteDocument | null };
export type RunCreateInput = components["schemas"]["RunCreate"];
export type MonitorPolicyCreateInput = components["schemas"]["MonitorPolicyCreate"];
export type MonitorPolicyPatchInput = components["schemas"]["MonitorPolicyPatch"];
export type CatalogInput = components["schemas"]["CatalogRequest"];

export interface AuthUser {
  user_id: string;
  email: string;
  display_name: string;
  avatar_url: string | null;
  status: "PENDING_VERIFICATION" | "ACTIVE" | "SUSPENDED" | "DELETED";
  system_role: "USER" | "ADMIN";
  email_verified_at: string | null;
  workspace_id: string;
  workspace_name: string;
  workspace_role: "OWNER" | "ADMIN" | "MEMBER" | "VIEWER";
}

export interface AuthSessionState {
  user: AuthUser;
  csrf_token: string;
  auth_mode: "required" | "local-single-user";
}

export interface AuthLoginResult extends AuthSessionState {
  session: { session_id: string; user_id: string; workspace_id: string };
  return_to: string;
}

export interface AuthDeviceSession {
  session_id: string;
  current: boolean;
  device_id: string | null;
  ip_prefix: string | null;
  created_at: string;
  last_seen_at: string | null;
  absolute_expires_at: string;
  revoked_at: string | null;
}

export interface CredentialVaultStatus {
  available: boolean;
  backend: string;
  reason: string | null;
  storage_backend: "SYSTEM_KEYRING" | "EXTERNAL_SECRET_MANAGER";
  persistence_enabled: boolean;
}

export interface CredentialProfile {
  id: string;
  workspace_id: string;
  label: string;
  provider_id: string;
  storage_backend: "SYSTEM_KEYRING" | "EXTERNAL_SECRET_MANAGER";
  masked_suffix: string;
  status: string;
  version: number;
  last_verified_at: string | null;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface CredentialProfileCreateInput {
  label: string;
  provider_id: string;
  api_key: string;
}

export interface CatalogResult {
  status: "CONNECTED";
  target_id: string;
  auth_mode: "bearer" | "none";
  model_count: number;
  models: Array<{ id: string; probe_types: string[] }>;
  catalog_snapshot_id: string;
  catalog_expires_at: string;
}

export interface ProbeBatchCreateInput {
  target_id: string;
  catalog_snapshot_id: string;
  mode: "catalog_only" | "smoke" | "quality_suite";
  model_ids: string[];
  suite_revision_id: string | null;
  max_concurrency: 1 | 2;
  max_output_tokens: number;
  timeout_seconds: number;
  api_key: string | null;
  credential_profile_id: string | null;
  confirm_unknown_cost: boolean;
}

export interface ProbeBatchItem {
  item_id: string;
  ordinal: number;
  model_id: string;
  state: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "SKIPPED";
  run_id: string | null;
  failure_code: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ProbeBatch {
  batch_id: string;
  workspace_id: string;
  target_id: string;
  credential_profile_id: string | null;
  catalog_snapshot_id: string;
  suite_revision_id: string | null;
  mode: "catalog_only" | "smoke" | "quality_suite";
  state: "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
  model_count: number;
  max_concurrency: number;
  max_output_tokens: number;
  timeout_seconds: number;
  confirm_unknown_cost: boolean;
  cancel_requested_at: string | null;
  created_at: string;
  finished_at: string | null;
  counts: Record<string, number>;
  items: ProbeBatchItem[];
}

export type EvaluationDistributionPolicy =
  | "BUNDLED"
  | "IMPORT_REQUIRED"
  | "LICENSE_REVIEW"
  | "RESEARCH_ONLY"
  | "RUNNER_REQUIRED"
  | "BLOCKED";

export interface EvaluationDatasetRevisionSummary {
  id: string;
  revision: number;
  content_sha256: string;
  item_count: number;
  category_count: number;
  language_codes: string[];
  manifest: Record<string, unknown>;
  created_at: string;
}

export interface EvaluationDataset {
  id: string;
  workspace_id: string | null;
  scope: "SYSTEM" | "WORKSPACE";
  slug: string;
  name: string;
  description: string;
  license_spdx: string;
  license_url: string;
  source_url: string | null;
  source_version: string | null;
  source_verified_at: string | null;
  distribution_policy: EvaluationDistributionPolicy;
  default_scorer_id: string;
  version: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  latest_revision: EvaluationDatasetRevisionSummary | null;
}

export interface EvaluationDatasetItemPreview {
  item_index?: number;
  item_id?: string;
  id?: string;
  category: string;
  language: string;
  input: { messages: Array<{ role: string; content: string }>; choices?: string[] };
  reference: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface EvaluationDatasetRevision extends EvaluationDatasetRevisionSummary {
  dataset_id: string;
  schema_version: string;
  items?: EvaluationDatasetItemPreview[];
}

export interface EvaluationUploadPreview {
  schema_version: string;
  content_sha256: string;
  item_count: number;
  category_count: number;
  categories: Record<string, number>;
  language_codes: string[];
  preview: EvaluationDatasetItemPreview[];
  preview_truncated: boolean;
}

export interface EvaluationDatasetMetadataInput {
  slug: string;
  name: string;
  description: string;
  license_spdx: string;
  license_url: string;
  source_url: string | null;
  distribution_policy: "BUNDLED" | "IMPORT_REQUIRED" | "LICENSE_REVIEW";
  default_scorer_id: string;
  format: "jsonl" | "csv";
  csv_mapping: EvaluationCsvMapping | null;
  rights_confirmed: boolean;
}

export type EvaluationCsvField = "id" | "input" | "reference_answer" | "category" | "language" | "scorer";
export type EvaluationCsvMapping = Record<EvaluationCsvField, string>;

export interface EvaluationDatasetPatchInput {
  version: number;
  name?: string;
  description?: string;
  license_spdx?: string;
  license_url?: string;
  source_url?: string | null;
  default_scorer_id?: string;
}

export interface EvaluationScorer {
  scorer_id: string;
  version: string;
  label: string;
  description: string;
  execution: "DETERMINISTIC_LOCAL";
}

export interface EvaluationRunInput {
  dataset_revision_id: string;
  channel_id: string;
  catalog_snapshot_id: string;
  credential_profile_id: string | null;
  model_ids: string[];
  sample_strategy: "first" | "random" | "stratified";
  sample_seed: number;
  sample_count: number;
  scorer_id: string;
  max_output_tokens: number;
  timeout_seconds: number;
  concurrency: 1 | 2;
  max_cost_usd: number;
  confirm_unknown_chat_capability: boolean;
}

export interface EvaluationRunCreateInput extends EvaluationRunInput {
  api_key: string | null;
  confirm_unknown_cost: boolean;
}

export interface EvaluationRunPreview {
  dataset_revision_id: string;
  model_count: number;
  sample_count: number;
  maximum_calls: number;
  maximum_output_tokens: number;
  concurrency: number;
  estimated_cost_usd: number | null;
  cost_status: "KNOWN" | "UNKNOWN";
  cost_budget_enforcement: "ENFORCED_KNOWN_PRICING" | "UNAVAILABLE_UNKNOWN_PRICING";
  requires_unknown_cost_confirmation: boolean;
  model_source_id: string;
  unknown_chat_capability_models: string[];
  comparable: boolean;
}

export interface EvaluationRunModel {
  model_id: string;
  provider_model_id: string;
  state: string;
  completed_items: number;
  passed_items: number;
  failed_items: number;
  unknown_items: number;
  metrics: {
    overall_score?: number | null;
    confidence_interval_95?: { low: number; high: number; method: string } | null;
    category_scores?: Record<string, number | null>;
    success_rate?: number | null;
    usage?: Record<string, number | null>;
    known_cost_usd?: number | null;
    cost_completeness?: string;
    latency?: Record<string, { p50: number | null; p95: number | null }>;
    data_completeness?: number;
  };
}

export interface EvaluationRun {
  id: string;
  workspace_id: string;
  dataset_id: string;
  dataset_revision_id: string;
  channel_id: string;
  credential_profile_id: string | null;
  model_source_id: string;
  state: "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
  scorer_id: string;
  scorer_version: string;
  sample_strategy: string;
  sample_seed: number;
  sample_count: number;
  model_count: number;
  concurrency: number;
  max_output_tokens: number;
  timeout_seconds: number;
  max_cost_usd: number;
  request_snapshot: Record<string, unknown>;
  aggregate_result: Record<string, unknown> | null;
  failure_code: string | null;
  cancel_requested_at: string | null;
  created_at: string;
  finished_at: string | null;
  archived_at: string | null;
  models: EvaluationRunModel[];
}

export interface EvaluationRunItem {
  model_id: string;
  item_id: string;
  category: string;
  sequence: number;
  state: string;
  score: number | null;
  status: "PASS" | "FAIL" | "UNKNOWN";
  reason_code: string;
  latency: Record<string, number | null>;
  usage: Record<string, number | null>;
  output_sha256: string | null;
  safe_facts: Record<string, unknown>;
  created_at: string;
}

export interface EvaluationRunEvent {
  sequence: number;
  event_id: string;
  event_type: string;
  model_id: string | null;
  item_id: string | null;
  state: string;
  safe_facts: Record<string, unknown>;
  occurred_at: string;
}

export interface PartnerApplicationInput {
  site_name: string;
  website_url: string;
  terms_url: string;
  privacy_url: string;
  contact_email: string;
  api_base_url: string;
  protocol: "openai-compatible" | "anthropic-messages" | "gemini-native";
  region: string;
  model_claims: string[];
  pricing_notes: string;
  source_evidence_url: string;
  monitoring_credential_id: string | null;
}

export interface PartnerApplication extends PartnerApplicationInput {
  id: string;
  workspace_id: string;
  status: "DRAFT" | "SUBMITTED" | "OWNERSHIP_PENDING" | "MANUAL_REVIEW" | "BASELINE_TEST" | "PROBATION" | "APPROVED" | "REJECTED" | "PUBLISHED";
  version: number;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
}

export type RunState = "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
export type ResultStatus = "PASS" | "WARN" | "FAIL" | "UNKNOWN" | null;
export type ProbeType =
  | "chat"
  | "vision"
  | "embedding"
  | "image_generation"
  | "audio_speech"
  | "audio_transcription";

export interface Provider {
  id: string;
  name: string;
  english_name: string;
  base_url: string;
  default_model: string;
  target_kind: "local" | "cloud";
  requires_api_key: boolean;
}

export interface ProbeComponent {
  id: ProbeType;
  label: string;
  icon: string;
  scenario: string;
  steps: Array<{ id: string; label: string; description: string }>;
}

export interface ExecutionBackend {
  id: "local" | "temporal";
  available: boolean;
  status: string;
  supported_probe_types?: ProbeType[];
  supports_suites?: boolean;
}

export interface Bootstrap {
  product: { name: string; english_name: string; version: string };
  execution_backends: ExecutionBackend[];
  defaults: Record<string, unknown>;
  providers: Provider[];
  probe_components: ProbeComponent[];
  stats: {
    runs: number;
    running: number;
    pass_rate: number | null;
    targets: number;
    suites: number;
    agent_sessions?: number;
    monitor_policies?: number;
  };
}

export interface AgentTool {
  id: string;
  name: string;
  description: string;
  mode: "read_only";
}

export interface AgentSkill {
  id: string;
  name: string;
  description: string;
  allowed_tools: string[];
  starters: string[];
}

export interface AgentBootstrap {
  runtime: {
    framework: string;
    model_adapter: string;
    memory: string;
    max_iterations: number;
    automatic_model_retries: number;
    billable_tools_enabled: boolean;
  };
  tools: AgentTool[];
  skills: AgentSkill[];
  stats: { sessions: number };
}

export interface AgentSession {
  session_id: string;
  title: string;
  target_id: string;
  target_version: number;
  base_url: string;
  target_kind: "local" | "cloud";
  provider_id: string | null;
  model: string;
  skill_id: string;
  version: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface AgentMessage {
  message_id: string;
  session_id: string;
  sequence: number;
  role: "user" | "assistant";
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface AgentEvent {
  event_id: string;
  session_id: string;
  sequence: number;
  event_type: string;
  name: string;
  status: "RUNNING" | "PASS" | "WARN" | "FAIL";
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface AgentSessionCreateInput {
  title: string;
  target_id: string;
  model: string | null;
  skill_id: string;
}

export interface AgentReply {
  session: AgentSession;
  message: AgentMessage;
  events: AgentEvent[];
}

export interface Target {
  id: string;
  name: string;
  target_kind: "local" | "cloud";
  provider_id: string | null;
  base_url: string;
  default_model: string;
  credential_ref: string | null;
  credential_ref_configured: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface SuiteDocument {
  apiVersion: "probe.ai/v1alpha1";
  kind: "ProbeSuite";
  metadata: { name: string; version: string };
  spec: {
    layer: `L${0 | 1 | 2 | 3 | 4 | 5 | 6}`;
    protocol: "openai-chat";
    request: { prompt: string; stream: boolean; max_output_tokens: number };
    sampling: {
      warmup: number;
      requests: number;
      concurrency: number;
      timeout_seconds: number;
      max_cost_usd: number;
    };
    assertions: Array<Record<string, unknown>>;
  };
}

export interface SuiteRevision {
  id: string;
  suite_id: string;
  revision: number;
  document: SuiteDocument;
  sha256: string;
  created_at: string;
}

export interface Suite {
  id: string;
  name: string;
  description: string;
  version: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  latest_revision: SuiteRevision;
}

export interface WorkflowStep {
  id: string;
  label: string;
  description?: string;
  status: "PENDING" | "RUNNING" | "PASS" | "WARN" | "FAIL" | "SKIPPED";
  started_at?: string | null;
  finished_at?: string | null;
  facts?: string[];
}

export interface DimensionScore {
  dimension: string;
  score: number | null;
  status: string;
  metrics?: Record<string, unknown>;
}

export interface Measurement {
  status_code: number | null;
  ttfb_ms: number | null;
  ttft_ms: number | null;
  e2e_ms: number | null;
  error_class: string | null;
  evidence: Record<string, unknown>;
}

export interface Run {
  run_id: string;
  target_id: string | null;
  suite_revision_id: string | null;
  monitor_policy_id: string | null;
  run_kind: "component" | "suite";
  execution_backend: "local" | "temporal";
  state: RunState;
  result_status: ResultStatus;
  created_at: string;
  finished_at: string | null;
  archived_at: string | null;
  failure_code: string | null;
  cancel_requested_at: string | null;
  config: {
    base_url: string;
    model: string;
    target_kind: string;
    provider_id: string | null;
    run_mode: string;
    probe_type: ProbeType;
    stream: boolean;
    timeout_seconds: number;
    max_output_tokens: number;
  };
  workflow: {
    status: string;
    component_id: ProbeType;
    component_label?: string;
    steps: WorkflowStep[];
  } | null;
  result?: {
    status: string;
    reason_codes: string[];
    dimension_scores: DimensionScore[];
    measurements: Measurement[];
  } | null;
}

export type MonitorStatus = "UNKNOWN" | "UP" | "DEGRADED" | "DOWN";

export interface MonitorPolicy {
  id: string;
  name: string;
  target_id: string;
  suite_revision_id: string | null;
  run_kind: "component" | "suite";
  probe_type: ProbeType;
  execution_backend: "local" | "temporal";
  model: string;
  stream: boolean;
  timeout_seconds: number;
  interval_seconds: number;
  failure_threshold: number;
  recovery_threshold: number;
  schedule_offset_seconds: number;
  enabled: boolean;
  version: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  last_dispatch_failure_code: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface MonitorBucket {
  started_at: string;
  total: number;
  pass: number;
  warn: number;
  fail: number;
  unknown: number;
  pass_rate: number;
  p95_e2e_ms: number | null;
  p95_ttft_ms: number | null;
}

export interface MonitorPolicyOverview extends MonitorPolicy {
  status: MonitorStatus;
  consecutive_successes: number;
  consecutive_failures: number;
  last_observation: ResultStatus;
  last_observed_at: string | null;
  latest_error_class: string | null;
  sample_count: number;
  buckets: MonitorBucket[];
}

export interface MonitoringOverview {
  window: "90m" | "24h" | "7d" | "30d";
  window_seconds: number;
  bucket_seconds: number;
  generated_at: string;
  timeline: string[];
  summary: {
    policies: number;
    unknown: number;
    up: number;
    degraded: number;
    down: number;
    samples: number;
  };
  policies: MonitorPolicyOverview[];
}

export interface MonitorIncident {
  id: string;
  policy_id: string;
  run_id: string;
  event_type: "DOWN" | "DEGRADED" | "RECOVERED";
  from_status: MonitorStatus;
  to_status: MonitorStatus;
  error_class: string | null;
  observed_at: string;
}

export interface RunEvent {
  event_id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  phase: string;
  status: string;
  occurred_at: string;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details: Array<{ field?: string; message: string; type?: string }>;
    request_id: string | null;
  };
}
