import { useEffect, useMemo, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  Clock3,
  History,
  Pause,
  Pencil,
  Play,
  Plus,
  Radio,
  RotateCcw,
  ShieldAlert,
  Trash2,
  X,
} from "lucide-react";
import { useForm } from "react-hook-form";
import { Link } from "react-router-dom";
import { z } from "zod";
import { api } from "../api";
import type {
  MonitorBucket,
  MonitorPolicy,
  MonitorPolicyCreateInput,
  MonitoringOverview,
  ProbeType,
} from "../types";
import { EmptyState, ErrorNotice, formatTime, MetricCard, PageHead, StatusPill } from "../ui";

const policySchema = z.object({
  name: z.string().min(1, "请输入策略名称").max(120),
  target_id: z.string().min(1, "请选择目标"),
  run_kind: z.enum(["component", "suite"]),
  probe_type: z.enum([
    "chat",
    "vision",
    "embedding",
    "image_generation",
    "audio_speech",
    "audio_transcription",
  ]),
  suite_revision_id: z.string(),
  execution_backend: z.enum(["local", "temporal"]),
  model: z.string().max(256),
  stream: z.boolean(),
  timeout_seconds: z.number().min(0.1).max(300),
  interval_seconds: z.number().int().min(60).max(2_592_000),
  failure_threshold: z.number().int().min(1).max(10),
  recovery_threshold: z.number().int().min(1).max(10),
});
type PolicyForm = z.infer<typeof policySchema>;
type Window = MonitoringOverview["window"];

const defaults: PolicyForm = {
  name: "五分钟聊天脉冲",
  target_id: "",
  run_kind: "component",
  probe_type: "chat",
  suite_revision_id: "",
  execution_backend: "local",
  model: "",
  stream: true,
  timeout_seconds: 30,
  interval_seconds: 300,
  failure_threshold: 2,
  recovery_threshold: 1,
};

