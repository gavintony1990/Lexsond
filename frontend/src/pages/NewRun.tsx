import { useEffect, useMemo, useRef } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Cloud, Cpu, Fingerprint, KeyRound, Play, Radio, Server, ShieldCheck, Waves } from "lucide-react";
import { useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { z } from "zod";
import { api } from "../api";
import type { ProbeType } from "../types";
import { ErrorNotice, PageHead } from "../ui";

const schema = z.object({
  target_id: z.string().min(1, "请选择目标"),
  run_kind: z.enum(["component", "suite"]),
  probe_type: z.enum(["chat", "vision", "embedding", "image_generation", "audio_speech", "audio_transcription"]),
  suite_revision_id: z.string(),
  execution_backend: z.enum(["local", "temporal"]),
  model: z.string().min(1, "请输入模型 ID").max(256),
  stream: z.boolean(),
  timeout_seconds: z.number().min(0.1).max(300),
  api_key: z.string().max(8192),
});
type RunForm = z.infer<typeof schema>;
type DurableRunForm = Omit<RunForm, "api_key">;

export function NewRun() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const targets = useQuery({ queryKey: ["targets", false], queryFn: () => api.targets(false) });
  const suites = useQuery({ queryKey: ["suites", false], queryFn: () => api.suites(false) });
  const form = useForm<RunForm>({ resolver: zodResolver(schema), defaultValues: { target_id: "", run_kind: "component", probe_type: "chat", suite_revision_id: "", execution_backend: "local", model: "", stream: true, timeout_seconds: 30, api_key: "" } });
  const transientKey = useRef<string | null>(null);
  const requestKey = useRef<string | null>(null);
  const targetId = form.watch("target_id");
  const runKind = form.watch("run_kind");
  const probeType = form.watch("probe_type");
  const backend = form.watch("execution_backend");
  const selectedTarget = targets.data?.find((item) => item.id === targetId);
  const selectedSuite = suites.data?.find((item) => item.latest_revision.id === form.watch("suite_revision_id"));
  const temporal = bootstrap.data?.execution_backends.find((item) => item.id === "temporal");
  const component = bootstrap.data?.probe_components.find((item) => item.id === probeType);

  useEffect(() => {
    if (selectedTarget?.default_model) form.setValue("model", selectedTarget.default_model);
  }, [selectedTarget, form]);
  useEffect(() => {
    if (runKind === "suite") {
      form.setValue("probe_type", "chat");
      if (selectedSuite) {
        form.setValue("stream", selectedSuite.latest_revision.document.spec.request.stream);
        form.setValue("timeout_seconds", selectedSuite.latest_revision.document.spec.sampling.timeout_seconds);
      }
    }
  }, [runKind, selectedSuite, form]);
  useEffect(() => {
    if (probeType !== "chat" && backend === "temporal") form.setValue("execution_backend", "local");
    if (!["chat", "vision"].includes(probeType)) form.setValue("stream", false);
  }, [probeType, backend, form]);

  const start = useMutation({
    gcTime: 0,
    mutationFn: async (value: DurableRunForm) => {
      const apiKey = transientKey.current;
      transientKey.current = null;
      const idempotencyKey = requestKey.current;
      if (!idempotencyKey) throw new Error("missing run idempotency key");
      return api.createRun({
      target_id: value.target_id,
      run_kind: value.run_kind,
      probe_type: value.run_kind === "suite" ? "chat" : value.probe_type,
      suite_revision_id: value.run_kind === "suite" ? value.suite_revision_id : null,
      execution_backend: value.execution_backend,
      model: value.model,
      stream: value.stream,
      timeout_seconds: value.timeout_seconds,
      api_key: value.execution_backend === "local" ? apiKey : null,
      }, idempotencyKey);
    },
    onSuccess: (run) => {
      requestKey.current = null;
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/runs/${run.run_id}`);
    },
    onSettled: () => {
      transientKey.current = null;
      form.setValue("api_key", "");
    },
  });
  const submit = (value: RunForm) => {
    transientKey.current = value.execution_backend === "local" ? value.api_key || null : null;
    requestKey.current = crypto.randomUUID();
    form.setValue("api_key", "");
    const { api_key: _discarded, ...durableValue } = value;
    start.mutate(durableValue);
  };
  const totalCalls = useMemo(() => selectedSuite ? selectedSuite.latest_revision.document.spec.sampling.warmup + selectedSuite.latest_revision.document.spec.sampling.requests : 1, [selectedSuite]);

  return (
    <div className="page-stack">
      <PageHead eyebrow="NEW DESCENT / 发起下潜" title="编排一次有界质量探测" description="先冻结目标、模态与预算，再让执行器接触真实 API。页面不会记住本次输入的 Key。" />
      <form className="run-composer" onSubmit={form.handleSubmit(submit)}>
        <section className="composer-main panel">
          <div className="composer-section"><div className="composer-number">01</div><div className="composer-content"><span className="eyebrow">TARGET BEACON</span><h2>选择目标与模型</h2><div className="target-selector">{targets.data?.map((target) => <label className={targetId === target.id ? "selected" : ""} key={target.id}><input type="radio" value={target.id} {...form.register("target_id")} /><div className="mini-target-icon">{target.target_kind === "cloud" ? <Cloud size={18} /> : <Server size={18} />}</div><span><b>{target.name}</b><small>{target.base_url}</small></span><i /></label>)}</div>{!targets.data?.length && <div className="inline-empty">先到“探测目标”页面添加至少一个目标。</div>}<label className="wide-field">模型 ID<input {...form.register("model")} placeholder="model-id" /></label></div></div>

          <div className="composer-section"><div className="composer-number">02</div><div className="composer-content"><span className="eyebrow">PROBE ENVELOPE</span><h2>选择单项探针或套件</h2><div className="segmented"><label className={runKind === "component" ? "active" : ""}><input type="radio" value="component" {...form.register("run_kind")} /><Cpu size={16} />单项探针</label><label className={runKind === "suite" ? "active" : ""}><input type="radio" value="suite" {...form.register("run_kind")} /><Fingerprint size={16} />聊天套件</label></div>{runKind === "component" ? <div className="component-picker">{bootstrap.data?.probe_components.map((item) => <label className={probeType === item.id ? "selected" : ""} key={item.id}><input type="radio" value={item.id} {...form.register("probe_type")} /><b>{item.icon}</b><span>{item.label}<small>{item.scenario}</small></span><i /></label>)}</div> : <div className="suite-picker">{suites.data?.map((suite) => <label className={form.watch("suite_revision_id") === suite.latest_revision.id ? "selected" : ""} key={suite.id}><input type="radio" value={suite.latest_revision.id} {...form.register("suite_revision_id")} /><span><b>{suite.name}</b><small>R{suite.latest_revision.revision} · {suite.latest_revision.document.spec.sampling.requests} samples</small></span><strong>${suite.latest_revision.document.spec.sampling.max_cost_usd}</strong></label>)}</div>}</div></div>

          <div className="composer-section"><div className="composer-number">03</div><div className="composer-content"><span className="eyebrow">EXECUTION FABRIC</span><h2>选择执行器与临时认证</h2><div className="backend-picker"><label className={backend === "local" ? "selected" : ""}><input type="radio" value="local" {...form.register("execution_backend")} /><Radio size={20} /><span><b>本地执行器</b><small>六模态 · 进程内存临时 Key</small></span><i className="ready">READY</i></label><label className={`${backend === "temporal" ? "selected" : ""} ${!temporal?.available || (runKind === "component" && probeType !== "chat") ? "disabled" : ""}`}><input type="radio" value="temporal" {...form.register("execution_backend")} disabled={!temporal?.available || (runKind === "component" && probeType !== "chat")} /><Waves size={20} /><span><b>Temporal 工作流</b><small>聊天与套件 · credential_ref</small></span><i>{temporal?.available ? "READY" : "OFFLINE"}</i></label></div>
            {backend === "local" && selectedTarget && <label className="secret-field"><KeyRound size={17} /><span><b>{selectedTarget.target_kind === "cloud" ? "本次 API Key" : "可选本地鉴权 Key"}</b><small>只存在于当前请求和执行线程，完成后清空</small></span><input type="password" autoComplete="off" placeholder="sk-••••••••" {...form.register("api_key")} /></label>}
            {backend === "temporal" && !selectedTarget?.credential_ref_configured && <div className="warning-notice">该目标没有 credential_ref，不能交给独立 Worker 执行。</div>}
            <div className="form-row"><label>超时（秒）<input type="number" step="0.1" {...form.register("timeout_seconds", { valueAsNumber: true })} /></label><label className="check-field"><span>请求流式响应</span><input type="checkbox" {...form.register("stream")} disabled={!(["chat", "vision"] as ProbeType[]).includes(probeType)} /></label></div>
          </div></div>
        </section>

        <aside className="composer-summary panel">
          <span className="eyebrow">DESCENT MANIFEST</span><h2>运行清单</h2>
          <div className="manifest-radar"><i /><i /><i /><b>{component?.icon ?? "AI"}</b></div>
          <dl><div><dt>目标</dt><dd>{selectedTarget?.name ?? "未选择"}</dd></div><div><dt>模型</dt><dd>{form.watch("model") || "未指定"}</dd></div><div><dt>探针</dt><dd>{runKind === "suite" ? selectedSuite?.name ?? "未选择套件" : component?.label ?? "—"}</dd></div><div><dt>执行器</dt><dd>{backend.toUpperCase()}</dd></div><div><dt>最大调用</dt><dd>{totalCalls} CALL{totalCalls > 1 ? "S" : ""}</dd></div></dl>
          <div className="safety-seal"><ShieldCheck size={20} /><p><b>NO SECRET PERSISTENCE</b><span>Key、Authorization 与原始输出不会写入运行历史。</span></p></div>
          {start.error && <ErrorNotice error={start.error} />}
          {Object.keys(form.formState.errors).length > 0 && <div className="error-notice">请补全目标、模型、套件和安全边界。</div>}
          <button className="launch-button" disabled={start.isPending || !selectedTarget || (backend === "temporal" && (!temporal?.available || !selectedTarget.credential_ref_configured))}><Play size={18} fill="currentColor" />{start.isPending ? "正在建立链路…" : "冻结配置并发起"}<ArrowRight size={17} /></button>
          <small className="launch-caption">一次执行尝试 · 无 LangChain 隐藏重试</small>
        </aside>
      </form>
    </div>
  );
}
