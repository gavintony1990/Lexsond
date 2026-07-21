const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  bootstrap: null,
  runs: [],
  current: null,
  pollTimer: null,
  detectTimer: null,
  detectSequence: 0,
  discoverySequence: 0,
  keyRevision: 0,
  detectionRevision: -1,
  providerDecision: "IDLE",
  targetKind: "local",
  launching: false,
  modelCatalog: new Map(),
};

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  updateClock();
  window.setInterval(updateClock, 1000);
  initialize().catch((error) => {
    toast(error.message || "控制台初始化失败", true);
  });
});

async function initialize() {
  const [bootstrap, runs] = await Promise.all([
    api("/api/bootstrap"),
    api("/api/runs"),
  ]);
  state.bootstrap = bootstrap;
  state.runs = runs;
  applyBootstrap();
  renderHistory();
  if (runs.length) {
    await selectRun(runs[0].run_id);
  }
}

function bindEvents() {
  $("#probe-form").addEventListener("submit", launchRun);
  $("#refresh-runs").addEventListener("click", refreshRuns);
  $("#discover-models").addEventListener("click", discoverModels);
  $("#toggle-key").addEventListener("click", toggleKey);
  $("#detect-key").addEventListener("click", detectProvider);
  $("#api-key").addEventListener("input", scheduleProviderDetection);
  $("#provider-source").addEventListener("change", selectProviderManually);
  $("#base-url").addEventListener("input", markCustomTargetEdited);
  $("#run-mode").addEventListener("change", syncModeControls);
  $("#probe-type").addEventListener("change", syncProbeControls);
  $("#model").addEventListener("input", syncSelectedModel);
  $$('input[name="target_kind"]').forEach((input) => {
    input.addEventListener("change", syncTargetKind);
  });
}

function applyBootstrap() {
  const { defaults, product } = state.bootstrap;
  $("#run-mode").value = defaults.run_mode;
  $("#probe-type").value = defaults.probe_type || "chat";
  $("#stream").checked = defaults.stream;
  $("#timeout").value = defaults.timeout_seconds;
  $("#console-version").textContent = `V${product.version}`;
  const kindInput = $(`input[name="target_kind"][value="${defaults.target_kind}"]`);
  if (kindInput) kindInput.checked = true;
  syncTargetKind(true);
  syncModeControls();
}

function syncModeControls() {
  syncProbeControls();
  const isCanary = $("#run-mode").value === "canary";
  $("#timeout").title = isCanary ? "标准套件使用固定的 30 秒超时" : "单请求超时";
  $("#stream").closest(".switch-field").title = isCanary
    ? "标准套件使用固定的流式设置"
    : "切换单请求是否流式返回";
}

function syncProbeControls() {
  const probeType = $("#probe-type").value;
  const canaryOption = $('#run-mode option[value="canary"]');
  const canarySupported = probeType === "chat";
  canaryOption.disabled = !canarySupported;
  if (!canarySupported && $("#run-mode").value === "canary") {
    $("#run-mode").value = "single";
  }
  const streamSupported = ["chat", "vision"].includes(probeType);
  if (!streamSupported) $("#stream").checked = false;
  $("#stream").disabled = !streamSupported;
  syncRunButton();
  if (state.bootstrap) renderWorkflow(null, probeType);
}

function syncTargetKind(initial = false) {
  if (!state.bootstrap) return;
  const checked = $('input[name="target_kind"]:checked');
  state.targetKind = checked?.value || "local";
  const isCloud = state.targetKind === "cloud";
  const keyInput = $("#api-key");
  keyInput.required = isCloud;
  keyInput.value = "";
  keyInput.type = "password";
  $("#detect-key").hidden = !isCloud;
  $("#api-key-label").textContent = isCloud
    ? "API Key · 云服务必填"
    : "API Key · 本地服务可选";
  keyInput.placeholder = isCloud
    ? "仅在本机内存中识别与使用"
    : "本地无鉴权请留空；DeepSeek Key 请切换云服务";
  state.keyRevision += 1;
  cancelPendingDetection();
  populateProviders();

  if (isCloud) {
    clearTarget();
    setProviderDecision("IDLE", state.keyRevision);
    $("#source-kicker").textContent = "KEY SIGNATURE / LOCAL ONLY";
    renderDetection("idle", "等待云服务密钥", "先识别密钥来源，避免把密钥发送到错误端点。", "WAITING");
  } else {
    const defaultId = initial ? state.bootstrap.defaults.provider_id : "ollama";
    const provider = providersForTarget().find((item) => item.id === defaultId)
      || providersForTarget()[0];
    if (provider) applyProvider(provider);
    setProviderDecision("CONFIRMED", state.keyRevision);
    $("#source-kicker").textContent = "REAL ENDPOINT / DIRECT";
    renderDetection("matched", `本地目标：${provider?.name || "自定义服务"}`, "可无密钥连接；DeepSeek / OpenAI Key 请先切换到云服务。", "LOCAL");
  }
  resetConnection();
}

function providersForTarget() {
  return (state.bootstrap?.providers || []).filter(
    (provider) => provider.target_kind === state.targetKind,
  );
}

