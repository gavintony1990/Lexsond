import { expect, test, type Page, type Route } from "playwright/test";

const now = "2026-07-22T00:00:00+00:00";
const workspaceId = "10000000-0000-4000-8000-000000000001";
const userId = "10000000-0000-4000-8000-000000000002";
const quickDatasetId = "10000000-0000-4000-8000-000000000003";
const quickRevisionId = "10000000-0000-4000-8000-000000000004";
const channelId = "10000000-0000-4000-8000-000000000005";
const credentialId = "10000000-0000-4000-8000-000000000006";
const catalogId = "10000000-0000-4000-8000-000000000007";
const evaluationRunId = "10000000-0000-4000-8000-000000000008";
const digest = "9".repeat(64);

const revision = {
  id: quickRevisionId,
  revision: 1,
  content_sha256: digest,
  item_count: 80,
  category_count: 8,
  language_codes: ["en", "zh-CN"],
  manifest: { version: "1.0.0", provenance: "lexsond-original" },
  created_at: now,
};

const quickEval = {
  id: quickDatasetId,
  workspace_id: null,
  scope: "SYSTEM",
  slug: "lexsond-quickeval",
  name: "Lexsond QuickEval v1",
  description: "80 道项目原创、确定性、文本评测题。",
  license_spdx: "Apache-2.0",
  license_url: "https://www.apache.org/licenses/LICENSE-2.0",
  source_url: null,
  distribution_policy: "BUNDLED",
  default_scorer_id: "normalized_exact_match",
  version: 1,
  created_at: now,
  updated_at: now,
  archived_at: null,
  latest_revision: revision,
};

const externalDatasets = [
  {
    ...quickEval,
    id: "10000000-0000-4000-8000-000000000011",
    slug: "humaneval",
    name: "HumanEval",
    description: "代码能力目录",
    license_spdx: "MIT",
    distribution_policy: "RUNNER_REQUIRED",
    latest_revision: null,
  },
  {
    ...quickEval,
    id: "10000000-0000-4000-8000-000000000012",
    slug: "c-eval",
    name: "C-Eval",
    description: "非商业中文评测目录",
    license_spdx: "CC-BY-NC-SA-4.0",
    distribution_policy: "RESEARCH_ONLY",
    latest_revision: null,
  },
];

const completedRun = {
  id: evaluationRunId,
  workspace_id: workspaceId,
  dataset_id: quickDatasetId,
  dataset_revision_id: quickRevisionId,
  channel_id: channelId,
  credential_profile_id: credentialId,
  model_source_id: "openai",
  state: "COMPLETED",
  scorer_id: "dataset_reference",
  scorer_version: "1.0.0",
  sample_strategy: "random",
  sample_seed: 42,
  sample_count: 20,
  model_count: 2,
  concurrency: 2,
  max_output_tokens: 64,
  timeout_seconds: 30,
  max_cost_usd: 1,
  request_snapshot: { prompt_template: "lexsond-messages/v1", sample_item_ids: ["arithmetic-001"] },
  aggregate_result: { comparable: true, completed_calls: 40 },
  failure_code: null,
  cancel_requested_at: null,
  created_at: now,
  finished_at: now,
  archived_at: null,
  models: ["model-alpha", "model-beta"].map((model, index) => ({
    model_id: model,
    provider_model_id: model,
    state: "COMPLETED",
    completed_items: 20,
    passed_items: 18 - index,
    failed_items: 2 + index,
    unknown_items: 0,
    metrics: {
      overall_score: index ? 0.85 : 0.9,
      confidence_interval_95: { low: 0.7, high: 1, method: "bootstrap-sha256/v1" },
      category_scores: { arithmetic: index ? 0.8 : 1, logic: 0.8 },
      success_rate: 1,
      usage: { total_tokens: 120 },
      known_cost_usd: 0,
      cost_completeness: "UNKNOWN",
      latency: { e2e_ms: { p50: 120, p95: 180 } },
      data_completeness: 1,
    },
  })),
};

