import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "./pages/ApiKeyManagement";

const bootstrap = {
  product: { name: "Lexsond", english_name: "Lexsond", version: "0.8.0" },
  execution_backends: [], defaults: {}, probe_components: [],
  stats: { runs: 0, running: 0, pass_rate: null, targets: 0, suites: 0 },
  providers: [{ id: "openai", name: "OpenAI", english_name: "OpenAI", base_url: "https://api.openai.com/v1", default_model: "gpt-test", target_kind: "cloud", requires_api_key: true }],
};

describe("API Key management", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("reads clipboard only after a click and clears component/query state after save", async () => {
    const secret = "sk-explicit-clipboard-sentinel";
    const readText = vi.fn(async () => `Authorization: Bearer ${secret}`);
    const writeText = vi.fn(async () => undefined);
    const localSet = vi.fn();
    const sessionSet = vi.fn();
    vi.stubGlobal("localStorage", { getItem: vi.fn(() => null), setItem: localSet, removeItem: vi.fn(), clear: vi.fn() });
    vi.stubGlobal("sessionStorage", { getItem: vi.fn(() => null), setItem: sessionSet, removeItem: vi.fn(), clear: vi.fn() });
    Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
    Object.defineProperty(navigator, "clipboard", { value: { readText, writeText }, configurable: true });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/bootstrap")) return response(bootstrap);
      if (url.includes("credential-vault/status")) return response({ available: true, backend: "test-keyring", reason: null, storage_backend: "SYSTEM_KEYRING", persistence_enabled: true });
      if (url.includes("credential-profiles") && init?.method === "POST") return response({ id: "40000000-0000-4000-8000-000000000001", workspace_id: "20000000-0000-4000-8000-000000000001", label: "Primary", provider_id: "openai", storage_backend: "SYSTEM_KEYRING", masked_suffix: "inel", status: "ACTIVE", version: 1, last_verified_at: null, last_used_at: null, created_at: "2026-07-22T00:00:00Z", updated_at: "2026-07-22T00:00:00Z", archived_at: null });
      if (url.includes("credential-profiles")) return response([]);
      return response([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><CredentialsPage /></MemoryRouter></QueryClientProvider>);

    expect(readText).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "粘贴并识别" }));
    await waitFor(() => expect(readText).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("API Key")).toHaveValue(secret);

    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Primary" } });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "openai" } });
    fireEvent.click(screen.getByRole("button", { name: "保存到系统密钥库" }));
    await screen.findByText(/已保存到系统密钥库/);

    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(secret);
    expect(localSet).not.toHaveBeenCalled();
    expect(sessionSet).not.toHaveBeenCalled();
  });

  it("keeps manual entry available when clipboard permission is denied", async () => {
    const readText = vi.fn(async () => { throw new DOMException("denied", "NotAllowedError"); });
    Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
    Object.defineProperty(navigator, "clipboard", { value: { readText }, configurable: true });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/bootstrap")) return response(bootstrap);
      if (url.includes("credential-vault/status")) return response({ available: true, backend: "test", reason: null, storage_backend: "SYSTEM_KEYRING", persistence_enabled: true });
      return response([]);
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><CredentialsPage /></MemoryRouter></QueryClientProvider>);

    fireEvent.click(await screen.findByRole("button", { name: "粘贴并识别" }));
    expect(await screen.findByText("denied")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "sk-manual-entry" } });
    expect(screen.getByLabelText("API Key")).toHaveValue("sk-manual-entry");
  });
});

function response(data: unknown): Response {
  return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
}
