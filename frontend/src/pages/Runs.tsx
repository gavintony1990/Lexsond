import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowUpRight, Filter, History, Play, RotateCcw, Trash2 } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { EmptyState, ErrorNotice, formatTime, PageHead, StatusPill } from "../ui";

export function Runs() {
  const [showArchived, setShowArchived] = useState(false);
  const [filter, setFilter] = useState("ALL");
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: ["runs", showArchived],
    queryFn: () => api.runs(showArchived),
    refetchInterval: 3_000,
  });
  const mutate = useMutation({
    mutationFn: async ({ action, id }: { action: "archive" | "restore" | "purge"; id: string }) => {
      if (action === "archive") return api.archiveRun(id);
      if (action === "restore") return api.restoreRun(id);
      return api.purgeRun(id);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["runs"] }),
  });
  const values = (runs.data ?? []).filter((run) =>
    filter === "ALL" ? true : filter === "RUNNING" ? run.state === "RUNNING" : run.result_status === filter,
  );

  return (
    <div className="page-stack">
      <PageHead
        eyebrow="RUN ARCHIVE / 运行档案"
        title="每次下潜，都留下可验证的刻度"
        description="运行配置在发起后冻结；你可以归档、恢复或显式清除终态记录，但不能改写历史证据。"
        action={<Link to="/runs/new" className="primary-action"><Play size={15} fill="currentColor" />发起探测</Link>}
      />
      {runs.error && <ErrorNotice error={runs.error} />}
      <div className="toolbar panel-lite">
        <div className="filter-group"><Filter size={15} />{["ALL", "PASS", "WARN", "FAIL", "RUNNING"].map((item) => <button className={filter === item ? "active" : ""} onClick={() => setFilter(item)} key={item}>{item}</button>)}</div>
        <label className="toggle-label"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} /><span />显示已归档</label>
      </div>
      <section className="panel data-panel">
        <div className="data-table run-table">
          <div className="table-row table-head"><span>状态</span><span>模型 / 类型</span><span>目标</span><span>执行器</span><span>时间</span><span>操作</span></div>
          {values.map((run) => (
            <div className="table-row" key={run.run_id}>
              <span><StatusPill status={run.result_status ?? run.state} /></span>
              <span className="cell-main"><strong>{run.config.model}</strong><small>{run.config.probe_type} · {run.run_kind}</small></span>
              <span className="endpoint-cell" title={run.config.base_url}>{run.config.base_url}</span>
              <span className="backend-chip">{run.execution_backend}</span>
              <span className="mono-cell">{formatTime(run.created_at)}</span>
              <span className="row-actions">
                <Link to={`/runs/${run.run_id}`} className="icon-button" aria-label="查看详情"><ArrowUpRight size={16} /></Link>
                {run.archived_at ? <>
                  <button className="icon-button" onClick={() => mutate.mutate({ action: "restore", id: run.run_id })} aria-label="恢复"><RotateCcw size={15} /></button>
                  <button className="icon-button danger" onClick={() => window.confirm("永久清除该运行及其事件？此操作不可恢复。") && mutate.mutate({ action: "purge", id: run.run_id })} aria-label="永久清除"><Trash2 size={15} /></button>
                </> : run.state !== "RUNNING" && <button className="icon-button" onClick={() => mutate.mutate({ action: "archive", id: run.run_id })} aria-label="归档"><Archive size={15} /></button>}
              </span>
            </div>
          ))}
        </div>
        {!values.length && <EmptyState icon={History} title="当前筛选没有记录" body="切换状态筛选，或发起一次新的探测。" />}
      </section>
    </div>
  );
}
