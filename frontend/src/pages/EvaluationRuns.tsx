import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  BarChart3,
  Check,
  CircleDollarSign,
  Clock3,
  Gauge,
  Hash,
  KeyRound,
  Layers3,
  Play,
  Radio,
  RefreshCw,
  ShieldCheck,
  Square,
  TimerReset,
  Archive,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, subscribeToEvaluationRun } from "../api";
import type { EvaluationRun, EvaluationRunInput, EvaluationRunModel } from "../types";
import { EmptyState, ErrorNotice, formatTime, PageHead, StatusPill } from "../ui";
import { SuiteModuleTabs } from "./SuiteModuleTabs";

export function EvaluationRuns() {
  const { evaluationRunId } = useParams();
  const [searchParams] = useSearchParams();
  if (evaluationRunId === "new") return <EvaluationRunCreate initialDatasetId={searchParams.get("dataset")} />;
  if (evaluationRunId) return <EvaluationRunDetail runId={evaluationRunId} />;
  return <EvaluationRunHistory />;
}

function EvaluationRunHistory() {
  const queryClient = useQueryClient();
  const [showArchived, setShowArchived] = useState(false);
  const runs = useQuery({ queryKey: ["evaluation-runs", showArchived], queryFn: () => api.evaluationRuns(showArchived), refetchInterval: 4000 });
  const lifecycle = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "archive" | "restore" | "purge" }) => {
      if (action === "archive") await api.archiveEvaluationRun(id);
      else if (action === "restore") await api.restoreEvaluationRun(id);
      else await api.purgeEvaluationRun(id);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["evaluation-runs"] }),
  });
  return (
    <div className="page-stack evaluation-runs-page">
      <SuiteModuleTabs />
      <PageHead eyebrow="BENCHMARK HISTORY / 评测记录" title="可复现的多模型评测历史" description="准确率不会混入 API 可用性或协议分。只有数据修订、样本、参数和评分器完全一致的模型才可横向比较。" action={<Link className="primary-action" to="/suites/evaluation-runs/new"><Play size={15} />发起数据集评测</Link>} />
      <div className="evaluation-toolbar panel-lite"><label className="toggle-label"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} /><span />显示已归档</label></div>
      {(runs.error || lifecycle.error) && <ErrorNotice error={runs.error || lifecycle.error} />}
      <section className="panel evaluation-run-list">
        <header className="table-head"><span>运行 / 数据修订</span><span>模型 × 样本</span><span>评分策略</span><span>状态</span><span>创建时间</span><span /></header>
        {runs.data?.map((run) => <article key={run.id}><div><code>{run.id.slice(0, 12)}…</code><small>{run.dataset_revision_id.slice(0, 12)}…</small></div><b>{run.model_count} × {run.sample_count}</b><span>{run.scorer_id} · seed {run.sample_seed}</span><StatusPill status={run.archived_at ? "ARCHIVED" : run.state} /><time>{formatTime(run.created_at)}</time><div className="row-actions"><Link className="icon-button" to={`/suites/evaluation-runs/${run.id}`} aria-label="查看评测"><BarChart3 size={15} /></Link>{run.state !== "RUNNING" && (!run.archived_at ? <button className="icon-button" aria-label="归档评测" onClick={() => lifecycle.mutate({ id: run.id, action: "archive" })}><Archive size={14} /></button> : <><button className="icon-button" aria-label="恢复评测" onClick={() => lifecycle.mutate({ id: run.id, action: "restore" })}><RotateCcw size={14} /></button><button className="icon-button danger" aria-label="永久清除评测" onClick={() => { if (window.confirm("永久清除该评测的安全结果与事件？此操作不可恢复。")) lifecycle.mutate({ id: run.id, action: "purge" }); }}><Trash2 size={14} /></button></>)}</div></article>)}
        {!runs.data?.length && !runs.isLoading && <EmptyState icon={BarChart3} title="还没有评测记录" body="从 QuickEval 或工作区数据集开始一次受预算的多模型比较。" action={<Link className="primary-action" to="/suites/datasets">选择数据集</Link>} />}
      </section>
    </div>
  );
}

