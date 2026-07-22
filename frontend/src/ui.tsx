import type { PropsWithChildren, ReactNode } from "react";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Archive,
  ArrowRight,
  BookOpen,
  Bot,
  Database,
  FlaskConical,
  Gauge,
  Menu,
  Play,
  Radio,
  Server,
  Waves,
  X,
  ChevronDown,
  LogOut,
  UserCircle,
  KeyRound,
  ScanLine,
  Layers3,
  Handshake,
  HeartPulse,
  ShieldCheck,
  UsersRound,
} from "lucide-react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api, ApiError } from "./api";
import type { ResultStatus, RunState } from "./types";
import { useOptionalAuth } from "./auth";

const navigation = [
  { to: "/overview", label: "总览与入门", code: "01", icon: Gauge },
  { to: "/api-keys/credentials", match: "/api-keys", label: "API Key 管理", code: "02", icon: KeyRound },
  { to: "/probes/single", label: "单模型探测", code: "03", icon: ScanLine },
  { to: "/probes/api-key", label: "API Key 模型探测", code: "04", icon: Layers3 },
  { to: "/partners/onboarding", label: "合作中转站入驻", code: "05", icon: Handshake },
  { to: "/partners/monitoring", label: "合作中转站持续监控", code: "06", icon: HeartPulse },
  { to: "/assistant", label: "诊断助手 ChatGPT", code: "07", icon: Bot },
  { to: "/suites", label: "探测套件管理", code: "08", icon: FlaskConical },
];

const apiKeyChildren = [
  { to: "/api-keys/credentials", label: "密钥" },
  { to: "/api-keys/channels", label: "渠道" },
  { to: "/api-keys/vendors", label: "模型厂商" },
  { to: "/api-keys/sources", label: "模型来源" },
];

