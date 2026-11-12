/* AstrBot 把桥接 SDK 注入在 </body> 之前，比本文件执行晚：
   所以不能在这里抓一次全局（那时还不存在），要每次调用时再去找。 */
let bridge = null;

function resolveBridge() {
  const candidates = [];
  try { candidates.push(window.AstrBotPluginView, window.AstrBotPluginPage); } catch (err) {}
  try { candidates.push(window.parent.AstrBotPluginView, window.parent.AstrBotPluginPage); } catch (err) {}
  try { candidates.push(window.top.AstrBotPluginView, window.top.AstrBotPluginPage); } catch (err) {}
  return candidates.find((item) => item && typeof item.apiGet === "function") || null;
}

const $ = (id) => document.getElementById(id);

const TEXT_FIELDS = [
  "api_base", "model_id", "language_code", "instructions_default", "edge_voice",
];
const NUMBER_FIELDS = [
  "speed", "volume", "timeout", "max_chars", "max_segments",
  "speak_threshold", "daily_quota", "cooldown_seconds",
];
const CHECK_FIELDS = [
  "enable", "auto_fallback", "group_enable", "dual_output",
  "keep_audio", "edge_fallback", "read_relation", "schedule_aware",
];

let voices = [];
let sceneLabels = {};
let sceneVoices = {};
let activeScene = "default";

/* ---------- 桥接 ---------- */

/** 桥接会先剥掉一层 {code,data}，这里两种形态都认，免得面板拿到 undefined。 */
function unwrap(res) {
  if (res == null) return null;
  let value = res;
  if (value && typeof value === "object" && "data" in value && "code" in value) {
    value = value.data;
  }
  if (value && typeof value === "object" && "data" in value && value.code === undefined) {
    value = value.data;
  }
  return value;
}

function offline() {
  return !bridge || typeof bridge.apiGet !== "function";
}

async function apiGet(path) {
  bridge = bridge || resolveBridge();
  if (!bridge) return null;
  try {
    return unwrap(await bridge.apiGet(path));
  } catch (err) {
    console.warn("apiGet failed", path, err);
    return null;
  }
}

async function apiPost(path, body) {
  bridge = bridge || resolveBridge();
  if (!bridge) return null;
  try {
    return unwrap(await bridge.apiPost(path, body || {}));
  } catch (err) {
    console.warn("apiPost failed", path, err);
    return null;
  }
}

/* ---------- 主题 ---------- */

function setTheme(mode) {
  document.documentElement.dataset.theme = mode;
  try { localStorage.setItem("amo-voice-theme", mode); } catch (_) {}
}

(function initTheme() {
  let saved = "";
  try { saved = localStorage.getItem("amo-voice-theme") || ""; } catch (_) {}
  setTheme(saved === "dark" ? "dark" : "light");
})();

$("theme").addEventListener("click", () => {
  const now = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  setTheme(now);
});

/* ---------- 标签页 ---------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("on", item === tab));
    const name = tab.dataset.tab;
    document.querySelectorAll(".pane").forEach((pane) => {
      pane.hidden = pane.id !== "pane-" + name;
    });
    if (name === "voices" && !voices.length) pullVoices();
    if (name === "history") loadHistory();
  });
});

/* ---------- 概览 ---------- */