function EvaluationRunCreate({ initialDatasetId }: { initialDatasetId: string | null }) {
  const queryClient = useQueryClient();
  const datasets = useQuery({ queryKey: ["evaluation-datasets"], queryFn: () => api.evaluationDatasets(false) });
  const channels = useQuery({ queryKey: ["targets", false], queryFn: () => api.targets(false) });
  const credentials = useQuery({ queryKey: ["credential-profiles", false], queryFn: () => api.credentialProfiles(false) });
  const scorers = useQuery({ queryKey: ["evaluation-scorers"], queryFn: api.evaluationScorers });
  const [datasetId, setDatasetId] = useState(initialDatasetId ?? "");
  const [channelId, setChannelId] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [temporaryKey, setTemporaryKey] = useState("");
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [sampleStrategy, setSampleStrategy] = useState<"first" | "random" | "stratified">("random");
  const [sampleSeed, setSampleSeed] = useState(42);
  const [sampleCount, setSampleCount] = useState(20);
  const [maxOutputTokens, setMaxOutputTokens] = useState(64);
  const [timeoutSeconds, setTimeoutSeconds] = useState(30);
  const [scorerId, setScorerId] = useState("dataset_reference");
  const [maxCostUsd, setMaxCostUsd] = useState(1);
  const [confirmUnknownChat, setConfirmUnknownChat] = useState(false);
  const selectedDataset = datasets.data?.find((value) => value.id === datasetId);
  const selectedChannel = channels.data?.find((value) => value.id === channelId);
  const catalog = useMutation({
    mutationFn: () => api.catalog(channelId, temporaryKey || null, credentialId || null),
    onSuccess: () => setSelectedModels([]),
    onSettled: () => setTemporaryKey(""),
  });
  const baseInput: EvaluationRunInput | null = selectedDataset?.latest_revision && catalog.data && selectedModels.length ? {
    dataset_revision_id: selectedDataset.latest_revision.id,
    channel_id: channelId,
    catalog_snapshot_id: catalog.data.catalog_snapshot_id,
    credential_profile_id: credentialId || null,
    model_ids: selectedModels,
    sample_strategy: sampleStrategy,
    sample_seed: sampleSeed,
    sample_count: sampleCount,
    scorer_id: scorerId,
    max_output_tokens: maxOutputTokens,
    timeout_seconds: timeoutSeconds,
    concurrency: 2,
    max_cost_usd: maxCostUsd,
    confirm_unknown_chat_capability: confirmUnknownChat,
  } : null;
  const preview = useMutation({ mutationFn: () => {
    if (!baseInput) throw new Error("请先完成数据集、渠道和模型选择");
    return api.previewEvaluationRun(baseInput);
  }});
  const executionCredentialMissing = selectedChannel?.target_kind === "cloud" && !credentialId && !temporaryKey;
  const create = useMutation({
    mutationFn: () => {
      if (!baseInput || !preview.data) throw new Error("请先完成预算预览");
      return api.createEvaluationRun({ ...baseInput, api_key: temporaryKey || null, confirm_unknown_cost: true }, crypto.randomUUID());
    },
    onSuccess: (run) => {
      setTemporaryKey("");
      queryClient.invalidateQueries({ queryKey: ["evaluation-runs"] });
      window.location.assign(`/suites/evaluation-runs/${run.id}`);
    },
    onSettled: () => setTemporaryKey(""),
  });

  useEffect(() => { preview.reset(); }, [datasetId, channelId, credentialId, selectedModels.join("|"), sampleStrategy, sampleSeed, sampleCount, scorerId, maxOutputTokens, timeoutSeconds, maxCostUsd, confirmUnknownChat]);
  const canDiscover = !!channelId && (selectedChannel?.target_kind === "local" || !!credentialId || !!temporaryKey);
  const unknownSelectedModels = catalog.data?.models.filter((model) => selectedModels.includes(model.id) && !model.probe_types.length) ?? [];

  return (
    <div className="page-stack evaluation-create-page">
      <SuiteModuleTabs />
      <Link to="/suites/evaluation-runs" className="back-link"><ArrowLeft size={14} />返回评测记录</Link>
      <PageHead eyebrow="REPRODUCIBLE BENCHMARK / 新建评测" title="冻结数据、模型与预算后再调用" description="一个运行只允许同一模型来源、同一渠道和同一凭据。默认 20 条、并发 2、temperature 0，模型调用不自动重试。" />
      {(catalog.error || preview.error || create.error) && <ErrorNotice error={catalog.error || preview.error || create.error} />}
      <div className="evaluation-create-grid">
        <section className="panel evaluation-config-panel">
          <div className="config-section"><header><span>01</span><div><b>数据修订</b><small>只选择已有不可变内容的可运行数据集</small></div></header><label>数据集<select value={datasetId} onChange={(event) => { setDatasetId(event.target.value); setSelectedModels([]); }}><option value="">请选择</option>{datasets.data?.filter((value) => value.latest_revision && value.distribution_policy === "BUNDLED").map((value) => <option key={value.id} value={value.id}>{value.name} · R{value.latest_revision?.revision}</option>)}</select></label>{selectedDataset?.latest_revision && <div className="selection-proof"><Hash size={14} /><code>{selectedDataset.latest_revision.content_sha256.slice(0, 24)}…</code><span>{selectedDataset.latest_revision.item_count} items</span></div>}</div>
          <div className="config-section"><header><span>02</span><div><b>渠道与凭据</b><small>模型必须来自本次凭据实际可见目录</small></div></header><div className="form-row"><label>渠道<select value={channelId} onChange={(event) => { setChannelId(event.target.value); catalog.reset(); setSelectedModels([]); }}><option value="">请选择</option>{channels.data?.map((value) => <option key={value.id} value={value.id}>{value.name} · {value.provider_id ?? "custom"}</option>)}</select></label><label>已保存 Key<select value={credentialId} onChange={(event) => { setCredentialId(event.target.value); setTemporaryKey(""); catalog.reset(); }}><option value="">仅本次 / 本地免密</option>{credentials.data?.filter((value) => !value.archived_at && (!selectedChannel?.provider_id || value.provider_id === selectedChannel.provider_id)).map((value) => <option key={value.id} value={value.id}>{value.label} · ••••{value.masked_suffix}</option>)}</select></label></div>{!credentialId && selectedChannel?.target_kind === "cloud" && <label>临时 API Key<input type="password" autoComplete="off" value={temporaryKey} onChange={(event) => setTemporaryKey(event.target.value)} placeholder={catalog.data ? "目录请求后已清空；执行前请再次输入" : "仅用于发现；请求后立即清空"} /></label>}<button className="secondary-action" disabled={!canDiscover || catalog.isPending} onClick={() => catalog.mutate()}><RefreshCw size={14} />{catalog.isPending ? "读取目录中…" : "读取该 Key 可见模型"}</button></div>
          {catalog.data && <div className="config-section"><header><span>03</span><div><b>选择 1–10 个 Chat 模型</b><small>{catalog.data.model_count} 个可见 · 已选 {selectedModels.length}</small></div></header><div className="model-choice-grid">{catalog.data.models.map((model) => { const selected = selectedModels.includes(model.id); const declaredNonChat = model.probe_types.length > 0 && !model.probe_types.includes("chat"); return <button type="button" key={model.id} className={selected ? "selected" : ""} disabled={declaredNonChat} title={declaredNonChat ? "目录声明该模型不支持 Chat 文本评测" : undefined} onClick={() => setSelectedModels((current) => selected ? current.filter((value) => value !== model.id) : current.length < 10 ? [...current, model.id] : current)}><span>{selected ? <Check size={13} /> : null}</span><b>{model.id}</b><small>{declaredNonChat ? "非 Chat" : model.probe_types.length ? "Chat" : "能力 UNKNOWN"}</small></button>; })}</div>{unknownSelectedModels.length > 0 && <label className="rights-confirm"><input type="checkbox" checked={confirmUnknownChat} onChange={(event) => setConfirmUnknownChat(event.target.checked)} /><span><b>确认 {unknownSelectedModels.length} 个模型的 Chat 能力为 UNKNOWN</b><small>目录未声明能力；这不是按模型名推断，运行可能得到协议失败。</small></span></label>}</div>}
          <div className="config-section"><header><span>04</span><div><b>抽样与调用护栏</b><small>全部参数写入不可变请求快照</small></div></header><div className="form-row three"><label>抽样<select value={sampleStrategy} onChange={(event) => setSampleStrategy(event.target.value as typeof sampleStrategy)}><option value="first">first</option><option value="random">random</option><option value="stratified">stratified</option></select></label><label>Seed<input type="number" value={sampleSeed} onChange={(event) => setSampleSeed(Number(event.target.value))} /></label><label>样本数<input type="number" min={1} max={200} value={sampleCount} onChange={(event) => setSampleCount(Number(event.target.value))} /></label></div><div className="form-row three"><label>评分器<select value={scorerId} onChange={(event) => setScorerId(event.target.value)}><option value="dataset_reference">使用每题引用评分器</option>{scorers.data?.map((scorer) => <option key={scorer.scorer_id} value={scorer.scorer_id}>{scorer.label} · v{scorer.version}</option>)}</select></label><label>单次超时（秒）<input type="number" min={1} max={120} value={timeoutSeconds} onChange={(event) => setTimeoutSeconds(Number(event.target.value))} /></label><label>固定并发<input value="2" readOnly aria-label="固定并发" /></label></div><div className="form-row"><label>每次最大输出 Token<input type="number" min={1} max={1024} value={maxOutputTokens} onChange={(event) => setMaxOutputTokens(Number(event.target.value))} /></label><label>费用声明 USD<input type="number" min={0.01} max={10000} step={0.01} value={maxCostUsd} onChange={(event) => setMaxCostUsd(Number(event.target.value))} /></label></div></div>
        </section>
        <aside className="panel evaluation-budget-panel"><header><ShieldCheck size={19} /><div><span className="eyebrow">EXECUTION ENVELOPE</span><h2>运行前确认</h2></div></header><dl><div><dt>MODELS</dt><dd>{selectedModels.length || "—"}</dd></div><div><dt>SAMPLES / MODEL</dt><dd>{sampleCount}</dd></div><div><dt>MAX CALLS</dt><dd>{selectedModels.length ? selectedModels.length * sampleCount : "—"}</dd></div><div><dt>CONCURRENCY</dt><dd>2</dd></div><div><dt>MAX OUTPUT</dt><dd>{selectedModels.length ? selectedModels.length * sampleCount * maxOutputTokens : "—"} tok</dd></div></dl>{!preview.data ? <button className="primary-action full" disabled={!baseInput || preview.isPending || (unknownSelectedModels.length > 0 && !confirmUnknownChat)} onClick={() => preview.mutate()}><Gauge size={15} />{preview.isPending ? "验证约束中…" : "生成预算预览"}</button> : <><div className="unknown-cost-warning"><AlertTriangle size={17} /><div><b>价格未知，美元上限不可执行</b><span>最多 {preview.data.maximum_calls} 次调用；仅调用数、Token、并发、超时与取消护栏可执行。</span></div></div><div className="comparison-seal"><Radio size={15} /><p><b>可横向比较</b><span>同一修订、item IDs、seed、模板和评分器版本</span></p></div><label className="rights-confirm"><input type="checkbox" checked readOnly /><span><b>确认费用未知；${maxCostUsd.toFixed(2)} 仅为预算声明</b><small>provider 未提供可信价格，Lexsond 不会声称已执行美元硬上限。</small></span></label><button className="primary-action full" disabled={create.isPending || executionCredentialMissing} onClick={() => create.mutate()}><Play size={15} />{create.isPending ? "创建任务中…" : executionCredentialMissing ? "请重新输入临时 Key" : "确认并开始评测"}</button></>}</aside>
      </div>
    </div>
  );
}

