/* AetherNode 前端逻辑：原生 JS，无构建步骤。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  models: [],
  downloads: [],
  peers: [],
  catalog: [],
  relays: [],
  runtime: null,
  peerStats: {},
  lan: {},
  sys: null,
  chatModel: "",
  expanded: new Set(),
  logLines: [],
};

/* ---------------- 基础 ---------------- */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function toast(msg, kind = "info") {
  const box = el("div", "toast " + kind, msg);
  $("#toasts").appendChild(box);
  setTimeout(() => {
    box.style.opacity = "0";
    box.style.transition = "opacity .3s";
    setTimeout(() => box.remove(), 300);
  }, kind === "err" ? 6000 : 3200);
}

/* 骨架屏：长任务与首屏加载时给出明确占位，而不是一片空白 */
function skeleton(container, rows = 3, hint = "加载中…") {
  if (!container) return;
  container.innerHTML = "";
  if (hint) container.appendChild(el("div", "empty", hint));
  for (let i = 0; i < rows; i++) {
    const item = el("div", "item skeleton-item");
    item.appendChild(el("div", "skeleton-bar", null)).style.width = 60 - i * 12 + "%";
    container.appendChild(item);
  }
}

/* 带超时与明确错误信息的请求；失败时把 HTTP 状态也带出来，方便判断是网络还是服务问题 */
async function api(path, opts = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), opts.timeout || 30000);
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...opts,
    });
  } catch (err) {
    clearTimeout(timeout);
    const msg = err.name === "AbortError" ? `请求超时（${(opts.timeout || 30000) / 1000}s）` : "网络不可达";
    throw new Error(`${msg}：${path}`);
  }
  clearTimeout(timeout);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) {}
    throw new Error(detail);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

function shortId(id) {
  return id ? id.slice(0, 10) + "…" + id.slice(-4) : "";
}

/* ---------------- 视图切换 ---------------- */
function initTabs() {
  $$("#tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
      $$(".view").forEach((v) =>
        v.classList.toggle("active", v.id === "view-" + btn.dataset.view)
      );
    });
  });
}

/* ---------------- 状态徽标 ---------------- */
function renderPills() {
  const rt = S.runtime || {};
  const pr = $("#pill-runtime");
  const map = {
    ready: ["已加载", "ok"],
    starting: ["加载中", "warn"],
    failed: ["失败", "err"],
    stopping: ["停止中", "warn"],
    idle: ["未加载", ""],
  };
  const [label, cls] = map[rt.state] || ["未知", ""];
  pr.textContent = rt.model ? `${label} · ${rt.model}` : label;
  pr.className = "pill " + cls;

  const conn = S.relays.filter((r) => r.status === "connected").length;
  const rp = $("#pill-relay");
  rp.textContent = `relay ${conn}/${S.relays.length}`;
  rp.className = "pill " + (conn > 0 ? "ok" : S.relays.length ? "err" : "");

  const pp = $("#pill-peers");
  pp.textContent = `peers ${S.peers.length}`;
  pp.className = "pill " + (S.peers.length > 0 ? "ok" : "");
}

/* ---------------- 运行时 ---------------- */
function renderRuntime() {
  const rt = S.runtime || {};
  const box = $("#runtime-info");
  box.innerHTML = "";

  const rows = [
    ["状态", rt.state || "-", rt.state === "ready" ? "ok" : rt.state === "failed" ? "err" : ""],
    ["模型", rt.model || "未加载", ""],
    ["端口", rt.port || "-", ""],
    ["参数", rt.params ? `ngl=${rt.params.ngl} ctx=${rt.params.ctx} 大小=${rt.params.size_gb}GB` : "-", ""],
    ["说明", (rt.params && rt.params.note) || "-", ""],
    ["运行时长", rt.uptime ? rt.uptime + "s" : "-", ""],
    ["二进制", rt.binary || "未安装", rt.binary ? "ok" : "warn"],
    ["错误", rt.error || "-", rt.error ? "err" : ""],
  ];
  for (const [k, v, cls] of rows) {
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v " + (cls || ""), String(v)));
  }
}

function pushLog(line, stream) {
  const box = $("#runtime-log");
  if (!box) return;
  S.logLines.push({ line, stream });
  if (S.logLines.length > 600) S.logLines.shift();
  const span = el("div", stream === "cmd" ? "cmd" : stream === "err" ? "err" : "", line);
  box.appendChild(span);
  while (box.childElementCount > 600) box.firstElementChild.remove();
  if ($("#log-follow").checked) box.scrollTop = box.scrollHeight;
}

/* ---------------- 下载 ---------------- */
function renderDownloads() {
  const box = $("#downloads");
  box.innerHTML = "";
  if (!S.downloads.length) {
    box.appendChild(el("div", "empty", "暂无下载任务"));
    return;
  }
  for (const t of S.downloads) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    row.appendChild(el("span", "item-title", t.file));
    const kindMap = { done: "green", failed: "red", cancelled: "grey", running: "amber", pending: "amber" };
    row.appendChild(el("span", "tag " + (kindMap[t.status] || ""), t.status));
    row.appendChild(el("span", "spacer"));

    if (t.status === "running") {
      const btn = el("button", "mini ghost danger", "取消");
      btn.onclick = () => cancelDownload(t.id);
      row.appendChild(btn);
    }
    item.appendChild(row);

    if (t.status === "running" || t.status === "pending") {
      const bar = el("div", "progress");
      const fill = el("i");
      fill.style.width = (t.pct || 0) + "%";
      bar.appendChild(fill);
      item.appendChild(bar);
    }

    const sub = el(
      "div",
      "item-sub",
      `${fmtBytes(t.done)} / ${fmtBytes(t.total)} · ${t.pct || 0}% · ${t.speed_mbps || 0} MB/s · ${t.elapsed}s`
    );
    item.appendChild(sub);
    if (t.error) item.appendChild(el("div", "item-sub", "错误：" + t.error));
    box.appendChild(item);
  }
}

/* ---------------- 本地模型 ---------------- */
function renderLocalModels() {
  const box = $("#local-models");
  box.innerHTML = "";
  if (!S.models.length) {
    box.appendChild(el("div", "empty", "还没有本地模型，先从上方搜索并下载一个"));
    return;
  }
  for (const m of S.models) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    row.appendChild(el("span", "item-title", m.display));
    if (m.loaded) row.appendChild(el("span", "tag green", "已加载"));
    row.appendChild(el("span", "tag", m.quant || "?"));
    if (m.meta && m.meta.block_count)
      row.appendChild(el("span", "tag grey", m.meta.block_count + " 层"));
    row.appendChild(el("span", "spacer"));

    const load = el("button", "mini primary", "加载");
    load.onclick = () => loadModel(m.key);
    const del = el("button", "mini ghost danger", "删除");
    del.onclick = () => deleteModel(m.key, m.display);
    row.appendChild(load);
    row.appendChild(del);
    item.appendChild(row);

    const detail = [
      `${m.size_gb} GB`,
      m.meta && m.meta.context_length ? `原生 ctx ${m.meta.context_length}` : "",
      m.meta && m.meta.architecture ? m.meta.architecture : "",
      m.repo,
    ].filter(Boolean).join(" · ");
    item.appendChild(el("div", "item-sub", detail));
    box.appendChild(item);
  }
}