function ago(ts) {
  if (!ts) return "还没说过话";
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return Math.round(diff) + " 秒前";
  if (diff < 3600) return Math.round(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.round(diff / 3600) + " 小时前";
  return Math.round(diff / 86400) + " 天前";
}

async function loadStatus() {
  const data = await apiGet("status");
  if (!data) {
    $("statusMeta").textContent = offline()
      ? "面板桥接不可用：请在 AstrBot 插件页打开本页。"
      : "读取状态失败。";
    return;
  }
  if (data.link) amoLinkRender(data.link);
  $("enablePill").textContent = data.enabled ? "已启用" : "已关闭";
  $("enablePill").classList.toggle("off", !data.enabled);
  $("providerName").textContent = data.provider || "—";
  $("apiBase").textContent = data.api_base || "—";
  $("keyMasked").textContent = data.has_key ? data.api_key_masked : "未填写";
  $("defaultVoice").textContent = data.default_voice || "未选";
  const quota = data.daily_quota > 0 ? "/" + data.daily_quota : "";
  $("usedToday").textContent = (data.used_today || 0) + quota;
  $("lastSpoke").textContent = ago(data.last_ts);
  $("companionState").textContent = data.companion ? "已连接" : "未检测到";
  $("statusMeta").textContent =
    "v" + data.version + " · 音色 " + (data.voice_count || 0) + " 个 · 已运行 " +
    Math.round((data.uptime_seconds || 0) / 60) + " 分钟";
}

/* ---------- 试听 ---------- */

$("testBtn").addEventListener("click", async () => {
  const text = ($("testText").value || "").trim();
  const btn = $("testBtn");
  btn.disabled = true;
  $("testMeta").textContent = "合成中…";
  const data = await apiPost("test", { text });
  if (data && data.code === 1) {
    $("testMeta").textContent = "试听失败：" + (data.message || "未知原因");
    return;
  }
  btn.disabled = false;
  if (!data || !data.audio) {
    $("testMeta").textContent = "失败：" + ((data && data.message) || "没有返回音频");
    return;
  }
  const audio = $("testAudio");
  audio.src = data.audio;
  audio.hidden = false;
  audio.play().catch(() => {});
  $("testMeta").textContent =
    Math.round(data.bytes / 1024) + "KB · " + data.elapsed_ms + "ms · " + data.provider + " · " + data.voice;
});

/* ---------- 配置 ---------- */

function fillConfig(data) {
  TEXT_FIELDS.forEach((key) => {
    const el = $(key);
    if (el) el.value = data[key] == null ? "" : data[key];
  });
  NUMBER_FIELDS.forEach((key) => {
    const el = $(key);
    if (el && data[key] != null) el.value = data[key];
  });
  CHECK_FIELDS.forEach((key) => {
    const el = $(key);
    if (el) el.checked = Boolean(data[key]);
  });
  if (data.api_key_masked) {
    $("keyHint").textContent = "已保存：" + data.api_key_masked + "（留空不修改）";
  }
  const select = $("default_voice");
  const current = data.default_voice || "";
  if (select) {
    const opts = voices.length ? voices : (current ? [{ id: current, name: current }] : []);
    select.innerHTML = opts
      .map((item) => `<option value="${item.id}"${item.id === current ? " selected" : ""}>${item.name || item.id}</option>`)
      .join("") || '<option value="">还没拉取</option>';
  }
}

async function loadConfig() {
  const data = await apiGet("config");
  if (!data) {
    $("saveHint").textContent = "读取配置失败。";
    return;
  }
  $("provider").value = data.provider || "breeze";
  fillConfig(data);
}

function collectConfig() {
  const payload = {};
  TEXT_FIELDS.forEach((key) => {
    const el = $(key);
    if (el) payload[key] = el.value.trim();
  });
  NUMBER_FIELDS.forEach((key) => {
    const el = $(key);
    if (el && el.value !== "") payload[key] = Number(el.value);
  });
  CHECK_FIELDS.forEach((key) => {
    const el = $(key);
    if (el) payload[key] = el.checked;
  });
  payload.provider = $("provider").value;
  payload.default_voice = $("default_voice").value;
  payload.api_key = $("api_key").value.trim();
  return payload;
}

$("clearKey").addEventListener("click", async () => {
  if (!confirm("清除已保存的 API Key？")) return;
  const res = await apiPost("config/clear_key", {});
  $("saveHint").textContent = res ? "密钥已清除。" : "清除失败。";
  $("keyHint").textContent = "密钥已清除，重新填写后再保存。";
  loadStatus();
});

$("save").addEventListener("click", async () => {
  const btn = $("save");
  btn.disabled = true;
  $("saveHint").textContent = "保存中…";
  const res = await apiPost("config/save", collectConfig());
  btn.disabled = false;
  if (!res) {
    $("saveHint").textContent = "保存失败（没有响应）。";
    return;
  }
  $("api_key").value = "";
  const bits = [res.saved ? "已保存并落盘。" : "已应用到内存（本次重启前有效）。"];
  if (res.api_key_masked) {
    $("keyHint").textContent = "已保存：" + res.api_key_masked + "（留空不修改）";
  }
  if (res.key_status === "ok") {
    bits.push("key 校验通过。");
  } else if (res.key_status) {
    bits.push("key 校验未通过（" + res.key_status + "），去控制台换一个新的再保存。");
  }
  $("saveHint").textContent = bits.join(" ");
  loadStatus();
});

/* ---------- 音色 ---------- */

function renderVoices() {
  const keyword = ($("voiceSearch").value || "").trim().toLowerCase();
  const list = $("voiceList");
  const items = voices.filter((item) => {
    if (!keyword) return true;
    return (
      (item.name || "").toLowerCase().includes(keyword) ||
      (item.id || "").toLowerCase().includes(keyword) ||
      (item.language || "").toLowerCase().includes(keyword)
    );
  });
  if (!items.length) {
    list.innerHTML = '<li class="empty">' + (voices.length ? "没有匹配的音色" : "还没有音色，先拉取") + "</li>";
    return;
  }
  list.innerHTML = items
    .map((item) => {
      const bits = [item.language, item.gender, item.age].filter(Boolean).join(" · ");
      const used = (sceneVoices[activeScene] || "") === item.id;
      return (
        '<li><div class="voice-item"><span class="vname">' +
        escapeHtml(item.name || item.id) +
        '</span><span class="vmeta">' + escapeHtml(bits) +
        '</span></div><button class="act" data-voice="' + escapeHtml(item.id) + '">' +
        (used ? "已设为当前场景" : "设为这个场景") +
        "</button></li>"
      );
    })
    .join("");
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function pullVoices() {
  const btn = $("pullVoices");
  btn.disabled = true;
  $("voiceHint").textContent = "拉取中…";
  const res = await apiPost("voices/pull", {});
  btn.disabled = false;
  if (!res) {
    $("voiceHint").textContent = "拉取失败：面板桥接没回话（没有响应）。";
    return;
  }
  if (res.code === 1) {
    $("voiceHint").textContent = "拉取失败：" + (res.message || "未知原因");
    return;
  }
  $("voiceHint").textContent = "拉到 " + (res.count || 0) + " 个音色（" + (res.provider || "?") + "）。";
  await loadVoices();
  loadStatus();
}

async function loadVoices() {
  const data = await apiGet("voices");
  if (!data) return;
  voices = data.items || [];
  sceneLabels = data.scene_labels || {};
  sceneVoices = data.scene_voices || {};
  if (data.default_voice && !sceneVoices.default) {
    sceneVoices.default = data.default_voice;
  }
  $("voiceMeta").textContent = voices.length
    ? voices.length + " 个音色 · " + (data.provider || "")
    : "还没拉取";
  renderSceneChips();
  renderVoices();
  renderSceneMap();
  const select = $("default_voice");
  if (select && voices.length) {
    const current = data.default_voice || "";
    select.innerHTML = voices
      .map((item) => `<option value="${item.id}"${item.id === current ? " selected" : ""}>${escapeHtml(item.name || item.id)}</option>`)
      .join("");
  }
}

function renderSceneChips() {
  const box = $("sceneChips");
  box.innerHTML = Object.keys(sceneLabels)
    .map(
      (key) =>
        '<button class="chip' + (key === activeScene ? " on" : "") + '" data-scene="' + key + '">' +
        escapeHtml(sceneLabels[key]) + "</button>"
    )
    .join("");
}

function renderSceneMap() {
  const lines = Object.keys(sceneLabels)
    .filter((key) => sceneVoices[key])
    .map((key) => sceneLabels[key] + " → " + sceneVoices[key]);
  $("sceneMap").textContent = lines.length ? lines.join("　") : "还没分配，全部用默认音色。";
}

document.addEventListener("click", async (ev) => {
  const sceneBtn = ev.target.closest("[data-scene]");
  if (sceneBtn) {
    activeScene = sceneBtn.dataset.scene;
    renderSceneChips();
    renderVoices();
    return;
  }
  const voiceBtn = ev.target.closest("[data-voice]");
  if (voiceBtn) {
    const voice = voiceBtn.dataset.voice;
    const res = await apiPost("voices/scene", { scene: activeScene, voice });
    if (res) {
      sceneVoices[activeScene] = voice;
      if (activeScene === "default") {
        const select = $("default_voice");
        if (select) select.value = voice;
      }
      renderVoices();
      renderSceneMap();
      $("voiceHint").textContent =
        "已把「" + (sceneLabels[activeScene] || activeScene) + "」设成 " + voice + "。";
    }
    return;
  }
});

$("pullVoices").addEventListener("click", pullVoices);
$("pullFromConfig").addEventListener("click", async () => {
  await pullVoices();
  await loadConfig();
});
$("voiceSearch").addEventListener("input", renderVoices);

/* ---------- 记录 ---------- */

function fmtTime(ts) {
  if (!ts) return "";
  const date = new Date(ts * 1000);
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return hh + ":" + mm;
}

async function loadHistory() {
  const data = await apiGet("history");
  const list = $("history");
  const items = (data && data.items) || [];
  if (!items.length) {
    list.innerHTML = '<li class="empty">暂无记录</li>';
    return;
  }
  list.innerHTML = items
    .map((item) => {
      const ok = item.allowed;
      const tail = ok ? (item.ms || 0) + "ms" : "未发";
      return (
        "<li><span class=\"t\">" + fmtTime(item.ts) + "</span>" +
        '<span class="p" title="' + escapeHtml(item.reason || "") + '">' +
        escapeHtml(item.text || item.reason || "") +
        '</span><span class="s ' + (ok ? "ok" : "bad") + '">' + tail + "</span></li>"
      );
    })
    .join("");
}

$("refreshHistory").addEventListener("click", loadHistory);

/* ---------- 启动 ---------- */

loadStatus();
loadConfig();
if (document.querySelector('.tab[data-tab="voices"]').classList.contains("on")) {
  loadVoices();
}
setInterval(loadStatus, 30000);

/* 桥接 SDK 比本文件晚执行：等它出现再补一次真正的加载，否则首屏永远是空的。 */
(function waitForBridgeAndLoad(retries) {
  bridge = bridge || resolveBridge();
  if (bridge) {
    console.info("[面板] 桥接已就绪，开始加载");
    loadStatus();
    loadConfig();
    loadVoices();
    loadHistory();
    return;
  }
  if (retries > 0) {
    setTimeout(() => waitForBridgeAndLoad(retries - 1), 120);
    return;
  }
  console.warn("[面板] 6 秒内没等到 AstrBot 桥接，面板处于离线态（接口全部跳过）");
})(50);

/* ---- 连接状态：我这个挂载插件自己有没有连上终端/记忆库 ---- */
function amoLinkDot(ok) { return '<span style="color:' + (ok ? "#2e9e5b" : "#d83b3b") + '">\u25cf</span>'; }
function amoLinkRender(d) {
  const box = document.getElementById("linkbar");
  if (!box) return;
  box.onclick = null;
  if (!d) {
    // 失败要能自己说明白、能点重试，别只留一句「读不到」
    box.innerHTML = '连接状态：读取失败（health 接口没响应，<span style="text-decoration:underline;cursor:pointer">点这里重试</span>）';
    box.onclick = () => amoLinkLoad();
    return;
  }
  const hb = (typeof d.last_heartbeat === "number" && d.last_heartbeat >= 0)
    ? ("心跳 " + d.last_heartbeat + " 秒前") : "从未连上过";
  box.innerHTML = [
    amoLinkDot(d.terminal_connected) + " 终端" + (d.terminal_connected ? " v" + (d.terminal_version || "?") : " 未连接"),
    amoLinkDot(d.data_connected) + " 记忆库" + (d.data_connected ? " v" + (d.data_version || "?") : " 未连接"),
    "bot_id " + (d.bot_id || "\u2014"),
    hb
  ].join(' <span style="opacity:.35">\uff5c</span> ');
  box.title = d.error || "";
}

async function amoLinkFetch() {
  try { return await apiGet("health"); } catch (e) { return null; }
}
async function amoLinkLoad() {
  // 面板脚本比 AstrBot 的桥接先跑，头几次拿不到数据是正常的：
  // 等桥接就绪再重试，别一上来就把「读不到」画给人看。
  const box = document.getElementById("linkbar");
  if (box && /检查中|读取失败/.test(box.textContent || "")) box.textContent = "连接状态：读取中…";
  for (let i = 0; i < 20; i += 1) {
    const d = await amoLinkFetch();
    if (d) { amoLinkRender(d); return; }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  amoLinkRender(null);
}

amoLinkLoad();
setInterval(amoLinkLoad, 30000);
