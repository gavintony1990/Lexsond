import { useEffect, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Braces, Clock3, Copy, FlaskConical, Layers3, Plus, RotateCcw, ShieldCheck, Trash2, X } from "lucide-react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { api } from "../api";
import type { Suite, SuiteDocument } from "../types";
import { EmptyState, ErrorNotice, formatTime, PageHead, StatusPill } from "../ui";
import { SuiteModuleTabs } from "./SuiteModuleTabs";

const schema = z.object({
  name: z.string().min(1).max(120),
  description: z.string().max(1000),
  document_version: z.string().min(1).max(40),
  layer: z.enum(["L0", "L1", "L2", "L3"]),
  prompt: z.string().min(1).max(10_000),
  stream: z.boolean(),
  max_output_tokens: z.number().int().min(1).max(4096),
  warmup: z.number().int().min(0).max(20),
  requests: z.number().int().min(1).max(100),
  concurrency: z.number().int().min(1).max(10),
  timeout_seconds: z.number().min(0.1).max(300),
  max_cost_usd: z.number().positive().max(100),
  exact_text: z.string().max(1000),
  success_rate: z.number().min(0).max(1),
});
type SuiteForm = z.infer<typeof schema>;

const defaults: SuiteForm = {
  name: "openai-compatible-smoke",
  description: "单模型、单断言的有界聊天金丝雀",
  document_version: "0.1.0",
  layer: "L2",
  prompt: "Reply with exactly: PROBE_OK",
  stream: true,
  max_output_tokens: 64,
  warmup: 0,
  requests: 1,
  concurrency: 1,
  timeout_seconds: 30,
  max_cost_usd: 0.1,
  exact_text: "PROBE_OK",
  success_rate: 0.99,
};

