import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArrowLeft,
  ArrowRight,
  DatabaseZap,
  FileJson2,
  Filter,
  History,
  LockKeyhole,
  Play,
  Plus,
  RotateCcw,
  Search,
  ShieldAlert,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type {
  EvaluationDataset,
  EvaluationCsvField,
  EvaluationCsvMapping,
  EvaluationDatasetMetadataInput,
  EvaluationDatasetPatchInput,
  EvaluationDistributionPolicy,
} from "../types";
import { EmptyState, ErrorNotice, formatTime, PageHead, StatusPill } from "../ui";
import { SuiteModuleTabs } from "./SuiteModuleTabs";

const policyCopy: Record<EvaluationDistributionPolicy, { label: string; note: string; runnable: boolean }> = {
  BUNDLED: { label: "可运行", note: "内容已固定并通过本地校验", runnable: true },
  IMPORT_REQUIRED: { label: "需要导入", note: "先固定来源版本、许可证和 SHA-256", runnable: false },
  LICENSE_REVIEW: { label: "许可审核", note: "具体数据与代码许可尚需人工复核", runnable: false },
  RESEARCH_ONLY: { label: "仅限研究", note: "非商业许可，商业云不得自动复制", runnable: false },
  RUNNER_REQUIRED: { label: "需要 Runner", note: "需要独立的安全执行器，本版本不运行", runnable: false },
  BLOCKED: { label: "已阻止", note: "当前策略禁止导入或执行", runnable: false },
};

const initialMetadata: EvaluationDatasetMetadataInput = {
  slug: "",
  name: "",
  description: "",
  license_spdx: "LicenseRef-Proprietary",
  license_url: "",
  source_url: null,
  distribution_policy: "BUNDLED",
  default_scorer_id: "normalized_exact_match",
  format: "jsonl",
  csv_mapping: null,
  rights_confirmed: false,
};

const csvFields: { field: EvaluationCsvField; label: string }[] = [
  { field: "id", label: "题目 ID" },
  { field: "input", label: "输入文本" },
  { field: "reference_answer", label: "参考答案" },
  { field: "category", label: "任务分类" },
  { field: "language", label: "语言" },
  { field: "scorer", label: "评分器" },
];

export function EvaluationDatasets() {
  const { datasetId } = useParams();
  if (datasetId) return <EvaluationDatasetDetail datasetId={datasetId} />;
  return <EvaluationDatasetLibrary />;
}