function populateProviders() {
  const select = $("#provider-source");
  select.replaceChildren();
  const custom = document.createElement("option");
  custom.value = "";
  custom.textContent = state.targetKind === "local"
    ? "自定义本地 OpenAI 兼容端点"
    : "自定义云端 OpenAI 兼容服务";
  select.append(custom);
  providersForTarget().forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = `${provider.name} / ${provider.english_name}`;
    select.append(option);
  });
}

function toggleKey() {
  const input = $("#api-key");
  input.type = input.type === "password" ? "text" : "password";
}

function scheduleProviderDetection() {
  if (state.targetKind !== "cloud") {
    resetConnection();
    return;
  }
  if (state.detectTimer) window.clearTimeout(state.detectTimer);
  markKeyDirty();
  $("#detect-key").disabled = false;
  if (!$("#api-key").value.trim()) {
    setProviderDecision("IDLE", state.keyRevision);
    renderDetection("idle", "等待密钥", "输入后仅按格式在本机识别，不会联网试探供应商。", "WAITING");
    return;
  }
  state.detectTimer = window.setTimeout(detectProvider, 420);
}

async function detectProvider() {
  if (state.targetKind !== "cloud") return true;
  if (state.detectTimer) window.clearTimeout(state.detectTimer);
  state.detectTimer = null;
  const apiKey = $("#api-key").value;
  if (!apiKey.trim()) {
    setProviderDecision("IDLE", state.keyRevision);
    renderDetection("idle", "等待密钥", "请先输入 API Key，再进行本地格式识别。", "WAITING");
    return false;
  }

  const sequence = ++state.detectSequence;
  const revision = state.keyRevision;
  const button = $("#detect-key");
  button.disabled = true;
  setProviderDecision("DETECTING", -1);
  renderDetection("detecting", "正在识别密钥签名", "只分析密钥格式，不连接任何外部 API。", "SCANNING");
  try {
    const result = await api("/api/providers/detect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKey,
        provider_id: $("#provider-source").value || null,
      }),
    });
    if (sequence !== state.detectSequence || revision !== state.keyRevision) return false;
    applyDetectionResult(result, revision);
    return true;
  } catch (error) {
    if (sequence !== state.detectSequence || revision !== state.keyRevision) return false;
    clearTarget();
    setProviderDecision("ERROR", revision);
    renderDetection("unknown", "识别服务暂不可用", error.message || "请手动选择 API 来源。", "ERROR");
    return false;
  } finally {
    if (sequence === state.detectSequence) button.disabled = false;
  }
}

function applyDetectionResult(result, revision) {
  if (["MATCHED", "CONFIRMED"].includes(result.status) && result.provider) {
    applyProvider(result.provider);
    setProviderDecision(result.status, revision);
    renderDetection(
      "matched",
      result.status === "CONFIRMED"
        ? `已按选择确认：${result.provider.name}`
        : `已识别：${result.provider.name}`,
      result.status === "CONFIRMED"
        ? `通用密钥前缀无法唯一识别厂商，已安全绑定到 ${result.provider.base_url}。`
        : `已补全 ${result.provider.base_url} 与建议模型 ${result.provider.default_model}。`,
      result.status === "CONFIRMED" ? "USER" : result.confidence,
    );
    return;
  }
  if (result.status === "MANUAL" && result.provider) {
    applyProvider(result.provider);
    setProviderDecision("MANUAL", revision);
    renderDetection(
      "unknown",
      `已选择：${result.provider.name}`,
      "密钥格式无法验证来源；连接模型时将仅发送到所选厂商的固定官方端点。",
      "UNVERIFIED",
    );
    return;
  }
  if (result.status === "AMBIGUOUS") {
    clearTarget();
    setProviderDecision("AMBIGUOUS", revision);
    renderDetection(
      "ambiguous",
      "密钥格式被多个来源共同使用",
      "为避免误投密钥，请确认来源后再启动探针。",
      "CONFIRM",
      result.candidates,
    );
    return;
  }
  clearTarget();
  setProviderDecision("CUSTOM", revision);
  renderDetection(
    "unknown",
    "未识别出唯一来源",
    "可从来源列表选择预设，或直接填写自定义 Base URL 与模型。",
    "MANUAL",
  );
}

function renderDetection(kind, title, detail, confidence, candidates = []) {
  const panel = $("#source-detection");
  panel.className = `source-detection ${kind}`;
  $("#source-title").textContent = title;
  $("#source-detail").textContent = detail;
  $("#detection-confidence").textContent = confidence;
  const candidateList = $("#source-candidates");
  candidateList.replaceChildren();
  candidates.forEach((provider) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "source-candidate";
    button.textContent = provider.name;
    button.addEventListener("click", async () => {
      cancelPendingDetection();
      $("#provider-source").value = provider.id;
      resetConnection();
      await detectProvider();
    });
    candidateList.append(button);
  });
  if (kind === "ambiguous") {
    const customButton = document.createElement("button");
    customButton.type = "button";
    customButton.className = "source-candidate";
    customButton.textContent = "自定义端点";
    customButton.addEventListener("click", chooseCustomTarget);
    candidateList.append(customButton);
  }
}

function applyProvider(provider) {
  $("#provider-source").value = provider.id;
  $("#base-url").value = provider.base_url;
  $("#model").value = provider.default_model;
  resetConnection();
}