/* ---------------- 网络 ---------------- */
function renderNodeInfo() {
  const s = S.sys || {};
  const p2p = S.p2pStatus || {};
  const box = $("#node-info");
  box.innerHTML = "";
  const rows = [
    ["节点名", s.node_id ? (p2p.node_name || "-") : "-"],
    ["节点 ID", shortId(s.node_id || "")],
    ["npub", (s.npub || "").slice(0, 28) + "…"],
    ["局域网 IP", p2p.lan_ip || "-"],
    ["对外端点", (p2p.own_ad && p2p.own_ad.endpoint) || "未广播"],
    ["可达性", (p2p.own_ad && p2p.own_ad.reachability) || "-"],
    ["内置 relay", (S.lan && S.lan.share_hint) || "-"],
    ["OpenAI 入口", (S.lan && S.lan.openai_hint) || "-"],
  ];
  for (const [k, v] of rows) {
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v", String(v)));
  }
  const toggle = $("#advertise-toggle");
  if (toggle) toggle.checked = !!p2p.advertise;
}

function renderRelays() {
  const box = $("#relays");
  box.innerHTML = "";
  if (!S.relays.length) {
    box.appendChild(el("div", "empty", "没有 relay 连接"));
    return;
  }
  for (const r of S.relays) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    const cls = r.status === "connected" ? "green" : r.status === "error" ? "red" : "amber";
    row.appendChild(el("span", "tag " + cls, r.status));
    row.appendChild(el("span", "item-title", r.url));
    if (r.builtin) row.appendChild(el("span", "tag grey", "内置"));
    row.appendChild(el("span", "spacer"));
    row.appendChild(el("span", "item-sub", `收 ${r.events_in} / 发 ${r.events_out}`));
    if (!r.builtin) {
      const rm = el("button", "mini ghost danger", "移除");
      rm.onclick = () => removeRelay(r.url);
      row.appendChild(rm);
    }
    item.appendChild(row);
    if (r.error) item.appendChild(el("div", "item-sub", r.error));
    box.appendChild(item);
  }
}

function renderPeers() {
  const box = $("#peers");
  box.innerHTML = "";
  if (!S.peers.length) {
    box.appendChild(el("div", "empty", "还没有发现其他节点。确认双方连到同一个 relay，并开启了广播"));
    return;
  }
  for (const p of S.peers) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    row.appendChild(el("span", "item-title", p.name));
    const rc = { public: "green", lan: "amber", "relay-only": "red" }[p.reachability] || "grey";
    row.appendChild(el("span", "tag " + rc, p.reachability));
    if (p.stale) row.appendChild(el("span", "tag red", "过期"));
    row.appendChild(el("span", "spacer"));
    const probe = el("button", "mini ghost", "探测");
    probe.onclick = () => probePeer(p.node_id);
    row.appendChild(probe);
    item.appendChild(row);
    item.appendChild(el("div", "item-sub", shortId(p.node_id) + " · 广告龄 " + p.age + "s"));

    const models = (p.models || []).map((m) => m.id).join("，");
    item.appendChild(el("div", "item-sub", models ? "提供：" + models : "未广播模型"));

    const ep = el("div", "item-sub", p.endpoint || "无端点（仅 relay）");
    item.appendChild(ep);
    box.appendChild(item);
  }
}

function renderCatalog() {
  const box = $("#catalog");
  box.innerHTML = "";
  if (!S.catalog.length) {
    box.appendChild(el("div", "empty", "目录为空：本机未加载模型，网络内也没有节点广播"));
    return;
  }
  for (const m of S.catalog) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    row.appendChild(el("span", "item-title", m.id));
    row.appendChild(el("span", "tag " + (m.kind === "local" ? "green" : ""), m.kind === "local" ? "本机" : "网络"));
    if (m.loaded) row.appendChild(el("span", "tag green", "可用"));
    if (m.ctx) row.appendChild(el("span", "tag grey", "ctx " + m.ctx));
    if (m.size_gb) row.appendChild(el("span", "tag grey", m.size_gb + "GB"));
    row.appendChild(el("span", "spacer"));
    row.appendChild(el("span", "item-sub", "来源 x" + (m.sources || 1)));
    item.appendChild(row);
    if (m.note) item.appendChild(el("div", "item-sub", m.note));
    box.appendChild(item);
  }
}

/* ---------------- HuggingFace ---------------- */
function renderCurated(items) {
  const box = $("#hub-curated");
  box.innerHTML = "";
  for (const it of items) {
    const chip = el("div", "chip", it.id);
    chip.title = it.note || "";
    chip.onclick = () => {
      $("#hub-q").value = it.id;
      searchHub();
    };
    box.appendChild(chip);
  }
}

function renderSearchResults(results) {
  const box = $("#hub-results");
  box.innerHTML = "";
  if (!results.length) {
    box.appendChild(el("div", "empty", "没有结果，换个关键词试试（只检索 GGUF 仓库）"));
    return;
  }
  for (const r of results) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    row.appendChild(el("span", "item-title", r.id));
    row.appendChild(el("span", "tag grey", "↓ " + r.downloads));
    row.appendChild(el("span", "tag grey", "♥ " + r.likes));
    if (r.param_hint !== "?") row.appendChild(el("span", "tag", r.param_hint));
    row.appendChild(el("span", "spacer"));
    const btn = el("button", "mini ghost", "查看量化");
    btn.onclick = () => toggleFiles(r);
    row.appendChild(btn);
    item.appendChild(row);
    item.appendChild(el("div", "item-sub", "更新于 " + (r.last_modified || "-").slice(0, 19)));

    if (S.expanded.has(r.id)) {
      const files = el("div", "list");
      files.dataset.repo = r.id;
      files.appendChild(el("div", "item-sub", "正在读取文件列表…"));
      item.appendChild(files);
    }
    box.appendChild(item);
  }
}