function EvaluationDatasetLibrary() {
  const [search, setSearch] = useState("");
  const [policy, setPolicy] = useState<"ALL" | EvaluationDistributionPolicy>("ALL");
  const [language, setLanguage] = useState("ALL");
  const [category, setCategory] = useState("ALL");
  const [showArchived, setShowArchived] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const datasets = useQuery({
    queryKey: ["evaluation-datasets", showArchived],
    queryFn: () => api.evaluationDatasets(showArchived),
  });
  const visible = useMemo(
    () => (datasets.data ?? []).filter((dataset) => {
      const matchesSearch = `${dataset.name} ${dataset.slug} ${dataset.description}`.toLowerCase().includes(search.toLowerCase());
      const languages = dataset.latest_revision?.language_codes ?? [];
      const categories = Object.keys(dataset.latest_revision?.manifest?.categories ?? {});
      return matchesSearch
        && (policy === "ALL" || dataset.distribution_policy === policy)
        && (language === "ALL" || languages.includes(language))
        && (category === "ALL" || categories.includes(category));
    }),
    [category, datasets.data, language, policy, search],
  );
  const languages = useMemo(() => Array.from(new Set((datasets.data ?? []).flatMap((dataset) => dataset.latest_revision?.language_codes ?? []))).sort(), [datasets.data]);
  const categories = useMemo(() => Array.from(new Set((datasets.data ?? []).flatMap((dataset) => Object.keys(dataset.latest_revision?.manifest?.categories ?? {})))).sort(), [datasets.data]);
  const system = visible.filter((dataset) => dataset.scope === "SYSTEM");
  const workspace = visible.filter((dataset) => dataset.scope === "WORKSPACE");

  return (
    <div className="page-stack evaluation-datasets-page">
      <SuiteModuleTabs />
      <PageHead
        eyebrow="VERSIONED CORPUS / 评测数据集"
        title="用同一份证据，公平比较多个模型"
        description="每次运行冻结数据修订、样本 ID、随机种子与评分器版本。外部目录只声明来源和策略，不代表 Lexsond 获得再分发权。"
        action={<button className="primary-action" onClick={() => setUploadOpen(true)}><Plus size={16} />上传数据集</button>}
      />
      {datasets.error && <ErrorNotice error={datasets.error} />}
      <div className="evaluation-toolbar panel-lite">
        <label className="search-box"><Search size={15} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索名称、slug 或说明" /></label>
        <label><Filter size={14} /><select value={policy} onChange={(event) => setPolicy(event.target.value as typeof policy)}><option value="ALL">全部策略</option>{Object.entries(policyCopy).map(([value, copy]) => <option key={value} value={value}>{copy.label}</option>)}</select></label>
        <label><select aria-label="按语言筛选" value={language} onChange={(event) => setLanguage(event.target.value)}><option value="ALL">全部语言</option>{languages.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <label><select aria-label="按任务分类筛选" value={category} onChange={(event) => setCategory(event.target.value)}><option value="ALL">全部任务</option>{categories.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <label className="toggle-label"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} /><span />显示已归档</label>
      </div>

      <DatasetSection title="SYSTEM / 内置与外部目录" note="内置内容可直接运行；目录项不会自动下载。" datasets={system} />
      <DatasetSection title="WORKSPACE / 我的数据集" note="工作区私有，不提供公开分享链接。" datasets={workspace} />
      {!visible.length && !datasets.isLoading && <div className="panel"><EmptyState icon={DatabaseZap} title="没有匹配的数据集" body="调整筛选，或上传一份 UTF-8 JSONL / CSV 数据集。" /></div>}
      {uploadOpen && <DatasetUploadDrawer onClose={() => setUploadOpen(false)} />}
    </div>
  );
}

function DatasetSection({ title, note, datasets }: { title: string; note: string; datasets: EvaluationDataset[] }) {
  return (
    <section className="dataset-section">
      <header><div><span className="eyebrow">{title}</span><p>{note}</p></div><b>{datasets.length.toString().padStart(2, "0")}</b></header>
      <div className="dataset-grid stagger-grid">
        {datasets.map((dataset) => <DatasetCard key={dataset.id} dataset={dataset} />)}
      </div>
    </section>
  );
}

function DatasetCard({ dataset }: { dataset: EvaluationDataset }) {
  const copy = policyCopy[dataset.distribution_policy];
  return (
    <article className={`dataset-card policy-${dataset.distribution_policy.toLowerCase()} ${dataset.archived_at ? "archived" : ""}`}>
      <header>
        <div className="dataset-scope-mark"><DatabaseZap size={18} /><span>{dataset.scope}</span></div>
        <StatusPill status={dataset.archived_at ? "ARCHIVED" : copy.label} />
      </header>
      <div className="dataset-title"><span>{dataset.slug}</span><h2>{dataset.name}</h2><p>{dataset.description}</p></div>
      <div className="dataset-facts">
        <div><b>{dataset.latest_revision?.item_count ?? "—"}</b><span>ITEMS</span></div>
        <div><b>{dataset.latest_revision?.category_count ?? "—"}</b><span>CATEGORIES</span></div>
        <div><b>{dataset.latest_revision ? `R${dataset.latest_revision.revision}` : "CATALOG"}</b><span>REVISION</span></div>
      </div>
      <div className="license-strip"><ShieldAlert size={14} /><b>{dataset.license_spdx}</b><span>{copy.note}</span></div>
      {dataset.source_url && <a className="source-link" href={dataset.source_url} target="_blank" rel="noreferrer">来源 · {dataset.source_version ?? "未固定导入版本"}</a>}
      <footer>
        <Link to={`/suites/datasets/${dataset.id}`} className="secondary-action">查看详情 <ArrowRight size={14} /></Link>
        {copy.runnable && dataset.latest_revision && !dataset.archived_at && <Link to={`/suites/evaluation-runs/new?dataset=${dataset.id}`} className="ghost-action"><Play size={14} />使用数据集评测</Link>}
      </footer>
    </article>
  );
}

function DatasetUploadDrawer({ onClose }: { onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [metadata, setMetadata] = useState(initialMetadata);
  const [csvHeaders, setCsvHeaders] = useState<string[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const scorers = useQuery({ queryKey: ["evaluation-scorers"], queryFn: api.evaluationScorers });
  const validate = useMutation({ mutationFn: () => {
    if (!file) throw new Error("请先选择文件");
    return api.validateEvaluationUpload(file, metadata.format, metadata.csv_mapping);
  }});
  const create = useMutation({
    mutationFn: () => {
      if (!file || !validate.data) throw new Error("请先完成预览校验");
      return api.createEvaluationDataset(file, metadata);
    },
    onSuccess: () => {
      setFile(null);
      validate.reset();
      if (fileRef.current) fileRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["evaluation-datasets"] });
      onClose();
    },
  });
  const step = !file ? 1 : !validate.data ? 2 : 3;
  const csvMappingReady = metadata.format !== "csv" || (
    metadata.csv_mapping !== null
    && csvFields.every(({ field }) => Boolean(metadata.csv_mapping?.[field]))
    && new Set(Object.values(metadata.csv_mapping)).size === csvFields.length
  );
  const selectFile = async (next: File | null) => {
    setFile(next);
    validate.reset();
    const format = next?.name.toLowerCase().endsWith(".csv") ? "csv" : "jsonl";
    if (next && format === "csv") {
      const headers = parseCsvHeader(await next.slice(0, 16 * 1024).text());
      const mapping = Object.fromEntries(
        csvFields.map(({ field }) => [field, headers.includes(field) ? field : ""]),
      ) as EvaluationCsvMapping;
      setCsvHeaders(headers);
      setMetadata((current) => ({ ...current, format, csv_mapping: mapping }));
    } else {
      setCsvHeaders([]);
      setMetadata((current) => ({ ...current, format, csv_mapping: null }));
    }
  };
  return (
    <div className="drawer-layer">
      <button className="drawer-scrim" onClick={onClose} aria-label="关闭上传向导" />
      <aside className="drawer evaluation-upload-drawer" role="dialog" aria-modal="true" aria-label="上传评测数据集">
        <header><div><span className="eyebrow">DATASET INGEST / STEP {step} OF 3</span><h2>创建不可变数据集修订</h2></div><button className="icon-button" onClick={onClose}><X size={19} /></button></header>
        <div className="upload-stepper"><i className="done" /><i className={step >= 2 ? "done" : ""} /><i className={step >= 3 ? "done" : ""} /></div>
        <section className="upload-dropzone">
          <UploadCloud size={26} />
          <div><b>{file?.name ?? "选择 JSONL 或 CSV"}</b><span>最大 10 MiB · UTF-8 · 最多 10,000 条</span></div>
          <input ref={fileRef} type="file" accept=".jsonl,.csv,application/json,text/csv,text/plain" onChange={(event) => { void selectFile(event.target.files?.[0] ?? null); }} />
        </section>
        {file && metadata.format === "csv" && !validate.data && <CsvMappingFields headers={csvHeaders} mapping={metadata.csv_mapping} onChange={(csv_mapping) => setMetadata({ ...metadata, csv_mapping })} />}
        {file && !validate.data && <button className="primary-action full" onClick={() => validate.mutate()} disabled={validate.isPending || !csvMappingReady}><FileJson2 size={15} />{validate.isPending ? "流式校验中…" : "校验并生成预览"}</button>}
        {(validate.error || create.error) && <ErrorNotice error={validate.error || create.error} />}
        {validate.data && <>
          <div className="upload-facts"><div><b>{validate.data.item_count}</b><span>ROWS</span></div><div><b>{validate.data.category_count}</b><span>CATEGORIES</span></div><div><b>{validate.data.language_codes.join(" / ")}</b><span>LANGUAGES</span></div></div>
          <div className="preview-hash"><LockKeyhole size={14} /><code>{validate.data.content_sha256}</code></div>
          <div className="form-stack compact-fields">
            <div className="form-row"><label>名称<input value={metadata.name} onChange={(event) => setMetadata({ ...metadata, name: event.target.value })} /></label><label>Slug<input value={metadata.slug} onChange={(event) => setMetadata({ ...metadata, slug: event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })} /></label></div>
            <label>说明<textarea rows={2} value={metadata.description} onChange={(event) => setMetadata({ ...metadata, description: event.target.value })} /></label>
            <div className="form-row"><label>许可证 SPDX / 私有声明<input value={metadata.license_spdx} onChange={(event) => setMetadata({ ...metadata, license_spdx: event.target.value })} /></label><label>许可说明 URL<input value={metadata.license_url} onChange={(event) => setMetadata({ ...metadata, license_url: event.target.value })} /></label></div>
            <label>默认评分器<select value={metadata.default_scorer_id} onChange={(event) => setMetadata({ ...metadata, default_scorer_id: event.target.value })}>{scorers.data?.map((scorer) => <option value={scorer.scorer_id} key={scorer.scorer_id}>{scorer.label} · v{scorer.version}</option>)}</select></label>
            <label className="rights-confirm"><input type="checkbox" checked={metadata.rights_confirmed} onChange={(event) => setMetadata({ ...metadata, rights_confirmed: event.target.checked })} /><span><b>我确认拥有上传与评测这些数据的权利</b><small>数据集保持工作区私有；内容将按不可信输入处理。</small></span></label>
          </div>
          <div className="dataset-preview-table"><header><span>PREVIEW / 前 {validate.data.preview.length} 条</span><small>预览可能包含敏感业务数据，请只在授权工作区查看。</small></header>{validate.data.preview.slice(0, 5).map((item) => <div key={item.id ?? item.item_id}><code>{item.id ?? item.item_id}</code><span>{item.category}</span><small>{item.language}</small></div>)}</div>
          <div className="form-actions"><button className="ghost-action" onClick={() => { validate.reset(); }}>返回</button><button className="primary-action" disabled={create.isPending || !metadata.name || !metadata.slug || !metadata.license_url || !metadata.rights_confirmed} onClick={() => create.mutate()}>{create.isPending ? "事务写入中…" : "确认并创建修订"}</button></div>
        </>}
      </aside>
    </div>
  );
}

function CsvMappingFields({ headers, mapping, onChange }: { headers: string[]; mapping: EvaluationCsvMapping | null; onChange: (value: EvaluationCsvMapping) => void }) {
  if (!headers.length || !mapping) return <div className="unknown-cost-warning"><ShieldAlert size={16} /><div><b>无法读取 CSV 表头</b><span>请确认文件为 UTF-8 CSV，且第一条记录包含列名。</span></div></div>;
  return <section className="panel-lite form-stack"><header><div><span className="eyebrow">CSV FIELD MAPPING</span><b>把源列映射为标准字段</b></div></header><div className="form-row three">{csvFields.map(({ field, label }) => <label key={field}>{label}<select value={mapping[field]} onChange={(event) => onChange({ ...mapping, [field]: event.target.value })}><option value="">请选择源列</option>{headers.map((header) => <option key={header} value={header}>{header}</option>)}</select></label>)}</div><small>六个字段必须选择不同列；服务端会再次校验并把映射写入修订 manifest。</small></section>;
}

function parseCsvHeader(text: string): string[] {
  const source = text.replace(/^\uFEFF/, "");
  const values: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === '"') {
      if (quoted && source[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      values.push(current.trim());
      current = "";
    } else if ((character === "\n" || character === "\r") && !quoted) {
      values.push(current.trim());
      break;
    } else {
      current += character;
    }
  }
  if (values.length === 0 && current) values.push(current.trim());
  return values.filter((value, index) => value.length > 0 && values.indexOf(value) === index);
}

function EvaluationDatasetDetail({ datasetId }: { datasetId: string }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [revisionOpen, setRevisionOpen] = useState(false);
  const [editingMetadata, setEditingMetadata] = useState(false);
  const dataset = useQuery({ queryKey: ["evaluation-dataset", datasetId], queryFn: () => api.evaluationDataset(datasetId, true) });
  const revisions = useQuery({ queryKey: ["evaluation-revisions", datasetId], queryFn: () => api.evaluationDatasetRevisions(datasetId) });
  const latestNumber = dataset.data?.latest_revision?.revision;
  const latest = useQuery({ queryKey: ["evaluation-revision", datasetId, latestNumber], queryFn: () => api.evaluationDatasetRevision(datasetId, latestNumber!), enabled: !!latestNumber });
  const runs = useQuery({ queryKey: ["evaluation-runs", false], queryFn: () => api.evaluationRuns(false) });
  const relatedRuns = (runs.data ?? []).filter((run) => run.dataset_id === datasetId);
  const lifecycle = useMutation({
    mutationFn: async (action: "archive" | "restore" | "purge") => {
      if (action === "archive") await api.archiveEvaluationDataset(datasetId);
      else if (action === "restore") await api.restoreEvaluationDataset(datasetId);
      else await api.purgeEvaluationDataset(datasetId);
    },
    onSuccess: (_value, action) => {
      queryClient.invalidateQueries({ queryKey: ["evaluation-datasets"] });
      if (action === "purge") navigate("/suites/datasets", { replace: true });
      else queryClient.invalidateQueries({ queryKey: ["evaluation-dataset", datasetId] });
    },
  });
  if (dataset.error) return <div className="page-stack"><SuiteModuleTabs /><ErrorNotice error={dataset.error} /></div>;
  const value = dataset.data;
  return (
    <div className="page-stack evaluation-detail-page">
      <SuiteModuleTabs />
      <Link to="/suites/datasets" className="back-link"><ArrowLeft size={14} />返回数据集库</Link>
      {value && <>
        <PageHead eyebrow={`${value.scope} DATASET / ${value.slug}`} title={value.name} description={value.description || "无说明"} action={<div className="head-actions">{value.latest_revision && policyCopy[value.distribution_policy].runnable && !value.archived_at && <Link className="primary-action" to={`/suites/evaluation-runs/new?dataset=${value.id}`}><Play size={15} />使用该数据集评测</Link>}{value.scope === "WORKSPACE" && !value.archived_at && <><button className="secondary-action" onClick={() => setRevisionOpen(true)}><Plus size={14} />创建新修订</button><button className="ghost-action" onClick={() => setEditingMetadata((current) => !current)}>编辑 metadata</button></>}{value.scope === "WORKSPACE" && (!value.archived_at ? <button className="ghost-action" onClick={() => lifecycle.mutate("archive")}><Archive size={14} />归档</button> : <><button className="ghost-action" onClick={() => lifecycle.mutate("restore")}><RotateCcw size={14} />恢复</button><button className="ghost-action danger" onClick={() => { if (window.confirm("永久清除该数据集及其未被引用的修订？此操作不可恢复。")) lifecycle.mutate("purge"); }}><Trash2 size={14} />永久清除</button></>)}</div>} />
        {editingMetadata && <DatasetMetadataEditor key={value.version} dataset={value} onSaved={() => { setEditingMetadata(false); queryClient.invalidateQueries({ queryKey: ["evaluation-dataset", datasetId] }); }} />}
        <section className="dataset-detail-grid"><article className="panel provenance-panel"><header><ShieldAlert size={17} /><h2>来源与许可</h2></header><dl><div><dt>LICENSE</dt><dd><a href={value.license_url} target="_blank" rel="noreferrer">{value.license_spdx}</a></dd></div><div><dt>POLICY</dt><dd>{policyCopy[value.distribution_policy].label}</dd></div><div><dt>SOURCE VERSION</dt><dd>{value.source_version ?? (value.scope === "SYSTEM" ? "未固定导入版本" : "工作区声明")}</dd></div><div><dt>VERIFIED</dt><dd>{value.source_verified_at ?? "未由系统核验"}</dd></div><div><dt>CONTENT HASH</dt><dd><code>{value.latest_revision?.content_sha256 ?? "尚未导入"}</code></dd></div><div><dt>UPDATED</dt><dd>{formatTime(value.updated_at)}</dd></div></dl>{value.source_url && <a href={value.source_url} target="_blank" rel="noreferrer">查看官方来源</a>}<p>{policyCopy[value.distribution_policy].note}</p></article><article className="panel revision-overview"><header><History size={17} /><h2>不可变版本</h2></header>{revisions.data?.map((revision) => <div className="revision-row" key={revision.id}><span>R{revision.revision}</span><div><b>{revision.item_count} items</b><code>{revision.content_sha256.slice(0, 18)}…</code></div><small>{formatTime(revision.created_at)}</small></div>)}</article></section>
        <section className="panel dataset-samples"><header><div><span className="eyebrow">SAMPLE PREVIEW</span><h2>样本预览</h2></div><span className="privacy-reminder"><ShieldAlert size={14} />最多显示 20 条；不会写入日志或分析埋点</span></header>{latest.data?.items?.map((item) => <article key={item.item_id ?? item.id}><code>{item.item_id ?? item.id}</code><span>{item.category} · {item.language}</span><p>{item.input.messages[0]?.content.slice(0, 240)}</p></article>) ?? <EmptyState icon={FileJson2} title="目录项无本地内容" body="按许可策略完成固定版本导入后，才会显示样本并允许运行。" />}</section>
        <section className="panel revision-overview"><header><History size={17} /><h2>引用该数据集的评测记录</h2></header>{relatedRuns.length ? relatedRuns.map((run) => <Link className="revision-row" key={run.id} to={`/suites/evaluation-runs/${run.id}`}><span>{run.state}</span><div><b>{run.model_count} models × {run.sample_count} items</b><code>{run.dataset_revision_id.slice(0, 18)}…</code></div><small>{formatTime(run.created_at)}</small></Link>) : <EmptyState icon={History} title="暂无评测引用" body="运行后会在这里保留对应修订、seed 与评分器版本的跳转。" />}</section>
      </>}
      {revisionOpen && <RevisionUploadDrawer datasetId={datasetId} onClose={() => setRevisionOpen(false)} />}
    </div>
  );
}

function DatasetMetadataEditor({ dataset, onSaved }: { dataset: EvaluationDataset; onSaved: () => void }) {
  const [value, setValue] = useState<EvaluationDatasetPatchInput>({
    version: dataset.version,
    name: dataset.name,
    description: dataset.description,
    license_spdx: dataset.license_spdx,
    license_url: dataset.license_url,
    source_url: dataset.source_url,
    default_scorer_id: dataset.default_scorer_id,
  });
  const save = useMutation({
    mutationFn: () => api.updateEvaluationDataset(dataset.id, value),
    onSuccess: onSaved,
  });
  return <section className="panel dataset-metadata-editor"><header><div><span className="eyebrow">OPTIMISTIC METADATA / VERSION {dataset.version}</span><h2>编辑数据集说明</h2></div></header>{save.error && <ErrorNotice error={save.error} />}<div className="form-row"><label>名称<input value={value.name} onChange={(event) => setValue({ ...value, name: event.target.value })} /></label><label>许可证 SPDX<input value={value.license_spdx} onChange={(event) => setValue({ ...value, license_spdx: event.target.value })} /></label></div><label>说明<textarea rows={3} value={value.description} onChange={(event) => setValue({ ...value, description: event.target.value })} /></label><div className="form-row"><label>许可 URL<input value={value.license_url} onChange={(event) => setValue({ ...value, license_url: event.target.value })} /></label><label>来源 URL（可空）<input value={value.source_url ?? ""} onChange={(event) => setValue({ ...value, source_url: event.target.value || null })} /></label></div><footer><span>如果版本已被他人修改，服务端会拒绝本次保存。</span><button className="primary-action" disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? "保存中…" : "保存 metadata"}</button></footer></section>;
}