function EvaluationRunDetail({ runId }: { runId: string }) {
  const queryClient = useQueryClient();
  const run = useQuery({ queryKey: ["evaluation-run", runId], queryFn: () => api.evaluationRun(runId, true), refetchInterval: (query) => query.state.data?.state === "RUNNING" ? 1500 : false });
  const items = useQuery({ queryKey: ["evaluation-run-items", runId], queryFn: () => api.evaluationRunItems(runId), refetchInterval: run.data?.state === "RUNNING" ? 2000 : false });
  const cancel = useMutation({ mutationFn: () => api.cancelEvaluationRun(runId), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["evaluation-run", runId] }) });
  useEffect(() => {
    if (run.data?.state !== "RUNNING") return;
    return subscribeToEvaluationRun(runId, () => {
      queryClient.invalidateQueries({ queryKey: ["evaluation-run", runId] });
      queryClient.invalidateQueries({ queryKey: ["evaluation-run-items", runId] });
    });
  }, [queryClient, run.data?.state, runId]);
  if (run.error) return <div className="page-stack"><SuiteModuleTabs /><ErrorNotice error={run.error} /></div>;
  const value = run.data;
  if (!value) return <div className="page-stack"><SuiteModuleTabs /><div className="panel loading-panel">正在读取评测证据…</div></div>;
  const completed = value.models.reduce((sum, model) => sum + model.completed_items, 0);
  const total = value.model_count * value.sample_count;
  return (
    <div className="page-stack evaluation-run-detail">
      <SuiteModuleTabs />
      <Link to="/suites/evaluation-runs" className="back-link"><ArrowLeft size={14} />返回评测记录</Link>
      <PageHead eyebrow={`EVALUATION / ${value.id.slice(0, 12)}`} title="模型评测证据" description="逐题回答正文不进入普通业务表；这里只展示 hash、确定性评分、Token、费用完整性与时延。" action={value.state === "RUNNING" ? <button className="ghost-action danger" onClick={() => cancel.mutate()}><Square size={13} fill="currentColor" />取消未开始调用</button> : <StatusPill status={value.state} />} />
      {(items.error || cancel.error) && <ErrorNotice error={items.error || cancel.error} />}
      <section className="evaluation-progress panel"><div className="progress-orbit"><span>{Math.round((completed / Math.max(total, 1)) * 100)}%</span><i style={{ "--progress": `${(completed / Math.max(total, 1)) * 360}deg` } as React.CSSProperties} /></div><div><span className="eyebrow">LIVE PROGRESS</span><h2>{completed} / {total} 条已形成评分事实</h2><p>{value.state === "RUNNING" ? "SSE 正在恢复严格递增事件；刷新页面不会丢失进度。" : `运行已结束 · ${value.state}`}</p></div><div className="progress-counts"><span><b>{value.models.reduce((sum, model) => sum + model.passed_items, 0)}</b>PASS</span><span><b>{value.models.reduce((sum, model) => sum + model.failed_items, 0)}</b>FAIL</span><span><b>{value.models.reduce((sum, model) => sum + model.unknown_items, 0)}</b>UNKNOWN</span></div></section>
      <ReproducibilityStrip run={value} />
      <section className="panel model-comparison"><header><div><span className="eyebrow">MODEL × METRIC</span><h2>模型比较矩阵</h2></div><span className="comparison-lock"><ShieldCheck size={14} />比较条件已冻结</span></header><div className="comparison-table"><div className="table-head"><span>模型</span><span>总分 / 95% CI</span><span>成功率</span><span>Token</span><span>费用</span><span>E2E P50 / P95</span><span>完整性</span></div>{value.models.map((model) => <ModelMetricRow key={model.model_id} model={model} />)}</div></section>
      <CategoryComparison models={value.models} />
      <section className="panel evaluation-items"><header><div><span className="eyebrow">SAFE ITEM FACTS</span><h2>逐题结果</h2></div><span>不显示完整模型回答</span></header><div className="table-head"><span>模型 / Item</span><span>分类</span><span>分数</span><span>状态</span><span>原因</span><span>E2E</span><span>输出指纹</span></div>{items.data?.map((item) => <article key={`${item.model_id}:${item.sequence}`}><div><b>{item.model_id}</b><code>{item.item_id}</code></div><span>{item.category}</span><b>{item.score === null ? "—" : item.score.toFixed(3)}</b><StatusPill status={item.status} /><code>{item.reason_code}</code><span>{item.latency.e2e_ms ?? "—"} ms</span><code>{item.output_sha256 ? `${item.output_sha256.slice(0, 12)}…` : "—"}</code></article>)}</section>
    </div>
  );
}

