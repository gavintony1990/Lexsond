import type { PropsWithChildren, ReactNode } from "react";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  ArrowRight,
  BookOpen,
  Bot,
  Database,
  FlaskConical,
  Gauge,
  Grid3X3,
  Menu,
  Play,
  Radio,
  Server,
  Target as TargetIcon,
  Waves,
  X,
} from "lucide-react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api, ApiError } from "./api";
import type { ResultStatus, RunState } from "./types";

const navigation = [
  { to: "/", label: "总览与入门", code: "00", icon: Gauge },
  { to: "/targets", label: "添加 API 目标", code: "01", icon: TargetIcon },
  { to: "/runs/new", label: "发起探测", code: "02", icon: Play },
  { to: "/runs", label: "查看结果", code: "03", icon: Activity },
  { to: "/monitoring", label: "持续监控", code: "04", icon: Grid3X3 },
  { to: "/agent", label: "诊断助手", code: "05", icon: Bot },
  { to: "/suites", label: "探测套件", code: "06", icon: FlaskConical },
];

export function Shell({ children }: PropsWithChildren) {
  const [open, setOpen] = useState(false);
  const [guideOpen, setGuideOpen] = useState(false);
  const location = useLocation();
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const current = navigation.find((item) =>
    item.to === "/" ? location.pathname === "/" : location.pathname.startsWith(item.to),
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
        <div className="rail-caption">CONTROL DECK / 07</div>
        <nav className="primary-nav" aria-label="主导航">
          {navigation.map(({ to, label, code, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              onClick={() => setOpen(false)}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              <Icon size={19} />
              <span>{label}</span>
              <em>{code}</em>
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
            <Link to="/runs/new" className="primary-action compact">
              <Play size={15} fill="currentColor" />
              发起探测
            </Link>
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
