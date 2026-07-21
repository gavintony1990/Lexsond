import { useEffect, useMemo, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Cloud, KeyRound, Laptop, Plus, Radio, RotateCcw, Search, Server, Trash2, X } from "lucide-react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { api } from "../api";
import type { Target } from "../types";
import { EmptyState, ErrorNotice, formatTime, PageHead, StatusPill } from "../ui";

const targetSchema = z.object({
  name: z.string().min(1, "请输入目标名称").max(120),
  provider_id: z.string(),
  target_kind: z.enum(["local", "cloud"]),
  base_url: z.string().url("请输入完整 HTTP(S) 地址"),
  default_model: z.string().max(256),
  credential_ref: z.string().max(2048),
});
type TargetForm = z.infer<typeof targetSchema>;

export function Targets() {
  const [showArchived, setShowArchived] = useState(false);
  const [editing, setEditing] = useState<Target | null | "new">(null);
  const [catalogTarget, setCatalogTarget] = useState<Target | null>(null);
  const [catalogKey, setCatalogKey] = useState("");
  const queryClient = useQueryClient();
  const bootstrap = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap });
  const targets = useQuery({ queryKey: ["targets", showArchived], queryFn: () => api.targets(showArchived) });
  const providers = bootstrap.data?.providers ?? [];
  const form = useForm<TargetForm>({
    resolver: zodResolver(targetSchema),
    defaultValues: { name: "", provider_id: "ollama", target_kind: "local", base_url: "http://127.0.0.1:11434/v1", default_model: "", credential_ref: "" },
  });

  useEffect(() => {
    if (editing === "new") {
      form.reset({ name: "", provider_id: "ollama", target_kind: "local", base_url: "http://127.0.0.1:11434/v1", default_model: "", credential_ref: "" });
    } else if (editing) {
      form.reset({ name: editing.name, provider_id: editing.provider_id ?? "", target_kind: editing.target_kind, base_url: editing.base_url, default_model: editing.default_model, credential_ref: editing.credential_ref ?? "" });
    }
  }, [editing, form]);

  const save = useMutation({
    mutationFn: async (value: TargetForm) => {
      const payload = { ...value, provider_id: value.provider_id || null, credential_ref: value.credential_ref || null };
      if (editing && editing !== "new") return api.updateTarget(editing.id, { ...payload, version: editing.version });
      return api.createTarget(payload);
    },
    onSuccess: () => {
      setEditing(null);
      queryClient.invalidateQueries({ queryKey: ["targets"] });
      queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
    },
  });
  const lifecycle = useMutation({
    mutationFn: async ({ action, target }: { action: "archive" | "restore" | "purge"; target: Target }) => {
      if (action === "archive") return api.archiveTarget(target.id);
      if (action === "restore") return api.restoreTarget(target.id);
      return api.purgeTarget(target.id);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["targets"] }),
  });
  const catalog = useMutation({
    gcTime: 0,
    mutationFn: () => api.catalog(catalogTarget!.id, catalogKey || null),
    onSettled: () => setCatalogKey(""),
  });
  const closeCatalog = () => {
    setCatalogKey("");
    setCatalogTarget(null);
    catalog.reset();
  };

  const selectedProvider = useMemo(() => providers.find((item) => item.id === form.watch("provider_id")), [providers, form.watch("provider_id")]);
  const chooseProvider = (providerId: string) => {
    form.setValue("provider_id", providerId);
    const provider = providers.find((item) => item.id === providerId);
    if (provider) {
      form.setValue("target_kind", provider.target_kind);
      form.setValue("base_url", provider.base_url);
      form.setValue("default_model", provider.default_model);
    }
  };

  return (
    <div className="page-stack">
      <PageHead eyebrow="TARGET ARRAY / 目标阵列" title="绑定要测量的海域" description="目标只保存地址、模型与非机密凭据引用；真实 API Key 每次运行时临时输入。" action={<button className="primary-action" onClick={() => setEditing("new")}><Plus size={16} />新增目标</button>} />
      {(targets.error || save.error) && <ErrorNotice error={targets.error || save.error} />}
      <div className="toolbar panel-lite"><div className="toolbar-caption"><Radio size={15} /><b>{targets.data?.length ?? 0}</b> 个已注册信标</div><label className="toggle-label"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} /><span />显示已归档</label></div>
      <section className="target-grid stagger-grid">
        {targets.data?.map((target) => {
          const provider = providers.find((item) => item.id === target.provider_id);
          return (
            <article className={`target-card ${target.archived_at ? "archived" : ""}`} key={target.id}>
              <header>
                <div className={`target-icon ${target.target_kind}`}>
                  {target.target_kind === "cloud" ? <Cloud size={21} /> : <Laptop size={21} />}
                  <i />
                </div>
                <div><span>{provider?.english_name ?? "CUSTOM ENDPOINT"}</span><h2>{target.name}</h2></div>
                <StatusPill status={target.archived_at ? "ARCHIVED" : "BOUND"} />
              </header>
              <div className="target-address"><span>BASE URL</span><code>{target.base_url}</code></div>
              <dl>
                <div><dt>DEFAULT MODEL</dt><dd>{target.default_model || "未指定"}</dd></div>
                <div><dt>AUTH BINDING</dt><dd>{target.credential_ref_configured ? <><KeyRound size={13} /> SECRET REF</> : "EPHEMERAL / NONE"}</dd></div>
                <div><dt>UPDATED</dt><dd>{formatTime(target.updated_at)}</dd></div>
              </dl>
              <footer>
                {!target.archived_at ? <>
                  <button className="secondary-action" onClick={() => { setCatalogKey(""); setCatalogTarget(target); catalog.reset(); }}><Search size={14} />发现模型</button>
                  <button className="ghost-action" onClick={() => setEditing(target)}>编辑</button>
                  <button className="icon-button" onClick={() => lifecycle.mutate({ action: "archive", target })} aria-label="归档"><Archive size={15} /></button>
                </> : <>
                  <button className="secondary-action" onClick={() => lifecycle.mutate({ action: "restore", target })}><RotateCcw size={14} />恢复</button>
                  <button className="danger-action" onClick={() => window.prompt(`输入目标名称「${target.name}」确认永久清除`) === target.name && lifecycle.mutate({ action: "purge", target })}><Trash2 size={14} />清除</button>
                </>}
              </footer>
            </article>
          );
        })}
      </section>
      {!targets.data?.length && <div className="panel"><EmptyState icon={Server} title="目标阵列为空" body="先添加本地模型服务或云端 OpenAI 兼容 API。" action={<button className="primary-action" onClick={() => setEditing("new")}>添加第一个目标</button>} /></div>}

      {editing && (
        <div className="drawer-layer" role="dialog" aria-modal="true" aria-label="目标编辑器">
          <button className="drawer-scrim" onClick={() => setEditing(null)} aria-label="关闭" />
          <aside className="drawer">
            <header><div><span className="eyebrow">TARGET MANIFEST</span><h2>{editing === "new" ? "新增探测目标" : "编辑目标"}</h2></div><button className="icon-button" onClick={() => setEditing(null)}><X size={19} /></button></header>
            <form onSubmit={form.handleSubmit((value) => save.mutate(value))} className="form-stack">
              <label>目标名称<input {...form.register("name")} placeholder="例如：DeepSeek 主账号" />{form.formState.errors.name && <small>{form.formState.errors.name.message}</small>}</label>
              <label>Provider<select {...form.register("provider_id")} onChange={(event) => chooseProvider(event.target.value)}><option value="">自定义兼容端点</option>{providers.map((provider) => <option value={provider.id} key={provider.id}>{provider.name}</option>)}</select></label>
              <div className="form-row"><label>目标类型<select {...form.register("target_kind")}><option value="local">本地</option><option value="cloud">云端</option></select></label><label>默认模型<input {...form.register("default_model")} placeholder="model-id" /></label></div>
              <label>Base URL<input {...form.register("base_url")} spellCheck={false} />{form.formState.errors.base_url && <small>{form.formState.errors.base_url.message}</small>}</label>
              {form.watch("target_kind") === "cloud" && <label>生产凭据引用 <span className="optional">可选</span><input {...form.register("credential_ref")} placeholder="vault://ai/deepseek" /><small>只保存 Secret Manager URI，不保存明文 Key。</small></label>}
              <div className="manifest-preview"><Server size={17} /><div><b>{selectedProvider?.english_name ?? "CUSTOM"}</b><span>{form.watch("base_url") || "等待地址"}</span></div></div>
              {save.error && <ErrorNotice error={save.error} />}
              <div className="form-actions"><button type="button" className="ghost-action" onClick={() => setEditing(null)}>取消</button><button className="primary-action" disabled={save.isPending}>{save.isPending ? "保存中…" : "保存目标"}</button></div>
            </form>
          </aside>
        </div>
      )}

      {catalogTarget && (
        <div className="modal-layer" role="dialog" aria-modal="true" aria-label="模型目录">
          <button className="drawer-scrim" onClick={closeCatalog} aria-label="关闭" />
          <section className="modal-card catalog-modal">
            <header><div><span className="eyebrow">MODEL CATALOG</span><h2>{catalogTarget.name}</h2></div><button className="icon-button" onClick={closeCatalog}><X size={19} /></button></header>
            <p>Key 仅用于本次 `/models` 请求，完成后立即从输入状态清除。</p>
            {catalogTarget.target_kind === "cloud" && <label>临时 API Key<input type="password" value={catalogKey} onChange={(event) => setCatalogKey(event.target.value)} autoComplete="off" placeholder="sk-••••••••" /></label>}
            <button className="primary-action wide" onClick={() => catalog.mutate()} disabled={catalog.isPending}>{catalog.isPending ? "正在测深…" : "连接并读取模型"}</button>
            {catalog.error && <ErrorNotice error={catalog.error} />}
            {catalog.data && <div className="catalog-results"><div className="catalog-count"><b>{catalog.data.model_count}</b><span>MODELS REPORTED</span></div>{catalog.data.models.map((model) => <div className="catalog-row" key={model.id}><code>{model.id}</code><span>{model.probe_types.join(" · ") || "capability unknown"}</span></div>)}</div>}
          </section>
        </div>
      )}
    </div>
  );
}