function ReproducibilityStrip({ run }: { run: EvaluationRun }) {
  const snapshot = run.request_snapshot;
  return <section className="repro-strip"><span><Layers3 size={14} /><b>DATASET</b><code>{run.dataset_revision_id.slice(0, 12)}…</code></span><span><Hash size={14} /><b>SEED</b><code>{run.sample_seed}</code></span><span><TimerReset size={14} /><b>SAMPLING</b><code>{run.sample_strategy} / {run.sample_count}</code></span><span><ShieldCheck size={14} /><b>SCORER</b><code>{run.scorer_id} · v{run.scorer_version}</code></span><span><KeyRound size={14} /><b>TEMPLATE</b><code>{String(snapshot.prompt_template ?? "—")}</code></span></section>;
}

function ModelMetricRow({ model }: { model: EvaluationRunModel }) {
  const ci = model.metrics.confidence_interval_95;
  const usage = model.metrics.usage ?? {};
  const e2e = model.metrics.latency?.e2e_ms;
  return <article><div><b>{model.model_id}</b><StatusPill status={model.state} /></div><div><b>{model.metrics.overall_score?.toFixed(3) ?? "—"}</b><small>{ci ? `${ci.low.toFixed(3)}–${ci.high.toFixed(3)}` : "证据不足"}</small></div><span>{model.metrics.success_rate == null ? "—" : `${(model.metrics.success_rate * 100).toFixed(1)}%`}</span><span>{usage.total_tokens ?? "UNKNOWN"}</span><span>{model.metrics.cost_completeness === "COMPLETE" && model.metrics.known_cost_usd != null ? `$${model.metrics.known_cost_usd.toFixed(4)}` : "UNKNOWN"}</span><span>{e2e ? `${e2e.p50 ?? "—"} / ${e2e.p95 ?? "—"} ms` : "—"}</span><span>{model.metrics.data_completeness === undefined ? "—" : `${(model.metrics.data_completeness * 100).toFixed(0)}%`}</span></article>;
}

