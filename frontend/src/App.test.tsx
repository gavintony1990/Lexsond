import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const bootstrap = {
  product: { name: "Lexsond", english_name: "Lexsond · 码海测深", version: "0.8.0" },
  execution_backends: [
    { id: "local", available: true, status: "READY" },
    { id: "temporal", available: false, status: "NOT_CONFIGURED", supported_probe_types: ["chat"], supports_suites: true },
  ],
  defaults: {},
  providers: [],
  probe_components: [],
  stats: { runs: 0, running: 0, pass_rate: null, targets: 0, suites: 0, agent_sessions: 0, monitor_policies: 0 },
};

describe("observatory console", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders the target control plane without exposing a saved API key field", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap") ? bootstrap : [];
      return new Response(JSON.stringify({ data }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/targets"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "绑定要测量的海域" })).toBeInTheDocument();
    expect(screen.getByText("目标只保存地址、模型与非机密凭据引用；真实 API Key 每次运行时临时输入。")).toBeInTheDocument();
    expect(screen.queryByLabelText("API Key")).not.toBeInTheDocument();
  });

  it("clears a temporary catalog key immediately after discovery", async () => {
    const cloudTarget = {
      id: "00000000-0000-4000-8000-000000000001",
      name: "Cloud relay",
      target_kind: "cloud",
      provider_id: null,
      base_url: "https://models.example.invalid/v1",
      default_model: "chat-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap")
        ? bootstrap
        : url.includes("/catalog")
          ? { models: [{ id: "chat-model", probe_types: ["chat"] }], model_count: 1 }
          : [cloudTarget];
      return new Response(JSON.stringify({ data }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/targets"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "发现模型" }));
    const keyInput = await screen.findByLabelText("临时 API Key");
    fireEvent.change(keyInput, { target: { value: "sk-temporary-browser-only" } });
    fireEvent.click(screen.getByRole("button", { name: "连接并读取模型" }));

    await screen.findByText("MODELS REPORTED");
    await waitFor(() => expect(keyInput).toHaveValue(""));
  });

  it("shows but disables Temporal when the backend is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap") ? bootstrap : [];
      return new Response(JSON.stringify({ data }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/runs/new"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "编排一次有界质量探测" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Temporal 工作流/ })).toBeDisabled();
    expect(screen.getAllByText("OFFLINE").length).toBeGreaterThan(0);
  });

  it("renders the continuous monitoring matrix without any API key field", async () => {
    const cloudTarget = {
      id: "00000000-0000-4000-8000-000000000010",
      name: "Cloud monitor target",
      target_kind: "cloud",
      provider_id: null,
      base_url: "https://models.example.invalid/v1",
      default_model: "chat-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const monitoring = {
      window: "24h",
      window_seconds: 86400,
      bucket_seconds: 3600,
      generated_at: "2026-07-21T00:00:00Z",
      timeline: ["2026-07-21T00:00:00Z"],
      summary: { policies: 0, unknown: 0, up: 0, degraded: 0, down: 0, samples: 0 },
      policies: [],
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap")
        ? bootstrap
        : url.includes("/monitoring/overview")
          ? monitoring
          : url.includes("/targets")
            ? [cloudTarget]
            : [];
      return new Response(JSON.stringify({ data }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/monitoring"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "中转站可用性热力图" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "新建持续探测" }));
    expect(screen.getByText("不保存 API Key")).toBeInTheDocument();
    expect(screen.queryByLabelText(/API Key/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("探测目标"), {
      target: { value: cloudTarget.id },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("云端目标");
    expect(screen.getByRole("button", { name: "创建并启用" })).toBeDisabled();
  });

  it("opens an existing monitor policy in the versioned edit drawer", async () => {
    const target = {
      id: "00000000-0000-4000-8000-000000000011",
      name: "Local monitor target",
      target_kind: "local",
      provider_id: "ollama",
      base_url: "http://127.0.0.1:11434/v1",
      default_model: "qwen3:8b",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const policy = {
      id: "00000000-0000-4000-8000-000000000012",
      name: "Existing pulse",
      target_id: target.id,
      suite_revision_id: null,
      run_kind: "component",
      probe_type: "chat",
      execution_backend: "local",
      model: "qwen3:8b",
      stream: true,
      timeout_seconds: 30,
      interval_seconds: 300,
      failure_threshold: 2,
      recovery_threshold: 1,
      schedule_offset_seconds: 12,
      enabled: true,
      version: 3,
      next_run_at: "2026-07-21T00:05:00Z",
      last_run_at: null,
      last_run_id: null,
      last_dispatch_failure_code: null,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const overview = {
      window: "24h",
      window_seconds: 86400,
      bucket_seconds: 3600,
      generated_at: "2026-07-21T00:00:00Z",
      timeline: [],
      summary: { policies: 1, unknown: 1, up: 0, degraded: 0, down: 0, samples: 0 },
      policies: [],
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap")
        ? bootstrap
        : url.includes("/monitoring/overview")
          ? overview
          : url.includes("/monitor-policies")
            ? [policy]
            : url.includes("/targets")
              ? [target]
              : [];
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/monitoring"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Existing pulse")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    expect(screen.getByRole("heading", { name: "编辑持续探测策略" })).toBeInTheDocument();
    expect(screen.getByLabelText("策略名称")).toHaveValue("Existing pulse");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
  });

  it("clears a run key on failure and never stores it in mutation variables", async () => {
    const localTarget = {
      id: "00000000-0000-4000-8000-000000000002",
      name: "Local authenticated relay",
      target_kind: "local",
      provider_id: null,
      base_url: "http://127.0.0.1:8091/v1",
      default_model: "mock-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.body && url.endsWith("/runs")) {
        return new Response(JSON.stringify({ error: { code: "UPSTREAM_ERROR", message: "safe failure", details: null, request_id: "test" } }), {
          status: 502,
          headers: { "Content-Type": "application/json" },
        });
      }
      const data = url.includes("/bootstrap") ? bootstrap : url.includes("/targets") ? [localTarget] : [];
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/runs/new"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("radio", { name: /Local authenticated relay/ }));
    await waitFor(() => expect(screen.getByLabelText("模型 ID")).toHaveValue("mock-model"));
    const keyInput = screen.getByLabelText(/可选本地鉴权 Key/);
    const secret = "test-browser-transient-key";
    fireEvent.change(keyInput, { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: /冻结配置并发起/ }));

    await screen.findByText(/safe failure/);
    const post = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/runs") && Boolean(init?.body));
    expect(post).toBeDefined();
    expect(String(post?.[1]?.body)).toContain(secret);
    expect(keyInput).toHaveValue("");
    expect(JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state))).not.toContain(secret);
  });

  it("renders the LangChain Agent call graph, Skills, Tools, and memory entry", async () => {
    const localTarget = {
      id: "00000000-0000-4000-8000-000000000003",
      name: "Agent target",
      target_kind: "local",
      provider_id: null,
      base_url: "http://127.0.0.1:8091/v1",
      default_model: "mock-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const agentBootstrap = {
      runtime: {
        framework: "LangChain",
        model_adapter: "OpenAI-compatible BaseChatModel",
        memory: "repository checkpointer",
        max_iterations: 4,
        automatic_model_retries: 0,
        billable_tools_enabled: false,
      },
      tools: [{ id: "list_probe_targets", name: "读取探测目标", description: "读取目标", mode: "read_only" }],
      skills: [{ id: "connection-diagnosis", name: "连接与鉴权诊断", description: "诊断连接", allowed_tools: ["list_probe_targets"], starters: ["检查连接"] }],
      stats: { sessions: 0 },
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/agent/bootstrap")
        ? agentBootstrap
        : url.includes("/agent/sessions")
          ? []
          : url.includes("/targets")
            ? [localTarget]
            : bootstrap;
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agent"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "让探针证据进入智能体回路" })).toBeInTheDocument();
    expect(screen.getByText("React 意图入口")).toBeInTheDocument();
    expect(screen.getAllByText("LangChain Agent").length).toBeGreaterThan(0);
    expect(await screen.findByRole("heading", { name: "建立一条可恢复的诊断链路" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Agent 临时 API Key")).not.toBeInTheDocument();
  });

  it("clears the Agent key on model failure and excludes it from mutation state", async () => {
    const sessionId = "00000000-0000-4000-8000-000000000010";
    const target = {
      id: "00000000-0000-4000-8000-000000000011",
      name: "Agent relay",
      target_kind: "local",
      provider_id: null,
      base_url: "http://127.0.0.1:8091/v1",
      default_model: "mock-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const session = {
      session_id: sessionId,
      title: "Agent failure boundary",
      target_id: target.id,
      target_version: 1,
      base_url: target.base_url,
      target_kind: "local",
      provider_id: null,
      model: "mock-model",
      skill_id: "connection-diagnosis",
      version: 1,
      created_at: target.created_at,
      updated_at: target.updated_at,
      archived_at: null,
    };
    const agentBootstrap = {
      runtime: { framework: "LangChain", model_adapter: "BaseChatModel", memory: "repository checkpointer", max_iterations: 4, automatic_model_retries: 0, billable_tools_enabled: false },
      tools: [],
      skills: [{ id: "connection-diagnosis", name: "连接与鉴权诊断", description: "诊断连接", allowed_tools: [], starters: [] }],
      stats: { sessions: 1 },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.body && url.endsWith(`/agent/sessions/${sessionId}/messages`)) {
        return new Response(JSON.stringify({ error: { code: "AGENT_MODEL_ERROR", message: "safe model failure", details: [], request_id: "agent-test" } }), { status: 502, headers: { "Content-Type": "application/json" } });
      }
      const data = url.includes("/agent/bootstrap")
        ? agentBootstrap
        : url.includes(`/agent/sessions/${sessionId}/messages`) || url.includes(`/agent/sessions/${sessionId}/events`)
          ? []
          : url.includes(`/agent/sessions/${sessionId}`)
            ? session
            : url.includes("/agent/sessions")
              ? [session]
              : url.includes("/targets")
                ? [target]
                : bootstrap;
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/agent/${sessionId}`]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    const prompt = await screen.findByLabelText("给探针智能体发送消息");
    const keyInput = screen.getByLabelText("Agent 临时 API Key");
    const secret = "test-agent-browser-key";
    fireEvent.change(prompt, { target: { value: `检查这次模型失败 ${secret}` } });
    fireEvent.change(keyInput, { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByText(/safe model failure/);
    await waitFor(() => expect(keyInput).toHaveValue(""));
    const post = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/messages") && Boolean(init?.body));
    expect(String(post?.[1]?.body)).toContain(secret);
    expect(JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state))).not.toContain(secret);
  });
});