async function toggleFiles(repo) {
  if (S.expanded.has(repo.id)) {
    S.expanded.delete(repo.id);
  } else {
    S.expanded.add(repo.id);
  }
  renderSearchResults(S.searchResults || []);
  if (!S.expanded.has(repo.id)) return;

  try {
    const data = await api("/api/hub/files?repo=" + encodeURIComponent(repo.id));
    const holder = document.querySelector(`.list[data-repo="${CSS.escape(repo.id)}"]`);
    if (!holder) return;
    holder.innerHTML = "";
    if (!data.files.length) {
      holder.appendChild(el("div", "empty", "该仓库没有 GGUF 文件"));
      return;
    }
    for (const f of data.files) {
      const r = el("div", "item-row");
      r.appendChild(el("span", "tag", f.quant));
      r.appendChild(el("span", "item-sub", f.size_gb + " GB"));
      r.appendChild(el("span", "item-sub", f.fname));
      r.appendChild(el("span", "spacer"));
      const b = el("button", "mini primary", "下载");
      b.onclick = () => downloadFile(repo.id, f.path);
      r.appendChild(b);
      holder.appendChild(r);
    }
  } catch (err) {
    toast("读取文件列表失败：" + err.message, "err");
  }
}

/* ---------------- 动作 ---------------- */
async function searchHub() {
  const q = $("#hub-q").value.trim();
  if (!q) return;
  const box = $("#hub-results");
  box.innerHTML = "";
  box.appendChild(el("div", "empty", "搜索中…"));
  try {
    const data = await api("/api/hub/search?q=" + encodeURIComponent(q));
    S.searchResults = data.results || [];
    renderSearchResults(S.searchResults);
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(el("div", "empty", err.message));
    toast("搜索失败：" + err.message, "err");
  }
}

async function downloadFile(repo, file) {
  try {
    await api("/api/models/download", {
      method: "POST",
      body: JSON.stringify({ repo, file }),
    });
    toast("已加入下载队列：" + file.split("/").pop(), "ok");
    refreshDownloads();
  } catch (err) {
    toast("创建下载失败：" + err.message, "err");
  }
}

