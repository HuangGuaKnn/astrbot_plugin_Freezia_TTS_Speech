// Freezia TTS Speech · WebUI
const $ = (id) => document.getElementById(id);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const SVG_NS = "http://www.w3.org/2000/svg";

const PANELS = {
  overview: ["总览", "语音回复的运行状态与最近的判定"],
  profiles: ["发声档案", "决定什么时候开口、用什么方式发送"],
  sessions: ["会话与全局", "TTS 服务、超时，以及给会话分配档案"],
  lab: ["过滤与试听", "看一条回复会被怎样清洗、判定和念出来"],
};
const MODE_LABELS = { both: "文字 + 语音", voice_only: "仅语音", text_only: "仅文字" };
const STATUS = {
  sent: { label: "已发送", pill: "ok", icon: "send" },
  failed: { label: "失败", pill: "bad", icon: "x" },
  skipped: { label: "跳过", pill: "", icon: "skip" },
  pending: { label: "合成中", pill: "busy", icon: "wave" },
};
const SOURCE_CHIPS = {
  user: ["用户要求", "user"],
  tool: ["角色主动", "tool"],
  voice: ["语音对语音", "voice"],
};
const EMOTION_SOURCES = { tool: "角色指定", llm: "LLM 判断", marker: "符号", manual: "手动" };
// 新增情绪行时预填的判断符号，只是建议，可以改
const EMOTION_HINTS = {
  happy: ["哈哈", "嘿嘿", "好耶"],
  sad: ["呜", "难过", "哭"],
  angry: ["哼", "生气", "讨厌"],
  surprised: ["诶", "欸", "居然"],
  shy: ["♡", "害羞", "脸红"],
  gentle: ["乖", "摸摸", "没事的"],
};
const LANG_CODE = /^[A-Za-z][\w-]{0,15}$/;
const STAGE_LABELS = {
  plugin: "插件开关", profile: "档案开关", session: "会话静音", send_mode: "发送方式", mute: "不出声短语",
  content: "可朗读内容", force: "强制出声", scope: "聊天范围", length: "字数上限", cooldown: "冷却", probability: "概率",
};
const TRACE_STATES = {
  pass: { icon: "check", text: "通过" },
  stop: { icon: "x", text: "拦下" },
  speak: { icon: "volume", text: "出声" },
  chance: { icon: "dice", text: "按概率" },
  bypass: { icon: "skip", text: "强制跳过" },
  unreached: { icon: "dot", text: "未判定" },
};
const SAMPLES = [
  ["动作描写", "*轻轻拉了拉你的袖子* 你回来啦～今天辛苦了！"],
  ["括号心理", "（偷偷看了一眼）……这个，是送给我的吗♡"],
  ["引号台词", "「才、才不是特意等你的！」她别过脸去"],
  ["Markdown", "**提醒**：记得按时吃饭！\n- 早饭\n- 午饭"],
  ["链接与代码", "资料在这里 https://example.com/docs\n```python\nprint('hi')\n```"],
  ["颜文字", "好耶好耶(๑•̀ㅂ•́)و✧ 一起出发吧🎉"],
  ["情绪", "最喜欢你了！♡"],
  ["长回复", "今天我们聊了好多事情，从早上的天气说到晚上的晚饭，又从晚饭说到周末想去的地方。".repeat(2)],
];

const app = {
  bridge: null,
  model: null,
  panel: "overview",
  inflight: 0,
  eventFilter: "all",
  selected: null, // 正在编辑的档案名
  creating: false,
  pendingSwitch: null,
};

// ---------- 工具 ----------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") el.style.cssText = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

function icon(name, cls = "i") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function toast(message, type = "ok") {
  const box = $("toasts");
  const el = h("div", { class: `toast ${type === "error" ? "error" : ""}`, role: type === "error" ? "alert" : "status" },
    icon(type === "error" ? "alert" : "check"), h("span", {}, message));
  box.append(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => {
    el.classList.add("leave");
    setTimeout(() => el.remove(), 220);
  }, type === "error" ? 6000 : 2800);
}

function pct(value) {
  return `${Math.round(value * 100)}%`;
}

function parseUmo(umo) {
  const [platform = "", type = "", ...rest] = String(umo || "").split(":");
  const id = rest.join(":");
  if (!id) return null;
  const kind = /group/i.test(type) ? "群" : /friend|private/i.test(type) ? "私" : type;
  return { platform, kind, id };
}

function sessionLabel(umo) {
  const parsed = parseUmo(umo);
  if (!parsed) return h("span", { class: "sess", title: umo }, umo);
  return h("span", { class: "sess", title: umo }, h("b", {}, parsed.kind), `${parsed.platform} · ${parsed.id}`);
}

