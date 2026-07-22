import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckSquare, ClipboardPaste, KeyRound, Search, ShieldCheck, SquareArrowOutUpRight, XCircle } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { parseClipboardCredential } from "../credentialClipboard";
import type { ProbeBatchCreateInput } from "../types";
import { EmptyState, ErrorNotice, PageHead, StatusPill } from "../ui";

interface VisibleModel { id: string; probe_types: string[] }

export function ApiKeyCatalogProbe() {
  const { batchId } = useParams();
  return batchId ? <BatchDetail batchId={batchId} /> : <CatalogProbeComposer />;
}

function CatalogProbeComposer() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const batchInputRef = useRef<HTMLInputElement>(null);
  const targets = useQuery({ queryKey: ["targets", false], queryFn: () => api.targets(false) });
  const profiles = useQuery({ queryKey: ["credential-profiles", false], queryFn: () => api.credentialProfiles(false) });
  const suites = useQuery({ queryKey: ["suites", false], queryFn: () => api.suites(false) });
  const [targetId, setTargetId] = useState("");
  const [credentialMode, setCredentialMode] = useState<"temporary" | "saved">("temporary");
  const [credentialProfileId, setCredentialProfileId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [batchApiKey, setBatchApiKey] = useState("");
  const [models, setModels] = useState<VisibleModel[]>([]);
  const [catalogSnapshotId, setCatalogSnapshotId] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [mode, setMode] = useState<ProbeBatchCreateInput["mode"]>("smoke");
  const [suiteRevisionId, setSuiteRevisionId] = useState("");
  const [confirmUnknownCost, setConfirmUnknownCost] = useState(false);
  const [loading, setLoading] = useState(false);
  const [batchLoading, setBatchLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  async function pasteOnce(target: "catalog" | "batch") {
    setError(null);
    if (!window.isSecureContext || !navigator.clipboard?.readText) {
      setError(new Error("浏览器不允许读取剪贴板，请手动粘贴"));
      return;
    }
    try {
      const parsed = parseClipboardCredential(await navigator.clipboard.readText());
      if (target === "catalog") setApiKey(parsed); else setBatchApiKey(parsed);
    } catch (cause) { setError(displayError(cause)); }
    (target === "catalog" ? inputRef : batchInputRef).current?.focus();
  }

  async function discover(event: React.FormEvent) {
    event.preventDefault();
    setLoading(true); setError(null); setModels([]); setSelected([]); setCatalogSnapshotId("");
    try {
      const result = await api.catalog(
        targetId,
        credentialMode === "temporary" ? apiKey || null : null,
        credentialMode === "saved" ? credentialProfileId || null : null,
      );
      setModels(result.models);
      setCatalogSnapshotId(result.catalog_snapshot_id);
    } catch (cause) { setError(displayError(cause)); }
    finally {
      if (inputRef.current) inputRef.current.value = "";
      setApiKey(""); setLoading(false);
    }
  }

  async function createBatch() {
    setBatchLoading(true); setError(null);
    try {
      const batch = await api.createProbeBatch({
        target_id: targetId,
        catalog_snapshot_id: catalogSnapshotId,
        mode,
        model_ids: selected,
        suite_revision_id: mode === "quality_suite" ? suiteRevisionId : null,
        max_concurrency: 2,
        max_output_tokens: 8,
        timeout_seconds: 30,
        api_key: credentialMode === "temporary" && mode !== "catalog_only" ? batchApiKey || null : null,
        credential_profile_id: credentialMode === "saved" ? credentialProfileId : null,
        confirm_unknown_cost: mode === "catalog_only" ? false : confirmUnknownCost,
      }, crypto.randomUUID());
      setBatchApiKey("");
      if (batchInputRef.current) batchInputRef.current.value = "";
      navigate(`/probes/api-key/${batch.batch_id}`);
    } catch (cause) { setError(displayError(cause)); }
    finally {
      setBatchApiKey("");
      if (batchInputRef.current) batchInputRef.current.value = "";
      setBatchLoading(false);
    }
  }

  function toggle(modelId: string) {
    setSelected((current) => current.includes(modelId)
      ? current.filter((value) => value !== modelId)
      : current.length < 10 ? [...current, modelId] : current);
  }

  const visible = models.filter((model) => model.id.toLowerCase().includes(filter.toLowerCase()));
  const target = targets.data?.find((value) => value.id === targetId);
  const compatibleProfiles = profiles.data?.filter((profile) =>
    profile.status === "ACTIVE" && (!target?.provider_id || profile.provider_id === target.provider_id)
  ) ?? [];
  const suite = suites.data?.find((value) => value.latest_revision.id === suiteRevisionId);
  const callsPerModel = mode === "catalog_only" ? 0 : mode === "smoke" ? 1 : suite
    ? suite.latest_revision.document.spec.sampling.warmup + suite.latest_revision.document.spec.sampling.requests
    : 0;
  const temporaryExecutionMissing = credentialMode === "temporary" && mode !== "catalog_only" && !batchApiKey;
  const createDisabled = selected.length === 0 || !catalogSnapshotId || batchLoading
    || (mode === "quality_suite" && !suiteRevisionId)
    || (mode !== "catalog_only" && !confirmUnknownCost)
    || temporaryExecutionMissing;

  return <div className="page-stack catalog-batch-page">
    <PageHead eyebrow="CATALOG → BOUNDED BATCH" title="API Key 模型探测" description="对一个渠道读取一次真实目录，最多选择 10 个模型；快速可用性每模型恰好一次生成调用，质量对比复用同一不可变套件。" action={<Link className="secondary-action" to="/suites/evaluation-runs/new"><SquareArrowOutUpRight size={14} />使用数据集评测</Link>} />
    <section className="panel catalog-control"><form onSubmit={discover}>
      <label>渠道<select required value={targetId} onChange={(event) => { setTargetId(event.target.value); setCredentialProfileId(""); setModels([]); setCatalogSnapshotId(""); }}><option value="">选择已确认渠道</option>{targets.data?.map((targetValue) => <option key={targetValue.id} value={targetValue.id}>{targetValue.name} · {targetValue.base_url}</option>)}</select></label>
      <fieldset className="credential-source"><legend>凭据来源</legend><label><input type="radio" name="credential-source" checked={credentialMode === "temporary"} onChange={() => setCredentialMode("temporary")} />仅本次使用</label><label><input type="radio" name="credential-source" checked={credentialMode === "saved"} onChange={() => { setCredentialMode("saved"); setApiKey(""); setBatchApiKey(""); if (inputRef.current) inputRef.current.value = ""; }} />使用已保存凭据</label></fieldset>
      {credentialMode === "temporary" ? <label>临时 API Key<div className="secret-entry"><input ref={inputRef} type="password" autoComplete="off" maxLength={8192} value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="目录请求后立即清空" /><button type="button" aria-label="粘贴 API Key" onClick={() => void pasteOnce("catalog")}><ClipboardPaste size={17} /></button></div></label> : <label>已保存凭据<select required value={credentialProfileId} onChange={(event) => setCredentialProfileId(event.target.value)}><option value="">选择与渠道 Provider 匹配的凭据</option>{compatibleProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.label} · ••••{profile.masked_suffix}</option>)}</select></label>}
      <button className="primary-action" type="submit" disabled={!targetId || loading || (credentialMode === "saved" && !credentialProfileId)}><Search size={16} />{loading ? "正在读取一次目录…" : "读取该 Key 可见模型"}</button>
    </form><footer><ShieldCheck size={15} /><span>只访问所选渠道；共享前缀 Key 不会被试投到其他 Provider。</span><StatusPill status="CATALOG_ONCE" /></footer></section>
    {error && <ErrorNotice error={error} />}
    {models.length === 0 && !loading && <EmptyState icon={KeyRound} title="尚未读取模型目录" body="选择渠道后读取一次 /models；目录成功不代表每个模型都有生成权限。" />}
    {models.length > 0 && <>
      <section className="panel model-picker"><header><div><span className="eyebrow">VISIBLE MODELS</span><h2>当前 Key 可见模型</h2><p>已选择 {selected.length}/10 · 当前预计生成调用 {selected.length * callsPerModel}</p></div><label><Search size={15} /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="搜索模型" /></label></header><div className="model-check-grid">{visible.map((model) => { const checked = selected.includes(model.id); const blocked = !checked && selected.length >= 10; return <button type="button" key={model.id} className={checked ? "selected" : ""} disabled={blocked} onClick={() => toggle(model.id)}><CheckSquare size={16} /><span>{model.id}</span><small>{model.probe_types.join(" · ") || "UNKNOWN"}</small></button>; })}</div></section>
      <section className="panel batch-confirm"><header><div><span className="eyebrow">EXECUTION ENVELOPE</span><h2>确认探测边界</h2></div><StatusPill status={`${selected.length} MODELS`} /></header>
        <div className="batch-options"><label>探测模式<select value={mode} onChange={(event) => setMode(event.target.value as ProbeBatchCreateInput["mode"])}><option value="catalog_only">仅目录确认（0 次生成）</option><option value="smoke">快速模型可用性（每模型 1 次）</option><option value="quality_suite">质量对比（统一套件）</option></select></label>{mode === "quality_suite" && <label>不可变套件版本<select value={suiteRevisionId} onChange={(event) => setSuiteRevisionId(event.target.value)}><option value="">选择套件</option>{suites.data?.map((value) => <option value={value.latest_revision.id} key={value.latest_revision.id}>{value.name} · R{value.latest_revision.revision}</option>)}</select></label>}</div>
        {credentialMode === "temporary" && mode !== "catalog_only" && <label className="batch-temporary-key">临时 Key 已在目录请求后清除，请为计费执行再次输入<div className="secret-entry"><input ref={batchInputRef} type="password" autoComplete="off" maxLength={8192} value={batchApiKey} onChange={(event) => setBatchApiKey(event.target.value)} /><button type="button" aria-label="再次粘贴 API Key" onClick={() => void pasteOnce("batch")}><ClipboardPaste size={17} /></button></div></label>}
        <div className="batch-budget"><div><b>{selected.length}</b><span>模型</span></div><div><b>{selected.length * callsPerModel}</b><span>最大生成调用</span></div><div><b>2</b><span>最大并发</span></div><div><b>{mode === "smoke" ? 8 : suite?.latest_revision.document.spec.request.max_output_tokens ?? 0}</b><span>每请求 Token 上限</span></div><div><b>UNKNOWN</b><span>预计费用</span></div></div>
        {mode !== "catalog_only" && <label className="unknown-cost-confirm"><input type="checkbox" checked={confirmUnknownCost} onChange={(event) => setConfirmUnknownCost(event.target.checked)} /><span>我已知晓当前目录没有可信价格，实际费用未知；仍按上述硬上限执行。</span></label>}
        <footer><span>批次创建后模型、渠道、凭据引用、套件版本与边界不可修改。</span><button className="primary-action" type="button" disabled={createDisabled} onClick={() => void createBatch()}>{batchLoading ? "正在创建…" : "确认并发起批次"}</button></footer>
      </section>
    </>}
  </div>;
}