async function cancelDownload(id) {
  try {
    await api("/api/downloads/" + id, { method: "DELETE" });
    refreshDownloads();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function loadModel(key) {
  try {
    await api("/api/models/load", { method: "POST", body: JSON.stringify({ key }) });
    toast("开始加载模型，进度见运行时日志", "ok");
  } catch (err) {
    toast("加载失败：" + err.message, "err");
  }
}

async function deleteModel(key, name) {
  if (!confirm("删除模型文件：" + name + "？")) return;
  try {
    await api("/api/models/" + encodeURIComponent(key), { method: "DELETE" });
    toast("已删除", "ok");
    refreshModels();
  } catch (err) {
    toast("删除失败：" + err.message, "err");
  }
}

async function unloadModel() {
  try {
    await api("/api/models/unload", { method: "POST" });
    toast("已卸载模型", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function installRuntime() {
  try {
    await api("/api/runtime/install", { method: "POST" });
    toast("开始准备 llama.cpp 运行时（约 300MB，首次会下载）", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function toggleAdvertise(enabled) {
  try {
    const r = await api("/api/p2p/advertise", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    toast(
      enabled
        ? `已开启广播，投递到 ${r.relays} 个 relay，模型 ${r.models.length} 个`
        : "已关闭广播",
      enabled ? "ok" : "warn"
    );
    refreshP2P();
  } catch (err) {
    toast("广播开关失败：" + err.message, "err");
  }
}

async function publishNow() {
  try {
    const r = await api("/api/p2p/publish", { method: "POST" });
    toast(`已发布到 ${r.relays} 个 relay`, r.relays ? "ok" : "warn");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function addRelay() {
  const url = $("#relay-url").value.trim();
  if (!url) return;
  try {
    await api("/api/p2p/relays", {
      method: "POST",
      body: JSON.stringify({ action: "add", url }),
    });
    $("#relay-url").value = "";
    toast("已添加 relay", "ok");
    refreshP2P();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function removeRelay(url) {
  try {
    await api("/api/p2p/relays", {
      method: "POST",
      body: JSON.stringify({ action: "remove", url }),
    });
    refreshP2P();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function probePeer(nodeId) {
  try {
    const r = await api("/api/p2p/probe", {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId }),
    });
    toast(
      r.ok
        ? `可达：${r.endpoint}，模型 ${r.models.length} 个`
        : `不可达：${r.reason || r.endpoint}`,
      r.ok ? "ok" : "err"
    );
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ---------------- 对话 ---------------- */
function appendMsg(role, text, meta) {
  const wrap = el("div", "msg " + role);
  const bubble = el("div", "bubble", text);
  const m = el("div", "meta", meta || "");
  wrap.appendChild(bubble);
  wrap.appendChild(m);
  $("#chat-log").appendChild(wrap);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return wrap;
}

async function sendChat() {
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  const model = $("#chat-model").value || "";
  appendMsg("user", text, "");
  input.value = "";
  const wrap = appendMsg("assistant", "", "等待响应…");
  const bubble = wrap.querySelector(".bubble");
  const meta = wrap.querySelector(".meta");

  let res;
  try {
    res = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        stream: true,
        messages: [{ role: "user", content: text }],
      }),
    });
  } catch (err) {
    bubble.textContent = "请求失败：" + err.message;
    return;
  }

  const route = res.headers.get("X-Aether-Route") || "?";
  const node = res.headers.get("X-Aether-Node") || "?";
  meta.textContent = `路由 ${route} · ${node}`;
  $("#chat-route").textContent = `上次：${route} · ${node}`;

  if (!res.ok) {
    bubble.textContent = "请求失败：" + (await res.text()).slice(0, 300);
    return;
  }

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const raw of lines) {
      const line = raw.trim();
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        const j = JSON.parse(data);
        const delta = (j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content) || "";
        if (delta) {
          bubble.textContent += delta;
          $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
        }
      } catch (_) {}
    }
  }
  if (!bubble.textContent) bubble.textContent = "（空响应）";
  refreshCatalog();
}

function renderChatModels() {
  const sel = $("#chat-model");
  const prev = sel.value;
  sel.innerHTML = "";
  const auto = el("option", null, "自动选择（本机优先）");
  auto.value = "";
  sel.appendChild(auto);
  for (const m of S.catalog) {
    const opt = el("option", null, `${m.id}${m.kind === "local" ? "（本机）" : "（网络）"}`);
    opt.value = m.id;
    sel.appendChild(opt);
  }
  if (prev) sel.value = prev;
}

/* ---------------- 数据刷新 ---------------- */
async function refreshModels() {
  try {
    const d = await api("/api/models");
    S.models = d.models || [];
    renderLocalModels();
  } catch (err) {}
}

async function refreshDownloads() {
  try {
    const d = await api("/api/downloads");
    S.downloads = d.tasks || [];
    renderDownloads();
  } catch (err) {}
}

async function refreshRuntime() {
  try {
    S.runtime = await api("/api/runtime");
    renderRuntime();
    renderPills();
  } catch (err) {}
}

async function refreshP2P() {
  try {
    const [st, peers, catalog, lan] = await Promise.all([
      api("/api/p2p/status"),
      api("/api/p2p/peers"),
      api("/api/p2p/catalog"),
      api("/api/p2p/lan"),
    ]);
    S.p2pStatus = st;
    S.relays = st.relays || [];
    S.peers = peers.peers || [];
    S.peerStats = peers.stats || {};
    S.catalog = catalog.models || [];
    S.lan = lan || {};
    renderRelays();
    renderPeers();
    renderCatalog();
    renderNodeInfo();
    renderChatModels();
    renderPills();
  } catch (err) {}
}

async function refreshSystem() {
  try {
    S.sys = await api("/api/system");
    renderNodeInfo();
    renderOnboarding();
  } catch (err) {}
}

/* ---------------- 首次启动引导 ---------------- */
function renderOnboarding() {
  const panel = $("#onboarding-panel");
  if (!panel) return;
  const ob = (S.sys && S.sys.onboarding) || {};
  if (!ob.first_run || ob.dismissed) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "";

  const steps = [
    {
      title: "安装推理运行时",
      desc: "llama.cpp 与 CUDA 运行时约 680MB，只需装一次",
      done: !!ob.has_runtime,
      action: () => {
        installRuntime();
      },
      cta: "下载运行时",
    },
    {
      title: "下载一个模型",
      desc: "建议先从小模型试：Qwen2.5-1.5B 的 Q4_K_M 约 1GB，CPU 也能跑",
      done: !!ob.has_model,
      action: () => {
        $("#hub-q").value = "qwen2.5 1.5b gguf";
        searchHub();
      },
      cta: "去搜索模型",
    },
    {
      title: "决定是否对外共享",
      desc: "默认只监听本机。共享会绑定 0.0.0.0 并自动生成 api_key",
      done: !!ob.share_enabled,
      action: () => {
        const btn = document.querySelector('#tabs button[data-view="settings"]');
        if (btn) btn.click();
      },
      cta: "去设置",
    },
  ];

  const box = $("#onboarding-steps");
  box.innerHTML = "";
  steps.forEach((step, i) => {
    const item = el("div", "item");
    const row = el("div", "item-row");
    row.appendChild(el("span", "tag " + (step.done ? "green" : "amber"), step.done ? "已完成" : `第 ${i + 1} 步`));
    row.appendChild(el("span", "item-title", step.title));
    row.appendChild(el("span", "spacer"));
    if (!step.done) {
      const b = el("button", "mini primary", step.cta);
      b.onclick = step.action;
      row.appendChild(b);
    }
    item.appendChild(row);
    item.appendChild(el("div", "item-sub", step.desc));
    box.appendChild(item);
  });
}

async function dismissOnboarding() {
  try {
    await api("/api/onboarding/dismiss", { method: "POST" });
    if (S.sys) S.sys.onboarding.dismissed = true;
    renderOnboarding();
  } catch (err) {
    toast("保存失败：" + err.message, "err");
  }
}

/* ---------------- 账本 ---------------- */
async function refreshLedger() {
  try {
    const [sum, ents, bat] = await Promise.all([
      api("/api/ledger/summary"),
      api("/api/ledger/entries?limit=40"),
      api("/api/ledger/batches?limit=10"),
    ]);
    S.ledger = { summary: sum.summary || {}, credit: sum.credit || [], db: sum.db };
    S.entries = ents.entries || [];
    S.batches = bat.batches || [];
    renderLedger();
    await refreshAudits();
    await refreshEmission();
    await refreshChain();
  } catch (err) {}
}

function renderLedger() {
  const L = S.ledger || {};
  const sum = L.summary || {};
  const cons = sum.consume || {};
  const serv = sum.serve || {};

  const box = $("#ledger-summary");
  box.innerHTML = "";
  const rows = [
    ["总记录", `${sum.total_entries || 0} 条，其中未结算 ${sum.unsettled || 0}`],
    ["我调用别人", `${cons.count || 0} 次 · ${cons.in_tok || 0}+${cons.out_tok || 0} tokens · 应付 ${(cons.amount || 0).toFixed(6)}`],
    ["别人调用我", `${serv.count || 0} 次 · ${serv.in_tok || 0}+${serv.out_tok || 0} tokens · 应收 ${(serv.amount || 0).toFixed(6)}`],
    ["估算条目", `${(cons.estimated || 0) + (serv.estimated || 0)} 条（无 usage 时的兜底，不计入精确结算）`],
    ["账本库", L.db || "-"],
  ];
  for (const [k, v] of rows) {
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v", String(v)));
  }

  const cbox = $("#ledger-credit");
  cbox.innerHTML = "";
  if (!L.credit || !L.credit.length) {
    cbox.appendChild(el("div", "empty", "还没有跨节点往来"));
  } else {
    for (const c of L.credit) {
      const item = el("div", "item");
      const row = el("div", "item-row");
      row.appendChild(el("span", "item-title", shortId(c.node_id)));
      const netCls = c.net > 0 ? "green" : c.net < 0 ? "red" : "grey";
      row.appendChild(el("span", "tag " + netCls, `净额 ${c.net.toFixed(6)}`));
      row.appendChild(el("span", "spacer"));
      row.appendChild(el("span", "item-sub", `应收 ${c.receivable.toFixed(6)} · 应付 ${c.payable.toFixed(6)}`));
      item.appendChild(row);
      cbox.appendChild(item);
    }
  }

  const ebox = $("#ledger-entries");
  ebox.innerHTML = "";
  if (!S.entries.length) {
    ebox.appendChild(el("div", "empty", "还没有计量记录，发一次对话就会产生"));
  } else {
    for (const e of S.entries) {
      const item = el("div", "item");
      const row = el("div", "item-row");
      row.appendChild(el("span", "tag " + (e.role === "serve" ? "green" : "amber"), e.role));
      row.appendChild(el("span", "item-title", e.model));
      row.appendChild(el("span", "tag grey", e.source));
      if (e.estimated) row.appendChild(el("span", "tag red", "估算"));
      if (e.receipt) row.appendChild(el("span", "tag green", "有收据"));
      if (e.batch_id) row.appendChild(el("span", "tag grey", "已结算"));
      row.appendChild(el("span", "spacer"));
      row.appendChild(el("span", "item-sub", `${e.in_tok}+${e.out_tok} tok · ${e.amount.toFixed(6)}`));
      item.appendChild(row);
      item.appendChild(el("div", "item-sub", `req ${shortId(e.req_id)}${e.peer_node ? " · 对端 " + shortId(e.peer_node) : ""}`));
      ebox.appendChild(item);
    }
  }

  const bbox = $("#ledger-batches");
  bbox.innerHTML = "";
  if (!S.batches.length) {
    bbox.appendChild(el("div", "empty", "还没有结算批次"));
  } else {
    for (const b of S.batches) {
      const item = el("div", "item");
      const row = el("div", "item-row");
      row.appendChild(el("span", "item-title", b.id));
      row.appendChild(el("span", "tag grey", `${b.count} 条`));
      row.appendChild(el("span", "spacer"));
      const vf = el("button", "mini ghost", "校验凭证");
      vf.onclick = () => verifyBatch(b.path);
      row.appendChild(vf);
      item.appendChild(row);
      item.appendChild(el("div", "item-sub", `金额 ${b.amount.toFixed(6)} · ${b.total_in}+${b.total_out} tok`));
      item.appendChild(el("div", "item-sub", "root " + b.merkle_root));
      bbox.appendChild(item);
    }
  }
}

async function settleLedger() {
  try {
    const r = await api("/api/ledger/settle", { method: "POST", body: JSON.stringify({}) });
    if (r.created) {
      toast(`已结算 ${r.batch.count} 条，root ${r.batch.merkle_root.slice(0, 12)}…`, "ok");
    } else {
      toast(r.reason || "没有待结算记录", "warn");
    }
    refreshLedger();
  } catch (err) {
    toast("结算失败：" + err.message, "err");
  }
}

async function verifyBatch(path) {
  if (!path) return;
  try {
    const v = await api("/api/ledger/verify", { method: "POST", body: JSON.stringify({ path }) });
    toast(
      v.ok ? `凭证校验通过：${v.entry_count} 条叶子，root 可复现` : `凭证校验失败：${v.bad_leaves.length} 片叶子异常`,
      v.ok ? "ok" : "err"
    );
  } catch (err) {
    toast("校验失败：" + err.message, "err");
  }
}

/* ---------------- 抽样复算 ---------------- */
async function refreshAudits() {
  try {
    const a = await api("/api/ledger/audits?limit=20");
    S.audits = a.audits || [];
    S.auditStats = a.stats || {};
    renderAudits();
  } catch (err) {}
}

function renderAudits() {
  const st = S.auditStats || {};
  const box = $("#audit-stats");
  box.innerHTML = "";
  const rows = [
    ["累计复算", `${st.total || 0} 次`],
    ["通过", `${st.pass || 0}`],
    ["可疑", `${st.suspect || 0}`],
    ["跳过", `${st.skipped || 0}（网络内没有第二个同模型节点）`],
    ["失败", `${st.error || 0}`],
  ];
  for (const [k, v] of rows) {
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v", String(v)));
  }

  const list = $("#audit-list");
  list.innerHTML = "";
  if (!S.audits || !S.audits.length) {
    list.appendChild(el("div", "empty", "还没有复算记录。需要网络内至少有两个提供同一模型的节点"));
    return;
  }
  for (const a of S.audits) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    const cls = a.verdict === "pass" ? "green" : a.verdict === "suspect" ? "red" : "grey";
    row.appendChild(el("span", "tag " + cls, a.verdict));
    row.appendChild(el("span", "item-title", a.model));
    row.appendChild(el("span", "spacer"));
    row.appendChild(el("span", "item-sub", `相似度 ${a.similarity}`));
    item.appendChild(row);
    item.appendChild(el("div", "item-sub",
      `目标 ${shortId(a.target_node)} · 复算者 ${shortId(a.verifier)} · 原 ${a.original_in}+${a.original_out} / 复算 ${a.verified_in}+${a.verified_out}`));
    if (a.detail) item.appendChild(el("div", "item-sub", a.detail.slice(0, 200)));
    list.appendChild(item);
  }
}

async function probeAudit() {
  const model = (S.catalog && S.catalog.length) ? S.catalog[0].id : "";
  if (!model) {
    toast("没有可用模型，先加载一个或让网络内节点广播", "warn");
    return;
  }
  try {
    const r = await api("/api/ledger/audit/probe", {
      method: "POST",
      body: JSON.stringify({ model }),
    });
    const bad = (r.pairs || []).filter((p) => p.verdict !== "pass").length;
    toast(
      `复算完成：${r.providers.length} 个提供者，${r.pairs.length} 组比对，其中 ${bad} 组不一致`,
      bad ? "warn" : "ok"
    );
    refreshAudits();
  } catch (err) {
    toast("复算失败：" + err.message, "err");
  }
}

/* ---------------- 贡献与发放 ---------------- */
async function refreshEmission() {
  try {
    const [cur, plans, bw] = await Promise.all([
      api("/api/ledger/emission/current"),
      api("/api/ledger/emission/plans?limit=8"),
      api("/api/ledger/bandwidth"),
    ]);
    S.emission = cur;
    S.emissionPlans = plans.plans || [];
    S.bandwidth = bw.stats || {};
    renderEmission();
  } catch (err) {}
}

function renderEmission() {
  const e = S.emission || {};
  const box = $("#emission-info");
  box.innerHTML = "";
  const mine = (e.allocations || []).find((a) => (S.sys && a.node === S.sys.node_id)) || (e.allocations || [])[0];
  const rows = [
    ["当前 epoch", `${e.epoch || "-"}（每小时一个）`],
    ["本期总排放", `${e.emission || 0}`],
    ["网络总点数", `${e.total_points || 0}`],
    ["我的点数", mine ? `${mine.points}（服务 ${mine.serve_tokens} tok · 中继 ${mine.relay_bytes} B）` : "0"],
    ["我的份额", mine ? `${(mine.share * 100).toFixed(2)}%` : "-"],
    ["我可领", mine ? `${mine.amount}` : "-"],
    ["中继承载", `${S.bandwidth.relay_total_bytes || 0} 字节`],
    ["分配数", `${(e.allocations || []).length}`],
  ];
  for (const [k, v] of rows) {
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v", String(v)));
  }
  if (e.merkle_root) {
    box.appendChild(el("div", "k", "本期 root"));
    box.appendChild(el("div", "v", e.merkle_root));
  }

  const list = $("#emission-list");
  list.innerHTML = "";
  if (!S.emissionPlans || !S.emissionPlans.length) {
    list.appendChild(el("div", "empty", "还没有已发布的清单。点上面的「发布清单」把本期的分配落盘"));
    return;
  }
  for (const p of S.emissionPlans) {
    const item = el("div", "item");
    const row = el("div", "item-row");
    row.appendChild(el("span", "tag green", "epoch " + p.epoch));
    row.appendChild(el("span", "item-sub", `${p.allocations} 个分配 · 排放 ${p.emission}`));
    row.appendChild(el("span", "spacer"));
    const vf = el("button", "mini ghost", "校验");
    vf.onclick = () => verifyEmission(p.path);
    row.appendChild(vf);
    item.appendChild(row);
    item.appendChild(el("div", "item-sub", "root " + (p.merkle_root || "")));
    list.appendChild(item);
  }
}

async function publishEmission() {
  try {
    const r = await api("/api/ledger/emission/publish", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (r.created) {
      toast(`已发布 epoch ${r.plan.epoch} 的清单，root ${r.plan.merkle_root.slice(0, 12)}…`, "ok");
    } else {
      toast(r.reason || "没有可发放的贡献", "warn");
    }
    refreshEmission();
  } catch (err) {
    toast("发布失败：" + err.message, "err");
  }
}

async function verifyEmission(path) {
  if (!path) return;
  try {
    const v = await api("/api/ledger/emission/verify", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    toast(
      v.ok ? `清单校验通过：${v.allocations} 个分配，root 可复现` : `清单校验失败：${v.bad_nodes.length} 个节点异常`,
      v.ok ? "ok" : "err"
    );
  } catch (err) {
    toast("校验失败：" + err.message, "err");
  }
}

/* ---------------- 链上结算载荷 ---------------- */
async function refreshChain() {
  try {
    const info = await api("/api/ledger/onchain/info");
    S.chain = info;
    const box = $("#chain-info");
    box.innerHTML = "";
    const rows = [
      ["节点 EVM 地址", info.evm_address],
      ["提交函数", info.commit_signature],
      ["函数选择器", info.commit_selector],
      ["叶子验证函数", info.verify_signature],
    ];
    for (const [k, v] of rows) {
      box.appendChild(el("div", "k", k));
      box.appendChild(el("div", "v", String(v)));
    }
  } catch (err) {}
}

async function buildChainTx() {
  const b = (S.batches || [])[0];
  if (!b) {
    toast("没有结算批次，先在「结算批次」里点结算", "warn");
    return;
  }
  const to = (prompt("结算合约地址", "") || "").trim();
  if (!to) return;
  const chainId = parseInt(prompt("chain_id", "31337") || "31337", 10);
  try {
    const r = await api("/api/ledger/onchain/sign", {
      method: "POST",
      body: JSON.stringify({
        to,
        root: b.merkle_root,
        epoch: Math.floor(b.ts / 3600),
        amount: Math.round(b.amount * 1e9),
        nonce: 0,
        chain_id: chainId,
      }),
    });
    $("#chain-tx").textContent = [
      "to      " + r.to,
      "from    " + r.from,
      "chain   " + r.chain_id,
      "v       " + r.v,
      "hash    " + r.hash,
      "",
      "raw     " + r.raw,
      "",
      "calldata " + r.calldata,
    ].join("\n");
    toast("已生成签名交易，可直接用 eth_sendRawTransaction 广播", "ok");
  } catch (err) {
    toast("生成失败：" + err.message, "err");
  }
}

/* ---------------- 设置 ---------------- */
const SETTING_GROUPS = [
  {
    title: "推理",
    keys: [
      { key: "context_limit", label: "上下文长度", type: "number", hint: "llama-server 的 -c，越大越吃显存" },
      { key: "parallel_slots", label: "并行槽位", type: "number", hint: "同时处理的对话数" },
      { key: "gpu_layers", label: "GPU 层数", type: "number", hint: "-1 = 自动判断是否全量卸载" },
    ],
  },
  {
    title: "路由",
    keys: [
      { key: "prefer_local", label: "优先本机", type: "bool" },
      { key: "auto_route", label: "自动路由到网络", type: "bool" },
      { key: "external_score", label: "外部端点基础分", type: "number", hint: "高于网络节点（约 32）则优先走外部 API" },
      { key: "route_timeout", label: "上游超时（秒）", type: "number" },
    ],
  },
  {
    title: "审计",
    keys: [
      { key: "audit_rate", label: "抽样复算比例", type: "number", hint: "0 = 关闭；0.05 = 5% 的跨节点请求被复算" },
      { key: "audit_threshold", label: "相似度阈值", type: "number", hint: "中文短答建议 0.85，过严会误判" },
    ],
  },
  {
    title: "防护",
    keys: [
      { key: "relay_rate_limit", label: "每节点每分钟请求上限", type: "number", hint: "0 = 不限" },
      { key: "api_max_concurrency", label: "本机并发上限", type: "number", hint: "超出直接 429，不排队" },
      { key: "tunnel_max_concurrency", label: "隧道并发上限", type: "number" },
    ],
  },
  {
    title: "P2P 与计价",
    keys: [
      { key: "node_name", label: "节点名", type: "text" },
      { key: "region", label: "区域标识", type: "text" },
      { key: "price_in", label: "输入价 (USD/1M)", type: "number" },
      { key: "price_out", label: "输出价 (USD/1M)", type: "number" },
      { key: "public_endpoint", label: "对外端点", type: "text", hint: "有端口映射或域名时填，否则留空" },
    ],
  },
];

function renderSettings() {
  const s = (S.settings || {});
  const host = $("#settings-form");
  host.innerHTML = "";

  for (const group of SETTING_GROUPS) {
    const panel = el("div", "panel");
    const head = el("div", "panel-head");
    head.appendChild(el("h2", null, group.title));
    panel.appendChild(head);

    const wrap = el("div", "settings-grid");
    for (const f of group.keys) {
      const cell = el("div", "setting");
      const label = el("label", null, f.label);
      cell.appendChild(label);

      let input;
      if (f.type === "bool") {
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !!s[f.key];
      } else {
        input = document.createElement("input");
        input.type = f.type === "number" ? "number" : "text";
        input.step = "any";
        input.value = s[f.key] === undefined || s[f.key] === null ? "" : s[f.key];
      }
      input.dataset.key = f.key;
      input.dataset.kind = f.type;
      input.className = "setting-input";
      cell.appendChild(input);

      if (f.hint) cell.appendChild(el("div", "setting-hint", f.hint));
      wrap.appendChild(cell);
    }
    panel.appendChild(wrap);
    host.appendChild(panel);
  }

  const bar = el("div", "panel");
  const row = el("div", "row");
  const save = el("button", "primary", "保存设置");
  save.onclick = saveSettingsForm;
  const reload = el("button", "ghost", "重新载入");
  reload.onclick = refreshSettings;
  row.appendChild(save);
  row.appendChild(reload);
  bar.appendChild(row);
  host.appendChild(bar);

  renderShareInfo();
}

function renderShareInfo() {
  const s = S.settings || {};
  const box = $("#share-info");
  box.innerHTML = "";
  const rows = [
    ["当前绑定", s.share_enabled ? "0.0.0.0（对外）" : "127.0.0.1（仅本机）"],
    ["鉴权", s.share_enabled ? (s.api_key ? "已启用，key " + s.api_key.slice(0, 6) + "…" : "异常：缺 key") : "不需要"],
    ["端口", s.api_port],
    ["说明", s.share_enabled ? "非本地访问需 X-Api-Key 或 Bearer" : "外部无法连接，最安全"],
  ];
  for (const [k, v] of rows) {
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v", String(v)));
  }
  $("#share-toggle").textContent = s.share_enabled ? "关闭共享" : "开启共享";
}

async function refreshSettings() {
  try {
    const r = await api("/api/settings");
    S.settings = r.settings || {};
    renderSettings();
    await refreshProviders();
    renderVersionInfo();
  } catch (err) {
    toast("读取设置失败：" + err.message, "err");
  }
}

/* ---------------- 版本 ---------------- */
function renderVersionInfo(update) {
  const box = $("#update-info");
  if (!box) return;
  const current = (S.sys && S.sys.version) || "-";
  box.innerHTML = "";
  const rows = [
    ["当前版本", current],
    ["状态", update ? updateText(update) : "未检查"],
  ];
  if (update && update.latest) rows.push(["最新版本", update.latest]);
  if (update && update.url) rows.push(["发布页", update.url]);
  if (update && update.error) rows.push(["说明", update.error]);
  for (const [k, v] of rows) {
    box.appendChild(el("div", "k", k));
    box.appendChild(el("div", "v", String(v)));
  }
}

function updateText(u) {
  switch (u.status) {
    case "ok":
      return u.update_available ? "有新版本可用" : "已是最新";
    case "no_releases":
      return "仓库暂无发布";
    case "rate_limited":
      return "GitHub 限流，稍后再试";
    case "unreachable":
      return "无法连接 GitHub";
    default:
      return "未知";
  }
}

async function checkUpdate() {
  const btn = $("#update-check");
  btn.disabled = true;
  btn.textContent = "检查中…";
  try {
    const u = await api("/api/update/check?force=true", { timeout: 20000 });
    renderVersionInfo(u);
    if (u.update_available) {
      toast(`发现新版本 ${u.latest}`, "ok");
    } else {
      toast(updateText(u), u.status === "ok" ? "ok" : "warn");
    }
  } catch (err) {
    toast("检查失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "检查更新";
  }
}

async function saveSettingsForm() {
  const patch = {};
  document.querySelectorAll(".setting-input").forEach((input) => {
    const key = input.dataset.key;
    const kind = input.dataset.kind;
    if (kind === "bool") {
      patch[key] = input.checked;
    } else if (kind === "number") {
      if (input.value.trim() === "") return;
      const n = Number(input.value);
      if (Number.isNaN(n)) {
        toast(`字段 ${key} 不是合法数字`, "err");
        return;
      }
      patch[key] = n;
    } else {
      patch[key] = input.value;
    }
  });
  try {
    const r = await api("/api/settings", { method: "POST", body: JSON.stringify(patch) });
    S.settings = r.settings || S.settings;
    toast(`已保存 ${r.changed.length} 项：${r.changed.join("、")}`, "ok");
    renderShareInfo();
  } catch (err) {
    toast("保存失败：" + err.message, "err");
  }
}

async function toggleShare() {
  const s = S.settings || {};
  const enable = !s.share_enabled;
  if (enable && !confirm("开启后本机会监听 0.0.0.0，网络可达。将自动生成 api_key 并要求非本地访问携带。继续？")) {
    return;
  }
  try {
    const r = await api("/api/system/share", {
      method: "POST",
      body: JSON.stringify({ enabled: enable }),
    });
    toast(
      enable
        ? `已开启共享${r.restart_required ? "，需重启生效" : ""}。api_key: ${r.api_key}`
        : "已关闭共享（重启后只监听本机）",
      "ok"
    );
    await refreshSettings();
  } catch (err) {
    toast("切换失败：" + err.message, "err");
  }
}

/* ---------------- 外部 API ---------------- */
async function refreshProviders() {
  try {
    const r = await api("/api/providers");
    S.providers = r.providers || [];
    const box = $("#provider-list");
    box.innerHTML = "";
    if (!S.providers.length) {
      box.appendChild(el("div", "empty", "还没有配置外部端点。填写上面的表单可以把自己常用的 API 纳入统一路由"));
      return;
    }
    for (const p of S.providers) {
      const item = el("div", "item");
      const row = el("div", "item-row");
      row.appendChild(el("span", "item-title", p.name));
      row.appendChild(el("span", "tag grey", p.has_key ? "key " + p.api_key_masked : "无 key"));
      row.appendChild(el("span", "spacer"));
      const t = el("button", "mini ghost", "测试");
      t.onclick = () => testProvider(p.name);
      const d = el("button", "mini ghost danger", "移除");
      d.onclick = () => removeProvider(p.name);
      row.appendChild(t);
      row.appendChild(d);
      item.appendChild(row);
      item.appendChild(el("div", "item-sub", p.endpoint));
      item.appendChild(el("div", "item-sub", "模型：" + (p.models.join("，") || "未指定")));
      box.appendChild(item);
    }
  } catch (err) {}
}

async function saveProvider() {
  const name = $("#prov-name").value.trim();
  const endpoint = $("#prov-endpoint").value.trim();
  if (!name || !endpoint) {
    toast("名称与 endpoint 必填", "warn");
    return;
  }
  const models = $("#prov-models").value.split(",").map((x) => x.trim()).filter(Boolean);
  const priceRaw = $("#prov-price").value.split(",").map((x) => Number(x.trim()) || 0);
  try {
    await api("/api/providers", {
      method: "POST",
      body: JSON.stringify({
        action: "add",
        name,
        endpoint,
        api_key: $("#prov-key").value.trim(),
        models,
        price: { in: priceRaw[0] || 0, out: priceRaw[1] || 0 },
      }),
    });
    toast("已保存端点 " + name, "ok");
    $("#prov-name").value = "";
    $("#prov-endpoint").value = "";
    $("#prov-key").value = "";
    $("#prov-models").value = "";
    $("#prov-price").value = "";
    refreshProviders();
  } catch (err) {
    toast("保存失败：" + err.message, "err");
  }
}

async function testProvider(name) {
  try {
    const r = await api("/api/providers", {
      method: "POST",
      body: JSON.stringify({ action: "test", name }),
    });
    toast(r.ok ? `连通正常，返回 ${r.models.length} 个模型` : "连通失败：" + (r.error || ""), r.ok ? "ok" : "err");
  } catch (err) {
    toast("测试失败：" + err.message, "err");
  }
}

async function removeProvider(name) {
  try {
    await api("/api/providers", {
      method: "POST",
      body: JSON.stringify({ action: "remove", name }),
    });
    refreshProviders();
  } catch (err) {
    toast("移除失败：" + err.message, "err");
  }
}

/* ---------------- 事件流 ---------------- */
function handleEvent(evt) {
  const { kind, payload } = evt;
  switch (kind) {
    case "runtime.state":
      S.runtime = Object.assign({}, S.runtime, payload);
      renderRuntime();
      renderPills();
      if (payload.state === "ready") {
        refreshModels();
        refreshP2P();
        toast("模型就绪：" + payload.model, "ok");
      }
      if (payload.state === "failed") toast("加载失败：" + (payload.error || ""), "err");
      break;

    case "runtime.log":
      pushLog(payload.line, payload.stream);
      break;

    case "runtime.download":
      if (payload.msg) pushLog(payload.msg, "cmd");
      break;

    case "runtime.binary":
      if (payload.ok) toast("运行时已就绪：" + payload.path, "ok");
      else toast("运行时准备失败：" + payload.error, "err");
      refreshRuntime();
      break;

    case "download.state":
      refreshDownloads();
      if (payload.status === "done") {
        toast("下载完成：" + payload.file.split("/").pop(), "ok");
        refreshModels();
      }
      if (payload.status === "failed") toast("下载失败：" + payload.error, "err");
      break;

    case "download.progress":
      for (let i = 0; i < S.downloads.length; i++) {
        if (S.downloads[i].id === payload.id) S.downloads[i] = payload;
      }
      renderDownloads();
      break;

    case "model.added":
      refreshModels();
      break;

    case "ledger.entry":
      refreshLedger();
      break;

    case "audit.result":
      refreshAudits();
      if (payload.verdict === "suspect") {
        toast(`复算判定可疑：相似度 ${payload.similarity}`, "err");
      }
      break;

    case "peer.update":
      refreshP2P();
      break;

    case "peer.reject":
      break;

    case "peer.pruned":
      refreshP2P();
      break;

    case "ad.published":
      toast(
        `广告已发布（${payload.relays} 个 relay，${(payload.models || []).length} 个模型）`,
        payload.relays ? "ok" : "warn"
      );
      refreshP2P();
      break;

    case "relay.state":
      refreshP2P();
      break;

    case "p2p.error":
      if (payload.msg) toast("P2P：" + payload.msg, "err");
      break;

    case "settings.updated":
      refreshSystem();
      break;

    default:
      break;
  }
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => setLinkState(true);
  es.onmessage = (e) => {
    try {
      handleEvent(JSON.parse(e.data));
    } catch (_) {}
  };
  es.onerror = () => {
    // EventSource 会自动重连，这里只把状态显示出来，不弹 toast 免得刷屏
    setLinkState(false);
  };
}

/* 连接状态指示：SSE 断开时用户需要知道数据停止更新了 */
function setLinkState(connected) {
  S.linkUp = connected;
  const dot = $("#link-state");
  if (!dot) return;
  dot.textContent = connected ? "实时" : "已断开，重连中…";
  dot.className = "pill " + (connected ? "ok" : "err");
}

/* 快捷键：Ctrl+1..6 切视图，Ctrl+Enter 发消息，Esc 关闭提示 */
function bindShortcuts() {
  const views = ["models", "runtime", "network", "ledger", "chat", "settings"];
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && !e.shiftKey && e.key >= "1" && e.key <= "6") {
      const target = views[Number(e.key) - 1];
      const btn = document.querySelector(`#tabs button[data-view="${target}"]`);
      if (btn) {
        e.preventDefault();
        btn.click();
      }
    }
    if (e.ctrlKey && e.key === "Enter") {
      const view = document.querySelector("#view-chat");
      if (view && view.classList.contains("active")) {
        e.preventDefault();
        sendChat();
      }
    }
    if (e.key === "Escape") {
      $("#toasts").innerHTML = "";
    }
  });
}

/* ---------------- 初始化 ---------------- */
async function init() {
  initTabs();
  bindShortcuts();

  $("#hub-search").onclick = searchHub;
  $("#hub-q").addEventListener("keydown", (e) => { if (e.key === "Enter") searchHub(); });
  $("#models-refresh").onclick = refreshModels;
  $("#dl-clear").onclick = async () => {
    await api("/api/downloads/clear", { method: "POST" });
    refreshDownloads();
  };
  $("#runtime-install").onclick = installRuntime;
  $("#runtime-unload").onclick = unloadModel;
  $("#advertise-toggle").onchange = (e) => toggleAdvertise(e.target.checked);
  $("#publish-now").onclick = publishNow;
  $("#relay-add").onclick = addRelay;
  $("#relay-url").addEventListener("keydown", (e) => { if (e.key === "Enter") addRelay(); });
  $("#peers-refresh").onclick = refreshP2P;
  $("#chat-send").onclick = sendChat;
  $("#ledger-refresh").onclick = refreshLedger;
  $("#ledger-settle").onclick = settleLedger;
  $("#audit-refresh").onclick = refreshAudits;
  $("#audit-probe").onclick = probeAudit;
  $("#emission-refresh").onclick = refreshEmission;
  $("#emission-publish").onclick = publishEmission;
  $("#chain-refresh").onclick = refreshChain;
  $("#chain-build").onclick = buildChainTx;
  $("#share-toggle").onclick = toggleShare;
  $("#provider-save").onclick = saveProvider;
  $("#onboarding-dismiss").onclick = dismissOnboarding;
  $("#update-check").onclick = checkUpdate;
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  });

  try {
    const c = await api("/api/hub/curated");
    renderCurated(c.items || []);
  } catch (_) {}

  // 首屏先给骨架，避免空白等待
  skeleton($("#local-models"), 2, "正在读取本地模型…");
  skeleton($("#ledger-entries"), 2, "正在读取账本…");

  await refreshSystem();
  await refreshRuntime();
  await refreshModels();
  await refreshDownloads();
  await refreshP2P();
  await refreshLedger();
  await refreshSettings();

  if (S.runtime && S.runtime.log_tail) {
    for (const line of S.runtime.log_tail) pushLog(line, "out");
  }

  connectEvents();
  setInterval(refreshDownloads, 3000);
  setInterval(refreshRuntime, 5000);
  setInterval(refreshP2P, 10000);
}

document.addEventListener("DOMContentLoaded", init);
