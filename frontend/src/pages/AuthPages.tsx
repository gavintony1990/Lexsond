import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, LockKeyhole, Mail, Radio, ShieldCheck, Waves } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth";

function AuthFrame({ children }: { children: React.ReactNode }) {
  return (
    <main className="auth-shell">
      <div className="auth-grid" aria-hidden="true" />
      <section className="auth-brand-panel">
        <Link to="/overview" className="auth-brand">
          <span className="auth-sonar"><i /><i /><i /><Waves size={27} /></span>
          <span><strong>Lexsond</strong><small>PROBE OBSERVATORY</small></span>
        </Link>
        <div className="auth-signal-copy">
          <span className="eyebrow">IDENTITY BOUNDARY / 身份边界</span>
          <h1>每一次测深，<br />都有清晰归属。</h1>
          <p>服务端会话、独立工作区与脱敏证据共同构成安全边界。浏览器不会持久化 Session、OAuth Token 或 API Key。</p>
        </div>
        <div className="auth-waveform" aria-hidden="true">
          {[18, 31, 22, 58, 36, 75, 45, 88, 52, 69, 29, 42, 21, 34].map((height, index) => (
            <i key={index} style={{ "--wave": `${height}%`, "--delay": `${index * 55}ms` } as React.CSSProperties} />
          ))}
        </div>
        <footer><ShieldCheck size={16} /><span>OPAQUE SESSION · ARGON2ID · CSRF</span></footer>
      </section>
      <section className="auth-form-panel">{children}</section>
    </main>
  );
}

export function LoginPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);
  const from = typeof location.state === "object" && location.state && "from" in location.state
    ? String(location.state.from)
    : "/overview";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const returnTo = await auth.login(email, password, from);
      setPassword("");
      if (passwordRef.current) passwordRef.current.value = "";
      navigate(returnTo, { replace: true });
    } catch (reason) {
      setPassword("");
      if (passwordRef.current) passwordRef.current.value = "";
      setError(reason instanceof ApiError ? reason.message : "登录暂时不可用，请稍后再试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthFrame>
      <form className="auth-card" onSubmit={submit}>
        <header><span className="eyebrow">WELCOME BACK / 欢迎回来</span><h2>登录观测站</h2><p>进入你的工作区，继续检查 API 连接和模型质量。</p></header>
        {error && <div className="auth-error" role="alert"><Radio size={16} />{error}</div>}
        <label><span>邮箱</span><div><Mail size={16} /><input aria-label="邮箱" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></div></label>
        <label><span>密码</span><div><LockKeyhole size={16} /><input ref={passwordRef} aria-label="密码" type="password" autoComplete="current-password" required minLength={12} onChange={(event) => setPassword(event.target.value)} /></div></label>
        <div className="auth-meta"><span>密码区分大小写</span><span>会话最长 7 天</span></div>
        <button className="auth-submit" disabled={busy || !email || password.length < 12}>{busy ? "正在建立安全会话…" : "登录"}<ArrowRight size={17} /></button>
        <footer><Link to="/forgot-password">忘记密码？</Link><br />还没有账号？ <Link to="/register">创建个人工作区</Link></footer>
      </form>
    </AuthFrame>
  );
}

export function RegisterPage() {
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.register(email, password, displayName);
      setSent(true);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "注册暂时不可用，请稍后再试");
    } finally {
      setPassword("");
      if (passwordRef.current) passwordRef.current.value = "";
      setBusy(false);
    }
  }

  return (
    <AuthFrame>
      <form className="auth-card" onSubmit={submit}>
        <header><span className="eyebrow">NEW OBSERVER / 新观测员</span><h2>{sent ? "检查你的邮箱" : "创建个人工作区"}</h2><p>{sent ? `验证链接已发送至 ${email}` : "你的渠道、运行、套件和诊断会话都将隔离在独立工作区。"}</p></header>
        {sent ? <div className="auth-success"><Mail size={22} /><p><b>等待邮箱验证</b><span>验证前可以浏览总览，但不能保存密钥或发起计费探测。</span></p></div> : <>
          {error && <div className="auth-error" role="alert"><Radio size={16} />{error}</div>}
          <label><span>显示名称</span><div><Radio size={16} /><input aria-label="显示名称" autoComplete="name" required maxLength={120} value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></div></label>
          <label><span>邮箱</span><div><Mail size={16} /><input aria-label="邮箱" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></div></label>
          <label><span>密码（至少 12 字符）</span><div><LockKeyhole size={16} /><input ref={passwordRef} aria-label="密码" type="password" autoComplete="new-password" required minLength={12} onChange={(event) => setPassword(event.target.value)} /></div></label>
          <button className="auth-submit" disabled={busy || !email || !displayName || password.length < 12}>{busy ? "正在创建…" : "创建账号"}<ArrowRight size={17} /></button>
        </>}
        <footer>{sent ? <Link to="/login">返回登录</Link> : <>已有账号？ <Link to="/login">直接登录</Link></>}</footer>
      </form>
    </AuthFrame>
  );
}

