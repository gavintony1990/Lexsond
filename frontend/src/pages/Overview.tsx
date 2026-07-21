import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowUpRight, CircleDot, Clock3, Radio, Server, Waves } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorNotice, formatTime, MetricCard, PageHead, StatusPill } from "../ui";

export function Overview() {
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const runs = useQuery({ queryKey: ["runs", false], queryFn: () => api.runs(false), refetchInterval: 4_000 });
  const stats = bootstrap.data?.stats;
  const recent = runs.data?.slice(0, 6) ?? [];
  const signal = recent.slice(0, 18).reverse();

  return (
    <div className="page-stack">
      <PageHead
        eyebrow="LIVE QUALITY SURFACE / 实时质量水面"
        title="看见一次 API 调用的深度"
        description="从连接握手到协议证据，所有信号被压缩为可追溯、无原始载荷的质量剖面。"
        action={<div className="capture-stamp"><span>CAPTURE</span><b>{formatTime(new Date().toISOString())}</b></div>}
      />

      {(bootstrap.error || runs.error) && <ErrorNotice error={bootstrap.error || runs.error} />}

      <section className="metric-grid stagger-grid">
        <MetricCard code="01" label="累计运行" value={stats?.runs ?? "—"} suffix=" RUNS" />
        <MetricCard code="02" label="通过率" value={stats?.pass_rate ?? "—"} suffix=" %" tone="mint" />
        <MetricCard code="03" label="正在采样" value={stats?.running ?? "—"} suffix=" LIVE" tone="amber" />
        <MetricCard code="04" label="已绑定目标" value={stats?.targets ?? "—"} suffix=" NODES" />
      </section>

      <section className="overview-grid">
        <article className="panel signal-panel reveal delay-1">
          <header className="panel-head">
            <div><span className="eyebrow">RECENT SIGNAL</span><h2>近期质量脉冲</h2></div>
            <Link to="/runs" className="text-link">全部记录 <ArrowUpRight size={14} /></Link>
          </header>
          <div className="signal-chart" role="img" aria-label="近期运行质量状态图">
            <div className="depth-lines" aria-hidden="true"><i /><i /><i /><i /></div>
            {signal.length ? signal.map((run, index) => {
              const level = run.result_status === "PASS" ? 74 : run.result_status === "WARN" ? 48 : run.state === "RUNNING" ? 60 : 26;
              return (
                <Link
                  to={`/runs/${run.run_id}`}
                  className={`signal-column result-${(run.result_status ?? run.state).toLowerCase()}`}
                  style={{ "--level": `${level}%`, "--delay": `${index * 35}ms` } as React.CSSProperties}
                  key={run.run_id}
                  title={`${run.config.model} · ${run.result_status ?? run.state}`}
                ><b /><i /></Link>
              );
            }) : <div className="chart-empty"><Waves size={28} /><span>等待第一束探测信号</span></div>}
          </div>
          <footer className="chart-legend">
            <span><i className="pass" />PASS</span><span><i className="warn" />WARN</span><span><i className="fail" />FAIL</span>
            <b>OLD ← TIME → NOW</b>
          </footer>
        </article>

        <article className="panel engine-panel reveal delay-2">
          <header className="panel-head"><div><span className="eyebrow">EXECUTION FABRIC</span><h2>执行引擎</h2></div><Server size={19} /></header>
          <div className="engine-list">
            {bootstrap.data?.execution_backends.map((backend) => (
              <div className={`engine-row ${backend.available ? "available" : "offline"}`} key={backend.id}>
                <div className="engine-orbit"><Radio size={17} /><i /><i /></div>
                <div><strong>{backend.id === "local" ? "本地执行器" : "Temporal 集群"}</strong><span>{backend.id.toUpperCase()} / {backend.status}</span></div>
                <StatusPill status={backend.available ? "READY" : "OFFLINE"} />
              </div>
            ))}
          </div>
          <div className="evidence-seal">
            <CircleDot size={18} />
            <p><b>证据边界已启用</b><span>仅保留哈希、字符数、时延与断言；密钥及原始输出不落盘。</span></p>
          </div>
        </article>
      </section>

      <section className="panel recent-panel reveal delay-3">
        <header className="panel-head">
          <div><span className="eyebrow">LATEST DESCENTS</span><h2>最近下潜记录</h2></div>
          <Link to="/runs/new" className="secondary-action"><Activity size={15} /> 新建探测</Link>
        </header>
        {recent.length ? (
          <div className="run-strip-list">
            {recent.map((run, index) => (
              <Link to={`/runs/${run.run_id}`} className="run-strip" key={run.run_id}>
                <span className="run-index">{String(index + 1).padStart(2, "0")}</span>
                <div className="run-sonar"><i /><b /></div>
                <div className="run-primary"><strong>{run.config.model}</strong><span>{run.config.probe_type.replaceAll("_", " ")} · {run.execution_backend}</span></div>
                <div className="run-endpoint">{run.config.base_url}</div>
                <div className="run-time"><Clock3 size={13} />{formatTime(run.created_at)}</div>
                <StatusPill status={run.result_status ?? run.state} />
                <ArrowUpRight size={15} className="row-arrow" />
              </Link>
            ))}
          </div>
        ) : (
          <EmptyState icon={Waves} title="海面尚无回波" body="绑定一个 API 目标并发起第一次有界探测。" action={<Link to="/runs/new" className="primary-action">开始探测</Link>} />
        )}
      </section>
    </div>
  );
}