export function Suites() {
  const [showArchived, setShowArchived] = useState(false);
  const [editing, setEditing] = useState<Suite | null | "new">(null);
  const [historySuite, setHistorySuite] = useState<Suite | null>(null);
  const queryClient = useQueryClient();
  const suites = useQuery({ queryKey: ["suites", showArchived], queryFn: () => api.suites(showArchived) });
  const revisions = useQuery({ queryKey: ["suite-revisions", historySuite?.id], queryFn: () => api.suiteRevisions(historySuite!.id), enabled: !!historySuite });
  const form = useForm<SuiteForm>({ resolver: zodResolver(schema), defaultValues: defaults });

  useEffect(() => {
    if (editing === "new") form.reset(defaults);
    else if (editing) form.reset(fromSuite(editing));
  }, [editing, form]);

  const save = useMutation({
    mutationFn: async (value: SuiteForm) => {
      const document = buildDocument(value);
      if (editing && editing !== "new") return api.updateSuite(editing.id, { version: editing.version, name: value.name, description: value.description, document });
      return api.createSuite({ name: value.name, description: value.description, document });
    },
    onSuccess: () => { setEditing(null); queryClient.invalidateQueries({ queryKey: ["suites"] }); },
  });
  const lifecycle = useMutation({
    mutationFn: async ({ action, suite }: { action: "archive" | "restore" | "purge"; suite: Suite }) => {
      if (action === "archive") return api.archiveSuite(suite.id);
      if (action === "restore") return api.restoreSuite(suite.id);
      return api.purgeSuite(suite.id);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["suites"] }),
  });
  const watched = form.watch();
  const totalCalls = Number(watched.warmup || 0) + Number(watched.requests || 0);

  return (
    <div className="page-stack">
      <SuiteModuleTabs />
      <PageHead eyebrow="CANARY LIBRARY / 套件库" title="把质量意图冻结成可运行规范" description="第一版编辑器严格覆盖 openai-chat 金丝雀：采样、预算、并发和断言都在发请求前完成校验。" action={<button className="primary-action" onClick={() => setEditing("new")}><Plus size={16} />创建套件</button>} />
      {(suites.error || save.error) && <ErrorNotice error={suites.error || save.error} />}
      <div className="toolbar panel-lite"><div className="toolbar-caption"><FlaskConical size={15} /><b>{suites.data?.length ?? 0}</b> 个有界套件</div><label className="toggle-label"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} /><span />显示已归档</label></div>
      <section className="suite-grid stagger-grid">
        {suites.data?.map((suite) => {
          const doc = suite.latest_revision.document;
          const sampling = doc.spec.sampling;
          return (
            <article className={`suite-card ${suite.archived_at ? "archived" : ""}`} key={suite.id}>
              <div className="suite-spine"><span>{doc.spec.layer}</span><i /><b>REV {suite.latest_revision.revision}</b></div>
              <div className="suite-body">
                <header><div><span>{doc.spec.protocol.toUpperCase()}</span><h2>{suite.name}</h2></div><StatusPill status={suite.archived_at ? "ARCHIVED" : "VALID"} /></header>
                <p>{suite.description || "无描述"}</p>
                <div className="suite-metrics"><div><b>{sampling.warmup + sampling.requests}</b><span>TOTAL CALLS</span></div><div><b>{sampling.concurrency}</b><span>CONCURRENCY</span></div><div><b>${sampling.max_cost_usd}</b><span>COST GUARD</span></div></div>
                <div className="assertion-chips">{doc.spec.assertions.slice(0, 5).map((item, index) => <span key={index}>{String(item.type).replaceAll("_", " ")}</span>)}</div>
                <footer><button className="secondary-action" onClick={() => setEditing(suite)}><Braces size={14} />编辑新修订</button><button className="ghost-action" onClick={() => setHistorySuite(suite)}><Layers3 size={14} />历史</button>{!suite.archived_at ? <button className="icon-button" onClick={() => lifecycle.mutate({ action: "archive", suite })}><Archive size={15} /></button> : <><button className="icon-button" onClick={() => lifecycle.mutate({ action: "restore", suite })}><RotateCcw size={15} /></button><button className="icon-button danger" onClick={() => window.prompt(`输入套件名称「${suite.name}」确认永久清除`) === suite.name && lifecycle.mutate({ action: "purge", suite })}><Trash2 size={15} /></button></>}</footer>
              </div>
            </article>
          );
        })}
      </section>
      {!suites.data?.length && <div className="panel"><EmptyState icon={FlaskConical} title="套件库为空" body="创建一个有请求数、并发和费用护栏的聊天金丝雀。" action={<button className="primary-action" onClick={() => setEditing("new")}>创建首个套件</button>} /></div>}

      {editing && <div className="drawer-layer"><button className="drawer-scrim" onClick={() => setEditing(null)} /><aside className="drawer suite-drawer"><header><div><span className="eyebrow">SUITE COMPILER</span><h2>{editing === "new" ? "编译新套件" : "创建不可变修订"}</h2></div><button className="icon-button" onClick={() => setEditing(null)}><X size={19} /></button></header><form onSubmit={form.handleSubmit((value) => save.mutate(value))} className="form-stack">
        <div className="form-row"><label>套件名称<input {...form.register("name")} /></label><label>文档版本<input {...form.register("document_version")} /></label></div>
        <label>用途说明<textarea {...form.register("description")} rows={2} /></label>
        <div className="form-row three"><label>层级<select {...form.register("layer")}><option>L0</option><option>L1</option><option>L2</option><option>L3</option></select></label><label>最大输出 Token<input type="number" {...form.register("max_output_tokens", { valueAsNumber: true })} /></label><label className="check-field"><span>流式响应</span><input type="checkbox" {...form.register("stream")} /></label></div>
        <label>固定 Prompt<textarea {...form.register("prompt")} rows={4} /></label>
        <div className="section-label">SAMPLING ENVELOPE</div>
        <div className="form-row three"><label>预热<input type="number" {...form.register("warmup", { valueAsNumber: true })} /></label><label>正式请求<input type="number" {...form.register("requests", { valueAsNumber: true })} /></label><label>并发<input type="number" {...form.register("concurrency", { valueAsNumber: true })} /></label></div>
        <div className="form-row"><label>超时（秒）<input type="number" step="0.1" {...form.register("timeout_seconds", { valueAsNumber: true })} /></label><label>费用上限（USD）<input type="number" step="0.01" {...form.register("max_cost_usd", { valueAsNumber: true })} /></label></div>
        <div className="section-label">QUALITY ASSERTIONS</div>
        <div className="form-row"><label>期望精确文本<input {...form.register("exact_text")} /></label><label>最低成功率<input type="number" step="0.01" {...form.register("success_rate", { valueAsNumber: true })} /></label></div>
        <div className="suite-budget-preview"><ShieldCheck size={20} /><div><b>{totalCalls} 次最大调用</b><span>{watched.requests} samples + {watched.warmup} warmup · concurrency {watched.concurrency}</span></div><strong>${watched.max_cost_usd || 0}</strong></div>
        {Object.keys(form.formState.errors).length > 0 && <div className="error-notice">套件字段超出协议边界，请检查请求数、并发和断言。</div>}
        {save.error && <ErrorNotice error={save.error} />}
        <div className="form-actions"><button type="button" className="ghost-action" onClick={() => setEditing(null)}>取消</button><button className="primary-action" disabled={save.isPending}>{save.isPending ? "编译中…" : "验证并保存"}</button></div>
      </form></aside></div>}

      {historySuite && <div className="modal-layer"><button className="drawer-scrim" onClick={() => setHistorySuite(null)} /><section className="modal-card revision-modal"><header><div><span className="eyebrow">IMMUTABLE HISTORY</span><h2>{historySuite.name}</h2></div><button className="icon-button" onClick={() => setHistorySuite(null)}><X size={19} /></button></header><div className="revision-list">{revisions.data?.map((revision) => <div className="revision-row" key={revision.id}><span>R{String(revision.revision).padStart(2, "0")}</span><div><b>v{revision.document.metadata.version}</b><code>{revision.sha256.slice(0, 16)}…</code></div><small><Clock3 size={12} />{formatTime(revision.created_at)}</small><button className="icon-button" onClick={() => navigator.clipboard?.writeText(JSON.stringify(revision.document, null, 2))}><Copy size={14} /></button></div>)}</div></section></div>}
    </div>
  );
}