async function selectProviderManually() {
  cancelPendingDetection();
  const providerId = $("#provider-source").value;
  if (!providerId) {
    clearTarget();
    setProviderDecision("CUSTOM", state.keyRevision);
    renderDetection(
      "unknown",
      state.targetKind === "local" ? "自定义本地端点" : "自定义云服务端点",
      "请手动填写 Base URL，然后读取目标真实模型列表。",
      "MANUAL",
    );
    return;
  }
  const provider = state.bootstrap?.providers.find((item) => item.id === providerId);
  if (!provider) return;
  applyProvider(provider);
  if (state.targetKind === "cloud") {
    if (!$("#api-key").value.trim()) {
      setProviderDecision("IDLE", state.keyRevision);
      renderDetection(
        "idle",
        `已选择：${provider.name}`,
        "请输入 API Key，来源状态将由服务端校验后更新。",
        "WAITING",
      );
      return;
    }
    await detectProvider();
    return;
  }
  setProviderDecision("CONFIRMED", state.keyRevision);
  renderDetection("matched", `已选择：${provider.name}`, `目标端点 ${provider.base_url}`, "MANUAL");
}

function markKeyDirty() {
  state.keyRevision += 1;
  state.detectSequence += 1;
  setProviderDecision("DIRTY", -1);
  resetConnection();
}

function cancelPendingDetection() {
  if (state.detectTimer) window.clearTimeout(state.detectTimer);
  state.detectTimer = null;
  state.detectSequence += 1;
  $("#detect-key").disabled = false;
}

function markCustomTargetEdited() {
  cancelPendingDetection();
  $("#provider-source").value = "";
  setProviderDecision("CUSTOM", state.keyRevision);
  renderDetection("unknown", "自定义 API 来源", "当前 Base URL 将作为已确认的真实目标。", "MANUAL");
  resetConnection();
}

function chooseCustomTarget() {
  cancelPendingDetection();
  clearTarget();
  setProviderDecision("CUSTOM", state.keyRevision);
  renderDetection("unknown", "自定义 API 来源", "请手动填写 Base URL，再读取真实模型。", "MANUAL");
}

function clearTarget() {
  $("#provider-source").value = "";
  $("#base-url").value = "";
  $("#model").value = "";
}

function setProviderDecision(decision, revision) {
  state.providerDecision = decision;
  state.detectionRevision = revision;
  syncRunButton();
}

function syncRunButton() {
  const blocked = state.targetKind === "cloud"
    && ["IDLE", "DIRTY", "DETECTING", "AMBIGUOUS", "ERROR"].includes(state.providerDecision);
  const button = $("#run-button");
  button.disabled = state.launching
    || blocked
    || ["catalog_only", "manual_required"].includes($("#probe-type").value);
}

function resetConnection() {
  state.discoverySequence += 1;
  state.modelCatalog = new Map();
  const discoverButton = $("#discover-models");
  if (discoverButton) discoverButton.disabled = false;
  const list = $("#model-options");
  if (list) list.replaceChildren();
  if (["catalog_only", "manual_required"].includes($("#probe-type").value)) {
    $("#probe-type").value = "chat";
  }
  renderCapability(
    "idle",
    "模型能力尚未识别",
    "读取目录后，将显示厂商 API 明确提供的输入、输出模态和可用探测类型。",
    "UNKNOWN",
  );
  syncProbeControls();
  renderConnection(
    "idle",
    "尚未读取模型目录",
    "将请求目标的真实模型目录接口，数据完全取自端点响应。",
    "NOT CHECKED",
  );
}

function renderCapability(kind, title, detail, status) {
  const panel = $("#model-capability");
  panel.className = `target-connection ${kind}`;
  $("#capability-title").textContent = title;
  $("#capability-detail").textContent = detail;
  $("#capability-status").textContent = status;
}

function syncSelectedModel() {
  const modelId = $("#model").value.trim();
  const entry = state.modelCatalog.get(modelId);
  if (!entry) {
    $("#probe-type").value = "manual_required";
    renderCapability(
      "empty",
      modelId ? "目录中没有该模型的能力元数据" : "请选择模型",
      "可手动选择探测类型；工具不会仅凭模型名称猜测模态。",
      "MANUAL",
    );
    syncProbeControls();
    return;
  }

  const inputs = entry.input_modalities || [];
  const outputs = entry.output_modalities || [];
  const probeTypes = entry.probe_types || [];
  if (entry.capability_source === "PROVIDER_METADATA" && probeTypes.length) {
    const recommended = probeTypes.includes("vision") ? "vision" : probeTypes[0];
    $("#probe-type").value = recommended;
    const modality = `${inputs.join("+") || "?"} → ${outputs.join("+") || "?"}`;
    renderCapability(
      "connected",
      `${entry.name} · ${modality}`,
      `厂商目录明确声明 · 可用探测：${probeTypes.join(" / ")}`,
      "DECLARED",
    );
  } else if (entry.capability_source === "PROVIDER_METADATA") {
    $("#probe-type").value = "catalog_only";
    const modality = `${inputs.join("+") || "?"} → ${outputs.join("+") || "?"}`;
    renderCapability(
      "empty",
      `${entry.name} · ${modality}`,
      `已从目录识别端点类型：${(entry.endpoint_types || []).join(" / ") || "未知"}；当前版本不发送主动请求。`,
      "CATALOG ONLY",
    );
  } else {
    $("#probe-type").value = "manual_required";
    renderCapability(
      "empty",
      `${entry.name} · 厂商未提供模态字段`,
      "模型已完整列出；请手动确认文本、视觉、向量、图像或音频探测类型。",
      "MANUAL",
    );
  }
  syncProbeControls();
}

