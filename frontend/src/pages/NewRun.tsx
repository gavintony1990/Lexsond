import { useEffect, useMemo, useRef, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Cloud, Cpu, Fingerprint, KeyRound, Play, Radio, Server, ShieldCheck, Waves } from "lucide-react";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
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
  max_output_tokens: z.number().int().min(1).max(4096),
  api_key: z.string().max(8192),
  credential_profile_id: z.string(),
});
type RunForm = z.infer<typeof schema>;
type DurableRunForm = Omit<RunForm, "api_key">;

export function NewRun() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const targets = useQuery({ queryKey: ["targets", false], queryFn: () => api.targets(false) });
  const suites = useQuery({ queryKey: ["suites", false], queryFn: () => api.suites(false) });
  const profiles = useQuery({ queryKey: ["credential-profiles", false], queryFn: () => api.credentialProfiles(false) });
  const [credentialMode, setCredentialMode] = useState<"temporary" | "saved">("temporary");
  const form = useForm<RunForm>({ resolver: zodResolver(schema), defaultValues: { target_id: "", run_kind: "component", probe_type: "chat", suite_revision_id: "", execution_backend: "local", model: "", stream: true, timeout_seconds: 30, max_output_tokens: 64, api_key: "", credential_profile_id: "" } });
  const transientKey = useRef<string | null>(null);
  const requestKey = useRef<string | null>(null);
  const appliedTargetParam = useRef<string | null>(null);
  const credentialScope = useRef<string | null>(null);
  const targetId = form.watch("target_id");
  const runKind = form.watch("run_kind");
  const probeType = form.watch("probe_type");
  const backend = form.watch("execution_backend");
  const apiKeyValue = form.watch("api_key");
  const credentialProfileId = form.watch("credential_profile_id");
  const selectedTarget = targets.data?.find((item) => item.id === targetId);
  const selectedSuite = suites.data?.find((item) => item.latest_revision.id === form.watch("suite_revision_id"));
  const temporal = bootstrap.data?.execution_backends.find((item) => item.id === "temporal");
  const component = bootstrap.data?.probe_components.find((item) => item.id === probeType);
  const requestedTargetId = searchParams.get("target");
  const runNeedsKey = backend === "local" && selectedTarget?.target_kind === "cloud"
    && (credentialMode === "temporary" ? !apiKeyValue.trim() : !credentialProfileId);
  const compatibleProfiles = profiles.data?.filter((profile) => profile.status === "ACTIVE" && profile.provider_id === selectedTarget?.provider_id) ?? [];

  useEffect(() => {
    if (!requestedTargetId || appliedTargetParam.current === requestedTargetId || !targets.data?.some((item) => item.id === requestedTargetId)) return;
    appliedTargetParam.current = requestedTargetId;
    form.setValue("target_id", requestedTargetId, { shouldDirty: false, shouldValidate: true });
  }, [form, requestedTargetId, targets.data]);
  useEffect(() => {
    if (selectedTarget?.default_model) form.setValue("model", selectedTarget.default_model);
  }, [selectedTarget, form]);
  useEffect(() => {
    if (runKind === "suite") {
      form.setValue("probe_type", "chat");
      if (selectedSuite) {
        form.setValue("stream", selectedSuite.latest_revision.document.spec.request.stream);
        form.setValue("timeout_seconds", selectedSuite.latest_revision.document.spec.sampling.timeout_seconds);
        form.setValue("max_output_tokens", selectedSuite.latest_revision.document.spec.request.max_output_tokens);
      }
    }
  }, [runKind, selectedSuite, form]);
  useEffect(() => {
    if (probeType !== "chat" && backend === "temporal") form.setValue("execution_backend", "local");
    if (!["chat", "vision"].includes(probeType)) form.setValue("stream", false);
  }, [probeType, backend, form]);
  useEffect(() => {
    const nextScope = `${targetId}:${backend}`;
    if (credentialScope.current === null) {
      credentialScope.current = nextScope;
      return;
    }
    if (credentialScope.current === nextScope) return;
    credentialScope.current = nextScope;
    transientKey.current = null;
    form.setValue("api_key", "");
    form.setValue("credential_profile_id", "");
    form.clearErrors("api_key");
  }, [backend, form, targetId]);

  const start = useMutation({
    gcTime: 0,
    networkMode: "always",
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
        max_output_tokens: value.max_output_tokens,
        api_key: value.execution_backend === "local" && credentialMode === "temporary" ? apiKey : null,
        credential_profile_id: value.execution_backend === "local" && credentialMode === "saved" ? value.credential_profile_id || null : null,
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
    const target = targets.data?.find((item) => item.id === value.target_id);
    if (value.execution_backend === "local" && target?.target_kind === "cloud" && credentialMode === "temporary" && !value.api_key.trim()) {
      form.setError("api_key", { type: "required", message: "云端目标必须输入本次临时 Key" });
      return;
    }
    if (value.execution_backend === "local" && target?.target_kind === "cloud" && credentialMode === "saved" && !value.credential_profile_id) {
      form.setError("credential_profile_id", { type: "required", message: "请选择已保存凭据" });
      return;
    }
    form.clearErrors("api_key");
    transientKey.current = value.execution_backend === "local" && credentialMode === "temporary" ? value.api_key || null : null;
    requestKey.current = crypto.randomUUID();
    form.setValue("api_key", "");
    const { api_key: _discarded, ...durableValue } = value;
    start.mutate(durableValue);
  };
  const totalCalls = useMemo(() => selectedSuite ? selectedSuite.latest_revision.document.spec.sampling.warmup + selectedSuite.latest_revision.document.spec.sampling.requests : 1, [selectedSuite]);

  return (
    <div className="page-stack">
      <PageHead eyebrow="NEW PROBE / 发起探测" title="发起一次探测" description="选择要测试的 API 和能力。提交前会显示最大调用次数，API Key 只用于本次请求。" />
      <form className="run-composer" onSubmit={form.handleSubmit(submit)}>
        <section className="composer-main panel">
          <div className="composer-section"><div className="composer-number">01</div><div className="composer-content"><span className="eyebrow">TARGET / 请求目标</span><h2>先选择要测试的 API</h2><p className="section-help">选择已经保存的服务。默认模型会自动带入，也可以只为本次探测调整。</p><div className="target-selector">{targets.data?.map((target) => <label className={targetId === target.id ? "selected" : ""} key={target.id}><input type="radio" value={target.id} {...form.register("target_id")} /><div className="mini-target-icon">{target.target_kind === "cloud" ? <Cloud size={18} /> : <Server size={18} />}</div><span><b>{target.name}</b><small>{target.base_url}</small></span><i /></label>)}</div>{!targets.data?.length && <div className="inline-empty actionable-empty"><span>还没有可用的 API 目标。</span><Link to="/targets" className="secondary-action">先添加探测目标</Link></div>}<label className="wide-field">本次使用的模型 ID<input {...form.register("model")} placeholder="例如：gpt-4.1-mini 或 qwen3:8b" /></label></div></div>

          <div className="composer-section"><div className="composer-number">02</div><div className="composer-content"><span className="eyebrow">CHECK / 检查内容</span><h2>选择要检查的能力</h2><p className="section-help"><b>第一次建议：</b>选择“单项探针”和“文本聊天”，只发送一次请求。套件适合后续做多次采样和质量断言。</p><div className="segmented"><label className={runKind === "component" ? "active" : ""}><input type="radio" value="component" {...form.register("run_kind")} /><Cpu size={16} />单项探针（推荐）</label><label className={runKind === "suite" ? "active" : ""}><input type="radio" value="suite" {...form.register("run_kind")} /><Fingerprint size={16} />多次采样套件</label></div>{runKind === "component" ? <div className="component-picker">{bootstrap.data?.probe_components.map((item) => <label className={probeType === item.id ? "selected" : ""} key={item.id}><input type="radio" value={item.id} {...form.register("probe_type")} /><b>{item.icon}</b><span>{item.label}<small>{item.scenario}</small></span><i /></label>)}</div> : <div className="suite-picker">{suites.data?.map((suite) => <label className={form.watch("suite_revision_id") === suite.latest_revision.id ? "selected" : ""} key={suite.id}><input type="radio" value={suite.latest_revision.id} {...form.register("suite_revision_id")} /><span><b>{suite.name}</b><small>R{suite.latest_revision.revision} · {suite.latest_revision.document.spec.sampling.requests} 次正式采样</small></span><strong>${suite.latest_revision.document.spec.sampling.max_cost_usd}</strong></label>)}</div>}</div></div>

          <div className="composer-section"><div className="composer-number">03</div><div className="composer-content"><span className="eyebrow">RUN / 运行方式</span><h2>选择从哪里发出请求</h2><p className="section-help">第一次直接使用本地执行器。Temporal 只适合已部署 Worker、并配置了凭据引用的长期任务。</p><div className="backend-picker"><label className={backend === "local" ? "selected" : ""}><input type="radio" value="local" {...form.register("execution_backend")} /><Radio size={20} /><span><b>本地执行器（推荐）</b><small>由当前服务直接请求目标 · Key 用完即清空</small></span><i className="ready">READY</i></label><label className={`${backend === "temporal" ? "selected" : ""} ${!temporal?.available || (runKind === "component" && probeType !== "chat") ? "disabled" : ""}`}><input type="radio" value="temporal" {...form.register("execution_backend")} disabled={!temporal?.available || (runKind === "component" && probeType !== "chat")} /><Waves size={20} /><span><b>Temporal 工作流（高级）</b><small>需要独立 Worker 与 credential_ref</small></span><i>{temporal?.available ? "READY" : "OFFLINE"}</i></label></div>
            {backend === "local" && selectedTarget && <><div className="segmented compact"><label className={credentialMode === "temporary" ? "active" : ""}><input type="radio" checked={credentialMode === "temporary"} onChange={() => { setCredentialMode("temporary"); form.setValue("credential_profile_id", ""); }} /><KeyRound size={15} />仅本次使用</label><label className={credentialMode === "saved" ? "active" : ""}><input type="radio" checked={credentialMode === "saved"} onChange={() => { setCredentialMode("saved"); form.setValue("api_key", ""); }} /><ShieldCheck size={15} />已保存凭据</label></div>{credentialMode === "temporary" ? <label className={`secret-field ${selectedTarget.target_kind === "cloud" && !runNeedsKey ? "ready" : ""}`}><KeyRound size={17} /><span><b>{selectedTarget.target_kind === "cloud" ? "本次 API Key" : "可选本地鉴权 Key"}</b><small>{selectedTarget.target_kind === "cloud" ? runNeedsKey ? "云端目标必须输入本次临时 Key" : "临时 Key 已就绪 · 提交后立即清空" : "只存在于当前请求和执行线程"}</small></span><input type="password" autoComplete="off" placeholder={selectedTarget.target_kind === "cloud" ? "请粘贴本次临时 API Key" : "可选"} {...form.register("api_key", { onChange: () => form.clearErrors("api_key") })} /></label> : <label className={`secret-field ${credentialProfileId ? "ready" : ""}`}><ShieldCheck size={17} /><span><b>系统密钥库凭据</b><small>服务端仅按引用在执行边界读取，不返回完整 Key</small></span><select {...form.register("credential_profile_id", { onChange: () => form.clearErrors("credential_profile_id") })}><option value="">选择匹配 Provider 的凭据</option>{compatibleProfiles.map((profile) => <option value={profile.id} key={profile.id}>{profile.label} · ••••{profile.masked_suffix}</option>)}</select></label>}</>}
            {backend === "temporal" && !selectedTarget?.credential_ref_configured && <div className="warning-notice">该目标没有 credential_ref，不能交给独立 Worker 执行。</div>}
            <div className="form-row three"><label>超时（秒）<input type="number" step="0.1" {...form.register("timeout_seconds", { valueAsNumber: true })} /></label><label>最大输出 Token<input type="number" {...form.register("max_output_tokens", { valueAsNumber: true })} disabled={runKind === "suite"} /></label><label className="check-field"><span>请求流式响应</span><input type="checkbox" {...form.register("stream")} disabled={!(["chat", "vision"] as ProbeType[]).includes(probeType)} /></label></div>
          </div></div>
        </section>

        <aside className="composer-summary panel">
          <span className="eyebrow">BEFORE START / 提交前确认</span><h2>本次会做什么</h2>
          <div className="manifest-radar"><i /><i /><i /><b>{component?.icon ?? "AI"}</b></div>
          <dl><div><dt>目标</dt><dd>{selectedTarget?.name ?? "未选择"}</dd></div><div><dt>模型</dt><dd>{form.watch("model") || "未指定"}</dd></div><div><dt>探针</dt><dd>{runKind === "suite" ? selectedSuite?.name ?? "未选择套件" : component?.label ?? "—"}</dd></div><div><dt>执行器</dt><dd>{backend.toUpperCase()}</dd></div><div><dt>最大调用</dt><dd>{totalCalls} CALL{totalCalls > 1 ? "S" : ""}</dd></div></dl>
          <div className="safety-seal"><ShieldCheck size={20} /><p><b>NO SECRET PERSISTENCE</b><span>Key、Authorization 与原始输出不会写入运行历史。</span></p></div>
          {start.error && <ErrorNotice error={start.error} />}
          {Object.keys(form.formState.errors).length > 0 && <div className="error-notice">请补全目标、模型、套件和安全边界。</div>}
          <button className="launch-button" disabled={start.isPending || !selectedTarget || runNeedsKey || (backend === "temporal" && (!temporal?.available || !selectedTarget.credential_ref_configured))}><Play size={18} fill="currentColor" />{start.isPending ? "正在发起探测…" : `开始探测 · 最多 ${totalCalls} 次调用`}<ArrowRight size={17} /></button>
          <small className="launch-caption">不会自动增加调用次数；失败后也不会隐藏重试。</small>
        </aside>
      </form>
    </div>
  );
}
