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
export type CatalogInput = components["schemas"]["CatalogRequest"];

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
