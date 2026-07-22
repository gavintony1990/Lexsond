import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Laptop, LogOut, ShieldCheck, UserCircle, UsersRound } from "lucide-react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { ErrorNotice, PageHead } from "../ui";

export function AccountSettings({ section }: { section: "profile" | "workspace" | "security" }) {
  const auth = useAuth();
  if (section === "security") return <SecuritySettings />;
  const user = auth.session!.user;
  const profile = section === "profile";
  return <>
    <PageHead
      eyebrow={profile ? "PROFILE / 个人资料" : "WORKSPACE / 工作区"}
      title={profile ? "个人资料" : "当前工作区"}
      description={profile ? "确认当前登录身份。资料修改接口将在账号生命周期下一阶段开放。" : "所有密钥、渠道、运行和诊断记忆都以此工作区为授权边界。"}
    />
    <section className="settings-card">
      {profile ? <UserCircle size={24} /> : <UsersRound size={24} />}
      <dl>
        <div><dt>{profile ? "显示名称" : "工作区"}</dt><dd>{profile ? user.display_name : user.workspace_name}</dd></div>
        <div><dt>{profile ? "邮箱" : "当前角色"}</dt><dd>{profile ? user.email : user.workspace_role}</dd></div>
        <div><dt>{profile ? "邮箱状态" : "隔离范围"}</dt><dd>{profile ? (user.email_verified_at ? "已验证" : "待验证") : "凭据 · 渠道 · 运行 · 套件 · 诊断"}</dd></div>
      </dl>
    </section>
  </>;
}

function SecuritySettings() {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const sessions = useQuery({
    queryKey: ["auth", "sessions"],
    queryFn: api.authSessions,
    enabled: auth.session?.auth_mode === "required",
  });
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentRef = useRef<HTMLInputElement>(null);
  const nextRef = useRef<HTMLInputElement>(null);

  async function changePassword(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.changePassword(currentPassword, newPassword);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "密码修改暂时不可用");
    } finally {
      setCurrentPassword("");
      setNewPassword("");
      if (currentRef.current) currentRef.current.value = "";
      if (nextRef.current) nextRef.current.value = "";
      setBusy(false);
    }
  }

  async function revoke(sessionId: string, current: boolean) {
    await api.revokeAuthSession(sessionId);
    if (current) await auth.logout();
    else await queryClient.invalidateQueries({ queryKey: ["auth", "sessions"] });
  }

  async function logoutAll() {
    try { await api.logoutAll(); } finally { await auth.logout(); }
  }

  return <>
    <PageHead eyebrow="SECURITY / 账号安全" title="密码与会话设备" description="密码变更会撤销所有服务端会话；会话令牌不会返回页面或进入浏览器持久化存储。" />
    {auth.session?.auth_mode === "local-single-user" ? <section className="settings-card"><ShieldCheck size={24} /><p>当前是 loopback 本地单用户模式，不存在云端账号密码或可撤销设备会话。</p></section> : <div className="settings-grid">
      <form className="settings-card settings-form" onSubmit={changePassword}>
        <header><KeyRound size={21} /><div><h2>修改密码</h2><p>至少 12 字符；修改成功后需要重新登录。</p></div></header>
        {error && <div className="auth-error" role="alert">{error}</div>}
        <label>当前密码<input ref={currentRef} type="password" autoComplete="current-password" required onChange={(event) => setCurrentPassword(event.target.value)} /></label>
        <label>新密码<input ref={nextRef} type="password" autoComplete="new-password" minLength={12} required onChange={(event) => setNewPassword(event.target.value)} /></label>
        <button className="primary-action" disabled={busy || !currentPassword || newPassword.length < 12}>{busy ? "正在轮换会话…" : "修改密码"}</button>
      </form>
      <section className="settings-card">
        <header><Laptop size={21} /><div><h2>会话设备</h2><p>撤销不认识的设备，或一次退出全部会话。</p></div></header>
        {sessions.error && <ErrorNotice error={sessions.error} />}
        <div className="session-list">{sessions.data?.map((session) => <article key={session.session_id}>
          <Laptop size={17} /><div><b>{session.current ? "当前设备" : `设备 ${session.device_id ?? "未知"}`}</b><span>{session.ip_prefix ?? "IP 未记录"} · {new Date(session.last_seen_at ?? session.created_at).toLocaleString()}</span></div>
          <button onClick={() => void revoke(session.session_id, session.current)}>撤销</button>
        </article>)}</div>
        <button className="danger-action" onClick={() => void logoutAll()}><LogOut size={15} />退出全部设备</button>
      </section>
    </div>}
  </>;
}
