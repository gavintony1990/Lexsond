import { useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArrowRight,
  Bot,
  BrainCircuit,
  Cpu,
  Database,
  KeyRound,
  MessageSquare,
  Plus,
  Send,
  ShieldCheck,
  Sparkles,
  Target as TargetIcon,
  Wrench,
} from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { AgentSessionCreateInput } from "../types";
import { EmptyState, ErrorNotice, formatTime, PageHead, StatusPill } from "../ui";

export function AgentWorkbench() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const catalog = useQuery({ queryKey: ["agent-bootstrap"], queryFn: api.agentBootstrap });
  const sessions = useQuery({ queryKey: ["agent-sessions", false], queryFn: () => api.agentSessions(false) });
  const targets = useQuery({ queryKey: ["targets", false], queryFn: () => api.targets(false) });
  const session = useQuery({
    queryKey: ["agent-session", sessionId],
    queryFn: () => api.agentSession(sessionId!),
    enabled: Boolean(sessionId),
  });
  const messages = useQuery({
    queryKey: ["agent-messages", sessionId],
    queryFn: () => api.agentMessages(sessionId!),
    enabled: Boolean(sessionId),
  });
  const events = useQuery({
    queryKey: ["agent-events", sessionId],
    queryFn: () => api.agentEvents(sessionId!),
    enabled: Boolean(sessionId),
  });
  const error = catalog.error || sessions.error || targets.error || session.error || messages.error || events.error;

  return (
    <div className="page-stack agent-page">
      <PageHead
        eyebrow="AGENT ORCHESTRATION / 智能编排"
        title="让探针证据进入智能体回路"
        description="React 接收意图，FastAPI 固化边界，LangChain 调用模型与只读工具，Repository Checkpointer 恢复上下文。"
        action={<div className="agent-safety-stamp"><ShieldCheck size={16} /><span>HUMAN-GATED</span><b>0 BILLABLE TOOLS</b></div>}
      />
      {Boolean(error) && <ErrorNotice error={error} />}
      <ArchitectureRibbon catalog={catalog.data} />

      <section className="agent-workbench panel">
        <SessionRail
          sessions={sessions.data ?? []}
          activeId={sessionId}
          onSelect={(id) => navigate(`/agent/${id}`)}
          onNew={() => navigate("/agent")}
        />
        {sessionId && session.data ? (
          <ConversationDeck
            session={session.data}
            messages={messages.data ?? []}
            events={events.data ?? []}
            skill={catalog.data?.skills.find((item) => item.id === session.data?.skill_id)}
            onArchived={async () => {
              await api.archiveAgentSession(sessionId);
              await queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
              navigate("/agent");
            }}
          />
        ) : (
          <SessionComposer
            targets={targets.data ?? []}
            skills={catalog.data?.skills ?? []}
            onCreated={async (value) => {
              const created = await api.createAgentSession(value);
              await queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
              navigate(`/agent/${created.session_id}`);
            }}
          />
        )}
        <AgentContext
          runtime={catalog.data?.runtime}
          tools={catalog.data?.tools ?? []}
          events={events.data ?? []}
          activeSkillId={session.data?.skill_id}
        />
      </section>
    </div>
  );
}

function ArchitectureRibbon({ catalog }: { catalog: Awaited<ReturnType<typeof api.agentBootstrap>> | undefined }) {
  const nodes = [
    { code: "01", label: "React 意图入口", note: "SESSION / MESSAGE", icon: MessageSquare },
    { code: "02", label: "FastAPI 边界", note: "VALIDATE / REDACT", icon: ShieldCheck },
    { code: "03", label: "LangChain Agent", note: "DECIDE / CALL TOOL", icon: BrainCircuit },
    { code: "04", label: "模型适配器", note: "OPENAI COMPATIBLE", icon: Cpu },
  ];
  return (
    <section className="architecture-ribbon reveal delay-1" aria-label="Agent 调用架构">
      {nodes.map(({ code, label, note, icon: Icon }, index) => (
        <div className="architecture-hop" key={code}>
          <article>
            <span>{code}</span><Icon size={18} />
            <div><b>{label}</b><small>{note}</small></div>
          </article>
          {index < nodes.length - 1 && <ArrowRight size={16} />}
        </div>
      ))}
      <div className="architecture-satellites">
        <span><Wrench size={14} />{catalog?.tools.length ?? 0} TOOLS</span>
        <span><Sparkles size={14} />{catalog?.skills.length ?? 0} SKILLS</span>
        <span><Database size={14} />CHECKPOINTER</span>
      </div>
    </section>
  );
}