function renderConnection(kind, title, detail, status) {
  const panel = $("#target-connection");
  panel.className = `target-connection ${kind}`;
  $("#connection-title").textContent = title;
  $("#connection-detail").textContent = detail;
  $("#connection-status").textContent = status;
}

async function ensureCloudTargetConfirmed() {
  if (state.targetKind !== "cloud") return true;
  if (!$("#api-key").value.trim()) {
    toast("云服务检测需要 API Key", true);
    return false;
  }
  if (
    state.detectionRevision !== state.keyRevision
    || ["DIRTY", "DETECTING"].includes(state.providerDecision)
  ) {
    await detectProvider();
  }
  if (state.providerDecision === "AMBIGUOUS") {
    toast("请先确认密钥对应的云服务来源", true);
    return false;
  }
  if (["IDLE", "DIRTY", "DETECTING", "ERROR"].includes(state.providerDecision)) {
    toast("密钥与云服务目标尚未完成确认", true);
    return false;
  }
  return true;
}

async function discoverModels() {
  const baseInput = $("#base-url");
  const keyInput = $("#api-key");
  if (!baseInput.reportValidity() || !(await ensureCloudTargetConfirmed())) return;

  const button = $("#discover-models");
  const sequence = ++state.discoverySequence;
  button.disabled = true;
  renderConnection("connecting", "正在连接真实目标", `读取 ${baseInput.value.trim()}/models`, "CONNECTING");
  try {
    const result = await api("/api/targets/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: baseInput.value.trim(),
        api_key: keyInput.value.trim() || null,
        target_kind: state.targetKind,
        provider_id: $("#provider-source").value || null,
        custom_target_confirmed: state.providerDecision === "CUSTOM",
      }),
    });
    if (sequence !== state.discoverySequence) return;
    const list = $("#model-options");
    list.replaceChildren();
    state.modelCatalog = new Map(
      (result.model_catalog || []).map((entry) => [entry.id, entry]),
    );
    result.models.forEach((model) => {
      const option = document.createElement("option");
      option.value = model;
      const entry = state.modelCatalog.get(model);
      if (entry) {
        const modalities = [
          ...(entry.input_modalities || []),
          ...(entry.output_modalities || []),
        ];
        option.label = `${entry.name} · ${[...new Set(modalities)].join("+") || "能力未知"}`;
      }
      list.append(option);
    });
    if (!$("#model").value.trim() && result.models.length) {
      $("#model").value = result.models[0];
    }
    syncSelectedModel();
    if (result.model_count) {
      const declaredCount = (result.model_catalog || []).filter(
        (entry) => entry.capability_source === "PROVIDER_METADATA",
      ).length;
      renderConnection(
        "connected",
        `已读取 ${result.model_count} 个真实模型`,
        `${result.auth_mode === "bearer" ? "Bearer 鉴权" : "无鉴权"} · ${declaredCount} 个带厂商能力元数据`,
        "CONNECTED",
      );
      toast(`真实连接成功：发现 ${result.model_count} 个模型`);
    } else {
      renderConnection(
        "empty",
        "真实目标已连接，但还没有模型",
        state.targetKind === "local"
          ? "请先在本地模型服务中下载或加载模型，再重新读取。"
          : "厂商 API 当前没有返回此 Key 可访问的模型。",
        "EMPTY",
      );
      toast("连接成功，但目标尚未提供可用模型", true);
    }
  } catch (error) {
    if (sequence !== state.discoverySequence) return;
    renderConnection("failed", "目标连接失败", error.message || "无法读取模型目录", "FAILED");
    toast(error.message || "无法读取真实模型列表", true);
  } finally {
    if (sequence === state.discoverySequence) button.disabled = false;
  }
}

async function launchRun(event) {
  event.preventDefault();
  const keyInput = $("#api-key");
  if (!(await ensureCloudTargetConfirmed())) return;
  if (!$("#probe-form").reportValidity()) return;
  if (["catalog_only", "manual_required"].includes($("#probe-type").value)) {
    toast("请先为该模型选择一个可执行的探测类型", true);
    return;
  }

  const payload = {
    base_url: $("#base-url").value.trim(),
    api_key: keyInput.value.trim() || null,
    model: $("#model").value.trim(),
    target_kind: state.targetKind,
    run_mode: $("#run-mode").value,
    probe_type: $("#probe-type").value,
    stream: $("#stream").checked,
    timeout_seconds: Number($("#timeout").value),
    provider_id: $("#provider-source").value || null,
    custom_target_confirmed: state.providerDecision === "CUSTOM",
  };

  setLaunching(true);
  try {
    const run = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.current = run;
    renderRun(run);
    await refreshRuns(false);
    toast("探针已进入执行队列");
    if (run.state === "RUNNING") startPolling(run.run_id);
  } catch (error) {
    toast(error.message || "无法启动探针", true);
  } finally {
    keyInput.value = "";
    keyInput.type = "password";
    state.detectSequence += 1;
    state.keyRevision += 1;
    if (state.targetKind === "cloud") {
      setProviderDecision("IDLE", state.keyRevision);
      renderDetection("idle", "密钥已从表单清除", "云服务配置已保留；再次检测需重新输入密钥。", "CLEARED");
    } else {
      setProviderDecision($("#provider-source").value ? "CONFIRMED" : "CUSTOM", state.keyRevision);
      renderDetection("matched", "本地真实目标已保留", "本地 API Key（如有）已从表单清除。", "LOCAL");
    }
    setLaunching(false);
  }
}