function CategoryComparison({ models }: { models: EvaluationRunModel[] }) {
  const categories = useMemo(() => Array.from(new Set(models.flatMap((model) => Object.keys(model.metrics.category_scores ?? {})))).sort(), [models]);
  if (!categories.length) return null;
  const radarReady = models.length === 1 && categories.length <= 8 && categories.every((category) => typeof models[0].metrics.category_scores?.[category] === "number");
  return <section className="panel category-comparison"><header><div><span className="eyebrow">CATEGORY PROFILE</span><h2>分类得分</h2></div><span>{radarReady ? "单模型确定证据雷达" : "多模型 / UNKNOWN 使用对照表"}</span></header>{radarReady ? <CategoryRadar categories={categories} model={models[0]} /> : <div className="category-score-table">{categories.map((category) => <div key={category}><b>{category}</b>{models.map((model) => <span key={model.model_id}>{model.model_id}: {model.metrics.category_scores?.[category]?.toFixed(3) ?? "UNKNOWN"}</span>)}</div>)}</div>}</section>;
}

function CategoryRadar({ categories, model }: { categories: string[]; model: EvaluationRunModel }) {
  const center = 120;
  const radius = 84;
  const point = (index: number, scale = 1) => { const angle = -Math.PI / 2 + (index / categories.length) * Math.PI * 2; return `${center + Math.cos(angle) * radius * scale},${center + Math.sin(angle) * radius * scale}`; };
  const values = categories.map((category) => model.metrics.category_scores?.[category] as number);
  return <div className="radar-wrap"><svg viewBox="0 0 240 240" role="img" aria-label={`${model.model_id} 分类得分雷达图`}><polygon className="radar-grid" points={categories.map((_, index) => point(index)).join(" ")} /><polygon className="radar-grid inner" points={categories.map((_, index) => point(index, 0.5)).join(" ")} />{categories.map((_, index) => <line key={index} x1={center} y1={center} x2={point(index).split(",")[0]} y2={point(index).split(",")[1]} />)}<polygon className="radar-value" points={values.map((value, index) => point(index, value)).join(" ")} /></svg><div className="radar-legend"><b>{model.model_id}</b>{categories.map((category, index) => <span key={category}><i />{category}<strong>{values[index].toFixed(3)}</strong></span>)}</div></div>;
}