function SessionRail({
  sessions,
  activeId,
  onSelect,
  onNew,
}: {
  sessions: Awaited<ReturnType<typeof api.agentSessions>>;
  activeId?: string;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <aside className="agent-session-rail">
      <header><div><span className="eyebrow">MEMORY INDEX</span><h2>会话记忆</h2></div><button className="icon-button" onClick={onNew} aria-label="新建 Agent 会话"><Plus size={17} /></button></header>
      <div className="agent-session-list">
        {sessions.map((item, index) => (
          <button key={item.session_id} className={item.session_id === activeId ? "active" : ""} onClick={() => onSelect(item.session_id)}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <div><b>{item.title}</b><small>{item.skill_id}<br />{formatTime(item.updated_at)}</small></div>
          </button>
        ))}
      </div>
      {!sessions.length && <div className="rail-empty"><Database size={20} /><span>暂无 checkpoint</span></div>}
      <footer><i className="live-dot" /><span>MEMORY REDACTION ON</span></footer>
    </aside>
  );
}

function SessionComposer({
  targets,
  skills,
  onCreated,
}: {
  targets: Awaited<ReturnType<typeof api.targets>>;
  skills: Awaited<ReturnType<typeof api.agentBootstrap>>["skills"];
  onCreated: (value: AgentSessionCreateInput) => Promise<void>;
}) {
  const [targetId, setTargetId] = useState("");
  const [skillId, setSkillId] = useState("connection-diagnosis");
  const [title, setTitle] = useState("连接诊断会话");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const selected = targets.find((target) => target.id === targetId);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!targetId) return;
    setBusy(true); setError(null);
    try {
      await onCreated({ title, target_id: targetId, model: model.trim() || null, skill_id: skillId });
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  if (!targets.length) {
    return <main className="agent-conversation empty"><EmptyState icon={TargetIcon} title="先绑定一个模型目标" body="Agent 会冻结目标地址和模型快照，之后才能调用 LangChain 模型。" action={<Link to="/targets" className="primary-action">前往目标管理</Link>} /></main>;
  }
  return (
    <main className="agent-conversation agent-onboarding">
      <div className="onboarding-orbit"><Bot size={37} /><i /><i /></div>
      <span className="eyebrow">NEW AGENT CHECKPOINT</span>
      <h2>建立一条可恢复的诊断链路</h2>
      <p>目标和模型在会话创建时冻结；Skill 决定系统指令与可调用工具集合。</p>
      {Boolean(error) && <ErrorNotice error={error} />}
      <form className="agent-session-form" onSubmit={submit}>
        <label>会话名称<input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={120} /></label>
        <label>模型目标<select value={targetId} onChange={(event) => { setTargetId(event.target.value); setModel(""); }} required><option value="">选择目标</option>{targets.map((target) => <option value={target.id} key={target.id}>{target.name} · {target.default_model || "未设置模型"}</option>)}</select></label>
        <label>Agent Skill<select value={skillId} onChange={(event) => setSkillId(event.target.value)}>{skills.map((skill) => <option value={skill.id} key={skill.id}>{skill.name}</option>)}</select></label>
        <label>模型覆盖 <small className="optional">OPTIONAL</small><input value={model} onChange={(event) => setModel(event.target.value)} placeholder={selected?.default_model || "沿用目标默认模型"} /></label>
        <button className="primary-action agent-create-button" disabled={busy || !targetId}><BrainCircuit size={17} />{busy ? "冻结上下文…" : "创建 Agent 会话"}<ArrowRight size={15} /></button>
      </form>
    </main>
  );
}

