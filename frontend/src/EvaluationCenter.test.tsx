import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EvaluationDatasets } from "./pages/EvaluationDatasets";
import { EvaluationRuns } from "./pages/EvaluationRuns";
import { SuiteModuleTabs } from "./pages/SuiteModuleTabs";

describe("evaluation center", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps evaluation inside the four Suite Management tabs", () => {
    render(<MemoryRouter><SuiteModuleTabs /></MemoryRouter>);
    const navigation = screen.getByRole("navigation", { name: "探测套件管理" });
    expect(within(navigation).getAllByRole("link").map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["探测套件", "/suites"],
      ["评测数据集", "/suites/datasets"],
      ["评分器", "/suites/scorers"],
      ["评测记录", "/suites/evaluation-runs"],
    ]);
  });

  it("separates runnable QuickEval from license and runner catalog records", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response([
      dataset("quick", "Lexsond QuickEval v1", "BUNDLED", true),
      dataset("human", "HumanEval", "RUNNER_REQUIRED", false),
      dataset("ceval", "C-Eval", "RESEARCH_ONLY", false),
    ])));
    renderPage(<Routes><Route path="/suites/datasets" element={<EvaluationDatasets />} /></Routes>, "/suites/datasets");

    expect(await screen.findByRole("heading", { name: "Lexsond QuickEval v1" })).toBeInTheDocument();
    const humanCard = screen.getByRole("heading", { name: "HumanEval" }).closest("article")!;
    const cevalCard = screen.getByRole("heading", { name: "C-Eval" }).closest("article")!;
    expect(within(humanCard).getByText("需要 Runner")).toBeInTheDocument();
    expect(within(humanCard).queryByRole("link", { name: /使用数据集评测/ })).not.toBeInTheDocument();
    expect(within(cevalCard).getByText("仅限研究")).toBeInTheDocument();
    expect(within(cevalCard).queryByRole("link", { name: /使用数据集评测/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /使用数据集评测/ })).toHaveLength(1);
  });

  it("requires an explicit six-field mapping for non-standard CSV headers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response([])));
    renderPage(<Routes><Route path="/suites/datasets" element={<EvaluationDatasets />} /></Routes>, "/suites/datasets");
    fireEvent.click(screen.getByRole("button", { name: "上传数据集" }));
    const csv = "row_id,prompt,gold,task,locale,metric\n1,Question,Answer,basic,en,exact_match\n";
    const file = new File([csv], "custom.csv", { type: "text/csv" });
    Object.defineProperty(file, "slice", { value: () => ({ text: async () => csv }) });
    fireEvent.change(screen.getByRole("dialog", { name: "上传评测数据集" }).querySelector('input[type="file"]')!, { target: { files: [file] } });

    expect(await screen.findByText("把源列映射为标准字段")).toBeInTheDocument();
    const values = ["row_id", "prompt", "gold", "task", "locale", "metric"];
    ["题目 ID", "输入文本", "参考答案", "任务分类", "语言", "评分器"].forEach((label, index) => {
      fireEvent.change(screen.getByLabelText(label), { target: { value: values[index] } });
    });
    expect(screen.getByRole("button", { name: /校验并生成预览/ })).toBeEnabled();
  });

  it("renders only safe per-item facts in a completed evaluation", async () => {
    const rawAnswer = "RAW_ANSWER_MUST_NOT_RENDER";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/items")) return response([{
        model_id: "model-a", item_id: "arithmetic-001", category: "arithmetic", sequence: 1,
        state: "COMPLETED", score: 1, status: "PASS", reason_code: "NORMALIZED_MATCH",
        latency: { e2e_ms: 31 }, usage: { total_tokens: 7 }, output_sha256: "a".repeat(64),
        safe_facts: { normalized_length: 2 }, created_at: "2026-07-22T00:00:01Z",
      }]);
      return response({
        id: "70000000-0000-4000-8000-000000000001", workspace_id: "20000000-0000-4000-8000-000000000001",
        dataset_id: "50000000-0000-4000-8000-000000000001", dataset_revision_id: "60000000-0000-4000-8000-000000000001",
        channel_id: "30000000-0000-4000-8000-000000000001", credential_profile_id: null,
        model_source_id: "openai", state: "COMPLETED", scorer_id: "dataset_reference", scorer_version: "1.0.0",
        sample_strategy: "random", sample_seed: 42, sample_count: 1, model_count: 1, concurrency: 2,
        max_output_tokens: 64, timeout_seconds: 30, max_cost_usd: 1,
        request_snapshot: { prompt_template: "lexsond-messages/v1" }, aggregate_result: {}, failure_code: null,
        cancel_requested_at: null, created_at: "2026-07-22T00:00:00Z", finished_at: "2026-07-22T00:00:01Z", archived_at: null,
        models: [{ model_id: "model-a", provider_model_id: "model-a", state: "COMPLETED", completed_items: 1, passed_items: 1, failed_items: 0, unknown_items: 0, metrics: { overall_score: 1, category_scores: { arithmetic: 1 }, success_rate: 1, data_completeness: 1 } }],
      });
    }));
    renderPage(<Routes><Route path="/suites/evaluation-runs/:evaluationRunId" element={<EvaluationRuns />} /></Routes>, "/suites/evaluation-runs/70000000-0000-4000-8000-000000000001");

    expect(await screen.findByRole("heading", { name: "模型评测证据" })).toBeInTheDocument();
    expect(await screen.findByText("arithmetic-001")).toBeInTheDocument();
    expect(screen.getAllByText("arithmetic").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain(rawAnswer);
    expect(document.body.textContent).toContain("aaaaaaaaaaaa…");
  });
});

function renderPage(element: React.ReactNode, path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>{element}</MemoryRouter></QueryClientProvider>);
}

function dataset(id: string, name: string, distributionPolicy: string, imported: boolean) {
  return {
    id, workspace_id: null, scope: "SYSTEM", slug: id, name, description: `${name} description`,
    license_spdx: name === "C-Eval" ? "CC-BY-NC-SA-4.0" : "MIT", license_url: "https://example.com/license",
    source_url: "https://example.com/source", distribution_policy: distributionPolicy,
    default_scorer_id: "normalized_exact_match", version: 1,
    created_at: "2026-07-22T00:00:00Z", updated_at: "2026-07-22T00:00:00Z", archived_at: null,
    latest_revision: imported ? { id: `${id}-revision`, revision: 1, content_sha256: "b".repeat(64), item_count: 80, category_count: 8, language_codes: ["en", "zh-CN"], manifest: {}, created_at: "2026-07-22T00:00:00Z" } : null,
  };
}

function response(data: unknown): Response {
  return new Response(JSON.stringify({ data }), { status: 200, headers: { "Content-Type": "application/json" } });
}