export function VerifyEmailPage() {
  const auth = useAuth();
  const token = useOneTimeFragmentToken();
  const [state, setState] = useState<"working" | "done" | "error">("working");

  useEffect(() => {
    if (!token) { setState("error"); return; }
    void auth.verifyEmail(token).then(() => setState("done"), () => setState("error"));
  }, [auth, token]);

  return (
    <AuthFrame>
      <section className="auth-card auth-result">
        <span className={`result-orbit ${state}`}><ShieldCheck size={28} /><i /><i /></span>
        <span className="eyebrow">EMAIL VERIFICATION</span>
        <h2>{state === "working" ? "正在验证…" : state === "done" ? "邮箱已验证" : "验证链接无效或已过期"}</h2>
        <p>{state === "done" ? "现在可以登录并使用完整的工作区能力。" : state === "error" ? "请回到登录页重新发送验证邮件。" : "正在确认一次性令牌，请不要关闭页面。"}</p>
        {state !== "working" && <Link className="auth-submit" to="/login">前往登录 <ArrowRight size={17} /></Link>}
      </section>
    </AuthFrame>
  );
}

export function ForgotPasswordPage() {
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.forgotPassword(email);
      setSent(true);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "密码重置服务暂时不可用");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthFrame>
      <form className="auth-card" onSubmit={submit}>
        <header><span className="eyebrow">ACCOUNT RECOVERY / 账号恢复</span><h2>{sent ? "检查你的邮箱" : "重置密码"}</h2><p>{sent ? "如果账号存在，一次性重置链接已经发送。" : "输入登录邮箱；公开响应不会透露账号是否存在。"}</p></header>
        {error && <div className="auth-error" role="alert"><Radio size={16} />{error}</div>}
        {!sent && <>
          <label><span>邮箱</span><div><Mail size={16} /><input aria-label="邮箱" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></div></label>
          <button className="auth-submit" disabled={busy || !email}>{busy ? "正在受理…" : "发送重置链接"}<ArrowRight size={17} /></button>
        </>}
        <footer><Link to="/login">返回登录</Link></footer>
      </form>
    </AuthFrame>
  );
}

export function ResetPasswordPage() {
  const auth = useAuth();
  const token = useOneTimeFragmentToken();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token) { setError("重置链接无效或已过期"); return; }
    setBusy(true);
    setError(null);
    try {
      await auth.resetPassword(token, password);
      setDone(true);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "密码重置暂时不可用");
    } finally {
      setPassword("");
      if (passwordRef.current) passwordRef.current.value = "";
      setBusy(false);
    }
  }

  return (
    <AuthFrame>
      <form className="auth-card" onSubmit={submit}>
        <header><span className="eyebrow">ONE-TIME RESET / 一次性重置</span><h2>{done ? "密码已更新" : "设置新密码"}</h2><p>{done ? "所有旧会话已撤销，请重新登录。" : "新密码至少 12 字符；提交后其他设备会立即退出。"}</p></header>
        {error && <div className="auth-error" role="alert"><Radio size={16} />{error}</div>}
        {!done && <>
          <label><span>新密码</span><div><LockKeyhole size={16} /><input ref={passwordRef} aria-label="新密码" type="password" autoComplete="new-password" required minLength={12} onChange={(event) => setPassword(event.target.value)} /></div></label>
          <button className="auth-submit" disabled={busy || password.length < 12}>{busy ? "正在撤销旧会话…" : "更新密码"}<ArrowRight size={17} /></button>
        </>}
        <footer><Link to="/login">{done ? "使用新密码登录" : "返回登录"}</Link></footer>
      </form>
    </AuthFrame>
  );
}

function useOneTimeFragmentToken(): string {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => {
    const fragment = location.hash.startsWith("#") ? location.hash.slice(1) : "";
    return new URLSearchParams(fragment).get("token") ?? "";
  });
  useLayoutEffect(() => {
    if (location.hash) navigate(location.pathname, { replace: true });
  }, [location.hash, location.pathname, navigate]);
  return token;
}
