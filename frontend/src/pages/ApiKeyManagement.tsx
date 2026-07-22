import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardPaste, Eye, EyeOff, KeyRound, LockKeyhole, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { NavLink } from "react-router-dom";
import { api, ApiError } from "../api";
import { parseClipboardCredential } from "../credentialClipboard";
import { EmptyState, ErrorNotice, PageHead, StatusPill, formatTime } from "../ui";

const tabs = [
  ["/api-keys/credentials", "密钥"],
  ["/api-keys/channels", "渠道"],
  ["/api-keys/vendors", "模型厂商"],
  ["/api-keys/sources", "模型来源"],
] as const;

export function ApiKeyTabs() {
  return <nav className="module-tabs" aria-label="API Key 管理二级导航">{tabs.map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}</nav>;
}

export function CredentialsPage() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [label, setLabel] = useState("");
  const [providerId, setProviderId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [clearClipboard, setClearClipboard] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const status = useQuery({ queryKey: ["credential-vault-status"], queryFn: api.credentialVaultStatus });
  const profiles = useQuery({ queryKey: ["credential-profiles"], queryFn: () => api.credentialProfiles(false) });
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const cloudProviders = (bootstrap.data?.providers ?? []).filter((provider) => provider.target_kind === "cloud");

  async function pasteOnce() {
    setError(null);
    setNotice(null);
    if (!window.isSecureContext || !navigator.clipboard?.readText) {
      setError(new Error("当前浏览器不允许安全读取剪贴板，请在下方手动粘贴"));
      inputRef.current?.focus();
      return;
    }
    try {
      const value = parseClipboardCredential(await navigator.clipboard.readText());
      setApiKey(value);
      setNotice("已读取一次剪贴板并完成格式清洗；尚未发送任何网络请求");
      inputRef.current?.focus();
    } catch (cause) {
      const message = typeof cause === "object" && cause !== null && "message" in cause
        ? String(cause.message)
        : "浏览器拒绝了剪贴板读取，请手动粘贴";
      setError(new Error(message));
      inputRef.current?.focus();
    }
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      await api.createCredentialProfile(
        { label, provider_id: providerId, api_key: apiKey },
        crypto.randomUUID(),
      );
      if (inputRef.current) inputRef.current.value = "";
      setApiKey("");
      setRevealed(false);
      setLabel("");
      if (clearClipboard && navigator.clipboard?.writeText) {
        try { await navigator.clipboard.writeText(""); }
        catch { setNotice("密钥已保存，但浏览器未允许清空系统剪贴板"); }
      }
      await queryClient.invalidateQueries({ queryKey: ["credential-profiles"] });
      setNotice((current) => current ?? "已保存到系统密钥库；Lexsond 不提供完整 Key 查看或导出");
    } catch (cause) {
      if (inputRef.current) inputRef.current.value = "";
      setApiKey("");
      setRevealed(false);
      setError(toDisplayError(cause));
    } finally {
      setSubmitting(false);
    }
  }

  async function remove(id: string, version: number) {
    if (!window.confirm("删除后无法从 Lexsond 恢复完整 API Key。确认继续？")) return;
    setError(null);
    try {
      await api.archiveCredentialProfile(id, version);
      await queryClient.invalidateQueries({ queryKey: ["credential-profiles"] });
    } catch (cause) { setError(toDisplayError(cause)); }
  }

  const vaultAvailable = status.data?.available === true;
  return <div className="page-stack api-key-page">
    <PageHead eyebrow="CREDENTIAL CONTROL" title="API Key 管理" description="密钥材料进入系统密钥库；PostgreSQL 只保存工作区隔离的掩码元数据。" />
    <ApiKeyTabs />
    <section className={`vault-status-card ${vaultAvailable ? "is-ready" : "is-unavailable"}`}>
      <div><ShieldCheck size={19} /><p><b>{vaultAvailable ? "安全存储可用" : "安全存储不可用"}</b><span>{status.data?.backend ?? "检测中"} · {status.data?.storage_backend ?? "—"}</span></p></div>
      <StatusPill status={vaultAvailable ? "READY" : "UNAVAILABLE"} />
      {!vaultAvailable && status.data?.reason && <small>{status.data.reason}</small>}
    </section>
    <section className="credential-layout">
      <form className="panel credential-create" onSubmit={submit} autoComplete="off">
        <header><span className="eyebrow">ADD CREDENTIAL</span><h2>粘贴并安全保存</h2><p>只在点击按钮时读取一次剪贴板；共享前缀不会被发送到多个 Provider 试探。</p></header>
        <label>名称<input value={label} maxLength={120} required onChange={(event) => setLabel(event.target.value)} placeholder="例如：OpenAI 生产低额度 Key" /></label>
        <label>Provider<select value={providerId} required onChange={(event) => setProviderId(event.target.value)}><option value="">选择并确认 Provider</option>{cloudProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select></label>
        <label>API Key<div className="secret-entry"><input ref={inputRef} type={revealed ? "text" : "password"} value={apiKey} required maxLength={8192} autoComplete="off" spellCheck={false} onChange={(event) => setApiKey(event.target.value)} placeholder="手动粘贴，或点击右侧按钮" /><button type="button" onClick={() => setRevealed((value) => !value)} aria-label={revealed ? "隐藏 API Key" : "短暂显示 API Key"}>{revealed ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label>
        <button className="secondary-action paste-action" type="button" onClick={() => void pasteOnce()}><ClipboardPaste size={17} />粘贴并识别</button>
        <label className="check-row"><input type="checkbox" checked={clearClipboard} onChange={(event) => setClearClipboard(event.target.checked)} /><span>保存成功后尝试清空系统剪贴板（默认关闭）</span></label>
        {notice && <div className="inline-success"><ShieldCheck size={16} />{notice}</div>}
        {error && (error instanceof ApiError ? <ErrorNotice error={error} /> : <div className="error-notice">{error instanceof Error ? error.message : "操作未完成"}</div>)}
        <button className="primary-action" disabled={!vaultAvailable || submitting || !apiKey || !providerId || !label.trim()} type="submit"><LockKeyhole size={17} />{submitting ? "正在写入安全存储…" : "保存到系统密钥库"}</button>
      </form>
      <section className="panel credential-list">
        <header><div><span className="eyebrow">SAVED PROFILES</span><h2>已保存密钥</h2></div><button className="icon-button" onClick={() => void profiles.refetch()} aria-label="刷新密钥列表"><RefreshCw size={17} /></button></header>
        {profiles.isError && <ErrorNotice error={profiles.error} />}
        {!profiles.isLoading && (profiles.data?.length ?? 0) === 0 && <EmptyState icon={KeyRound} title="还没有保存的密钥" body="你仍可在单模型探测中选择“仅本次使用”，无需持久化。" />}
        <div className="credential-rows">{profiles.data?.map((profile) => <article key={profile.id}><div className="credential-mark"><KeyRound size={18} /></div><div><h3>{profile.label}</h3><p>{profile.provider_id} · •••• {profile.masked_suffix || "未知"}</p><small>最近验证 {formatTime(profile.last_verified_at)}</small></div><StatusPill status={profile.status} /><button className="icon-button danger" onClick={() => void remove(profile.id, profile.version)} aria-label={`删除 ${profile.label}`}><Trash2 size={16} /></button></article>)}</div>
      </section>
    </section>
  </div>;
}

export function ProviderDirectoryPage({ mode }: { mode: "vendors" | "sources" }) {
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const title = mode === "vendors" ? "模型厂商" : "模型来源";
  return <div className="page-stack"><PageHead eyebrow="MODEL DIRECTORY" title={title} description={mode === "vendors" ? "浏览当前注册的模型品牌与直连 Provider；能力未知时保持 UNKNOWN。" : "浏览实际提供 API 的官方、聚合与本地来源及其协议地址。"} /><ApiKeyTabs />{bootstrap.isError && <ErrorNotice error={bootstrap.error} />}<section className="directory-grid">{bootstrap.data?.providers.map((provider) => <article className="panel" key={provider.id}><span className="eyebrow">{provider.target_kind.toUpperCase()}</span><h2>{provider.name}</h2><p>{provider.english_name}</p><code>{provider.base_url}</code><footer><StatusPill status="PROBE_SUPPORTED" /><span>{provider.requires_api_key ? "需要凭据" : "免凭据"}</span></footer></article>)}</section></div>;
}

function toDisplayError(cause: unknown): Error {
  return cause instanceof Error ? cause : new Error("操作未完成，请检查服务状态");
}