function buildDocument(value: SuiteForm): SuiteDocument {
  const assertions: Array<Record<string, unknown>> = [
    { type: "http_status", equals: 200 },
    { type: "success_rate", gte: value.success_rate },
    { type: "output_nonempty" },
  ];
  if (value.exact_text) assertions.push({ type: "exact_text", equals: value.exact_text });
  if (value.stream) assertions.push({ type: "sse_sequence_valid" }, { type: "finish_reason_present" }, { type: "pseudo_stream_absent" });
  return { apiVersion: "probe.ai/v1alpha1", kind: "ProbeSuite", metadata: { name: value.name, version: value.document_version }, spec: { layer: value.layer, protocol: "openai-chat", request: { prompt: value.prompt, stream: value.stream, max_output_tokens: value.max_output_tokens }, sampling: { warmup: value.warmup, requests: value.requests, concurrency: value.concurrency, timeout_seconds: value.timeout_seconds, max_cost_usd: value.max_cost_usd }, assertions } };
}

function fromSuite(suite: Suite): SuiteForm {
  const doc = suite.latest_revision.document;
  const exact = doc.spec.assertions.find((item) => item.type === "exact_text")?.equals;
  const success = doc.spec.assertions.find((item) => item.type === "success_rate")?.gte;
  return { name: suite.name, description: suite.description, document_version: doc.metadata.version, layer: doc.spec.layer as SuiteForm["layer"], prompt: doc.spec.request.prompt, stream: doc.spec.request.stream, max_output_tokens: doc.spec.request.max_output_tokens, warmup: doc.spec.sampling.warmup, requests: doc.spec.sampling.requests, concurrency: doc.spec.sampling.concurrency, timeout_seconds: doc.spec.sampling.timeout_seconds, max_cost_usd: doc.spec.sampling.max_cost_usd, exact_text: typeof exact === "string" ? exact : "", success_rate: typeof success === "number" ? success : 0.99 };
}