function setLaunching(active) {
  const button = $("#run-button");
  state.launching = active;
  syncRunButton();
  $("span", button).textContent = active ? "正在提交" : "启动探针";
}

async function refreshRuns(showToast = true) {
  try {
    state.runs = await api("/api/runs");
    renderHistory();
    if (showToast) toast("运行历史已刷新");
  } catch (error) {
    toast(error.message || "刷新失败", true);
  }
}

function renderHistory() {
  const list = $("#history-list");
  list.replaceChildren();
  if (!state.runs.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "NO RUNS YET\n启动第一次检测";
    list.append(empty);
    return;
  }

  state.runs.forEach((run) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "history-item";
    if (state.current?.run_id === run.run_id) item.classList.add("is-selected");
    item.addEventListener("click", () => selectRun(run.run_id));

    const dot = document.createElement("i");
    dot.className = `history-dot ${historyStatus(run)}`;
    const content = document.createElement("span");
    content.className = "history-content";
    const model = document.createElement("strong");
    model.textContent = run.config.model;
    const meta = document.createElement("span");
    const status = document.createElement("b");
    status.textContent = run.result_status || run.state;
    const time = document.createElement("time");
    time.textContent = relativeTime(run.created_at);
    meta.append(status, time);
    content.append(model, meta);
    item.append(dot, content);
    list.append(item);
  });
}

async function selectRun(runId) {
  clearPolling();
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    state.current = run;
    renderRun(run);
    renderHistory();
    if (run.state === "RUNNING") startPolling(run.run_id);
  } catch (error) {
    toast(error.message || "无法读取运行记录", true);
  }
}

function startPolling(runId) {
  clearPolling();
  const poll = async () => {
    try {
      const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
      if (state.current?.run_id !== runId) return;
      state.current = run;
      renderRun(run);
      if (run.state === "RUNNING") {
        state.pollTimer = window.setTimeout(poll, 400);
      } else {
        await refreshRuns(false);
        toast(run.result_status === "PASS" ? "检测完成：PASS" : "检测完成：发现异常", run.result_status !== "PASS");
      }
    } catch (error) {
      toast(error.message || "运行状态轮询失败", true);
      state.pollTimer = window.setTimeout(poll, 1800);
    }
  };
  state.pollTimer = window.setTimeout(poll, 120);
}

function clearPolling() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function renderRun(run) {
  const panel = $("#signal-panel");
  const status = $("#run-state");
  const shortId = run.run_id.slice(0, 8).toUpperCase();
  $("#selected-run-id").textContent = `RUN ${shortId}`;
  $("#result-model").textContent = `MODEL / ${run.config.model}`;
  $("#result-time").textContent = `CAPTURE / ${formatDate(run.created_at)}`;
  panel.classList.toggle("is-running", run.state === "RUNNING");
  status.className = `run-state ${historyStatus(run)}`;
  renderWorkflow(run.workflow, run.config.probe_type, true);

  if (run.state === "RUNNING") {
    status.innerHTML = "<i></i> SAMPLING";
    $("#overall-score").textContent = "··";
    $("#score-dial").style.setProperty("--score", 0);
    const activeStep = run.workflow?.steps?.find((step) => step.status === "RUNNING");
    const failedStep = run.workflow?.steps?.find((step) => step.status === "FAIL");
    $("#signal-label").textContent = activeStep
      ? `${activeStep.stage} / LIVE STEP`
      : "PIPELINE IN PROGRESS";
    $("#signal-title").textContent = activeStep
      ? activeStep.label
      : (failedStep ? `停在：${failedStep.label}` : `正在探测 ${run.config.model}`);
    $("#signal-detail").textContent = activeStep
      ? activeStep.description
      : (run.config.run_mode === "canary"
        ? "标准套件正在采集多个样本并计算四维评分。"
        : "步骤状态正在持久化；原始多模态载荷不会进入流程记录。");
    resetResultPanels();
    return;
  }

  if (run.state === "FAILED" || !run.result) {
    status.innerHTML = "<i></i> FAILED";
    $("#overall-score").textContent = "--";
    $("#score-dial").style.setProperty("--score", 0);
    $("#signal-label").textContent = "INTERNAL EXECUTION FAILURE";
    $("#signal-title").textContent = "运行未产生有效结果";
    $("#signal-detail").textContent = "执行边界已阻止异常正文进入历史库，请检查目标配置或服务状态。";
    resetResultPanels();
    renderDiagnostics([run.failure_code || "EXECUTION_ERROR"], false);
    return;
  }

  renderResult(run.result);
}

