import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiKeyCatalogProbe } from "./pages/ApiKeyCatalogProbe";

describe("API Key model catalog probe", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("uses one saved credential reference without placing its secret in the request", async () => {
    const requests: Array<{ url: string; body: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/targets?")) return response([{
        id: "30000000-0000-4000-8000-000000000001", name: "OpenAI channel",
        target_kind: "cloud", provider_id: "openai", base_url: "https://api.openai.com/v1",
        default_model: "gpt-test", credential_ref: null, credential_ref_configured: false,
        version: 1, created_at: "2026-07-22T00:00:00Z", updated_at: "2026-07-22T00:00:00Z", archived_at: null,
      }]);
      if (url.includes("/credential-profiles")) return response([{
        id: "40000000-0000-4000-8000-000000000001", workspace_id: "20000000-0000-4000-8000-000000000001",
        label: "Primary", provider_id: "openai", storage_backend: "SYSTEM_KEYRING", masked_suffix: "last",
        status: "ACTIVE", version: 1, last_verified_at: null, last_used_at: null,
        created_at: "2026-07-22T00:00:00Z", updated_at: "2026-07-22T00:00:00Z", archived_at: null,
      }]);
      if (url.includes("/catalog")) {
        requests.push({ url, body: String(init?.body) });
        return response({ models: [{ id: "gpt-test", probe_types: ["chat"] }], model_count: 1 });
      }
      return response([]);
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><ApiKeyCatalogProbe /></MemoryRouter></QueryClientProvider>);

    await screen.findByRole("option", { name: /OpenAI channel/ });
    fireEvent.change(screen.getByLabelText("渠道"), { target: { value: "30000000-0000-4000-8000-000000000001" } });
    fireEvent.click(screen.getByLabelText("使用已保存凭据"));
    await screen.findByRole("option", { name: /Primary/ });
    fireEvent.change(screen.getByLabelText("已保存凭据"), { target: { value: "40000000-0000-4000-8000-000000000001" } });
    fireEvent.click(screen.getByRole("button", { name: "读取该 Key 可见模型" }));

    await screen.findByText("gpt-test");
    expect(requests).toHaveLength(1);
    expect(JSON.parse(requests[0].body)).toEqual({
      api_key: null,
      credential_profile_id: "40000000-0000-4000-8000-000000000001",
    });
    expect(requests[0].body).not.toContain("sk-");
  });
});

function response(data: unknown): Response {
  return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
}
