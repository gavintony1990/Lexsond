import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Archive, ArrowLeft, Ban, Check, Circle, Clock3, Copy, Database, Radio, ShieldCheck, TriangleAlert, Waves } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, subscribeToRun } from "../api";
import type { RunEvent } from "../types";
import { ErrorNotice, formatTime, PageHead, StatusPill } from "../ui";

export function RunDetail() {
  const { runId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId, true),
    enabled: !!runId,
    refetchInterval: (query) => query.state.data?.state === "RUNNING" ? 700 : false,
  });
  useEffect(() => {
    if (!runId || run.data?.state !== "RUNNING") return;
    return subscribeToRun(runId, (event) => {
      setEvents((current) => current.some((item) => item.event_id === event.event_id) ? current : [...current, event]);
      queryClient.invalidateQueries({ queryKey: ["run", runId] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
    });
  }, [runId, run.data?.state, queryClient]);
  const action = useMutation({
    mutationFn: async (kind: "cancel" | "archive") => kind === "cancel" ? api.cancelRun(runId) : api.archiveRun(runId),
    onSuccess: (value) => {
      queryClient.setQueryData(["run", runId], value);
      if (value.archived_at) navigate("/runs");
    },
  });
  const value = run.data;
  const scores = value?.result?.dimension_scores ?? [];
  const measurements = value?.result?.measurements ?? [];
  const measurement = measurements[0];
  const overall = useMemo(() => {
    const numeric = scores.map((item) => item.score).filter((item): item is number => typeof item === "number");
    return numeric.length ? Math.round(numeric.reduce((sum, item) => sum + item, 0) / numeric.length) : value?.result_status === "PASS" ? 100 : value?.result_status === "WARN" ? 70 : value?.result_status === "FAIL" ? 30 : 0;
  }, [scores, value?.result_status]);

  if (run.error) return <div className="page-stack"><ErrorNotice error={run.error} /><Link to="/runs" className="secondary-action"><ArrowLeft size={15} />返回运行列表</Link></div>;
  if (!value) return <div className="loading-depth"><Waves size={34} /><span>正在读取深度记录…</span></div>;

  return (
    <div className="page-stack">
      <div className="detail-back"><Link to="/runs"><ArrowLeft size={15} />运行档案</Link><code>{value.run_id}</code><button className="icon-button" onClick={() => navigator.clipboard?.writeText(value.run_id)}><Copy size={14} /></button></div>
      <PageHead eyebrow={`RUN PROFILE / ${value.execution_backend.toUpperCase()}`} title={value.config.model} description={`${value.config.probe_type.replaceAll("_", " ")} · ${value.config.base_url}`} action={<div className="detail-actions">{value.state === "RUNNING" ? <button className="danger-action" disabled={Boolean(value.cancel_requested_at)} onClick={() => action.mutate("cancel")}><Ban size={15} />{value.cancel_requested_at ? "正在投递取消…" : "取消运行"}</button> : !value.archived_at && <button className="secondary-action" onClick={() => action.mutate("archive")}><Archive size={15} />归档</button>}<StatusPill status={value.cancel_requested_at && value.state === "RUNNING" ? "CANCEL_REQUESTED" : value.result_status ?? value.state} /></div>} />
      {action.error && <ErrorNotice error={action.error} />}

      <section className="detail-hero-grid">
        <article className="panel score-orbit-panel">
          <div className={`score-orbit score-${(value.result_status ?? value.state).toLowerCase()}`} style={{ "--score": overall } as React.CSSProperties}><i /><i /><div><span>QUALITY INDEX</span><strong>{value.state === "RUNNING" ? "··" : overall}</strong><small>/ 100</small></div></div>
          <div className="score-caption"><Radio size={15} /><span>{value.state === "RUNNING" ? "正在接收探针回波" : `证据状态 ${value.result_status ?? value.state}`}</span></div>
        </article>
        <article className="panel telemetry-panel">
          <header className="panel-head"><div><span className="eyebrow">TELEMETRY</span><h2>调用遥测</h2></div><Activity size={18} /></header>
          <div className="telemetry-grid"><Telemetry label="HTTP" value={measurement?.status_code ?? "—"} unit="STATUS" /><Telemetry label="TTFB" value={formatMetric(measurement?.ttfb_ms)} unit="MS" /><Telemetry label="TTFT" value={formatMetric(measurement?.ttft_ms)} unit="MS" /><Telemetry label="E2E" value={formatMetric(measurement?.e2e_ms)} unit="MS" /></div>
          <div className="telemetry-footer"><span><Clock3 size={13} />START {formatTime(value.created_at)}</span><span><Clock3 size={13} />END {formatTime(value.finished_at)}</span></div>
        </article>
        <article className="panel config-seal-panel">
          <header className="panel-head"><div><span className="eyebrow">FROZEN MANIFEST</span><h2>配置快照</h2></div><ShieldCheck size={18} /></header>
          <dl><div><dt>执行器</dt><dd>{value.execution_backend.toUpperCase()}</dd></div><div><dt>运行类型</dt><dd>{value.run_kind.toUpperCase()}</dd></div><div><dt>STREAM</dt><dd>{value.config.stream ? "ENABLED" : "DISABLED"}</dd></div><div><dt>TIMEOUT</dt><dd>{value.config.timeout_seconds}s</dd></div><div><dt>PROVIDER</dt><dd>{value.config.provider_id ?? "CUSTOM"}</dd></div><div><dt>SECRET</dt><dd className="safe-text">NOT STORED</dd></div></dl>
        </article>
      </section>

      <section className="panel timeline-panel">
        <header className="panel-head"><div><span className="eyebrow">WORKFLOW DEPTH</span><h2>动态执行时间线</h2></div><span className="timeline-count">{value.workflow?.steps.length ?? events.length} STAGES</span></header>
        <div className="workflow-timeline">{value.workflow?.steps.map((step, index) => <div className={`timeline-step step-${step.status.toLowerCase()}`} key={step.id}><div className="timeline-axis"><span>{String(index + 1).padStart(2, "0")}</span><i>{step.status === "PASS" ? <Check size={13} /> : step.status === "FAIL" ? <TriangleAlert size={13} /> : <Circle size={10} />}</i><b /></div><div className="timeline-content"><header><strong>{step.label}</strong><StatusPill status={step.status} /></header><p>{step.description}</p>{step.facts && step.facts.length > 0 && <div className="fact-list">{step.facts.map((fact) => <code key={fact}>{fact}</code>)}</div>}<small>{formatTime(step.started_at)} → {formatTime(step.finished_at)}</small></div></div>)}</div>
      </section>

      <section className="evidence-grid">
        <article className="panel dimension-panel"><header className="panel-head"><div><span className="eyebrow">DIMENSION SCORES</span><h2>质量维度</h2></div></header>{scores.length ? <div className="dimension-list">{scores.map((score) => <div className="dimension-row" key={score.dimension}><span>{score.dimension.toUpperCase()}</span><div><i style={{ width: `${score.score ?? 0}%` }} /></div><strong>{score.score ?? "—"}</strong><StatusPill status={score.status} /></div>)}</div> : <div className="inline-empty">等待评分阶段完成。</div>}</article>
        <article className="panel diagnostic-panel"><header className="panel-head"><div><span className="eyebrow">DIAGNOSTICS</span><h2>诊断与证据边界</h2></div><Database size={18} /></header><div className="diagnostic-list"><div><span>FAILURE CODE</span><b className={value.failure_code ? "danger-text" : "safe-text"}>{value.failure_code ?? "NONE"}</b></div><div><span>ERROR CLASS</span><b>{measurement?.error_class ?? "NONE"}</b></div><div><span>RAW RESPONSE</span><b className="safe-text">DISCARDED</b></div><div><span>AUTHORIZATION</span><b className="safe-text">EPHEMERAL</b></div></div>{value.result?.reason_codes?.length ? <div className="reason-codes">{value.result.reason_codes.map((code) => <code key={code}>{code}</code>)}</div> : null}</article>
      </section>
    </div>
  );
}

function Telemetry({ label, value, unit }: { label: string; value: string | number; unit: string }) {
  return <div className="telemetry-cell"><span>{label}</span><strong>{value}</strong><small>{unit}</small><i /></div>;
}

function formatMetric(value: number | null | undefined): string | number {
  return typeof value === "number" ? value.toFixed(1) : "—";
}
