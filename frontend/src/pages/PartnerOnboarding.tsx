import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileCheck2, Globe2, Send, ShieldCheck } from "lucide-react";
import { api } from "../api";
import type { PartnerApplicationInput } from "../types";
import { EmptyState, ErrorNotice, PageHead, StatusPill, formatTime } from "../ui";

const initialForm: PartnerApplicationInput = {
  site_name: "", website_url: "", terms_url: "", privacy_url: "",
  contact_email: "", api_base_url: "", protocol: "openai-compatible",
  region: "", model_claims: [], pricing_notes: "", source_evidence_url: "",
  monitoring_credential_id: null,
};

export function PartnerOnboarding() {
  const queryClient = useQueryClient();
  const applications = useQuery({ queryKey: ["partner-applications"], queryFn: api.partnerApplications });
  const credentials = useQuery({ queryKey: ["credential-profiles"], queryFn: () => api.credentialProfiles(false) });
  const [form, setForm] = useState(initialForm);
  const [modelsText, setModelsText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  function field<K extends keyof PartnerApplicationInput>(key: K, value: PartnerApplicationInput[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault(); setSaving(true); setError(null); setNotice(null);
    try {
      const model_claims = modelsText.split(",").map((value) => value.trim()).filter(Boolean);
      await api.createPartnerApplication({ ...form, model_claims }, crypto.randomUUID());
      setForm(initialForm); setModelsText(""); setNotice("草稿已保存；提交后会生成不可变审核快照");
      await queryClient.invalidateQueries({ queryKey: ["partner-applications"] });
    } catch (cause) { setError(cause instanceof Error ? cause : new Error("草稿保存失败")); }
    finally { setSaving(false); }
  }

  async function submit(id: string, version: number) {
    if (!window.confirm("提交后当前版本将冻结，进入所有权验证流程。确认提交？")) return;
    setError(null);
    try {
      await api.submitPartnerApplication(id, version);
      await queryClient.invalidateQueries({ queryKey: ["partner-applications"] });
    } catch (cause) { setError(cause instanceof Error ? cause : new Error("提交失败")); }
  }

  return <div className="page-stack partner-onboarding-page">
    <PageHead eyebrow="PARTNER INTAKE" title="合作中转站入驻" description="先保存工作区私有草稿；提交时冻结版本，再进入域名所有权验证、人工审核、基线测试与观察期。" />
    <section className="partner-flow" aria-label="入驻状态流程">{["DRAFT", "SUBMITTED", "OWNERSHIP_PENDING", "MANUAL_REVIEW", "BASELINE_TEST", "PROBATION", "APPROVED / REJECTED", "PUBLISHED"].map((step, index) => <span key={step}><b>{String(index + 1).padStart(2, "0")}</b>{step}</span>)}</section>
    <section className="partner-layout">
      <form className="panel partner-form" onSubmit={save}>
        <header><span className="eyebrow">NEW DRAFT</span><h2>中转站资料</h2><p>请使用专用低余额监控 Key；这里只选择已保存凭据元数据，不接收明文 Key。</p></header>
        <div className="form-row"><label>站点名称<input required maxLength={120} value={form.site_name} onChange={(event) => field("site_name", event.target.value)} /></label><label>联系人邮箱<input required type="email" maxLength={320} value={form.contact_email} onChange={(event) => field("contact_email", event.target.value)} /></label></div>
        <label>官网<input required type="url" placeholder="https://" value={form.website_url} onChange={(event) => field("website_url", event.target.value)} /></label>
        <div className="form-row"><label>服务条款<input required type="url" placeholder="https://" value={form.terms_url} onChange={(event) => field("terms_url", event.target.value)} /></label><label>隐私政策<input required type="url" placeholder="https://" value={form.privacy_url} onChange={(event) => field("privacy_url", event.target.value)} /></label></div>
        <label>API Base URL<input required type="url" placeholder="https://relay.example.com/v1" value={form.api_base_url} onChange={(event) => field("api_base_url", event.target.value)} /></label>
        <div className="form-row three"><label>协议<select value={form.protocol} onChange={(event) => field("protocol", event.target.value as PartnerApplicationInput["protocol"])}><option value="openai-compatible">OpenAI compatible</option><option value="anthropic-messages">Anthropic Messages</option><option value="gemini-native">Gemini native</option></select></label><label>地区<input required value={form.region} onChange={(event) => field("region", event.target.value)} placeholder="cn-beijing" /></label><label>专用监控凭据<select value={form.monitoring_credential_id ?? ""} onChange={(event) => field("monitoring_credential_id", event.target.value || null)}><option value="">暂不绑定</option>{credentials.data?.map((credential) => <option key={credential.id} value={credential.id}>{credential.label} · ••••{credential.masked_suffix}</option>)}</select></label></div>
        <label>模型声明（逗号分隔，最多 100 个）<textarea required value={modelsText} onChange={(event) => setModelsText(event.target.value)} placeholder="model-a, model-b" /></label>
        <label>价格与限流说明<textarea required maxLength={4000} value={form.pricing_notes} onChange={(event) => field("pricing_notes", event.target.value)} /></label>
        <label>价格/模型声明证据 URL<input required type="url" placeholder="https://" value={form.source_evidence_url} onChange={(event) => field("source_evidence_url", event.target.value)} /></label>
        {notice && <div className="inline-success"><ShieldCheck size={16} />{notice}</div>}{error && <ErrorNotice error={error} />}
        <button className="primary-action" disabled={saving} type="submit"><FileCheck2 size={16} />{saving ? "正在保存…" : "保存私有草稿"}</button>
      </form>
      <section className="panel partner-list"><header><span className="eyebrow">WORKSPACE APPLICATIONS</span><h2>当前工作区申请</h2></header>{applications.isError && <ErrorNotice error={applications.error} />}{!applications.isLoading && (applications.data?.length ?? 0) === 0 && <EmptyState icon={Globe2} title="还没有入驻草稿" body="保存后只有当前工作区成员可见；不会自动公开。" />}<div>{applications.data?.map((application) => <article key={application.id}><header><div><h3>{application.site_name}</h3><p>{application.api_base_url}</p></div><StatusPill status={application.status} /></header><dl><div><dt>协议 / 地区</dt><dd>{application.protocol} · {application.region}</dd></div><div><dt>模型声明</dt><dd>{application.model_claims.length}</dd></div><div><dt>更新时间</dt><dd>{formatTime(application.updated_at)}</dd></div></dl>{application.status === "DRAFT" && <button className="secondary-action" onClick={() => void submit(application.id, application.version)}><Send size={15} />提交并冻结审核快照</button>}</article>)}</div></section>
    </section>
  </div>;
}