function renderWorkflow(workflow, probeType = "chat", isRunContext = false) {
  const component = (state.bootstrap?.probe_components || []).find(
    (item) => item.id === (workflow?.component_id || probeType),
  );
  if (!component && !workflow) return;

  const isLegacy = isRunContext && !workflow;
  const isPreview = !workflow && !isLegacy;
  const value = workflow || {
    component_id: component.id,
    component_label: component.label,
    icon: component.icon,
    scenario: component.scenario,
    status: isLegacy ? "LEGACY" : "PREVIEW",
    current_step_id: null,
    steps: isLegacy
      ? []
      : component.steps.map((step) => ({
        ...step,
        status: "PENDING",
        started_at: null,
        finished_at: null,
        facts: [],
      })),
  };
  const steps = Array.isArray(value.steps) ? value.steps : [];
  const panel = $("#workflow-panel");
  panel.className = `panel workflow-panel is-${String(value.status || "preview").toLowerCase()}`;
  panel.classList.toggle("is-preview", isPreview);
  panel.classList.toggle("is-legacy", isLegacy);
  $("#workflow-icon").textContent = value.icon || component?.icon || "PRB";
  $("#workflow-kicker").textContent = isLegacy
    ? "LEGACY RUN / WORKFLOW UNAVAILABLE"
    : (isPreview
      ? "COMPONENT PREVIEW / NO TRAFFIC"
      : `COMPONENT RUN / ${value.run_mode?.toUpperCase() || "SINGLE"}`);
  $("#workflow-component").textContent = value.component_label || component?.label || "检测组件";
  $("#workflow-scenario").textContent = value.scenario || component?.scenario || "—";
  $("#workflow-status").textContent = value.status || "PREVIEW";

  const active = steps.find((step) => step.status === "RUNNING");
  const failed = steps.find((step) => step.status === "FAIL");
  const settled = steps.filter((step) => ["PASS", "FAIL", "SKIPPED"].includes(step.status)).length;
  const passed = steps.filter((step) => step.status === "PASS").length;
  const progress = steps.length ? settled / steps.length * 100 : 0;
  $("#workflow-progress-bar").style.width = `${progress}%`;
  $("#workflow-progress-label").textContent = isLegacy
    ? `0 / ${component.steps.length} CAPTURED · LEGACY`
    : (isPreview
      ? `${steps.length} STEPS / READY`
      : `${settled} / ${steps.length} SETTLED · ${passed} VERIFIED`);

  if (isLegacy) {
    $("#workflow-current-label").textContent = "旧记录没有分步流程数据";
    $("#workflow-current-detail").textContent = "该运行发生在流程追踪功能启用之前；结果仍可查看，但不会被误标为无流量预览。";
  } else if (active) {
    $("#workflow-current-label").textContent = active.label;
    $("#workflow-current-detail").textContent = active.description;
  } else if (failed) {
    $("#workflow-current-label").textContent = `失败停点 · ${failed.label}`;
    $("#workflow-current-detail").textContent = failed.description;
  } else if (["PASS", "WARN"].includes(value.status)) {
    $("#workflow-current-label").textContent = "全部检测步骤已结算";
    $("#workflow-current-detail").textContent = "每个节点的状态与安全测量摘要均已写入本地历史。";
  } else {
    $("#workflow-current-label").textContent = "等待启动";
    $("#workflow-current-detail").textContent = "下方展示该模态组件将实际执行的完整验证路径。";
  }

  const list = $("#workflow-steps");
  list.replaceChildren();
  if (isLegacy) {
    const notice = document.createElement("li");
    notice.className = "workflow-legacy";
    notice.textContent = "WORKFLOW UNAVAILABLE · LEGACY RESULT RETAINED";
    list.append(notice);
    return;
  }
  steps.forEach((step, index) => {
    const item = document.createElement("li");
    const status = String(step.status || "PENDING").toLowerCase();
    item.className = `workflow-step is-${status}`;
    item.dataset.stepId = step.id;

    const top = document.createElement("div");
    top.className = "workflow-step-top";
    const number = document.createElement("span");
    number.textContent = String(index + 1).padStart(2, "0");
    const stage = document.createElement("b");
    stage.textContent = step.stage;
    const marker = document.createElement("i");
    marker.setAttribute("aria-label", workflowStepStatusLabel(step.status));
    top.append(number, stage, marker);

    const title = document.createElement("h4");
    title.textContent = step.label;
    const description = document.createElement("p");
    description.textContent = step.description;
    const facts = document.createElement("div");
    facts.className = "workflow-facts";
    (step.facts || []).forEach((fact) => {
      const tag = document.createElement("span");
      tag.textContent = fact;
      facts.append(tag);
    });
    const footer = document.createElement("footer");
    const statusLabel = document.createElement("strong");
    statusLabel.textContent = workflowStepStatusLabel(step.status);
    const duration = document.createElement("time");
    duration.textContent = stepDuration(step);
    footer.append(statusLabel, duration);
    item.append(top, title, description, facts, footer);
    list.append(item);
  });
}

function workflowStepStatusLabel(status) {
  return {
    PENDING: "待执行",
    RUNNING: "检测中",
    PASS: "已验证",
    FAIL: "失败",
    SKIPPED: "已跳过",
  }[status] || "待执行";
}