function RevisionUploadDrawer({ datasetId, onClose }: { datasetId: string; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<"jsonl" | "csv">("jsonl");
  const [csvHeaders, setCsvHeaders] = useState<string[]>([]);
  const [csvMapping, setCsvMapping] = useState<EvaluationCsvMapping | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const validate = useMutation({ mutationFn: () => {
    if (!file) throw new Error("请先选择文件");
    return api.validateEvaluationUpload(file, format, csvMapping);
  }});
  const create = useMutation({
    mutationFn: () => {
      if (!file || !validate.data) throw new Error("请先校验新修订");
      return api.createEvaluationDatasetRevision(datasetId, file, format, csvMapping);
    },
    onSuccess: () => {
      setFile(null);
      validate.reset();
      if (fileRef.current) fileRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["evaluation-dataset", datasetId] });
      queryClient.invalidateQueries({ queryKey: ["evaluation-revisions", datasetId] });
      queryClient.invalidateQueries({ queryKey: ["evaluation-revision", datasetId] });
      onClose();
    },
  });
  const mappingReady = format !== "csv" || (csvMapping !== null && new Set(Object.values(csvMapping)).size === csvFields.length && csvFields.every(({ field }) => Boolean(csvMapping[field])));
  const selectFile = async (next: File | null) => {
    setFile(next);
    validate.reset();
    const nextFormat = next?.name.toLowerCase().endsWith(".csv") ? "csv" : "jsonl";
    setFormat(nextFormat);
    if (next && nextFormat === "csv") {
      const headers = parseCsvHeader(await next.slice(0, 16 * 1024).text());
      setCsvHeaders(headers);
      setCsvMapping(Object.fromEntries(csvFields.map(({ field }) => [field, headers.includes(field) ? field : ""])) as EvaluationCsvMapping);
    } else {
      setCsvHeaders([]);
      setCsvMapping(null);
    }
  };
  return <div className="drawer-layer"><button className="drawer-scrim" onClick={onClose} aria-label="关闭新修订向导" /><aside className="drawer evaluation-upload-drawer" role="dialog" aria-modal="true" aria-label="创建数据集新修订"><header><div><span className="eyebrow">NEW IMMUTABLE REVISION</span><h2>旧修订不会被覆盖</h2></div><button className="icon-button" onClick={onClose}><X size={19} /></button></header><section className="upload-dropzone"><UploadCloud size={26} /><div><b>{file?.name ?? "选择新 JSONL 或 CSV"}</b><span>完整快照 · 最大 10 MiB · 最多 10,000 条</span></div><input ref={fileRef} type="file" accept=".jsonl,.csv" onChange={(event) => { void selectFile(event.target.files?.[0] ?? null); }} /></section>{format === "csv" && file && !validate.data && <CsvMappingFields headers={csvHeaders} mapping={csvMapping} onChange={setCsvMapping} />}{(validate.error || create.error) && <ErrorNotice error={validate.error || create.error} />}{!validate.data ? <button className="primary-action full" disabled={!file || validate.isPending || !mappingReady} onClick={() => validate.mutate()}>{validate.isPending ? "校验中…" : "校验完整快照"}</button> : <><div className="upload-facts"><div><b>{validate.data.item_count}</b><span>ITEMS</span></div><div><b>{validate.data.category_count}</b><span>CATEGORIES</span></div><div><b>{validate.data.language_codes.join(" / ")}</b><span>LANGUAGES</span></div></div><div className="preview-hash"><LockKeyhole size={14} /><code>{validate.data.content_sha256}</code></div><p className="revision-warning">创建后题目不可修改或删除；历史评测继续引用旧修订。</p><button className="primary-action full" disabled={create.isPending} onClick={() => create.mutate()}>{create.isPending ? "事务写入中…" : "创建新修订"}</button></>}</aside></div>;
}