function BatchDetail({ batchId }: { batchId: string }) {
  const batch = useQuery({
    queryKey: ["probe-batch", batchId],
    queryFn: () => api.probeBatch(batchId),
    refetchInterval: (query) => query.state.data?.state === "RUNNING" ? 500 : false,
  });
  const [cancelError, setCancelError] = useState<Error | null>(null);
  if (batch.error) return <div className="page-stack"><ErrorNotice error={batch.error} /></div>;
  if (!batch.data) return <div className="page-stack"><PageHead eyebrow="BATCH" title="正在读取批次…" description="从 PostgreSQL 恢复批次和模型进度。" /></div>;
  const value = batch.data;
  return <div className="page-stack batch-detail-page">
    <PageHead eyebrow={`${value.mode.toUpperCase()} / ${value.model_count} MODELS`} title="模型批次结果" description="刷新页面后仍从 PostgreSQL 恢复；每个模型关联独立、可审计的原生探针运行。" action={<StatusPill status={value.state} />} />
    {cancelError && <ErrorNotice error={cancelError} />}
    <section className="panel batch-progress"><div><b>{value.items.filter((item) => ["COMPLETED", "FAILED", "CANCELLED", "SKIPPED"].includes(item.state)).length}</b><span>/ {value.model_count} 已终止</span></div><div className="progress-track"><i style={{ width: `${value.items.filter((item) => !["PENDING", "RUNNING"].includes(item.state)).length / value.model_count * 100}%` }} /></div>{value.state === "RUNNING" && <button className="danger-action" onClick={() => void api.cancelProbeBatch(batchId).then(() => batch.refetch()).catch((cause) => setCancelError(displayError(cause)))}><XCircle size={15} />取消未开始调用</button>}</section>
    <section className="panel batch-matrix"><header><span>模型</span><span>目录</span><span>生成 / 套件</span><span>运行证据</span></header>{value.items.map((item) => <div className="batch-matrix-row" key={item.item_id}><b>{item.model_id}</b><StatusPill status="VISIBLE" /><StatusPill status={item.state} />{item.run_id ? <Link to={`/runs/${item.run_id}`}>查看运行 <SquareArrowOutUpRight size={12} /></Link> : <span>{item.failure_code ?? "等待调度"}</span>}</div>)}</section>
    <Link className="secondary-action inline-action" to="/probes/api-key">发起另一个批次</Link>
  </div>;
}

function displayError(cause: unknown): Error {
  return cause instanceof Error ? cause : new Error("请求未完成，请检查渠道和凭据配置");
}