function stepDuration(step) {
  if (step.status === "RUNNING") return "LIVE";
  if (!step.started_at || !step.finished_at) return "—";
  const milliseconds = new Date(step.finished_at).getTime() - new Date(step.started_at).getTime();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "—";
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(2)} s`;
}

function renderResult(result) {
  const isPass = result.status === "PASS";
  const scores = result.dimension_scores
    .map((item) => item.score)
    .filter((score) => Number.isFinite(score));
  const overall = scores.length
    ? scores.reduce((sum, score) => sum + score, 0) / scores.length
    : (isPass ? 100 : 0);

  const status = $("#run-state");
  status.className = `run-state ${result.status.toLowerCase()}`;
  status.innerHTML = `<i></i> ${result.status}`;
  $("#overall-score").textContent = Math.round(overall);
  $("#score-dial").style.setProperty("--score", overall.toFixed(1));
  $("#signal-label").textContent = isPass ? "SIGNAL WITHIN POLICY" : "POLICY DEVIATION DETECTED";
  $("#signal-title").textContent = isPass ? "目标通过当前质量门槛" : "目标未通过当前质量门槛";
  $("#signal-detail").textContent = `${result.measurements.length} 个样本 · ${result.suite_name} / ${result.suite_version}`;

  renderDimensions(result.dimension_scores);
  renderMetrics(result);
  renderChart(result.measurements);
  renderMeasurements(result.measurements);
  const diagnostics = collectDiagnostics(result);
  renderDiagnostics(diagnostics, isPass);
}

function resetResultPanels() {
  renderDimensions([]);
  renderMetrics(null);
  renderChart([]);
  renderMeasurements([]);
  renderDiagnostics(["RUNNING"], true);
}

function renderDimensions(scores) {
  const byName = new Map(scores.map((item) => [item.dimension, item]));
  $$(".dimension-card").forEach((card) => {
    const value = byName.get(card.dataset.dimension);
    card.classList.remove("is-pass", "is-fail", "is-warn", "is-unknown");
    const score = value?.score;
    $(".dimension-score strong", card).textContent = Number.isFinite(score) ? Math.round(score) : "--";
    $(".meter i", card).style.width = `${Number.isFinite(score) ? Math.max(0, Math.min(100, score)) : 0}%`;
    $(".dimension-status", card).textContent = value?.status || "等待数据";
    $(".dimension-samples", card).textContent = `${value?.sample_count || 0} SAMPLES`;
    card.classList.add(`is-${(value?.status || "unknown").toLowerCase()}`);
  });
}

function renderMetrics(result) {
  if (!result) {
    ["metric-ttft", "metric-e2e", "metric-tps", "metric-tokens"].forEach((id) => {
      $(`#${id}`).textContent = "--";
    });
    return;
  }
  const performance = result.dimension_scores.find((item) => item.dimension === "performance");
  const measurements = result.measurements;
  const ttft = performance?.metrics?.p95_ttft_ms ?? percentile(measurements.map((m) => m.ttft_ms), .95);
  const e2e = performance?.metrics?.p95_e2e_ms ?? percentile(measurements.map((m) => m.e2e_ms), .95);
  const tps = average(measurements.map((m) => m.output_tps));
  const tokens = measurements.reduce((sum, m) => sum + (m.provider_reported_total_tokens || 0), 0);
  $("#metric-ttft").textContent = formatNumber(ttft, 1);
  $("#metric-e2e").textContent = formatNumber(e2e, 1);
  $("#metric-tps").textContent = formatNumber(tps, 1);
  $("#metric-tokens").textContent = tokens || "--";
}

function renderChart(measurements) {
  const chart = $("#latency-chart");
  chart.replaceChildren();
  const rows = measurements.filter((m) => Number.isFinite(m.ttft_ms) || Number.isFinite(m.e2e_ms));
  $("#chart-empty").hidden = rows.length > 0;
  if (!rows.length) return;

  const width = 720;
  const height = 260;
  const pad = { left: 45, right: 20, top: 18, bottom: 30 };
  const allValues = rows.flatMap((m) => [m.ttft_ms, m.e2e_ms]).filter(Number.isFinite);
  const maxValue = Math.max(...allValues, 10) * 1.12;
  const x = (index) => pad.left + (rows.length === 1 ? 0 : index * (width - pad.left - pad.right) / (rows.length - 1));
  const y = (value) => height - pad.bottom - (value / maxValue) * (height - pad.top - pad.bottom);

  for (let index = 0; index <= 4; index += 1) {
    const lineY = pad.top + index * (height - pad.top - pad.bottom) / 4;
    chart.append(svg("line", { x1: pad.left, y1: lineY, x2: width - pad.right, y2: lineY, class: "chart-grid" }));
    const label = svg("text", { x: 0, y: lineY + 3, class: "chart-label" });
    label.textContent = formatNumber(maxValue * (1 - index / 4), 0);
    chart.append(label);
  }

  ["ttft_ms", "e2e_ms"].forEach((key) => {
    const kind = key === "ttft_ms" ? "ttft" : "e2e";
    const points = rows
      .map((row, index) => Number.isFinite(row[key]) ? `${x(index)},${y(row[key])}` : null)
      .filter(Boolean)
      .join(" ");
    if (points) chart.append(svg("polyline", { points, class: `chart-line-${kind}` }));
    rows.forEach((row, index) => {
      if (Number.isFinite(row[key])) {
        chart.append(svg("circle", { cx: x(index), cy: y(row[key]), r: 3.5, class: `chart-point-${kind}` }));
      }
    });
  });

  rows.forEach((_, index) => {
    const label = svg("text", { x: x(index), y: height - 8, class: "chart-label", "text-anchor": "middle" });
    label.textContent = String(index + 1).padStart(2, "0");
    chart.append(label);
  });
}