export function Shell({ children }: PropsWithChildren) {
  const [open, setOpen] = useState(false);
  const [guideOpen, setGuideOpen] = useState(false);
  const [userOpen, setUserOpen] = useState(false);
  const [apiKeysOpen, setApiKeysOpen] = useState(false);
  const auth = useOptionalAuth();
  const location = useLocation();
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const current = navigation.find((item) =>
    location.pathname.startsWith(item.match ?? item.to),
  );

  return (
    <div className="observatory-shell">
      <div className="ambient-grid" aria-hidden="true" />
      <aside className={`side-rail ${open ? "is-open" : ""}`}>
        <div className="brand-lockup">
          <div className="sonar-mark" aria-hidden="true">
            <i /><i /><i />
            <Waves size={23} />
          </div>
          <div>
            <strong>Lexsond</strong>
            <span>PROBE OBSERVATORY</span>
          </div>
          <button className="icon-button rail-close" onClick={() => setOpen(false)} aria-label="关闭导航">
            <X size={18} />
          </button>
        </div>
        <div className="rail-caption">CONTROL DECK / 08</div>
        <nav className="primary-nav" aria-label="主导航">
          {navigation.map(({ to, match, label, code, icon: Icon }) => match === "/api-keys" ? (
            <div className="nav-cluster" key={to}>
              <div className="nav-parent">
                <NavLink data-primary-nav-item to={to} onClick={() => setOpen(false)} className={location.pathname.startsWith(match) ? "active" : ""}><Icon size={19} /><span>{label}</span><em>{code}</em></NavLink>
                <button type="button" aria-label={`${apiKeysOpen ? "收起" : "展开"}密钥管理子菜单`} aria-expanded={apiKeysOpen} onClick={() => setApiKeysOpen((value) => !value)}><ChevronDown size={14} /></button>
              </div>
              {apiKeysOpen && <div className="nav-children" role="navigation" aria-label="API Key 管理">{apiKeyChildren.map((child) => <NavLink key={child.to} to={child.to} onClick={() => setOpen(false)}>{child.label}</NavLink>)}</div>}
            </div>
          ) : (
            <NavLink data-primary-nav-item key={to} to={to} onClick={() => setOpen(false)} className={location.pathname.startsWith(to) ? "active" : ""}>
              <Icon size={19} /><span>{label}</span><em>{code}</em>
            </NavLink>
          ))}
        </nav>
        <div className="rail-spacer" />
        <div className="storage-card">
          <Database size={17} />
          <div>
            <span>LOCAL EVIDENCE</span>
            <strong>PostgreSQL · DURABLE</strong>
          </div>
          <i className="live-dot" />
        </div>
        <div className="rail-version">
          <span>CORE</span>
          <b>v{bootstrap.data?.product.version ?? "0.8.0"}</b>
          <span>NO RAW PAYLOAD</span>
        </div>
      </aside>

      <main className="main-deck">
        <header className="top-bar">
          <button className="icon-button mobile-menu" onClick={() => setOpen(true)} aria-label="打开导航">
            <Menu size={20} />
          </button>
          <div className="breadcrumb">
            <span>OBSERVATORY</span>
            <b>/</b>
            <strong>{current?.label ?? "运行详情"}</strong>
          </div>
          <div className="top-status">
            {auth?.session?.auth_mode === "local-single-user" && <span className="local-mode-badge">本地单用户模式</span>}
            <div className="signal-readout">
              <Radio size={15} />
              <span>LOCAL</span>
              <b>READY</b>
            </div>
            <div className="signal-readout temporal-readout">
              <Server size={15} />
              <span>TEMPORAL</span>
              <b className={bootstrap.data?.execution_backends[1]?.available ? "ok" : "muted"}>
                {bootstrap.data?.execution_backends[1]?.available ? "READY" : "OFFLINE"}
              </b>
            </div>
            <button className="help-action" onClick={() => setGuideOpen(true)}>
              <BookOpen size={15} />
              <span>使用指南</span>
            </button>
            <Link to="/probes/single/new" className="primary-action compact">
              <Play size={15} fill="currentColor" />
              发起探测
            </Link>
            {auth?.session && <div className="user-menu-wrap">
              <button className="user-menu-trigger" aria-label="用户菜单" aria-expanded={userOpen} onClick={() => setUserOpen((value) => !value)}>
                <span>{auth.session.user.display_name.slice(0, 1).toUpperCase()}</span>
                <div><b>{auth.session.user.display_name}</b><small>{auth.session.user.workspace_name}</small></div>
                <ChevronDown size={14} />
              </button>
              {userOpen && <div className="user-menu" role="menu">
                <header><UserCircle size={17} /><p><b>{auth.session.user.display_name}</b><span>{auth.session.user.email}</span></p></header>
                <div className="user-workspace"><span>当前工作区</span><b>{auth.session.user.workspace_name}</b><small>{auth.session.user.workspace_role}</small></div>
                <Link role="menuitem" to="/settings/profile" onClick={() => setUserOpen(false)}><UserCircle size={15} />个人资料</Link>
                <Link role="menuitem" to="/settings/workspace" onClick={() => setUserOpen(false)}><UsersRound size={15} />工作区</Link>
                <Link role="menuitem" to="/settings/security" onClick={() => setUserOpen(false)}><ShieldCheck size={15} />会话设备</Link>
                <button role="menuitem" onClick={() => void auth.logout()}><LogOut size={15} />退出登录</button>
              </div>}
            </div>}
          </div>
        </header>
        <div className="deck-content">{children}</div>
      </main>
      {guideOpen && (
        <div className="modal-layer guide-layer">
          <button className="drawer-scrim" onClick={() => setGuideOpen(false)} aria-label="关闭使用指南" />
          <section className="modal-card guide-modal" role="dialog" aria-modal="true" aria-label="快速使用指南">
            <header>
              <div><span className="eyebrow">QUICK START / 快速上手</span><h2>四步完成一次可信探测</h2></div>
              <button className="icon-button" onClick={() => setGuideOpen(false)} aria-label="关闭使用指南"><X size={19} /></button>
            </header>
            <p className="guide-lead">你不需要先理解所有指标。先完成一次最小探测，再从结果页逐项查看协议、性能和质量证据。</p>
            <ol className="guide-steps">
              <li><span>01</span><div><b>添加 API 目标</b><p>填写服务地址和默认模型。云端 API Key 不保存在目标中。</p></div></li>
              <li><span>02</span><div><b>选择一次最小探测</b><p>第一次建议：单项探针、文本聊天、本地执行器、30 秒超时。</p></div></li>
              <li><span>03</span><div><b>确认调用上限后运行</b><p>提交前会显示最大调用次数；套件还会显示并发和费用上限。</p></div></li>
              <li><span>04</span><div><b>从结论进入证据</b><p>先看 PASS、WARN 或 FAIL，再查看时延、协议事件与失败原因。</p></div></li>
            </ol>
            <div className="guide-safety"><Database size={18} /><p><b>密钥不会成为持久化记忆</b><span>临时 API Key 仅用于本次请求；历史记录保留归一化指标与安全证据。</span></p></div>
            <footer className="guide-actions">
              <Link to="/targets" className="secondary-action" onClick={() => setGuideOpen(false)}>去添加目标</Link>
              <Link to="/runs/new" className="primary-action" onClick={() => setGuideOpen(false)}>去发起探测 <ArrowRight size={15} /></Link>
            </footer>
          </section>
        </div>
      )}
      {open && <button className="rail-scrim" onClick={() => setOpen(false)} aria-label="关闭导航遮罩" />}
    </div>
  );
}

export function PageHead({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-head reveal">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action && <div className="page-action">{action}</div>}
    </div>
  );
}

export function StatusPill({ status }: { status: ResultStatus | RunState | string }) {
  const normalized = (status || "UNKNOWN").toString().toLowerCase();
  return (
    <span className={`status-pill status-${normalized}`}>
      <i />
      {status || "UNKNOWN"}
    </span>
  );
}

export function EmptyState({
  icon: Icon = Archive,
  title,
  body,
  action,
}: {
  icon?: typeof Archive;
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-orbit"><Icon size={25} /></div>
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function ErrorNotice({ error }: { error: unknown }) {
  const message = error instanceof ApiError ? `${error.code} · ${error.message}` : "请求未完成，请检查服务状态";
  return <div className="error-notice"><Radio size={17} /><span>{message}</span></div>;
}

export function MetricCard({
  code,
  label,
  value,
  suffix,
  tone = "cyan",
}: {
  code: string;
  label: string;
  value: string | number;
  suffix?: string;
  tone?: "cyan" | "amber" | "mint" | "red";
}) {
  return (
    <article className={`metric-card tone-${tone}`}>
      <header><span>{code}</span><i /></header>
      <p>{label}</p>
      <strong>{value}<small>{suffix}</small></strong>
      <div className="metric-track"><b /></div>
    </article>
  );
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