function envelope(route: Route, data: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

async function mockEvaluationApi(page: Page) {
  let workspaceDatasets: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/session") {
      return envelope(route, {
        user: {
          user_id: userId,
          email: "owner@example.test",
          display_name: "Eval Owner",
          avatar_url: null,
          status: "ACTIVE",
          system_role: "USER",
          email_verified_at: now,
          workspace_id: workspaceId,
          workspace_name: "Evaluation Workspace",
          workspace_role: "OWNER",
        },
        csrf_token: "csrf-fixture-only",
        auth_mode: "local-single-user",
      });
    }
    if (path === "/api/v1/bootstrap") {
      return envelope(route, {
        product: { name: "Lexsond", english_name: "Lexsond", version: "0.8.0" },
        execution_backends: [
          { id: "local", available: true, status: "READY" },
          { id: "temporal", available: false, status: "OFFLINE" },
        ],
        defaults: {}, providers: [], probe_components: [],
        stats: { runs: 0, running: 0, pass_rate: null, targets: 1, suites: 1 },
      });
    }
    if (path === "/api/v1/evaluation-datasets" && request.method() === "GET") {
      return envelope(route, [quickEval, ...externalDatasets, ...workspaceDatasets]);
    }
    if (path === "/api/v1/evaluation-datasets/validate-upload") {
      return envelope(route, {
        schema_version: "lexsond.evaluation-dataset/v1",
        content_sha256: "8".repeat(64),
        item_count: 1,
        category_count: 1,
        categories: { extraction: 1 },
        language_codes: ["zh-CN"],
        preview: [{
          id: "private-001", category: "extraction", language: "zh-CN",
          input: { messages: [{ role: "user", content: "只输出代号 R7。" }] },
          reference: { scorer: "exact_match", answer: "R7" }, metadata: {},
        }],
        preview_truncated: false,
      });
    }
    if (path === "/api/v1/evaluation-datasets" && request.method() === "POST") {
      const created = {
        ...quickEval,
        id: "10000000-0000-4000-8000-000000000013",
        workspace_id: workspaceId,
        scope: "WORKSPACE",
        slug: "private-eval",
        name: "Private Eval",
        description: "workspace fixture",
        license_spdx: "LicenseRef-Proprietary",
        latest_revision: { ...revision, id: "10000000-0000-4000-8000-000000000014", item_count: 1, category_count: 1 },
      };
      workspaceDatasets = [created];
      return envelope(route, created, 201);
    }
    if (path === "/api/v1/evaluation-scorers") {
      return envelope(route, [{
        scorer_id: "normalized_exact_match", version: "1.0.0",
        label: "Normalized exact match", description: "deterministic", execution: "DETERMINISTIC_LOCAL",
      }]);
    }
    if (path === "/api/v1/targets") {
      return envelope(route, [{
        id: channelId, name: "Official channel", target_kind: "cloud",
        provider_id: "openai", base_url: "https://api.example.test/v1",
        default_model: "model-alpha", credential_ref: null, version: 1,
        created_at: now, updated_at: now, archived_at: null,
      }]);
    }
    if (path === "/api/v1/credential-profiles") {
      return envelope(route, [{
        id: credentialId, workspace_id: workspaceId, label: "Saved credential",
        provider_id: "openai", storage_backend: "SYSTEM_KEYRING", masked_suffix: "7K9Q",
        status: "ACTIVE", version: 1, last_verified_at: now, last_used_at: null,
        created_at: now, updated_at: now, archived_at: null,
      }]);
    }
    if (path === `/api/v1/targets/${channelId}/catalog`) {
      return envelope(route, {
        status: "CONNECTED", target_id: channelId, auth_mode: "bearer", model_count: 2,
        models: [
          { id: "model-alpha", probe_types: ["chat"] },
          { id: "model-beta", probe_types: ["chat"] },
        ],
        catalog_snapshot_id: catalogId,
        catalog_expires_at: "2026-07-22T01:00:00+00:00",
      });
    }
    if (path === "/api/v1/evaluation-runs/preview") {
      return envelope(route, {
        dataset_revision_id: quickRevisionId, model_count: 2, sample_count: 20,
        maximum_calls: 40, maximum_output_tokens: 2560, concurrency: 2,
        estimated_cost_usd: null, cost_status: "UNKNOWN",
        requires_unknown_cost_confirmation: true, model_source_id: "openai", comparable: true,
      });
    }
    if (path === "/api/v1/evaluation-runs" && request.method() === "POST") {
      expect(request.headers()["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
      const body = request.postDataJSON();
      expect(body.model_ids).toEqual(["model-alpha", "model-beta"]);
      expect(body.api_key).toBeNull();
      expect(body.confirm_unknown_cost).toBe(true);
      return envelope(route, completedRun, 202);
    }
    if (path === `/api/v1/evaluation-runs/${evaluationRunId}`) {
      return envelope(route, completedRun);
    }
    if (path === `/api/v1/evaluation-runs/${evaluationRunId}/items`) {
      return envelope(route, [{
        model_id: "model-alpha", item_id: "arithmetic-001", category: "arithmetic",
        sequence: 1, state: "COMPLETED", score: 1, status: "PASS",
        reason_code: "NORMALIZED_EXACT_MATCH", latency: { e2e_ms: 120 },
        usage: { total_tokens: 6 }, output_sha256: "7".repeat(64), safe_facts: {}, created_at: now,
      }]);
    }
    return envelope(route, []);
  });
}