function renderMeasurements(measurements) {
  const body = $("#measurement-body");
  body.replaceChildren();
  $("#measurement-count").textContent = `${measurements.length} RECORDS`;
  if (!measurements.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 9;
    cell.textContent = "启动一次检测后，这里会列出每个请求的独立测量证据。";
    row.append(cell);
    body.append(row);
    return;
  }

  measurements.forEach((measurement, index) => {
    const row = document.createElement("tr");
    appendCell(row, String(index + 1).padStart(2, "0"), "request-index");
    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    const failed = Boolean(measurement.error_class);
    status.className = `table-status${failed ? " fail" : ""}`;
    status.textContent = failed ? measurement.error_class : "PASS";
    statusCell.append(status);
    row.append(statusCell);
    appendCell(row, measurement.status_code ?? "—");
    appendCell(row, unit(measurement.ttfb_ms, "ms"));
    appendCell(row, unit(measurement.ttft_ms, "ms"));
    appendCell(row, unit(measurement.e2e_ms, "ms"));
    appendCell(row, unit(measurement.output_tps, ""));
    appendCell(row, measurement.provider_reported_total_tokens ?? "—");

    const evidenceCell = document.createElement("td");
    const tags = document.createElement("div");
    tags.className = "table-evidence";
    if (measurement.evidence?.probe_type && measurement.evidence.probe_type !== "chat") {
      addTag(tags, measurement.evidence.probe_type.replaceAll("_", " ").toUpperCase(), "good");
    }
    if (measurement.evidence?.sse_done_received) addTag(tags, "SSE DONE", "good");
    if (measurement.finish_reason) addTag(tags, `FIN ${measurement.finish_reason}`, "good");
    if (measurement.evidence?.reasoning_content_observed) {
      addTag(tags, `THINK ${measurement.evidence.reasoning_output_chars} CHARS`, "");
    }
    if (measurement.evidence?.final_content_burst_observed) addTag(tags, "FINAL BURST", "");
    if (measurement.evidence?.pseudo_stream_suspected) addTag(tags, "PSEUDO", "warn");
    if (measurement.evidence?.embedding_dimensions) {
      addTag(tags, `${measurement.evidence.embedding_dimensions} DIMS`, "");
    }
    if (measurement.evidence?.generated_image_count) {
      addTag(tags, `${measurement.evidence.generated_image_count} IMAGE`, "");
    }
    if (measurement.evidence?.audio_bytes) {
      addTag(tags, `${measurement.evidence.audio_bytes} AUDIO BYTES`, "");
    }
    if (measurement.evidence?.output_text_chars) addTag(tags, `${measurement.evidence.output_text_chars} CHARS`, "");
    if (!tags.children.length) addTag(tags, "NO FLAGS", "");
    evidenceCell.append(tags);
    row.append(evidenceCell);
    body.append(row);
  });
}

function appendCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value;
  if (className) cell.className = className;
  row.append(cell);
}

function addTag(container, text, kind) {
  const tag = document.createElement("span");
  tag.className = `evidence-tag ${kind}`.trim();
  tag.textContent = text;
  container.append(tag);
}

function collectDiagnostics(result) {
  const values = new Set(result.reason_codes || []);
  result.dimension_scores.forEach((dimension) => {
    (dimension.reason_codes || []).forEach((code) => values.add(code));
  });
  result.measurements.forEach((measurement) => {
    if (measurement.error_class) values.add(measurement.error_class);
    if (measurement.evidence?.pseudo_stream_suspected) values.add("PSEUDO_STREAM_SUSPECTED");
  });
  if (!values.size) values.add("ALL_ASSERTIONS_SATISFIED");
  return [...values];
}

function renderDiagnostics(values, positive) {
  const list = $("#diagnostics");
  list.replaceChildren();
  values.forEach((value) => {
    const tag = document.createElement("span");
    tag.className = `diagnostic ${positive ? "good" : "bad"}`;
    tag.textContent = value;
    list.append(tag);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`服务返回了不可解析的响应（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    throw new Error(payload.error?.message || `请求失败（HTTP ${response.status}）`);
  }
  return payload.data;
}

function historyStatus(run) {
  return (run.result_status || run.state || "UNKNOWN").toLowerCase();
}

function average(values) {
  const numbers = values.filter(Number.isFinite);
  return numbers.length ? numbers.reduce((sum, value) => sum + value, 0) / numbers.length : null;
}

function percentile(values, ratio) {
  const numbers = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!numbers.length) return null;
  const index = Math.min(numbers.length - 1, Math.ceil(numbers.length * ratio) - 1);
  return numbers[index];
}

function formatNumber(value, digits = 1) {
  if (!Number.isFinite(value)) return "--";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function unit(value, suffix) {
  return Number.isFinite(value) ? `${formatNumber(value, 1)}${suffix ? ` ${suffix}` : ""}` : "—";
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function relativeTime(value) {
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatDate(value);
}

function svg(name, attributes) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function updateClock() {
  $("#local-clock").textContent = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

function toast(message, isError = false) {
  const item = document.createElement("div");
  item.className = `toast${isError ? " error" : ""}`;
  item.textContent = message;
  $("#toast-region").append(item);
  window.setTimeout(() => item.remove(), 3400);
}