function ConversationDeck({ session, messages, events, skill, onArchived }: {
  session: Awaited<ReturnType<typeof api.agentSession>>;
  messages: Awaited<ReturnType<typeof api.agentMessages>>;
  events: Awaited<ReturnType<typeof api.agentEvents>>;
  skill: Awaited<ReturnType<typeof api.agentBootstrap>>["skills"][number] | undefined;
  onArchived: () => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [keyValue, setKeyValue] = useState("");
  const draftRef = useRef("");
  const keyRef = useRef("");
  const send = useMutation({
    // Prompt and key stay outside mutation variables so TanStack's observer
    // cannot retain either value after a request settles.
    mutationFn: () => api.sendAgentMessage(session.session_id, draftRef.current, keyRef.current || null),
    onSuccess: () => {
      setDraft("");
    },
    onSettled: async () => {
      draftRef.current = "";
      keyRef.current = "";
      setKeyValue("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["agent-messages", session.session_id] }),
        queryClient.invalidateQueries({ queryKey: ["agent-events", session.session_id] }),
        queryClient.invalidateQueries({ queryKey: ["agent-sessions"] }),
      ]);
    },
  });
  const latestTools = useMemo(() => events.filter((event) => event.event_type === "TOOL_COMPLETED").slice(-3), [events]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (value) {
      draftRef.current = value;
      send.mutate();
    }
  }
  return (
    <main className="agent-conversation">
      <header className="conversation-head">
        <div><span className="eyebrow">ACTIVE THREAD / {session.skill_id}</span><h2>{session.title}</h2><p>{session.model} · {session.base_url}</p></div>
        <button className="icon-button" onClick={() => void onArchived()} aria-label="归档 Agent 会话"><Archive size={16} /></button>
      </header>
      <div className="message-stream" aria-live="polite">
        {!messages.length && <div className="agent-greeting"><div><Bot size={24} /></div><p><b>{skill?.name ?? "探针智能体"}已就绪</b><span>我会先调用只读工具读取证据，再区分事实、推断和建议。</span></p></div>}
        {messages.map((message) => (
          <article className={`agent-message ${message.role}`} key={message.message_id}>
            <div className="message-avatar">{message.role === "assistant" ? <Bot size={16} /> : <span>U</span>}</div>
            <div><header><b>{message.role === "assistant" ? "PROBE AGENT" : "OPERATOR"}</b><span>{formatTime(message.created_at)}</span></header><p>{message.content}</p></div>
          </article>
        ))}
        {send.isPending && <div className="agent-thinking"><i /><i /><i /><span>LangChain 正在编排工具与模型</span></div>}
      </div>
      {send.error && <ErrorNotice error={send.error} />}
      {!messages.length && skill && <div className="starter-row">{skill.starters.map((starter) => <button key={starter} onClick={() => setDraft(starter)}>{starter}</button>)}</div>}
      {latestTools.length > 0 && <div className="tool-receipts">{latestTools.map((event) => <span key={event.event_id}><Wrench size={12} />{event.name}<b>{event.status}</b></span>)}</div>}
      <form className="agent-prompt-dock" onSubmit={submit}>
        <textarea aria-label="给探针智能体发送消息" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="描述失败现象、运行 ID，或让 Agent 设计最小探测方案…" maxLength={4000} />
        <div className="prompt-controls">
          <label className="agent-key-field"><KeyRound size={14} /><span>{session.target_kind === "cloud" ? "本轮 API Key（必填）" : "本轮 Key（可选）"}</span><input aria-label="Agent 临时 API Key" type="password" value={keyValue} onChange={(event) => { keyRef.current = event.target.value; setKeyValue(event.target.value); }} autoComplete="off" /></label>
          <span><ShieldCheck size={13} />发送后立即清空，不写入 checkpoint</span>
          <button className="primary-action" disabled={send.isPending || !draft.trim() || (session.target_kind === "cloud" && !keyValue)}><Send size={15} />发送</button>
        </div>
      </form>
    </main>
  );
}

function AgentContext({ runtime, tools, events, activeSkillId }: {
  runtime: Awaited<ReturnType<typeof api.agentBootstrap>>["runtime"] | undefined;
  tools: Awaited<ReturnType<typeof api.agentBootstrap>>["tools"];
  events: Awaited<ReturnType<typeof api.agentEvents>>;
  activeSkillId?: string;
}) {
  return (
    <aside className="agent-context-panel">
      <header><span className="eyebrow">AGENT CORE</span><h2>编排上下文</h2></header>
      <div className="agent-core-card"><div className="core-pulse"><BrainCircuit size={25} /><i /></div><p><b>{runtime?.framework ?? "LangChain"}</b><span>BaseChatModel + Tool Loop</span></p><StatusPill status="READY" /></div>
      <dl className="runtime-facts">
        <div><dt>SKILL</dt><dd>{activeSkillId ?? "NOT SELECTED"}</dd></div>
        <div><dt>MEMORY</dt><dd>{runtime?.memory ?? "CHECKPOINTER"}</dd></div>
        <div><dt>MAX LOOP</dt><dd>{runtime?.max_iterations ?? 4}</dd></div>
        <div><dt>AUTO RETRY</dt><dd>{runtime?.automatic_model_retries ?? 0}</dd></div>
      </dl>
      <div className="context-section-head"><span>TOOL REGISTRY</span><b>{tools.length}</b></div>
      <div className="agent-tool-list">{tools.map((tool) => <article key={tool.id}><Wrench size={14} /><div><b>{tool.name}</b><span>{tool.id}</span></div><i>{tool.mode === "read_only" ? "R/O" : "GATED"}</i></article>)}</div>
      <div className="context-section-head"><span>LIVE TRACE</span><b>{events.length}</b></div>
      <div className="agent-trace-list">{events.slice(-6).reverse().map((event) => <article key={event.event_id}><i className={`trace-${event.status.toLowerCase()}`} /><div><b>{event.event_type.replaceAll("_", " ")}</b><span>{event.name}</span></div><small>#{event.sequence}</small></article>)}</div>
    </aside>
  );
}