function timeOf(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtMs(ms) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} 秒` : `${Math.round(ms)} ms`;
}

function unwrap(result) {
  if (result?.status === "error") throw new Error(result.message || "请求失败");
  return result?.status === "ok" && Object.hasOwn(result, "data") ? result.data : result;
}

async function api(path, method = "GET", body) {
  app.inflight += 1;
  try {
    if (app.bridge) {
      const result = method === "POST"
        ? await app.bridge.apiPost(`page/${path}`, body || {})
        : await app.bridge.apiGet(`page/${path}`);
      return unwrap(result);
    }
    // 脱离 AstrBot 的开发预览（scripts/dev_server.py）
    const response = await fetch(`/api/page/${path}`, {
      method,
      headers: method === "POST" ? { "Content-Type": "application/json" } : {},
      body: method === "POST" ? JSON.stringify(body || {}) : undefined,
      cache: "no-store",
    });
    let result;
    try {
      result = await response.json();
    } catch {
      throw new Error(`服务器没有返回有效 JSON（${response.status}）`);
    }
    if (!response.ok) throw new Error(result?.message || `请求失败（${response.status}）`);
    return unwrap(result);
  } finally {
    app.inflight -= 1;
  }
}

async function run(button, task) {
  if (button?.disabled) return undefined;
  if (button) {
    button.disabled = true;
    button.classList.add("loading");
  }
  try {
    return await task();
  } catch (error) {
    toast(error.message || String(error), "error");
    return undefined;
  } finally {
    if (button) {
      button.disabled = false;
      button.classList.remove("loading");
    }
  }
}

function armButton(button, label, onConfirm) {
  if (button.dataset.armed) {
    delete button.dataset.armed;
    button.classList.remove("armed");
    clearTimeout(button._armTimer);
    button.querySelector("span").textContent = button.dataset.label;
    onConfirm();
    return;
  }
  button.dataset.armed = "1";
  button.dataset.label = button.querySelector("span").textContent;
  button.querySelector("span").textContent = label;
  button.classList.add("armed");
  button._armTimer = setTimeout(() => {
    delete button.dataset.armed;
    button.classList.remove("armed");
    button.querySelector("span").textContent = button.dataset.label;
  }, 3500);
}

// ---------- 表单脏状态 ----------
class Dirty {
  constructor(bar, read) {
    this.bar = bar;
    this.read = read;
    this.snapshot = null; // 表单还没填充过时不算有修改
  }
  reset() {
    this.snapshot = JSON.stringify(this.read());
    this.update();
  }
  get dirty() {
    return this.snapshot !== null && JSON.stringify(this.read()) !== this.snapshot;
  }
  update() {
    const dirty = this.dirty;
    this.bar.classList.toggle("dirty", dirty);
    this.bar.querySelector(".dirty-note").textContent = dirty ? "有未保存的修改" : "所有修改已保存";
  }
}

// ---------- 标签输入 ----------
class TagInput {
  constructor(container, onChange) {
    this.container = container;
    this.values = [];
    this.onChange = onChange;
    this.input = h("input", { type: "text", maxlength: "20", placeholder: "输入后回车添加", "aria-label": "添加短语" });
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === "," || event.key === "，") {
        event.preventDefault();
        this.commit();
      } else if (event.key === "Backspace" && !this.input.value && this.values.length) {
        this.values.pop();
        this.render();
        this.onChange();
      }
    });
    this.input.addEventListener("blur", () => this.commit());
    container.addEventListener("click", (event) => {
      if (event.target === container) this.input.focus();
    });
  }
  commit() {
    const parts = this.input.value.split(/[,，\n]/).map((part) => part.trim()).filter(Boolean);
    this.input.value = "";
    let changed = false;
    for (const part of parts) {
      if (!this.values.includes(part) && this.values.length < 50) {
        this.values.push(part.slice(0, 20));
        changed = true;
      }
    }
    if (changed) {
      this.render();
      this.onChange();
    }
  }
  set(values) {
    this.values = [...values];
    this.input.value = "";
    this.render();
  }
  get() {
    const pending = this.input.value.trim();
    return pending && !this.values.includes(pending) ? [...this.values, pending] : [...this.values];
  }
  render() {
    const chips = this.values.map((value, index) =>
      h("span", { class: "tag" }, value,
        h("button", {
          type: "button", "aria-label": `移除「${value}」`,
          onclick: () => { this.values.splice(index, 1); this.render(); this.onChange(); },
        }, icon("x"))));
    this.container.replaceChildren(...chips, this.input);
  }
}

// ---------- 语言 ----------
function languages() {
  return app.model?.settings?.voice_languages || [];
}

function languageLabel(code) {
  if (!code) return "和文字相同";
  const lang = languages().find((l) => l.code === code);
  return lang ? `${lang.name}（${lang.code}）` : `${code}（未配置）`;
}

function languageOptions(select, current, { follow = null } = {}) {
  const options = [];
  if (follow !== null) options.push(h("option", { value: "@profile" }, follow));
  options.push(h("option", { value: "" }, "和文字相同"));
  for (const lang of languages()) options.push(h("option", { value: lang.code }, `${lang.name}（${lang.code}）`));
  if (current && current !== "@profile" && !languages().some((l) => l.code === current)) {
    options.push(h("option", { value: current }, `${current}（未配置）`));
  }
  select.replaceChildren(...options);
  select.value = current ?? "";
}

// ---------- 发音词典编辑器 ----------
class DictEditor {
  constructor(container, onChange) {
    this.onChange = onChange;
    this.head = h("div", { class: "dict-head" }, h("span", {}, "原文"), h("span"), h("span", {}, "读法"), h("span", {}, "语言"), h("span"));
    this.rows = h("div", { class: "dict-rows" });
    this.empty = h("span", { class: "muted small" }, "还没有词条");
    const add = h("button", {
      type: "button", class: "btn ghost small",
      onclick: () => { this.addRow(); this.changed(); this.rows.lastElementChild?.querySelector("input")?.focus(); },
    }, icon("plus"), "添加词条");
    container.replaceChildren(this.head, this.rows, h("div", { class: "dict-foot" }, add, this.empty));
  }
  changed() {
    this.sync();
    this.onChange();
  }
  sync() {
    const count = this.rows.children.length;
    this.head.hidden = count === 0;
    this.empty.hidden = count > 0;
  }
  langSelect(value) {
    const select = h("select", { "aria-label": "语言", onchange: () => this.onChange() });
    select.append(h("option", { value: "" }, "全部语言"));
    for (const lang of languages()) select.append(h("option", { value: lang.code }, lang.name));
    if (value && !languages().some((l) => l.code === value)) select.append(h("option", { value }, `${value}（未配置）`));
    select.value = value || "";
    return select;
  }
  addRow(entry = { from: "", to: "", lang: "" }) {
    const row = h("div", { class: "dict-row" },
      h("input", { value: entry.from, maxlength: "40", placeholder: "原文，如 Freezia", "aria-label": "原文" }),
      h("span", { class: "arrow" }, icon("arrow")),
      h("input", { value: entry.to, maxlength: "40", placeholder: "读法，留空表示删掉", "aria-label": "读法" }),
      this.langSelect(entry.lang),
      h("button", { type: "button", class: "icon-ghost", title: "删除词条", "aria-label": "删除词条", onclick: () => { row.remove(); this.changed(); } }, icon("trash")));
    this.rows.append(row);
    this.sync();
  }
  set(entries) {
    this.rows.replaceChildren();
    for (const entry of entries || []) this.addRow(entry);
    this.sync();
  }
  get() {
    return [...this.rows.children]
      .map((row) => {
        const [source, to] = row.querySelectorAll("input");
        return { from: source.value, to: to.value, lang: row.querySelector("select").value };
      })
      .filter((entry) => entry.from.trim());
  }
}

// ---------- 情绪音色 ----------
function emotions() {
  return app.model?.emotions || [];
}

function emotionName(code) {
  if (code === "neutral") return "平静";
  return emotions().find((e) => e.code === code)?.name || code;
}

function emotionChip(code, source) {
  if (!code) return null;
  const from = EMOTION_SOURCES[source];
  return h("span", { class: "tag-chip mood", title: from ? `情绪来源：${from}` : "" }, emotionName(code), from ? h("small", {}, from) : null);
}

class EmotionEditor {
  constructor(container, onChange) {
    this.onChange = onChange;
    this.head = h("div", { class: "emo-head" }, h("span", {}, "情绪"), h("span", {}, "TTS 服务"), h("span", {}, "判断符号（逗号分隔）"), h("span"));
    this.rows = h("div", { class: "emo-rows" });
    this.empty = h("span", { class: "muted small" }, "还没有配置，所有回复都用默认声音");
    this.add = h("button", {
      type: "button", class: "btn ghost small",
      onclick: () => {
        const used = new Set(this.get().map((entry) => entry.emotion));
        const next = emotions().find((e) => !used.has(e.code));
        if (!next) return;
        this.addRow({ emotion: next.code, provider_id: "", markers: EMOTION_HINTS[next.code] || [] });
        this.changed();
        this.rows.lastElementChild?.querySelectorAll("select")[1]?.focus();
      },
    }, icon("plus"), "添加情绪");
    container.replaceChildren(this.head, this.rows, h("div", { class: "dict-foot" }, this.add, this.empty));
  }
  changed() {
    this.sync();
    this.onChange();
  }
  sync() {
    const count = this.rows.children.length;
    this.head.hidden = count === 0;
    this.empty.hidden = count > 0;
    this.add.disabled = count >= emotions().length;
  }
  addRow(entry) {
    const mood = h("select", { "aria-label": "情绪" }, ...emotions().map((e) => h("option", { value: e.code }, e.name)));
    mood.value = entry.emotion;
    const provider = h("select", { "aria-label": "TTS 服务" });
    providerOptions(provider, entry.provider_id, "选择这个情绪用的服务");
    const markers = h("input", { value: (entry.markers || []).join("，"), placeholder: "判断符号，逗号分隔，如 哈哈，嘿嘿", title: "判断符号（逗号分隔）", "aria-label": "判断符号" });
    let hinted = (entry.markers || []).join("，") === (EMOTION_HINTS[entry.emotion] || []).join("，");
    mood.addEventListener("change", () => {
      // 符号还是上一个情绪的建议值时，跟着换成新情绪的建议
      if (hinted || !markers.value.trim()) markers.value = (EMOTION_HINTS[mood.value] || []).join("，");
      hinted = true;
    });
    markers.addEventListener("input", () => { hinted = false; });
    const row = h("div", { class: "emo-row" }, mood, provider, markers,
      h("button", { type: "button", class: "icon-ghost", title: "删除", "aria-label": "删除情绪", onclick: () => { row.remove(); this.changed(); } }, icon("trash")));
    this.rows.append(row);
    this.sync();
  }
  set(entries) {
    this.rows.replaceChildren();
    for (const entry of entries || []) this.addRow(entry);
    this.sync();
  }
  get() {
    return [...this.rows.children].map((row) => {
      const [mood, provider] = row.querySelectorAll("select");
      const markers = row.querySelector("input").value.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean);
      return { emotion: mood.value, provider_id: provider.value, markers };
    });
  }
}

// ---------- 状态与检查 ----------
function healthChecks(model) {
  const d = model.diagnostics || {};
  const s = model.settings;
  const list = [];
  if (d.error) list.push({ state: "bad", title: "读取 AstrBot 状态失败", text: d.error });
  list.push(s.enabled
    ? { state: "ok", title: "语音回复已开启", text: `默认档案「${s.default_profile}」` }
    : { state: "warn", title: "语音回复已暂停", text: "打开左下角的总开关后恢复" });
  list.push(d.builtin_tts_enabled
    ? { state: "warn", title: "AstrBot 自带的语音回复仍在开启", text: "本插件用来完全替代它，两者同时开启会把同一条回复合成两次。请在 AstrBot 配置的「AI」分组里关闭「语音回复」（provider_tts_settings.enable），TTS 服务提供商保留。" }
    : { state: "ok", title: "自带语音回复已关闭", text: "语音回复由本插件接管" });
  list.push(d.streaming_enabled
    ? { state: "bad", title: "流式输出已开启，本插件不会工作", text: "流式回复不经过插件，插件会跳过并在日志中警告。需要语音时请在 AstrBot 配置里关闭「流式输出」。" }
    : { state: "ok", title: "流式输出已关闭", text: "回复会经过插件处理" });
  if (!model.providers.length) {
    list.push({ state: "bad", title: "没有可用的 TTS 服务", text: model.provider_error || "请先在 AstrBot「服务提供商」里添加 TTS 服务（如 Genie TTS、GSVI、OpenAI TTS），再回到这里选择。" });
  } else if (s.tts_provider_id && !model.providers.some((p) => p.id === s.tts_provider_id)) {
    list.push({ state: "warn", title: "所选 TTS 服务不可用", text: `「${s.tts_provider_id}」不在 AstrBot 的 TTS 服务列表里，请在「会话与全局」重新选择。` });
  } else {
    list.push({ state: "ok", title: `TTS 服务：${model.resolved_provider}`, text: `${s.tts_provider_id ? "全局指定" : "自动选择第一个服务"} · 共 ${model.providers.length} 个可用` });
  }
  if (s.use_file_service && d.callback_api_base === false) {
    list.push({ state: "warn", title: "文件服务缺少 callback_api_base", text: "已开启「通过 AstrBot 文件服务发送语音」，但 AstrBot 没有设置 callback_api_base，会退回本地路径发送。" });
  }
  const langIssues = [];
  for (const lang of s.voice_languages) {
    if (!lang.provider_id) langIssues.push(`「${lang.name}」没有指定 TTS 服务`);
    else if (!model.providers.some((p) => p.id === lang.provider_id)) langIssues.push(`「${lang.name}」用的「${lang.provider_id}」不可用`);
  }
  for (const [name, profile] of Object.entries(s.profiles)) {
    if (profile.voice_language && !s.voice_languages.some((l) => l.code === profile.voice_language)) {
      langIssues.push(`档案「${name}」的语音语言「${profile.voice_language}」没有配置`);
    }
  }
  if (s.llm_provider_id && model.chat_providers && !model.chat_providers.some((p) => p.id === s.llm_provider_id)) {
    langIssues.push(`清洗与翻译用的模型「${s.llm_provider_id}」不可用`);
  }
  if (langIssues.length) {
    list.push({ state: "warn", title: "语言与翻译配置有问题", text: `${langIssues.join("；")}。请在「会话与全局」检查。` });
  } else if (s.voice_languages.length) {
    list.push({ state: "ok", title: `外语语音：${s.voice_languages.map((l) => l.name).join("、")}`, text: `翻译用${s.llm_provider_id ? `「${s.llm_provider_id}」` : "会话当前的对话模型"}，超时 ${s.llm_timeout} 秒` });
  }
  const moodIssues = [];
  for (const [name, profile] of Object.entries(s.profiles)) {
    for (const entry of profile.emotion_voices || []) {
      if (!entry.provider_id) moodIssues.push(`档案「${name}」的「${emotionName(entry.emotion)}」没有指定 TTS 服务`);
      else if (!model.providers.some((p) => p.id === entry.provider_id)) moodIssues.push(`档案「${name}」的「${emotionName(entry.emotion)}」用的「${entry.provider_id}」不可用，会改用默认声音`);
    }
  }
  if (moodIssues.length) list.push({ state: "warn", title: "情绪音色配置有问题", text: `${moodIssues.join("；")}。请在「发声档案」检查。` });
  return list;
}

function issueSummary(checks) {
  const bad = checks.filter((c) => c.state === "bad").length;
  const warn = checks.filter((c) => c.state === "warn").length;
  return { bad, warn, total: bad + warn };
}

// ---------- 渲染：外框 ----------
function renderChrome() {
  const m = app.model;
  const s = m.settings;
  const checks = healthChecks(m);
  const issues = issueSummary(checks);
  $("version").textContent = `v${m.version}`;
  $("master-switch").checked = s.enabled;
  $("master-sub").textContent = s.enabled ? `运行中 · ${s.default_profile}` : "已暂停";
  $("nav-alert").hidden = issues.total === 0;
  $("app").classList.toggle("is-busy", Boolean(m.runtime.busy || m.runtime.queue));

  const strip = $("issue-strip");
  if (app.panel !== "overview" && issues.total) {
    strip.hidden = false;
    strip.className = `issue-strip ${issues.bad ? "bad" : ""}`;
    strip.replaceChildren(icon("alert"), h("span", {}, `有 ${issues.total} 项运行检查需要处理`),
      h("button", { type: "button", onclick: () => showPanel("overview") }, "查看", icon("arrow")));
  } else {
    strip.hidden = true;
  }
}

// ---------- 渲染：总览 ----------
function renderOverview() {
  const m = app.model;
  const s = m.settings;
  const r = m.runtime;
  const c = r.counters;
  const checks = healthChecks(m);
  const issues = issueSummary(checks);
  const def = s.profiles[s.default_profile];

  const chip = $("hero-chip");
  const hero = $("hero");
  hero.classList.toggle("paused", !s.enabled || issues.bad > 0);
  if (issues.bad) {
    chip.className = "status-chip bad";
    chip.textContent = "需要处理";
    $("hero-title").textContent = `有 ${issues.bad} 项问题会让语音无法工作`;
  } else if (!s.enabled) {
    chip.className = "status-chip off";
    chip.textContent = "已暂停";
    $("hero-title").textContent = "语音回复已暂停";
  } else {
    chip.className = `status-chip ${issues.warn ? "warn" : ""}`;
    chip.textContent = issues.warn ? `运行中 · ${issues.warn} 项提醒` : "运行中";
    $("hero-title").textContent = r.busy ? "正在合成语音…" : "随时准备开口";
  }
  $("hero-text").textContent = `默认档案「${def.name}」· ${def.enabled ? `触发概率 ${pct(def.probability)}` : "档案已关闭"} · 被动上限 ${def.max_chars} 字 · ${MODE_LABELS[def.send_mode]}`;

  const sentTimes = r.events.filter((e) => e.status === "sent" && e.elapsed_ms != null).map((e) => e.elapsed_ms);
  const avg = sentTimes.length ? sentTimes.reduce((a, b) => a + b, 0) / sentTimes.length : null;
  $("k-decided").textContent = c.decided;
  $("k-decided-sub").textContent = `其中 ${c.speak} 条决定出声`;
  $("k-rate").textContent = c.decided ? pct(c.speak / c.decided) : "—";
  $("k-rate-sub").textContent = c.decided ? `已发送 ${c.sent} 条语音` : "还没有数据";
  $("k-latency").textContent = fmtMs(avg);
  $("k-latency-sub").textContent = sentTimes.length ? `最近 ${sentTimes.length} 条已发送语音` : "还没有数据";
  const expired = r.scheduler.expired;
  const timeout = r.scheduler.timeout;
  $("k-failed").textContent = c.failed;
  $("k-failed-sub").textContent = `超时 ${timeout} · 过期 ${expired}`;

  $("checks").replaceChildren(...checks.map((item) =>
    h("li", { class: `check ${item.state}` },
      h("span", { class: "check-icon" }, icon(item.state === "ok" ? "check" : item.state === "bad" ? "x" : "alert")),
      h("div", {}, h("strong", {}, item.title), h("p", {}, item.text)))));

  const stages = [
    ["message", "收到回复", c.decided],
    ["gate", "决定出声", c.speak],
    ["wave", "合成完成", r.scheduler.done],
    ["send", "已发送", c.sent],
  ];
  const pipeline = $("pipeline");
  pipeline.classList.toggle("flowing", Boolean(r.busy || r.queue));
  pipeline.replaceChildren(...stages.flatMap(([name, label, value], index) => {
    const node = h("div", { class: "stage" }, h("div", { class: "stage-node" }, icon(name)), h("strong", {}, value), h("span", {}, label));
    return index ? [h("div", { class: "link" }), node] : [node];
  }));
  $("queue-label").textContent = r.busy ? `正在合成 · 排队 ${r.queue}` : r.queue ? `排队 ${r.queue}` : "合成队列空闲";
  const mini = (label, value, bad) => h("span", { class: `mini ${bad && value ? "bad" : ""}` }, label, h("b", {}, value));
  $("pipe-foot").replaceChildren(
    mini("角色主动", c.tool), mini("翻译", c.translated), mini("缩写", c.shortened ?? 0),
    mini("LLM 失败", c.llm_failed, true), mini("合成出错", r.scheduler.failed, true),
    mini("合成超时", timeout, true), mini("排队过期", expired, true), mini("失败合计", c.failed, true));

  renderEvents();
}

function renderEvents() {
  const events = app.model.runtime.events.filter((e) => app.eventFilter === "all" || e.status === app.eventFilter);
  $("events").replaceChildren(...events.map((event) => {
    const status = STATUS[event.status] || STATUS.skipped;
    const meta = [
      h("span", { class: "num" }, timeOf(event.time)),
      sessionLabel(event.umo),
      h("span", {}, `档案 ${event.profile}`),
      h("span", { class: "why" }, event.reason),
    ];
    if (event.mode) meta.push(h("span", {}, MODE_LABELS[event.mode] || event.mode));
    if (event.elapsed_ms != null) meta.push(h("span", { class: "num" }, `耗时 ${fmtMs(event.elapsed_ms)}`));
    if (event.llm_ms != null) meta.push(h("span", { class: "num" }, `LLM ${fmtMs(event.llm_ms)}`));
    const chip = SOURCE_CHIPS[event.source];
    const tags = [
      chip ? h("span", { class: `tag-chip ${chip[1]}` }, chip[0]) : null,
      event.language ? h("span", { class: "tag-chip" }, languageLabel(event.language)) : null,
      emotionChip(event.emotion, event.emotion_source),
    ].filter(Boolean);
    const spokenDiffers = event.spoken && event.spoken !== event.text;
    return h("li", { class: `ev ${event.status}` },
      h("span", { class: "ev-icon" }, icon(status.icon)),
      h("div", {},
        h("p", { class: "ev-text" }, event.text ? `「${event.text}」` : h("em", {}, "（过滤后没有可朗读的内容）")),
        spokenDiffers ? h("p", { class: "ev-spoken" }, h("b", {}, "朗读"), event.spoken) : null,
        h("div", { class: "ev-meta" }, tags, meta),
        event.detail ? h("div", { class: `ev-detail ${event.status === "failed" ? "" : "soft"}` }, event.detail) : null),
      h("span", { class: `pill ${status.pill}` }, status.label));
  }));
  const total = app.model.runtime.events.length;
  $("events-empty").hidden = events.length > 0;
  $("events-empty-title").textContent = total ? "这个分类下暂时没有记录" : "还没有回复经过插件";
  $("events-empty-text").textContent = total ? "换个筛选条件看看。" : "先到「过滤与试听」看看一条回复会怎样被念出来。";
}

// ---------- 渲染：档案 ----------
const profileForm = () => $("profile-form");
let profileDirty;
let profileDict;
let emotionEditor;
const tagInputs = {};

function providerOptions(select, current, emptyLabel) {
  const providers = app.model?.providers || [];
  select.replaceChildren(h("option", { value: "" }, emptyLabel),
    ...providers.map((p) => h("option", { value: p.id }, p.type ? `${p.id}（${p.type}）` : p.id)));
  if (current && !providers.some((p) => p.id === current)) select.append(h("option", { value: current }, `${current}（当前不可用）`));
  select.value = current || "";
}

function profileOptions(select, current, { placeholder = "" } = {}) {
  const names = Object.keys(app.model.settings.profiles);
  select.replaceChildren(...(placeholder ? [h("option", { value: "" }, placeholder)] : []),
    ...names.map((name) => h("option", { value: name }, name)));
  select.value = names.includes(current) ? current : placeholder ? "" : app.model.settings.default_profile;
}

function usageCount(name) {
  return Object.values(app.model.settings.session_profiles).filter((p) => p === name).length;
}

function renderProfileList() {
  const s = app.model.settings;
  const names = Object.keys(s.profiles);
  $("profile-count").textContent = `${names.length} 个`;
  $("profile-list").replaceChildren(...names.map((name) => {
    const p = s.profiles[name];
    const uses = usageCount(name);
    return h("li", {},
      h("button", {
        type: "button",
        class: `pcard ${!app.creating && name === app.selected ? "active" : ""} ${p.enabled ? "" : "off"}`,
        onclick: () => requestSelect(name),
      },
      h("div", { class: "pcard-top" }, h("strong", {}, name),
        name === s.default_profile ? h("span", { class: "pill accent" }, "默认") : null,
        p.enabled ? null : h("span", { class: "pill" }, "已关闭")),
      h("div", { class: "pbar", title: `触发概率 ${pct(p.probability)}` }, h("span", { style: `width:${p.probability * 100}%` })),
      h("div", { class: "pcard-meta" },
        h("span", {}, "概率 ", h("b", {}, pct(p.probability))),
        h("span", {}, "上限 ", h("b", {}, p.max_chars), " 字"),
        h("span", {}, "冷却 ", h("b", {}, p.cooldown), " 秒"),
        h("span", {}, MODE_LABELS[p.send_mode]),
        p.voice_language ? h("span", {}, "语音 ", h("b", {}, languageLabel(p.voice_language))) : null,
        p.emotion_voices?.length ? h("span", {}, "情绪 ", h("b", {}, p.emotion_voices.length), " 种") : null,
        uses ? h("span", {}, h("b", {}, uses), " 个会话") : null)));
  }));
}

function readProfileForm() {
  const f = profileForm();
  return {
    name: f.elements.name.value.trim(),
    enabled: f.elements.enabled.checked,
    probability: Number(f.elements.probability.value),
    max_chars: Number(f.elements.max_chars.value),
    speak_max_chars: Number(f.elements.speak_max_chars.value),
    cooldown: Number(f.elements.cooldown.value),
    send_mode: f.elements.send_mode.value,
    group_force_both: f.elements.group_force_both.checked,
    allow_private: f.elements.allow_private.checked,
    allow_group: f.elements.allow_group.checked,
    filter_mode: f.elements.filter_mode.value,
    tts_provider_id: f.elements.tts_provider_id.value,
    force_phrases: tagInputs.force_phrases.get(),
    mute_phrases: tagInputs.mute_phrases.get(),
    emotion_boost: Number(f.elements.emotion_boost.value),
    emotion_markers: tagInputs.emotion_markers.get(),
    tool_enabled: f.elements.tool_enabled.checked,
    tool_interval: Number(f.elements.tool_interval.value),
    voice_language: f.elements.voice_language.value,
    voice_to_voice: f.elements.voice_to_voice.checked,
    pronunciations: profileDict.get(),
    emotion_voices: emotionEditor.get(),
  };
}

function updateProbability() {
  const range = profileForm().elements.probability;
  const value = Number(range.value);
  range.style.setProperty("--fill", `${value * 100}%`);
  $("prob-out").textContent = pct(value);
  $("prob-help").textContent = value <= 0
    ? "被动触发不会出声，只有强制出声短语生效"
    : value >= 1
      ? "每条符合条件的回复都会念出来"
      : `平均每 ${Math.round((1 / value) * 10) / 10} 条符合条件的回复念 1 条`;
}

function fillProfileForm(profile) {
  const f = profileForm();
  const s = app.model.settings;
  f.elements.name.value = profile.name;
  f.elements.enabled.checked = profile.enabled;
  f.elements.probability.value = profile.probability;
  f.elements.max_chars.value = profile.max_chars;
  f.elements.speak_max_chars.value = profile.speak_max_chars;
  f.elements.cooldown.value = profile.cooldown;
  f.elements.send_mode.value = profile.send_mode;
  f.elements.group_force_both.checked = profile.group_force_both;
  f.elements.allow_private.checked = profile.allow_private;
  f.elements.allow_group.checked = profile.allow_group;
  f.elements.filter_mode.value = profile.filter_mode;
  providerOptions(f.elements.tts_provider_id, profile.tts_provider_id, "跟随全局设置");
  tagInputs.force_phrases.set(profile.force_phrases);
  tagInputs.mute_phrases.set(profile.mute_phrases);
  tagInputs.emotion_markers.set(profile.emotion_markers);
  f.elements.emotion_boost.value = profile.emotion_boost;
  f.elements.tool_enabled.checked = profile.tool_enabled;
  f.elements.tool_interval.value = profile.tool_interval;
  languageOptions(f.elements.voice_language, profile.voice_language);
  f.elements.voice_to_voice.checked = profile.voice_to_voice;
  profileDict.set(profile.pronunciations);
  emotionEditor.set(profile.emotion_voices);
  updateProbability();

  const isDefault = !app.creating && profile.name === s.default_profile;
  $("profile-eyebrow").textContent = app.creating ? "新建档案" : isDefault ? "默认档案" : "编辑档案";
  $("profile-title").textContent = app.creating ? "未命名档案" : profile.name;
  $("set-default").hidden = app.creating || isDefault;
  $("delete-profile").hidden = app.creating || isDefault;
  $("try-profile").hidden = app.creating;
  profileDirty.reset();
}

function selectProfile(name) {
  app.creating = false;
  app.selected = name;
  fillProfileForm(app.model.settings.profiles[name]);
  renderProfileList();
}

function requestSelect(name) {
  if (!app.creating && name === app.selected) return;
  if (profileDirty.dirty && app.pendingSwitch !== name) {
    app.pendingSwitch = name;
    toast("当前档案有未保存的修改，再点一次将放弃修改", "error");
    setTimeout(() => { if (app.pendingSwitch === name) app.pendingSwitch = null; }, 4000);
    return;
  }
  app.pendingSwitch = null;
  selectProfile(name);
}

function renderProfiles({ force = false } = {}) {
  renderProfileList();
  if (!force && profileDirty.dirty) return;
  if (app.creating && !force) return;
  const profiles = app.model.settings.profiles;
  if (!app.selected || !profiles[app.selected]) app.selected = app.model.settings.default_profile;
  selectProfile(app.selected);
}

// ---------- 渲染：会话与全局 ----------
let globalDirty;
let mappingDirty;

function readGlobalForm() {
  const f = $("global-form");
  return {
    tts_provider_id: f.elements.tts_provider_id.value,
    default_profile: f.elements.default_profile.value,
    synth_timeout: Number(f.elements.synth_timeout.value),
    queue_expire: Number(f.elements.queue_expire.value),
    use_file_service: f.elements.use_file_service.checked,
    debug: f.elements.debug.checked,
  };
}

function renderGlobal({ force = false } = {}) {
  if (!force && globalDirty.dirty) return;
  const f = $("global-form");
  const s = app.model.settings;
  providerOptions(f.elements.tts_provider_id, s.tts_provider_id, "自动（第一个可用服务）");
  profileOptions(f.elements.default_profile, s.default_profile);
  f.elements.synth_timeout.value = s.synth_timeout;
  f.elements.queue_expire.value = s.queue_expire;
  f.elements.use_file_service.checked = s.use_file_service;
  f.elements.debug.checked = s.debug;
  globalDirty.reset();
}

let langDirty;
let dictDirty;
let globalDict;

function chatProviderOptions(select, current) {
  const providers = app.model?.chat_providers || [];
  select.replaceChildren(h("option", { value: "" }, "会话当前的对话模型"),
    ...providers.map((p) => h("option", { value: p.id }, p.type ? `${p.id}（${p.type}）` : p.id)));
  if (current && !providers.some((p) => p.id === current)) select.append(h("option", { value: current }, `${current}（当前不可用）`));
  select.value = current || "";
}

function langRow(lang = { code: "", name: "", provider_id: "" }) {
  const select = h("select", { "aria-label": "TTS 服务" });
  providerOptions(select, lang.provider_id, "选择这个语言用的 TTS 服务");
  const row = h("div", { class: "lang-row" },
    h("input", { value: lang.code, maxlength: "16", placeholder: "代码，如 ja", "aria-label": "语言代码" }),
    h("input", { value: lang.name, maxlength: "16", placeholder: "显示名，如 日语", "aria-label": "显示名" }),
    select,
    h("button", {
      type: "button", class: "icon-ghost", title: "删除语言", "aria-label": "删除语言",
      onclick: () => { row.remove(); langEmptyState(); langDirty.update(); },
    }, icon("trash")));
  return row;
}

function langEmptyState() {
  const list = $("lang-list");
  list.querySelector(".empty")?.remove();
  list.querySelector(".lang-head")?.remove();
  if (list.querySelector(".lang-row")) {
    list.prepend(h("div", { class: "lang-head" }, h("span", {}, "代码"), h("span", {}, "显示名"), h("span", {}, "TTS 服务"), h("span")));
  } else {
    list.append(h("div", { class: "empty" }, h("p", {}, "还没有配置外语"),
      h("small", {}, "例如在 AstrBot 里另建一个语言设为日语的 Genie TTS 条目，再点「添加语言」把它对应成 ja / 日语。")));
  }
}

function readLangForm() {
  const f = $("lang-form");
  return {
    voice_languages: $$(".lang-row", $("lang-list")).map((row) => {
      const [code, name] = row.querySelectorAll("input");
      return { code: code.value.trim(), name: name.value.trim(), provider_id: row.querySelector("select").value };
    }),
    llm_provider_id: f.elements.llm_provider_id.value,
    llm_timeout: Number(f.elements.llm_timeout.value),
    translate_fail: f.elements.translate_fail.value,
  };
}

function renderLangForm({ force = false } = {}) {
  if (!force && langDirty.dirty) return;
  const s = app.model.settings;
  $("lang-list").replaceChildren(...s.voice_languages.map((lang) => langRow(lang)));
  langEmptyState();
  const f = $("lang-form");
  chatProviderOptions(f.elements.llm_provider_id, s.llm_provider_id);
  f.elements.llm_timeout.value = s.llm_timeout;
  f.elements.translate_fail.value = s.translate_fail;
  langDirty.reset();
}

function renderGlobalDict({ force = false } = {}) {
  if (!force && dictDirty.dirty) return;
  globalDict.set(app.model.settings.pronunciations);
  dictDirty.reset();
}

function umoPreview(container, value) {
  const parsed = parseUmo(value.trim());
  container.replaceChildren(...(parsed
    ? [h("span", { class: "pill" }, parsed.platform), h("span", { class: "pill" }, parsed.kind === "群" ? "群聊" : parsed.kind === "私" ? "私聊" : parsed.kind), h("span", { class: "num" }, parsed.id)]
    : []));
}

function mappingRow(umo = "", profile = "") {
  const select = h("select", { "aria-label": "档案", onchange: () => mappingDirty.update() });
  profileOptions(select, profile, { placeholder: "选择档案" });
  const preview = h("div", { class: "umo-preview" });
  const input = h("input", {
    value: umo, placeholder: "会话 ID，例如 aiocqhttp:GroupMessage:123456", "aria-label": "会话 ID",
    oninput: () => { umoPreview(preview, input.value); mappingDirty.update(); },
  });
  umoPreview(preview, umo);
  const row = h("div", { class: "map-row" }, input, select,
    h("button", { type: "button", class: "icon-ghost", title: "移除", "aria-label": "移除", onclick: () => { row.remove(); mappingEmptyState(); mappingDirty.update(); } }, icon("trash")),
    preview);
  return row;
}

function readMappings() {
  return $$(".map-row", $("mapping-list")).map((row) => ({
    umo: row.querySelector("input").value.trim(),
    profile: row.querySelector("select").value,
  }));
}

function mappingEmptyState() {
  const list = $("mapping-list");
  const hasRows = list.querySelector(".map-row");
  list.querySelector(".empty")?.remove();
  if (!hasRows) {
    list.append(h("div", { class: "empty" }, h("p", {}, "所有会话都在使用默认档案"), h("small", {}, "点「添加会话」给某个群或私聊单独指定档案。")));
  }
}

function renderMappings({ force = false } = {}) {
  if (!force && mappingDirty.dirty) return;
  const entries = Object.entries(app.model.settings.session_profiles);
  $("mapping-list").replaceChildren(...entries.map(([umo, profile]) => mappingRow(umo, profile)));
  mappingEmptyState();
  mappingDirty.reset();
}

function renderMuted() {
  const muted = app.model.settings.muted_sessions;
  const list = $("muted-list");
  if (!muted.length) {
    list.replaceChildren(h("div", { class: "empty", style: "width:100%" }, h("p", {}, "没有静音的会话")));
    return;
  }
  list.replaceChildren(...muted.map((umo) =>
    h("span", { class: "muted-chip", title: umo }, icon("mute"), h("code", {}, umo),
      h("button", { type: "button", class: "btn ghost small", onclick: (e) => unmute(umo, e.currentTarget) }, "恢复语音"))));
}

// ---------- 渲染：试听 ----------
function renderLabOptions() {
  const lab = $("lab-form");
  profileOptions(lab.elements.profile, lab.elements.profile.value || app.model.settings.default_profile);
  const profile = app.model.settings.profiles[lab.elements.profile.value];
  const current = lab.elements.language.value || "@profile";
  languageOptions(lab.elements.language, current, { follow: `跟随档案（${languageLabel(profile?.voice_language || "")}）` });
  const mood = lab.elements.emotion;
  const picked = mood.value;
  mood.replaceChildren(h("option", { value: "" }, "自动判断"),
    ...emotions().map((e) => h("option", { value: e.code }, e.name)),
    h("option", { value: "neutral" }, "平静（默认声音）"));
  mood.value = picked;
  mood.disabled = !profile?.emotion_voices?.length;
  mood.title = mood.disabled ? "这个档案没有配置情绪音色" : "";
}

function diffChars(source, target) {
  const a = Array.from(source);
  const b = Array.from(target);
  const n = a.length;
  const m = b.length;
  if (n * m > 400000) return null;
  const w = m + 1;
  const dp = new Uint16Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * w + j] = a[i] === b[j] ? dp[(i + 1) * w + j + 1] + 1 : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
    }
  }
  // 同样长度的公共子序列可能有多种对齐方式。分别「尽早匹配」和「能删先删」回溯一次，
  // 取分段更少的那个，让被剥掉的动作描写、括号等尽量显示成连续的一整段。
  const trace = (preferDelete) => {
    const out = [];
    const push = (type, ch) => {
      const last = out[out.length - 1];
      if (last && last.type === type) last.text += ch;
      else out.push({ type, text: ch });
    };
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      const here = dp[i * w + j];
      if (a[i] === b[j] && !(preferDelete && dp[(i + 1) * w + j] === here)) { push("keep", a[i]); i++; j++; }
      else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) { push("del", a[i]); i++; }
      else { push("add", b[j]); j++; }
    }
    while (i < n) push("del", a[i++]);
    while (j < m) push("add", b[j++]);
    return out;
  };
  const eager = trace(false);
  const lazy = trace(true);
  return lazy.length < eager.length ? lazy : eager;
}

function renderPreview(source, preview) {
  const d = preview.decision;
  $("lab-placeholder").hidden = true;
  $("verdict").hidden = false;
  const verdict = $("verdict");
  const chance = d.trace.some((t) => t.state === "chance");
  verdict.className = `verdict ${d.speak ? (chance ? "chance" : "ok") : ""}`;
  $("verdict-icon").replaceChildren(icon(d.speak ? (chance ? "dice" : "volume") : "mute"));
  $("verdict-title").textContent = d.speak ? (d.forced ? "会出声 · 强制" : "会进入概率触发") : "不会出声";
  $("verdict-reason").textContent = d.reason;
  $("verdict-mode").textContent = d.speak ? MODE_LABELS[preview.send_mode] : `档案 ${preview.profile}`;

  $("filter-card").hidden = false;
  const diff = $("diff");
  const parts = diffChars(source.replace(/\r\n?/g, "\n"), preview.filtered);
  if (parts) {
    diff.replaceChildren(...parts.map((part) =>
      part.type === "keep" ? document.createTextNode(part.text) : h(part.type === "del" ? "del" : "ins", {}, part.text)));
  } else {
    diff.replaceChildren(document.createTextNode(source), h("span", { class: "note" }, "文本较长，未显示逐字对比"));
  }
  const spokenBox = $("spoken-box");
  spokenBox.classList.toggle("empty-speech", !preview.filtered);
  $("spoken").textContent = preview.filtered || "（过滤后没有可朗读的内容，不会合成）";
  $("spoken-label").textContent = "正则过滤后";
  const note = $("spoken-note");
  note.className = "spoken-note";
  note.hidden = !(preview.filtered && preview.uses_llm);
  const steps = [];
  if (preview.will_shorten) steps.push(`另写一段 ${preview.speak_max_chars} 字以内的短版`);
  if (preview.language) {
    const budget = !preview.will_shorten && preview.speak_max_chars ? `（译文不超过约 ${preview.speak_max_chars} 字，超出时截到句末）` : "";
    steps.push(`翻译成${preview.language_name}${budget}`);
  }
  if (!steps.length) steps.push("口语化清洗");
  note.textContent = `合成时会先经 LLM ${steps.join("并")}，再按发音词典处理，点「合成试听」查看实际朗读。`
    + (preview.will_shorten ? `过滤后 ${preview.chars} 字，超过朗读上限，文字照发全文。` : "");
  const ratio = preview.max_chars ? preview.chars / preview.max_chars : 0;
  const over = preview.chars > preview.max_chars;
  $("meter").classList.toggle("over", over);
  $("meter-fill").style.width = `${Math.min(100, ratio * 100)}%`;
  $("meter-label").textContent = `${preview.chars} / ${preview.max_chars} 字${over ? " · 超过被动上限" : ""}`;
  renderMoodLine(preview);

  $("trace-card").hidden = false;
  $("trace").replaceChildren(...d.trace.map((step) => {
    const meta = TRACE_STATES[step.state] || TRACE_STATES.pass;
    return h("li", { class: `st-${step.state}` },
      h("span", { class: "t-icon", title: meta.text }, icon(meta.icon)),
      h("span", { class: "t-label" }, step.label),
      h("span", { class: "t-note" }, step.note || meta.text));
  }));
}

// 情绪与将要使用的服务。档案没配情绪音色时不显示
function renderMoodLine(preview) {
  const line = $("mood-line");
  line.hidden = !preview.emotion_voices || !preview.filtered;
  if (line.hidden) return;
  const voice = preview.voice_provider ? h("span", {}, "用 ", h("b", {}, preview.voice_provider)) : null;
  let parts;
  if (preview.language) {
    parts = [h("span", {}, `念${preview.language_name}时不切换情绪音色`), voice];
  } else if (preview.emotion) {
    const from = preview.emotion_source === "marker" ? `符号「${preview.emotion_marker}」` : EMOTION_SOURCES[preview.emotion_source] || "";
    parts = [emotionChip(preview.emotion, ""), h("span", { class: "muted" }, from), voice,
      preview.llm_emotion ? h("span", { class: "muted" }, "· 合成时由 LLM 重新判断") : null];
  } else {
    parts = [h("span", {}, preview.llm_emotion ? "合成时由 LLM 判断情绪" : "没有判断出情绪，用默认声音"), voice];
  }
  line.replaceChildren(icon("spark"), ...parts.filter(Boolean));
}

// ---------- 播放器 ----------
const player = { peaks: null, raf: 0, ctx: null };

function base64ToBytes(dataUrl) {
  const binary = atob(dataUrl.slice(dataUrl.indexOf(",") + 1));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function decodePeaks(dataUrl, count) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return null;
  player.ctx ||= new AudioCtx();
  const buffer = await player.ctx.decodeAudioData(base64ToBytes(dataUrl).buffer);
  const data = buffer.getChannelData(0);
  const size = Math.max(1, Math.floor(data.length / count));
  const peaks = [];
  for (let i = 0; i < count; i++) {
    let max = 0;
    for (let j = i * size; j < Math.min(data.length, (i + 1) * size); j++) max = Math.max(max, Math.abs(data[j]));
    peaks.push(max);
  }
  const top = Math.max(...peaks, 0.001);
  return peaks.map((p) => p / top);
}

function barCount() {
  return Math.max(24, Math.floor($("wave-wrap").clientWidth / 5));
}

function drawWave() {
  const canvas = $("wave");
  const wrap = $("wave-wrap");
  const dpr = window.devicePixelRatio || 1;
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  if (!width) return;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const styles = getComputedStyle(document.documentElement);
  const played = styles.getPropertyValue("--primary").trim() || "#6a4fe0";
  const rest = styles.getPropertyValue("--border-strong").trim() || "#ccc";
  const audio = $("audio");
  const progress = audio.duration ? audio.currentTime / audio.duration : 0;
  const peaks = player.peaks || Array.from({ length: barCount() }, (_, i) => 0.25 + 0.2 * Math.abs(Math.sin(i * 0.7)));
  const step = width / peaks.length;
  const barWidth = Math.max(2, step * 0.6);
  peaks.forEach((peak, index) => {
    const barHeight = Math.max(3, peak * (height - 6));
    const x = index * step + (step - barWidth) / 2;
    ctx.fillStyle = index / peaks.length < progress ? played : rest;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, (height - barHeight) / 2, barWidth, barHeight, barWidth / 2);
    else ctx.rect(x, (height - barHeight) / 2, barWidth, barHeight);
    ctx.fill();
  });
}

function fmtTime(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function updatePlayer() {
  const audio = $("audio");
  $("play-time").textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
  $("play-btn").replaceChildren(icon(audio.paused ? "play" : "pause"));
  $("play-btn").setAttribute("aria-label", audio.paused ? "播放" : "暂停");
  drawWave();
}

function tick() {
  updatePlayer();
  if (!$("audio").paused) player.raf = requestAnimationFrame(tick);
}

async function loadAudio(result) {
  const audio = $("audio");
  audio.pause();
  $("player-card").hidden = false;
  audio.src = result.audio;
  $("download").href = result.audio;
  // replaceChildren 会把 null 变成文字「null」，先滤掉
  $("player-meta").replaceChildren(...[
    h("span", { class: "mini" }, "服务 ", h("b", {}, result.provider_id)),
    h("span", { class: "mini" }, "语言 ", h("b", {}, result.language_name || "和文字相同")),
    result.emotion ? h("span", { class: "mini" }, "情绪 ", h("b", {}, result.emotion_name), EMOTION_SOURCES[result.emotion_source] ? `（${EMOTION_SOURCES[result.emotion_source]}）` : "") : null,
    h("span", { class: "mini" }, "总耗时 ", h("b", {}, fmtMs(result.elapsed_ms))),
    result.llm_ms != null ? h("span", { class: "mini" }, "其中 LLM ", h("b", {}, fmtMs(result.llm_ms))) : null,
    h("span", { class: "mini" }, "大小 ", h("b", {}, `${Math.max(1, Math.round(result.bytes / 1024))} KB`)),
  ].filter(Boolean));
  // 实际朗读：经过 LLM 清洗 / 翻译和发音词典之后交给 TTS 的文字
  $("spoken").textContent = result.text;
  $("spoken-label").textContent = `实际朗读 · ${result.language_name || "和文字相同"}`;
  const note = $("spoken-note");
  note.hidden = !result.note && !result.llm_used;
  // 缩写成功之类的说明不算警告，只有失败、不可用才标成警告色
  note.className = `spoken-note ${/失败|不可用/.test(result.note || "") ? "warn" : ""}`;
  note.textContent = result.note || (result.language ? "已由 LLM 翻译" : "已由 LLM 口语化清洗");
  player.peaks = null;
  drawWave();
  try {
    player.peaks = await decodePeaks(result.audio, barCount());
  } catch {
    player.peaks = null;
  }
  updatePlayer();
  audio.play().catch(() => updatePlayer());
}

// ---------- 总渲染 ----------
function applyState(model) {
  app.model = model;
  const dot = $("conn-dot");
  dot.className = "dot ok";
  $("conn-label").textContent = app.bridge ? "已连接 AstrBot" : "开发预览";
}

function renderAll(options = {}) {
  renderChrome();
  renderOverview();
  renderProfiles(options);
  renderGlobal(options);
  renderLangForm(options);
  renderGlobalDict(options);
  renderMappings(options);
  renderMuted();
  renderLabOptions();
}

async function reload({ full = true } = {}) {
  applyState(await api("state"));
  if (full) renderAll();
  else {
    renderChrome();
    renderOverview();
  }
}

function showPanel(name) {
  app.panel = name;
  for (const section of $$("section[data-panel]")) section.hidden = section.dataset.panel !== name;
  for (const button of $$(".nav-item")) button.classList.toggle("active", button.dataset.panel === name);
  const [title, sub] = PANELS[name];
  $("panel-title").textContent = title;
  $("panel-sub").textContent = sub;
  if (app.model) renderChrome();
  if (name === "lab" && !$("player-card").hidden) requestAnimationFrame(drawWave);
  window.scrollTo({ top: 0 });
}

// ---------- 动作 ----------
async function saveSettings(patch, message, button) {
  return run(button, async () => {
    applyState(await api("settings", "POST", patch));
    renderAll({ force: false });
    if (message) toast(message);
    return true;
  });
}

async function unmute(umo, button) {
  await saveSettings({ muted_sessions: app.model.settings.muted_sessions.filter((u) => u !== umo) }, "已恢复该会话的语音", button);
}

function labBody() {
  const f = $("lab-form");
  return {
    text: f.elements.text.value,
    user_text: f.elements.user_text.value,
    profile: f.elements.profile.value,
    is_group: f.elements.scene.value === "group",
    language: f.elements.language.value || "",
    emotion: f.elements.emotion.disabled ? "" : f.elements.emotion.value,
    simulate_tool: f.elements.simulate_tool.checked,
    simulate_voice: f.elements.simulate_voice.checked,
  };
}

async function previewLab(button) {
  const body = labBody();
  if (!body.text.trim()) {
    toast("先输入一条角色回复", "error");
    return null;
  }
  return run(button, async () => {
    const preview = await api("preview", "POST", body);
    renderPreview(body.text, preview);
    return preview;
  });
}

// ---------- 事件绑定 ----------
function bind() {
  for (const button of $$(".nav-item")) button.addEventListener("click", () => showPanel(button.dataset.panel));
  for (const button of $$("[data-goto]")) button.addEventListener("click", () => showPanel(button.dataset.goto));
  $("refresh").addEventListener("click", (e) => run(e.currentTarget, async () => {
    await reload();
    toast("已刷新");
  }));
  $("master-switch").addEventListener("change", async (e) => {
    const enabled = e.currentTarget.checked;
    const ok = await saveSettings({ enabled }, enabled ? "语音回复已开启" : "语音回复已暂停");
    if (!ok) e.currentTarget.checked = !enabled;
  });
  $("event-filter").addEventListener("click", (e) => {
    const button = e.target.closest("button[data-filter]");
    if (!button) return;
    app.eventFilter = button.dataset.filter;
    for (const b of $$("button", $("event-filter"))) b.classList.toggle("active", b === button);
    if (app.model) renderEvents();
  });

  // 档案
  const f = profileForm();
  profileDirty = new Dirty($("profile-savebar"), readProfileForm);
  for (const container of $$("[data-tags]", f)) tagInputs[container.dataset.tags] = new TagInput(container, () => profileDirty.update());
  profileDict = new DictEditor(f.querySelector('[data-dict="profile"]'), () => profileDirty.update());
  emotionEditor = new EmotionEditor($("emotion-editor"), () => profileDirty.update());
  f.addEventListener("input", () => profileDirty.update());
  f.addEventListener("change", () => profileDirty.update());
  f.elements.probability.addEventListener("input", updateProbability);
  f.elements.name.addEventListener("input", () => {
    if (app.creating) $("profile-title").textContent = f.elements.name.value.trim() || "未命名档案";
  });
  $("new-profile").addEventListener("click", () => {
    if (profileDirty.dirty && app.pendingSwitch !== "__new__") {
      app.pendingSwitch = "__new__";
      toast("当前档案有未保存的修改，再点一次将放弃修改", "error");
      return;
    }
    app.pendingSwitch = null;
    app.creating = true;
    const base = app.model.settings.profiles[app.model.settings.default_profile];
    fillProfileForm({ ...base, name: "" });
    renderProfileList();
    f.elements.name.focus();
  });
  $("profile-reset").addEventListener("click", () => {
    if (app.creating) fillProfileForm({ ...app.model.settings.profiles[app.model.settings.default_profile], name: "" });
    else selectProfile(app.selected);
  });
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    const profile = readProfileForm();
    if (!profile.name) {
      toast("请填写档案名", "error");
      f.elements.name.focus();
      return;
    }
    const moods = profile.emotion_voices.map((entry) => entry.emotion);
    const repeated = moods.find((mood, index) => moods.indexOf(mood) !== index);
    if (repeated) {
      toast(`情绪「${emotionName(repeated)}」重复了，每种情绪只能对应一个服务`, "error");
      return;
    }
    run(e.submitter, async () => {
      applyState(await api("profiles/save", "POST", { original: app.creating ? null : app.selected, profile }));
      app.creating = false;
      app.selected = profile.name;
      renderAll();
      renderProfiles({ force: true });
      toast(`档案「${profile.name}」已保存`);
    });
  });
  $("set-default").addEventListener("click", (e) => saveSettings({ default_profile: app.selected }, `「${app.selected}」已设为默认档案`, e.currentTarget)
    .then(() => renderProfiles({ force: !profileDirty.dirty })));
  $("delete-profile").addEventListener("click", (e) => {
    const button = e.currentTarget;
    armButton(button, "确认删除？", () => {
      const name = app.selected;
      run(button, async () => {
        applyState(await api("profiles/delete", "POST", { name }));
        app.selected = app.model.settings.default_profile;
        renderAll();
        renderProfiles({ force: true });
        toast(`档案「${name}」已删除`);
      });
    });
  });
  $("try-profile").addEventListener("click", () => {
    $("lab-form").elements.profile.value = app.selected;
    showPanel("lab");
    $("lab-form").elements.text.focus();
  });

  // 全局
  const g = $("global-form");
  globalDirty = new Dirty($("global-savebar"), readGlobalForm);
  g.addEventListener("input", () => globalDirty.update());
  g.addEventListener("change", () => globalDirty.update());
  $("global-reset").addEventListener("click", () => renderGlobal({ force: true }));
  g.addEventListener("submit", (e) => {
    e.preventDefault();
    run(e.submitter, async () => {
      applyState(await api("settings", "POST", readGlobalForm()));
      renderAll();
      renderGlobal({ force: true });
      toast("全局设置已保存");
    });
  });

  // 语言与翻译
  const lf = $("lang-form");
  langDirty = new Dirty($("lang-savebar"), readLangForm);
  lf.addEventListener("input", () => langDirty.update());
  lf.addEventListener("change", () => langDirty.update());
  $("add-language").addEventListener("click", () => {
    const row = langRow();
    $("lang-list").append(row);
    langEmptyState();
    langDirty.update();
    row.querySelector("input").focus();
  });
  $("lang-reset").addEventListener("click", () => renderLangForm({ force: true }));
  lf.addEventListener("submit", (e) => {
    e.preventDefault();
    const data = readLangForm();
    const seen = new Set();
    for (const lang of data.voice_languages) {
      if (!LANG_CODE.test(lang.code)) {
        toast(`语言代码「${lang.code || "（空）"}」不合法：以字母开头，只用字母、数字、下划线或短横线`, "error");
        return;
      }
      if (seen.has(lang.code)) {
        toast(`语言代码「${lang.code}」重复了`, "error");
        return;
      }
      seen.add(lang.code);
    }
    run(e.submitter, async () => {
      applyState(await api("settings", "POST", data));
      renderAll();
      renderLangForm({ force: true });
      toast("语言与翻译已保存");
    });
  });

  // 全局发音词典
  const df = $("dict-form");
  dictDirty = new Dirty($("dict-savebar"), () => globalDict.get());
  globalDict = new DictEditor(df.querySelector('[data-dict="global"]'), () => dictDirty.update());
  df.addEventListener("input", () => dictDirty.update());
  df.addEventListener("change", () => dictDirty.update());
  $("dict-reset").addEventListener("click", () => renderGlobalDict({ force: true }));
  df.addEventListener("submit", (e) => {
    e.preventDefault();
    run(e.submitter, async () => {
      applyState(await api("settings", "POST", { pronunciations: globalDict.get() }));
      renderAll();
      renderGlobalDict({ force: true });
      toast("发音词典已保存");
    });
  });

  mappingDirty = new Dirty($("mapping-savebar"), readMappings);
  $("add-mapping").addEventListener("click", () => {
    const row = mappingRow();
    $("mapping-list").append(row);
    mappingEmptyState();
    mappingDirty.update();
    row.querySelector("input").focus();
  });
  $("mapping-reset").addEventListener("click", () => renderMappings({ force: true }));
  $("save-mapping").addEventListener("click", (e) => {
    const rows = readMappings().filter((row) => row.umo || row.profile);
    if (rows.some((row) => !row.umo || !row.profile)) {
      toast("有会话没有填完整：会话 ID 和档案都要填", "error");
      return;
    }
    const seen = new Set();
    for (const row of rows) {
      if (seen.has(row.umo)) {
        toast(`会话「${row.umo}」重复了`, "error");
        return;
      }
      seen.add(row.umo);
    }
    run(e.currentTarget, async () => {
      applyState(await api("settings", "POST", { session_profiles: rows }));
      renderAll();
      renderMappings({ force: true });
      toast("会话档案已保存");
    });
  });

  // 试听
  const lab = $("lab-form");
  $("samples").replaceChildren(...SAMPLES.map(([label, text]) =>
    h("button", { type: "button", class: "sample", onclick: () => { lab.elements.text.value = text; previewLab(); } }, label)));
  lab.addEventListener("submit", (e) => {
    e.preventDefault();
    previewLab(e.submitter);
  });
  lab.elements.profile.addEventListener("change", () => { if (app.model) renderLabOptions(); });
  lab.elements.text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      previewLab();
    }
  });
  $("synth-btn").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    const body = labBody();
    if (!body.text.trim()) {
      toast("先输入一条角色回复", "error");
      return;
    }
    $("app").classList.add("is-busy");
    await run(button, async () => {
      renderPreview(body.text, await api("preview", "POST", body));
      await loadAudio(await api("synthesize", "POST", { text: body.text, profile: body.profile, language: body.language, emotion: body.emotion, filter: true }));
    });
    if (app.model) renderChrome();
  });

  const audio = $("audio");
  $("play-btn").addEventListener("click", () => {
    if (audio.paused) audio.play().catch((error) => toast(`无法播放：${error.message}`, "error"));
    else audio.pause();
  });
  audio.addEventListener("play", () => { cancelAnimationFrame(player.raf); tick(); });
  audio.addEventListener("pause", updatePlayer);
  audio.addEventListener("ended", updatePlayer);
  audio.addEventListener("loadedmetadata", updatePlayer);
  $("wave-wrap").addEventListener("click", (e) => {
    if (!audio.duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    audio.currentTime = ((e.clientX - rect.left) / rect.width) * audio.duration;
    updatePlayer();
  });
  window.addEventListener("resize", () => { if (!$("player-card").hidden) drawWave(); });

  window.addEventListener("beforeunload", (e) => {
    if ([profileDirty, globalDirty, langDirty, dictDirty, mappingDirty].some((d) => d.dirty)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

function heroWave() {
  const heights = [18, 30, 46, 62, 40, 74, 56, 84, 50, 68, 36, 58, 78, 44, 30, 52, 66, 38, 24, 16];
  $("hero-wave").replaceChildren(...heights.map((height, index) =>
    h("span", { style: `--h:${height}px;--d:${-(index * 0.17).toFixed(2)}s` })));
}

function applyTheme(context) {
  if (context?.isDark != null) {
    document.documentElement.dataset.theme = context.isDark ? "dark" : "light";
    if (!$("player-card").hidden) requestAnimationFrame(drawWave);
  }
}

async function boot() {
  heroWave();
  bind();
  app.bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
  try {
    if (app.bridge) {
      applyTheme(await app.bridge.ready());
      app.bridge.onContext?.(applyTheme);
    }
    await reload();
  } catch (error) {
    $("conn-dot").className = "dot bad";
    $("conn-label").textContent = "连接失败";
    toast(app.bridge ? `连接失败：${error.message}` : "请在 AstrBot 控制台的插件页面中打开本页", "error");
  }
}

await boot();
setInterval(async () => {
  if (!app.model || app.inflight || document.hidden || app.panel !== "overview") return;
  try {
    await reload({ full: false });
  } catch {
    $("conn-dot").className = "dot bad";
    $("conn-label").textContent = "连接中断";
  }
}, 4000);
