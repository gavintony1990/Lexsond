import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    onlineManager.setOnline(true);
    cleanup();
    vi.restoreAllMocks();
  });

  it("gives a first-time operator an actionable three-step probe path", async () => {
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
        <MemoryRouter initialEntries={["/"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "第一次探测，从这里开始" })).toBeInTheDocument();
    expect(screen.getAllByText("添加 API 目标").length).toBeGreaterThan(0);
    expect(screen.getByText("运行推荐探针")).toBeInTheDocument();
    expect(screen.getByText("读懂探测结果")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /添加第一个目标/ })).toHaveAttribute("href", "/targets");
  });

  it("keeps the eight product modules in the required order and exposes API Key children", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify({ data: String(input).includes("/bootstrap") ? bootstrap : [] }), {
      status: 200, headers: { "Content-Type": "application/json" },
    })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/overview"]}><App /></MemoryRouter></QueryClientProvider>);

    await screen.findByRole("heading", { name: "第一次探测，从这里开始" });
    expect([...document.querySelectorAll("[data-primary-nav-item]")].map((element) => element.textContent?.trim())).toEqual([
      "总览与入门01", "API Key 管理02", "单模型探测03", "API Key 模型探测04",
      "合作中转站入驻05", "合作中转站持续监控06", "诊断助手 ChatGPT07", "探测套件管理08",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "展开密钥管理子菜单" }));
    const subnav = screen.getByRole("navigation", { name: "API Key 管理" });
    expect(within(subnav).getAllByRole("link").map((link) => link.textContent)).toEqual(["密钥", "渠道", "模型厂商", "模型来源"]);
  });

  it("opens a plain-language guide from the global navigation", async () => {
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
        <MemoryRouter initialEntries={["/"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "使用指南" }));
    const guide = screen.getByRole("dialog", { name: "快速使用指南" });
    expect(within(guide).getByRole("heading", { name: "四步完成一次可信探测" })).toBeInTheDocument();
    expect(within(guide).getByText(/第一次建议/)).toBeInTheDocument();
    expect(within(guide).getByRole("link", { name: "去添加目标" })).toHaveAttribute("href", "/targets");
    expect(within(guide).getByRole("link", { name: "去发起探测" })).toHaveAttribute("href", "/runs/new");
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

  it("switches a provider preset to a custom endpoint when its base URL changes", async () => {
    const provider = {
      id: "dashscope",
      name: "阿里云百炼",
      english_name: "Alibaba Cloud Model Studio",
      target_kind: "cloud",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      default_model: "qwen-plus",
      docs_url: "https://example.invalid/dashscope-docs",
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap") ? { ...bootstrap, providers: [provider] } : [];
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

    fireEvent.click(await screen.findByRole("button", { name: "新增目标" }));
    const providerSelect = screen.getByLabelText("Provider");
    const baseUrl = screen.getByLabelText("Base URL");
    fireEvent.change(providerSelect, { target: { value: provider.id } });

    expect(providerSelect).toHaveValue(provider.id);
    expect(baseUrl).toHaveValue(provider.base_url);
    expect(screen.getByText(/标准端点/)).toBeInTheDocument();

    fireEvent.change(baseUrl, {
      target: { value: "https://workspace.example.invalid/compatible-mode/v1" },
    });

    await waitFor(() => expect(providerSelect).toHaveValue(""));
    expect(screen.getByText(/已切换为自定义兼容端点/)).toBeInTheDocument();
  });

  it("links a saved target directly into a preselected run composer", async () => {
    const target = {
      id: "00000000-0000-4000-8000-000000000041",
      name: "Direct probe target",
      target_kind: "local",
      provider_id: null,
      base_url: "http://127.0.0.1:8091/v1",
      default_model: "direct-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap") ? bootstrap : url.includes("/targets") ? [target] : [];
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/targets"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("link", { name: "直接探测" })).toHaveAttribute("href", `/runs/new?target=${target.id}`);
    unmount();

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/runs/new?target=${target.id}`]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("radio", { name: /Direct probe target/ })).toBeChecked();
    await waitFor(() => expect(screen.getByLabelText("本次使用的模型 ID")).toHaveValue("direct-model"));
  });

  it("requires a temporary key before a local run against a cloud target", async () => {
    const target = {
      id: "00000000-0000-4000-8000-000000000042",
      name: "Cloud run target",
      target_kind: "cloud",
      provider_id: null,
      base_url: "https://cloud-run.example.invalid/v1",
      default_model: "cloud-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap") ? bootstrap : url.includes("/targets") ? [target] : [];
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/runs/new?target=${target.id}`]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("radio", { name: /Cloud run target/ })).toBeChecked();
    const launch = screen.getByRole("button", { name: /开始探测/ });
    const keyInput = screen.getByLabelText(/本次 API Key/);
    expect(launch).toBeDisabled();
    expect(screen.getByText(/云端目标必须输入本次临时 Key/)).toBeInTheDocument();
    fireEvent.submit(launch.closest("form")!);
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/runs"))).toBe(false);

    fireEvent.change(keyInput, { target: { value: "   " } });
    expect(launch).toBeDisabled();
    fireEvent.change(keyInput, { target: { value: "test-cloud-run-key" } });
    expect(launch).toBeEnabled();
  });

  it("clears a temporary run key when the selected target changes", async () => {
    const targets = ["A", "B"].map((name, index) => ({
      id: `00000000-0000-4000-8000-00000000005${index}`,
      name: `Cloud target ${name}`,
      target_kind: "cloud",
      provider_id: null,
      base_url: `https://cloud-${name.toLowerCase()}.example.invalid/v1`,
      default_model: `model-${name.toLowerCase()}`,
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap") ? bootstrap : url.includes("/targets") ? targets : [];
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/runs/new"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("radio", { name: /Cloud target A/ }));
    const keyInput = screen.getByLabelText(/本次 API Key/);
    fireEvent.change(keyInput, { target: { value: "test-target-a-only-key" } });
    expect(screen.getByRole("button", { name: /开始探测/ })).toBeEnabled();

    fireEvent.click(screen.getByRole("radio", { name: /Cloud target B/ }));
    await waitFor(() => expect(keyInput).toHaveValue(""));
    expect(screen.getByRole("button", { name: /开始探测/ })).toBeDisabled();
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
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
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
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/targets"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "发现模型" }));
    const keyInput = await screen.findByLabelText("临时 API Key");
    const discoverButton = screen.getByRole("button", { name: "连接并读取模型" });
    expect(discoverButton).toBeDisabled();
    expect(screen.getByText(/生产凭据引用仅供 Temporal Worker 使用/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "跳过发现，直接发起探测" })).toHaveAttribute("href", `/runs/new?target=${cloudTarget.id}`);
    fireEvent.click(discoverButton);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/catalog"))).toBe(false);

    const secret = "sk-temporary-browser-only";
    fireEvent.change(keyInput, { target: { value: secret } });
    expect(discoverButton).toBeEnabled();
    fireEvent.click(discoverButton);

    await waitFor(() => expect(keyInput).toHaveValue(""));
    expect(JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state))).not.toContain(secret);
    await screen.findByText("MODELS REPORTED");
  });

  it("clears a catalog key on failure without retaining it in mutation state", async () => {
    const cloudTarget = {
      id: "00000000-0000-4000-8000-000000000021",
      name: "Failing cloud relay",
      target_kind: "cloud",
      provider_id: null,
      base_url: "https://failure.example.invalid/v1",
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
      if (url.includes("/catalog")) {
        return new Response(JSON.stringify({ error: { code: "UPSTREAM_ERROR", message: "safe catalog failure", details: null, request_id: "catalog-test" } }), {
          status: 502,
          headers: { "Content-Type": "application/json" },
        });
      }
      const data = url.includes("/bootstrap") ? bootstrap : [cloudTarget];
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/targets"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "发现模型" }));
    const keyInput = screen.getByLabelText("临时 API Key");
    const secret = "test-catalog-failure-key";
    fireEvent.change(keyInput, { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: "连接并读取模型" }));

    await waitFor(() => expect(keyInput).toHaveValue(""));
    await screen.findByText(/safe catalog failure/);
    expect(JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state))).not.toContain(secret);
  });

  it("consumes a catalog key immediately instead of queuing it while offline", async () => {
    const cloudTarget = {
      id: "00000000-0000-4000-8000-000000000022",
      name: "Offline browser relay",
      target_kind: "cloud",
      provider_id: null,
      base_url: "https://offline.example.invalid/v1",
      default_model: "chat-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes("/bootstrap")
        ? bootstrap
        : url.includes("/catalog")
          ? { models: [], model_count: 0 }
          : [cloudTarget];
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/targets"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "发现模型" }));
    const keyInput = screen.getByLabelText("临时 API Key");
    const secret = "test-offline-catalog-key";
    fireEvent.change(keyInput, { target: { value: secret } });
    onlineManager.setOnline(false);
    fireEvent.click(screen.getByRole("button", { name: "连接并读取模型" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/catalog"))).toBe(true));
    expect(keyInput).toHaveValue("");
    expect(JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state))).not.toContain(secret);
  });

  it("prevents another catalog dialog while a previous request is in flight", async () => {
    const targets = ["A", "B"].map((name, index) => ({
      id: `00000000-0000-4000-8000-00000000003${index}`,
      name: `Cloud relay ${name}`,
      target_kind: "cloud",
      provider_id: null,
      base_url: `https://${name.toLowerCase()}.example.invalid/v1`,
      default_model: "chat-model",
      credential_ref: null,
      credential_ref_configured: false,
      version: 1,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
      archived_at: null,
    }));
    let resolveCatalog: ((response: Response) => void) | undefined;
    const pendingCatalog = new Promise<Response>((resolve) => { resolveCatalog = resolve; });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/catalog")) return pendingCatalog;
      const data = url.includes("/bootstrap") ? bootstrap : targets;
      return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/targets"]}><App /></MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click((await screen.findAllByRole("button", { name: "发现模型" }))[0]);
    fireEvent.change(screen.getByLabelText("临时 API Key"), { target: { value: "test-inflight-catalog-key" } });
    fireEvent.click(screen.getByRole("button", { name: "连接并读取模型" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "模型目录" })).getByRole("button", { name: "关闭" }));

    expect(screen.queryByRole("dialog", { name: "模型目录" })).not.toBeInTheDocument();
    const blockedButtons = await screen.findAllByRole("button", { name: "发现中…" });
    blockedButtons.forEach((button) => expect(button).toBeDisabled());

    resolveCatalog?.(new Response(JSON.stringify({ data: { models: [], model_count: 0 } }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await waitFor(() => screen.getAllByRole("button", { name: "发现模型" }).forEach((button) => expect(button).toBeEnabled()));
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

    expect(await screen.findByRole("heading", { name: "发起一次探测" })).toBeInTheDocument();
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
    await waitFor(() => expect(screen.getByLabelText("本次使用的模型 ID")).toHaveValue("mock-model"));
    const keyInput = screen.getByLabelText(/可选本地鉴权 Key/);
    const secret = "test-browser-transient-key";
    fireEvent.change(keyInput, { target: { value: secret } });
    onlineManager.setOnline(false);
    fireEvent.click(screen.getByRole("button", { name: /开始探测/ }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/runs"))).toBe(true));
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
