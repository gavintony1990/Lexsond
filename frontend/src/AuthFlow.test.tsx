import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RootApp } from "./App";

const session = {
  user: {
    user_id: "user-1",
    email: "observer@example.com",
    display_name: "Observer",
    avatar_url: null,
    status: "ACTIVE",
    system_role: "USER",
    email_verified_at: "2026-07-22T00:00:00Z",
    workspace_id: "workspace-1",
    workspace_name: "Observer 的个人工作区",
    workspace_role: "OWNER",
  },
  csrf_token: "session-csrf-only-in-memory",
  auth_mode: "required",
} as const;

const bootstrap = {
  product: { name: "Lexsond", english_name: "Lexsond", version: "0.8.0" },
  execution_backends: [
    { id: "local", available: true, status: "READY" },
    { id: "temporal", available: false, status: "NOT_CONFIGURED" },
  ],
  defaults: {}, providers: [], probe_components: [],
  stats: { runs: 0, running: 0, pass_rate: null, targets: 0, suites: 0, agent_sessions: 0, monitor_policies: 0 },
};

function envelope(data: unknown, status = 200) {
  return new Response(JSON.stringify(status >= 400 ? {
    error: { code: "AUTHENTICATION_REQUIRED", message: "请先登录", details: [], request_id: "request-1" },
  } : { data }), { status, headers: { "Content-Type": "application/json" } });
}

function renderRoot(path = "/overview") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><RootApp /></MemoryRouter></QueryClientProvider>),
  };
}

describe("authenticated application boundary", () => {
  const localValues = new Map<string, string>();
  const sessionValues = new Map<string, string>();

  beforeEach(() => {
    localValues.clear();
    sessionValues.clear();
    vi.stubGlobal("localStorage", storage(localValues));
    vi.stubGlobal("sessionStorage", storage(sessionValues));
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("does not render workspace data before the current session resolves", async () => {
    let resolveSession!: (value: Response) => void;
    const pending = new Promise<Response>((resolve) => { resolveSession = resolve; });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/auth/session")) return pending;
      if (url.endsWith("/bootstrap")) return Promise.resolve(envelope(bootstrap));
      return Promise.resolve(envelope([]));
    }));

    renderRoot();
    expect(screen.getByLabelText("正在加载当前用户")).toBeInTheDocument();
    expect(screen.queryByText("第一次探测，从这里开始")).not.toBeInTheDocument();
    resolveSession(envelope(session));
    expect(await screen.findByText("第一次探测，从这里开始")).toBeInTheDocument();
  });

  it("returns an anonymous visitor to a branded login page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => envelope(null, 401)));
    renderRoot("/suites");
    expect(await screen.findByRole("heading", { name: "登录观测站" })).toBeInTheDocument();
    expect(screen.queryByText("探测套件")).not.toBeInTheDocument();
  });

  it("uses pre-auth csrf, keeps tokens out of browser storage, and returns to the target page", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/auth/session")) return envelope(null, 401);
      if (url.endsWith("/auth/csrf")) return envelope({ csrf_token: "preauth-memory-token", expires_in: 600 });
      if (url.endsWith("/auth/login")) {
        const headers = new Headers(init?.headers);
        expect(headers.get("X-CSRF-Token")).toBe("preauth-memory-token");
        return envelope({ ...session, session: { session_id: "session-1", user_id: "user-1", workspace_id: "workspace-1" }, return_to: "/overview" });
      }
      if (url.endsWith("/bootstrap")) return envelope(bootstrap);
      return envelope([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { client } = renderRoot("/overview");

    fireEvent.change(await screen.findByLabelText("邮箱"), { target: { value: "observer@example.com" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct password" } });
    fireEvent.click(screen.getByRole("button", { name: /登录/ }));

    expect(await screen.findByText("第一次探测，从这里开始")).toBeInTheDocument();
    expect(localValues.size).toBe(0);
    expect(sessionValues.size).toBe(0);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain("correct password");
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/auth/login"))).toBe(true));
  });

  it("resets a password with pre-auth csrf and clears the password field", async () => {
    const resetToken = "reset-token-value-with-at-least-32-characters";
    const newPassword = "replacement password value";
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/auth/session")) return envelope(null, 401);
      if (url.endsWith("/auth/csrf")) return envelope({ csrf_token: "preauth-reset-csrf", expires_in: 600 });
      if (url.endsWith("/auth/reset-password")) {
        expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("preauth-reset-csrf");
        expect(JSON.parse(String(init?.body))).toEqual({ token: resetToken, new_password: newPassword });
        return envelope({ status: "PASSWORD_RESET" });
      }
      return envelope([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { client } = renderRoot(`/reset-password#token=${resetToken}`);

    const password = await screen.findByLabelText("新密码");
    fireEvent.change(password, { target: { value: newPassword } });
    fireEvent.click(screen.getByRole("button", { name: "更新密码" }));

    expect(await screen.findByRole("heading", { name: "密码已更新" })).toBeInTheDocument();
    expect(password).toHaveValue("");
    expect(localValues.size).toBe(0);
    expect(sessionValues.size).toBe(0);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(resetToken);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(newPassword);
  });
});

function storage(values: Map<string, string>): Storage {
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, String(value)); },
  };
}