test.beforeEach(async ({ page }) => {
  await mockEvaluationApi(page);
});

test("catalog separates runnable datasets from license and runner policies", async ({ page }) => {
  await page.goto("/suites/datasets");
  await expect(page.getByRole("link", { name: "探测套件" })).toBeVisible();
  await expect(page.getByRole("link", { name: "评测数据集" })).toBeVisible();
  await expect(page.getByRole("link", { name: "评分器" })).toBeVisible();
  await expect(page.getByRole("link", { name: "评测记录" })).toBeVisible();

  await expect(page.getByText("Lexsond QuickEval v1")).toBeVisible();
  await expect(page.getByText("需要 Runner")).toBeVisible();
  await expect(page.getByText("仅限研究")).toBeVisible();
  await expect(page.locator("article", { hasText: "HumanEval" }).getByText("使用数据集评测")).toHaveCount(0);
  await expect(page.locator("article", { hasText: "C-Eval" }).getByText("使用数据集评测")).toHaveCount(0);
});

test("uploads a private revision and completes a bounded two-model comparison", async ({ page }) => {
  await page.goto("/suites/datasets");
  await page.getByRole("button", { name: "上传数据集" }).click();
  await page.getByRole("dialog", { name: "上传评测数据集" }).locator('input[type="file"]').setInputFiles({
    name: "private-eval.jsonl",
    mimeType: "application/json",
    buffer: Buffer.from('{"id":"private-001","category":"extraction","language":"zh-CN","input":{"messages":[{"role":"user","content":"只输出代号 R7。"}]},"reference":{"scorer":"exact_match","answer":"R7"},"metadata":{}}\n'),
  });
  await page.getByRole("button", { name: "校验并生成预览" }).click();
  await expect(page.getByText("private-001")).toBeVisible();
  await page.getByLabel("名称").fill("Private Eval");
  await page.getByLabel("Slug").fill("private-eval");
  await page.getByText("我确认拥有上传与评测这些数据的权利").click();
  await page.getByRole("button", { name: "确认并创建修订" }).click();
  await expect(page.getByText("Private Eval")).toBeVisible();

  await page.locator("article", { hasText: "Lexsond QuickEval v1" }).getByRole("link", { name: "使用数据集评测" }).click();
  await page.getByLabel("渠道").selectOption(channelId);
  await page.getByLabel("已保存 Key").selectOption(credentialId);
  await page.getByRole("button", { name: "读取该 Key 可见模型" }).click();
  await page.getByRole("button", { name: "model-alpha" }).click();
  await page.getByRole("button", { name: "model-beta" }).click();
  await page.getByRole("button", { name: "生成预算预览" }).click();
  await expect(page.getByText("最多 40 次调用")).toBeVisible();
  await page.getByRole("button", { name: "确认并开始评测" }).click();

  await expect(page).toHaveURL(new RegExp(`/suites/evaluation-runs/${evaluationRunId}$`));
  await expect(page.getByRole("heading", { name: "模型比较矩阵" })).toBeVisible();
  await expect(page.getByText("model-alpha", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("arithmetic-001")).toBeVisible();
  await expect(page.getByText("不显示完整模型回答")).toBeVisible();
  const storage = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
  }));
  expect(storage).toEqual({ local: [], session: [] });
});