export function Monitoring() {
  const [windowValue, setWindowValue] = useState<Window>("24h");
  const [showArchived, setShowArchived] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<MonitorPolicy | null>(null);
  const queryClient = useQueryClient();
  const overview = useQuery({
    queryKey: ["monitoring-overview", windowValue],
    queryFn: () => api.monitoringOverview(windowValue),
    refetchInterval: 5_000,
  });
  const policies = useQuery({
    queryKey: ["monitor-policies", showArchived],
    queryFn: () => api.monitorPolicies(showArchived),
    refetchInterval: 5_000,
  });
  const incidents = useQuery({
    queryKey: ["monitor-incidents"],
    queryFn: () => api.monitorIncidents(30),
    refetchInterval: 5_000,
  });
  const targets = useQuery({ queryKey: ["targets", false], queryFn: () => api.targets(false) });
  const suites = useQuery({ queryKey: ["suites", false], queryFn: () => api.suites(false) });
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const targetById = useMemo(
    () => new Map((targets.data ?? []).map((target) => [target.id, target])),
    [targets.data],
  );
  const form = useForm<PolicyForm>({ resolver: zodResolver(policySchema), defaultValues: defaults });
  const executionBackend = form.watch("execution_backend");
  const runKind = form.watch("run_kind");
  const probeType = form.watch("probe_type");
  const selectedTarget = targetById.get(form.watch("target_id"));
  const temporalAvailable = bootstrap.data?.execution_backends.find(
    (backend) => backend.id === "temporal",
  )?.available ?? false;
  const backendIssue = !selectedTarget
    ? null
    : executionBackend === "local" && selectedTarget.target_kind === "cloud"
      ? "云端目标的持续探测不能保存临时 Key，请配置 credential_ref 并选择 Temporal。"
      : executionBackend === "temporal" && !temporalAvailable
        ? "Temporal 执行器当前未配置。"
        : executionBackend === "temporal" && !selectedTarget.credential_ref_configured
          ? "该目标尚未配置 credential_ref，不能交给 Temporal Worker。"
          : null;

  useEffect(() => {
    if (executionBackend === "temporal" && runKind === "component" && probeType !== "chat") {
      form.setValue("probe_type", "chat", { shouldValidate: true });
    }
  }, [executionBackend, form, probeType, runKind]);
  useEffect(() => {
    if (!(["chat", "vision"] as ProbeType[]).includes(probeType)) {
      form.setValue("stream", false, { shouldValidate: true });
    }
  }, [form, probeType]);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["monitor-policies"] });
    queryClient.invalidateQueries({ queryKey: ["monitoring-overview"] });
    queryClient.invalidateQueries({ queryKey: ["monitor-incidents"] });
    queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
  };
  const save = useMutation({
    mutationFn: (value: PolicyForm) => {
      if (backendIssue) {
        throw new Error(backendIssue);
      }
      if (value.run_kind === "suite" && !value.suite_revision_id) {
        throw new Error("请选择套件版本");
      }
      const payload: MonitorPolicyCreateInput = {
        ...value,
        probe_type: value.run_kind === "suite" ? "chat" : value.probe_type,
        suite_revision_id: value.run_kind === "suite" ? value.suite_revision_id : null,
        model: value.model || null,
        enabled: true,
      };
      if (editing) {
        return api.updateMonitorPolicy(editing.id, {
          ...payload,
          version: editing.version,
          enabled: editing.enabled,
        });
      }
      return api.createMonitorPolicy(payload);
    },
    onSuccess: () => {
      setCreating(false);
      setEditing(null);
      form.reset(defaults);
      refresh();
    },
  });
  const action = useMutation({
    mutationFn: async ({ type, policy }: { type: "toggle" | "run" | "archive" | "restore" | "purge"; policy: MonitorPolicy }) => {
      if (type === "toggle") return api.updateMonitorPolicy(policy.id, { version: policy.version, enabled: !policy.enabled });
      if (type === "run") return api.runMonitorPolicyNow(policy.id);
      if (type === "archive") return api.archiveMonitorPolicy(policy.id);
      if (type === "restore") return api.restoreMonitorPolicy(policy.id);
      return api.purgeMonitorPolicy(policy.id);
    },
    onSuccess: refresh,
  });

  const summary = overview.data?.summary;
  const activeRows = overview.data?.policies ?? [];
  const openCreate = () => {
    setEditing(null);
    form.reset(defaults);
    setCreating(true);
  };
  const openEdit = (policy: MonitorPolicy) => {
    setEditing(policy);
    form.reset({
      name: policy.name,
      target_id: policy.target_id,
      run_kind: policy.run_kind,
      probe_type: policy.probe_type,
      suite_revision_id: policy.suite_revision_id ?? "",
      execution_backend: policy.execution_backend,
      model: policy.model,
      stream: policy.stream,
      timeout_seconds: policy.timeout_seconds,
      interval_seconds: policy.interval_seconds,
      failure_threshold: policy.failure_threshold,
      recovery_threshold: policy.recovery_threshold,
    });
    setCreating(true);
  };
  const closeDrawer = () => {
    setCreating(false);
    setEditing(null);
    form.reset(defaults);
  };

  return (
    <div className="page-stack monitoring-page">
      <PageHead
        eyebrow="RELAY PULSE / 持续回波"
        title="中转站可用性热力图"
        description="按持久化策略持续发起有界探测；调度槽幂等、故障去抖，API Key 从不进入策略、事件或样本。"
        action={<button className="primary-action" onClick={openCreate}><Plus size={16} />新建持续探测</button>}
      />
      {(overview.error || policies.error || save.error || action.error) && <ErrorNotice error={overview.error || policies.error || save.error || action.error} />}

      <section className="metric-grid stagger-grid">
        <MetricCard code="UP" label="健康策略" value={summary?.up ?? "—"} suffix=" NODES" tone="mint" />
        <MetricCard code="DN" label="故障策略" value={summary?.down ?? "—"} suffix=" NODES" tone="red" />
        <MetricCard code="DG" label="性能降级" value={summary?.degraded ?? "—"} suffix=" NODES" tone="amber" />
        <MetricCard code="SP" label="窗口样本" value={summary?.samples ?? "—"} suffix=" PULSES" />
      </section>

      <section className="panel monitor-matrix-panel reveal delay-1">
        <header className="panel-head monitor-head">
          <div><span className="eyebrow">TIME BUCKET MATRIX</span><h2>持续质量矩阵</h2></div>
          <div className="window-tabs" aria-label="聚合时间窗口">
            {(["90m", "24h", "7d", "30d"] as Window[]).map((item) => (
              <button className={windowValue === item ? "active" : ""} onClick={() => setWindowValue(item)} key={item}>{item}</button>
            ))}
          </div>
        </header>
        {activeRows.length ? (
          <div className="monitor-matrix">
            <div className="matrix-axis"><span>POLICY / MODEL</span><b>OLDER</b><i /> <b>NOW</b></div>
            {activeRows.map((policy) => (
              <div className="matrix-row" key={policy.id}>
                <div className="matrix-label">
                  <StatusPill status={policy.status} />
                  <strong>{policy.name}</strong>
                  <span>{policy.model} · {policy.execution_backend}</span>
                </div>
                <div className="heat-strip">
                  {overview.data!.timeline.map((stamp) => {
                    const bucket = policy.buckets.find((item) => item.started_at === stamp);
                    return <HeatCell key={stamp} bucket={bucket} stamp={stamp} />;
                  })}
                </div>
                <div className="matrix-meta"><b>{policy.sample_count}</b><span>SAMPLES</span></div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon={Radio} title="尚无持续回波" body="创建策略后，调度器会按稳定错峰自动发起有界探测。" />
        )}
        <footer className="heat-legend"><span><i className="heat-pass" />PASS</span><span><i className="heat-warn" />WARN</span><span><i className="heat-fail" />FAIL</span><span><i className="heat-empty" />NO SAMPLE</span><b>BUCKET {Math.round((overview.data?.bucket_seconds ?? 0) / 60)} MIN</b></footer>
      </section>

      <section className="monitor-lower-grid">
        <article className="panel policy-panel reveal delay-2">
          <header className="panel-head"><div><span className="eyebrow">SCHEDULE MANIFEST</span><h2>调度策略</h2></div><label className="toggle-label"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} /><span />归档区</label></header>
          <div className="policy-list">
            {policies.data?.map((policy) => (
              <div className={`policy-row ${policy.archived_at ? "archived" : ""}`} key={policy.id}>
                <div className="policy-pulse"><Activity size={16} /><i /></div>
                <div><strong>{policy.name}</strong><span>{targetById.get(policy.target_id)?.name ?? "TARGET"} · 每 {formatInterval(policy.interval_seconds)}</span></div>
                <div className="policy-next"><Clock3 size={12} /><span>{policy.enabled ? formatTime(policy.next_run_at) : "PAUSED"}</span></div>
                {!policy.archived_at ? <>
                  <button className="icon-button" aria-label="编辑" onClick={() => openEdit(policy)}><Pencil size={14} /></button>
                  <button className="icon-button" aria-label={policy.enabled ? "暂停" : "启用"} onClick={() => action.mutate({ type: "toggle", policy })}>{policy.enabled ? <Pause size={14} /> : <Play size={14} />}</button>
                  <button className="secondary-action compact" disabled={!policy.enabled} onClick={() => action.mutate({ type: "run", policy })}><Radio size={13} />立即</button>
                  <button className="icon-button" aria-label="归档" onClick={() => action.mutate({ type: "archive", policy })}><Archive size={14} /></button>
                </> : <>
                  <button className="icon-button" aria-label="恢复" onClick={() => action.mutate({ type: "restore", policy })}><RotateCcw size={14} /></button>
                  <button className="icon-button danger" aria-label="永久清除" onClick={() => window.prompt(`输入策略名称「${policy.name}」确认永久清除`) === policy.name && action.mutate({ type: "purge", policy })}><Trash2 size={14} /></button>
                </>}
              </div>
            ))}
          </div>
        </article>

        <article className="panel incident-panel reveal delay-3">
          <header className="panel-head"><div><span className="eyebrow">STATE TRANSITIONS</span><h2>故障与恢复事件</h2></div><History size={18} /></header>
          <div className="incident-list">
            {incidents.data?.slice(0, 10).map((incident) => {
              const policy = policies.data?.find((item) => item.id === incident.policy_id);
              return (
                <Link to={`/runs/${incident.run_id}`} className={`incident-row incident-${incident.event_type.toLowerCase()}`} key={incident.id}>
                  <div>{incident.event_type === "RECOVERED" ? <RotateCcw size={15} /> : <ShieldAlert size={15} />}</div>
                  <p><strong>{incident.event_type}</strong><span>{policy?.name ?? incident.policy_id.slice(0, 8)} · {incident.error_class ?? `${incident.from_status} → ${incident.to_status}`}</span></p>
                  <time>{formatTime(incident.observed_at)}</time>
                </Link>
              );
            })}
            {!incidents.data?.length && <EmptyState icon={History} title="没有状态事件" body="连续失败达到阈值或服务恢复时才生成事件，单次抖动不会报警。" />}
          </div>
        </article>
      </section>

      {creating && (
        <div className="drawer-layer" role="dialog" aria-modal="true" aria-label="持续探测策略编辑器">
          <button className="drawer-scrim" onClick={closeDrawer} aria-label="关闭" />
          <aside className="drawer monitor-drawer">
            <header><div><span className="eyebrow">DURABLE SCHEDULE</span><h2>{editing ? "编辑持续探测策略" : "创建持续探测策略"}</h2></div><button className="icon-button" onClick={closeDrawer}><X size={19} /></button></header>
            <form className="form-stack" onSubmit={form.handleSubmit((value) => save.mutate(value))}>
              <label>策略名称<input {...form.register("name")} />{form.formState.errors.name && <small>{form.formState.errors.name.message}</small>}</label>
              <div className="form-row"><label>探测目标<select {...form.register("target_id")}><option value="">选择目标</option>{targets.data?.map((target) => <option value={target.id} key={target.id}>{target.name}</option>)}</select></label><label>执行后端<select {...form.register("execution_backend")}><option value="local">本地执行器</option><option value="temporal" disabled={!temporalAvailable}>Temporal{temporalAvailable ? "" : "（未配置）"}</option></select></label></div>
              <div className="form-row"><label>运行类型<select {...form.register("run_kind")}><option value="component">单项探针</option><option value="suite">聊天套件</option></select></label>{runKind === "component" ? <label>探针类型<select {...form.register("probe_type")} disabled={executionBackend === "temporal"}>{probeOptions.map(([id, label]) => <option value={id} key={id} disabled={executionBackend === "temporal" && id !== "chat"}>{label}</option>)}</select>{executionBackend === "temporal" && <small>Temporal 当前仅支持聊天单项与聊天套件</small>}</label> : <label>套件版本<select {...form.register("suite_revision_id")}><option value="">选择套件</option>{suites.data?.map((suite) => <option value={suite.latest_revision.id} key={suite.id}>{suite.name} · rev {suite.latest_revision.revision}</option>)}</select></label>}</div>
              <label>模型覆盖 <span className="optional">留空使用目标默认值</span><input {...form.register("model")} placeholder="model-id" /></label>
              <div className="form-row three"><label>间隔（秒）<input type="number" {...form.register("interval_seconds", { valueAsNumber: true })} /></label><label>超时（秒）<input type="number" step="0.1" {...form.register("timeout_seconds", { valueAsNumber: true })} /></label><label className="check-field"><span>流式</span><input type="checkbox" {...form.register("stream")} /></label></div>
              <div className="form-row"><label>连续失败阈值<input type="number" {...form.register("failure_threshold", { valueAsNumber: true })} /></label><label>连续恢复阈值<input type="number" {...form.register("recovery_threshold", { valueAsNumber: true })} /></label></div>
              <div className="monitor-safety-note"><ShieldAlert size={17} /><p><b>不保存 API Key</b><span>云端持续探测必须使用配置了 credential_ref 的目标并选择 Temporal；本地执行只支持无密钥目标。</span></p></div>
              {backendIssue && <div className="field-error" role="alert">{backendIssue}</div>}
              {save.error && <ErrorNotice error={save.error} />}
              <div className="form-actions"><button type="button" className="ghost-action" onClick={closeDrawer}>取消</button><button className="primary-action" disabled={save.isPending || Boolean(backendIssue)}>{save.isPending ? "保存中…" : editing ? "保存修改" : "创建并启用"}</button></div>
            </form>
          </aside>
        </div>
      )}
    </div>
  );
}

function HeatCell({ bucket, stamp }: { bucket: MonitorBucket | undefined; stamp: string }) {
  const tone = !bucket ? "empty" : bucket.fail ? "fail" : bucket.warn ? "warn" : bucket.pass ? "pass" : "unknown";
  const title = bucket
    ? `${formatTime(stamp)} · PASS ${bucket.pass} / WARN ${bucket.warn} / FAIL ${bucket.fail} · P95 ${bucket.p95_e2e_ms ?? "—"}ms`
    : `${formatTime(stamp)} · 无样本`;
  return <i className={`heat-cell heat-${tone}`} title={title}><b style={{ opacity: bucket ? Math.max(.35, bucket.pass_rate / 100) : .1 }} /></i>;
}

function formatInterval(seconds: number): string {
  if (seconds % 86_400 === 0) return `${seconds / 86_400} 天`;
  if (seconds % 3_600 === 0) return `${seconds / 3_600} 小时`;
  return `${Math.round(seconds / 60)} 分钟`;
}

const probeOptions: Array<[ProbeType, string]> = [
  ["chat", "文本聊天"],
  ["vision", "视觉理解"],
  ["embedding", "向量嵌入"],
  ["image_generation", "图片生成"],
  ["audio_speech", "语音合成"],
  ["audio_transcription", "语音转写"],
];
