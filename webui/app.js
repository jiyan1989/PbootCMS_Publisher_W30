/* PbootCMS 发文助手 W —— 前端（Chrome 多标签，多站点同时登录互不影响） */
"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

// A WebView File can carry a useful ``File.type`` even when its name has no
// extension and the bytes have no short magic signature.  Native picker/drop
// bridges materialize such files as local paths, so retain the non-secret MIME
// hint in memory and include only hints for paths present in a draft snapshot.
const FILE_MIME_HINTS = new Map();
function fileMimeKey(path) {
  return String(path || "").trim().replace(/[\\/]+/g, "\\").toLowerCase();
}
function rememberFileMime(path, mime) {
  const key = fileMimeKey(path);
  const value = String(mime || "").split(";", 1)[0].trim().toLowerCase();
  if (!key || !/^[a-z0-9][a-z0-9!#$&^_.+\-]*\/[a-z0-9][a-z0-9!#$&^_.+\-]*$/.test(value)) return;
  FILE_MIME_HINTS.set(key, value);
}
function rememberFileMimeHints(paths, types) {
  (Array.isArray(paths) ? paths : []).forEach((path, index) => {
    const hint = Array.isArray(types) ? types[index] : "";
    rememberFileMime(path, hint && typeof hint === "object" ? (hint.type || hint.mime) : hint);
  });
}
function collectFileMimeHints(value, output) {
  if (Array.isArray(value)) { value.forEach(item => collectFileMimeHints(item, output)); return; }
  if (!value || typeof value !== "object") {
    if (typeof value === "string") {
      const mime = FILE_MIME_HINTS.get(fileMimeKey(value));
      if (mime) output[value] = mime;
    }
    return;
  }
  Object.values(value).forEach(item => collectFileMimeHints(item, output));
}
function attachFileMimeHints(payload) {
  const output = payload && typeof payload === "object" ? payload : {};
  const hints = {};
  collectFileMimeHints(output, hints);
  output.asset_mimes = hints;
  return output;
}
function restoreFileMimeHints(draft) {
  const hints = draft && typeof draft.asset_mimes === "object" ? draft.asset_mimes : {};
  Object.entries(hints).forEach(([path, mime]) => rememberFileMime(path, mime));
}

function snapshotFileMimeHints(value) {
  const hints = {};
  collectFileMimeHints(value, hints);
  return hints;
}

// Capture the current WebView inputs used by the browser responsive-image
// algorithm. The Python side validates these values and remains conservative
// when a context is unavailable.
function responsiveContext() {
  const width = Number(window.innerWidth || document.documentElement?.clientWidth || 0);
  const height = Number(window.innerHeight || document.documentElement?.clientHeight || 0);
  const dpr = Number(window.devicePixelRatio || 1);
  const result = {
    viewport_width: Number.isFinite(width) && width > 0 ? Math.round(width) : 0,
    viewport_height: Number.isFinite(height) && height > 0 ? Math.round(height) : 0,
    device_pixel_ratio: Number.isFinite(dpr) && dpr > 0 ? dpr : 1,
  };
  // Static CSS media features that affect <picture>/<source> selection.  A
  // query is only copied when the embedded browser explicitly matches it;
  // unsupported features stay absent so the Python side fails closed instead
  // of inventing a preference.
  const matches = (query) => {
    try { return typeof window.matchMedia === "function" && window.matchMedia(query).matches; }
    catch (_) { return false; }
  };
  const firstMatch = (items) => {
    for (const [query, value] of items) if (matches(query)) return value;
    return "";
  };
  const colorScheme = firstMatch([
    ["(prefers-color-scheme: dark)", "dark"],
    ["(prefers-color-scheme: light)", "light"],
    ["(prefers-color-scheme: no-preference)", "no-preference"],
  ]);
  if (colorScheme) result.prefers_color_scheme = colorScheme;
  const reducedMotion = firstMatch([
    ["(prefers-reduced-motion: reduce)", "reduce"],
    ["(prefers-reduced-motion: no-preference)", "no-preference"],
  ]);
  if (reducedMotion) result.prefers_reduced_motion = reducedMotion;
  const contrast = firstMatch([
    ["(prefers-contrast: more)", "more"],
    ["(prefers-contrast: less)", "less"],
    ["(prefers-contrast: custom)", "custom"],
    ["(prefers-contrast: no-preference)", "no-preference"],
  ]);
  if (contrast) result.prefers_contrast = contrast;
  const forcedColors = firstMatch([
    ["(forced-colors: active)", "active"],
    ["(forced-colors: none)", "none"],
  ]);
  if (forcedColors) result.forced_colors = forcedColors;
  const invertedColors = firstMatch([
    ["(inverted-colors: inverted)", "inverted"],
    ["(inverted-colors: none)", "none"],
  ]);
  if (invertedColors) result.inverted_colors = invertedColors;
  const dynamicRange = firstMatch([
    ["(dynamic-range: high)", "high"],
    ["(dynamic-range: standard)", "standard"],
  ]);
  if (dynamicRange) result.dynamic_range = dynamicRange;
  const videoDynamicRange = firstMatch([
    ["(video-dynamic-range: high)", "high"],
    ["(video-dynamic-range: standard)", "standard"],
  ]);
  if (videoDynamicRange) result.video_dynamic_range = videoDynamicRange;
  const hover = firstMatch([["(hover: hover)", "hover"], ["(hover: none)", "none"]]);
  if (hover) result.hover = hover;
  const anyHover = firstMatch([["(any-hover: hover)", "hover"], ["(any-hover: none)", "none"]]);
  if (anyHover) result.any_hover = anyHover;
  const pointer = firstMatch([
    ["(pointer: fine)", "fine"], ["(pointer: coarse)", "coarse"], ["(pointer: none)", "none"],
  ]);
  if (pointer) result.pointer = pointer;
  const anyPointer = firstMatch([
    ["(any-pointer: fine)", "fine"], ["(any-pointer: coarse)", "coarse"], ["(any-pointer: none)", "none"],
  ]);
  if (anyPointer) result.any_pointer = anyPointer;
  const update = firstMatch([
    ["(update: fast)", "fast"], ["(update: slow)", "slow"], ["(update: none)", "none"],
  ]);
  if (update) result.update = update;
  const scripting = firstMatch([
    ["(scripting: enabled)", "enabled"], ["(scripting: initial-only)", "initial-only"],
    ["(scripting: none)", "none"],
  ]);
  if (scripting) result.scripting = scripting;
  const screen = window.screen || {};
  const depth = Number(screen.colorDepth || 0);
  if (Number.isFinite(depth) && depth >= 1 && depth <= 128) result.color_depth = Math.round(depth);
  const monochrome = Number(screen.monochrome || 0);
  if (Number.isFinite(monochrome) && monochrome >= 0 && monochrome <= 128) {
    result.monochrome_depth = Math.round(monochrome);
  }
  return result;
}
// Legacy call signatures remain documented for integrations/tests:
// api().load_edit_form(st.id, st.edit.artId)
// load_edit_form(st.id, snap.articleId, true)

// A preflight is a local snapshot plus optional live link probes. Reusing it
// forever is not browser-like: a URL or server-side validation can change
// while the source file stays untouched. Five minutes keeps repeated clicks
// cheap without turning a stale network observation into a guarantee.
const PREFLIGHT_CACHE_TTL_MS = 5 * 60 * 1000;
function preflightCacheFresh(cache, fingerprint) {
  if (!cache || cache.fingerprint !== fingerprint || !cache.snapshotToken) return false;
  const checked = Number(cache.checkedAt || 0);
  return Number.isFinite(checked) && checked > 0 &&
    Math.max(0, Date.now() - checked) <= PREFLIGHT_CACHE_TTL_MS;
}

/* ══════════ 统一美化对话框（替代原生 confirm/alert） ══════════
   返回 Promise<boolean>：确定=true，取消=false。支持多行正文与图标颜色。*/
let _dlgResolve = null;
let _promptTail = Promise.resolve();

async function acquirePromptSlot() {
  let release;
  const previous = _promptTail;
  _promptTail = new Promise((resolve) => { release = resolve; });
  await previous;
  return release;
}

function showDialog(opts) {
  const o = opts || {};
  const kind = o.kind || "confirm";      // confirm | success | warn | error
  const icons = { confirm: "❓", success: "✅", warn: "⚠", error: "❌" };
  $("dlgIcon").textContent = o.icon || icons[kind] || "❓";
  $("dlgIcon").className = "dlg-icon " + kind;
  $("dlgTitle").textContent = o.title || "请确认";
  const body = $("dlgBody");
  body.innerHTML = "";
  (Array.isArray(o.lines) ? o.lines : String(o.message || "").split("\n")).forEach((ln) => {
    const p = document.createElement("div");
    p.className = "dlg-line";
    p.textContent = ln;
    body.appendChild(p);
  });
  const okBtn = $("dlgOk"), cancelBtn = $("dlgCancel");
  okBtn.textContent = o.okText || "确定";
  cancelBtn.textContent = o.cancelText || "取消";
  cancelBtn.hidden = !!o.alert;          // alert 模式只留一个按钮
  okBtn.className = "cta" + (kind === "error" ? " danger" : "");
  $("dlgMask").hidden = false;
  okBtn.focus();
  return new Promise((resolve) => { _dlgResolve = resolve; });
}
function _closeDialog(val) {
  $("dlgMask").hidden = true;
  const r = _dlgResolve; _dlgResolve = null;
  if (r) r(val);
}
function confirmDialog(message, opts) {
  return showDialog(Object.assign({ message, kind: "confirm" }, opts || {}));
}
function alertDialog(message, opts) {
  return showDialog(Object.assign({ message, alert: true, kind: "success" }, opts || {}));
}

/* ══════════ 死链处理面板 ══════════
   逐条明确选择保留、替换或去链；默认不改原稿。
   返回 Promise：
     null                        → 用户取消本次操作
     [{href,new_href}, ...]      → 逐条处理方案（new_href 为空=移除）*/
let _linkResolve = null;
let _linkDeadCache = [];
function showDeadLinkPanel(deadList) {
  _linkDeadCache = deadList;
  const box = $("linkList");
  box.innerHTML = "";
  $("linkError").textContent = "";
  $("linkTitle").textContent = `发现 ${deadList.length} 条待处理内链`;
  deadList.forEach((d, idx) => {
    const row = document.createElement("div");
    row.className = "link-row";
    const anchor = d.text || "(无文字)";
    const modelTag = d.model ? `<span class="link-model">型号 ${esc(d.model)}</span>` : "";
    const hint = d.ambiguous
      ? `<span class="link-miss">同一型号匹配到 ${Number((d.candidates || []).length)} 个产品，` +
        `为避免填错已停止自动选择，请手动填写</span>`
      : (d.suggest
        ? `<span class="link-ok">✓ 产品库候选已实际访问验证，请核对</span>`
        : (d.candidate
          ? `<span class="link-miss">产品库候选未验证通过，不自动填充，请手动核对</span>`
          : (d.matched && d.missing_front_url
            ? `<span class="link-miss">产品库已找到该型号，但尚未获取可验证的前台链接；请先同步产品或手动填写</span>`
            : (d.model ? `<span class="link-miss">未在产品库找到该型号；可保留原链接、手动替换或明确去链</span>`
                     : `<span class="link-miss">链接检查未通过；请明确选择是否修改</span>`))));
    row.innerHTML =
      `<div class="link-anchor" title="锚文字">${esc(anchor)} ${modelTag}</div>` +
      `<div class="link-old">原链接：<span title="${esc(d.href)}">${esc(d.href)}</span>` +
      ` <em>[${esc(String(d.status || "404"))}]</em>` +
      `${/^https?:\/\//i.test(d.abs || d.href || "") ? '<button class="open-link link-open-old" type="button">打开原链接</button>' : ""}</div>` +
      `<select class="link-action" aria-label="第${idx + 1}条链接处理方式">` +
      `<option value="keep">保留原链接（不修改）</option><option value="replace">替换链接</option>` +
      `<option value="remove">去掉链接（保留内部文字、图片和格式）</option></select>` +
      `<input class="link-new" data-idx="${idx}" value="${esc(d.suggest || "")}" disabled` +
      ` aria-label="第${idx + 1}条链接替换地址" placeholder="选择替换后填写地址；留空不会删除链接">` +
      `<div class="link-status">${hint}</div>`;
    box.appendChild(row);
    row.querySelector(".link-action").addEventListener("change", (event) => {
      const input = row.querySelector(".link-new");
      input.disabled = event.target.value !== "replace";
      $("linkError").textContent = "";
      if (!input.disabled) input.focus();
    });
    const openOld = row.querySelector(".link-open-old");
    if (openOld) openOld.addEventListener("click", () => {
      const target = d.abs || d.href;
      const st = activeState();
      if (st && typeof openNativeRecordUrl === "function")
        return openNativeRecordUrl(st, target, "原链接", {allowBusy: true});
      return api().open_external_url(target);
    });
  });
  $("linkApplyVerified").disabled = !deadList.some((item) => item.suggest);
  $("linkMask").hidden = false;
  const first = box.querySelector(".link-action");
  if (first) first.focus();
  return new Promise((resolve) => { _linkResolve = resolve; });
}
function _collectLinkActions(deadList) {
  const actions = [];
  $("linkError").textContent = "";
  for (const row of $("linkList").querySelectorAll(".link-row")) {
    const inp = row.querySelector(".link-new");
    const mode = row.querySelector(".link-action").value;
    if (mode === "keep") continue;
    const idx = Number(inp.dataset.idx);
    if (!deadList[idx] || !["replace", "remove"].includes(mode) ||
        (mode === "replace" && !inp.value.trim())) {
      $("linkError").textContent = `第 ${idx + 1} 条链接：替换需填写地址；不修改请选择保留，删除链接请选择去链。`;
      inp.focus();
      return null;
    }
    actions.push(linkActionFor(deadList[idx], mode === "remove" ? "" : inp.value.trim()));
  }
  return actions;
}

function linkActionFor(source, newHref) {
  const action = { href: source.href, new_href: newHref };
  if (Number.isInteger(source.index)) action.index = source.index;
  if (Number.isInteger(source.occurrence)) action.occurrence = source.occurrence;
  return action;
}
function _closeLinkPanel(val) {
  $("linkMask").hidden = true;
  const r = _linkResolve; _linkResolve = null;
  if (r) r(val);
}

/* ── 标签模型 ──
   每个标签 = 一个后台站点会话。前端为每标签存一份 UI 状态，切换时重绘 DOM；
   后端每标签有独立 SiteSession（独立 client/cookie/锁），因此可并行操作。*/
let _seq = 0;
const TABS = new Map();          // tabId -> tabState
let ACTIVE = "";                 // 当前活动 tabId
let LAST_URL = "";               // 上次用过的后台地址（新标签预填）
let _credentialSeq = 0;          // 登录地址切换时，阻止较旧的账密响应回填
let _loginSeq = 0;               // 验证码/登录请求序号，阻止 A 站响应覆盖 B 站
let _appInfo = null;
let _lastBackupPath = "";

function newTabState() {
  const id = "t" + (++_seq);
  return {
    id,
    title: "新标签",
    url: "",
    networkMode: "direct",
    proxyUrl: "",
    loggedIn: false,
    busy: false,
    taskKind: "",
    taskStarted: false,
    _opSeq: 0,
    _requests: {},
    // 登录浮层临时态
    needLogin: true,
    // 功能区状态
    cats: [],
    activeFuncTab: "publish",
    pub: { html: "", htmlReady: false, htmlLoading: false, htmlInfo: "", htmlInfoClass: "",
           cat: "", catLoading: false, nativeUrl: "", mapping: {}, overrides: {}, fields: [], inlineCount: 0, remoteCount: 0, mediaCount: 0,
           submitter: null, submitterOptions: [],
           backendValues: {},
           manualImages: [], thumbPath: "", thumbUrl: "", width: "preserve", insertStrategy: "top",
           ico: "none", rememberThumb: false, carouselImages: [], carouselSize: "original",
           carouselW: 800, carouselH: 800,
           top: false, rec: false, head: false, flagChanges: {}, checkLinks: false,
           uploadMetadata: [], thumbServerInfo: "", thumbServerUrl: "",
           retryable: false, preflightCache: null,
           msg: "", msgClass: "" },
    edit: { cat: "", articles: [], articlesLoading: false, artId: "", nativeUrl: "",
             html: "", htmlReady: false, htmlLoading: false, mapping: {}, overrides: {},
             fields: [], backendValues: {}, formLoading: false, formReady: false, inlineCount: 0, remoteCount: 0, mediaCount: 0, cover: "part",
             submitter: null, submitterOptions: [],
             filter: "", linkReport: null, contentImages: [], contentHash: "", imageReplacements: {},
            thumbPath: "", thumbUrl: "", ico: "none", carouselImages: [],
            uploadMetadata: [], thumbServerInfo: "", thumbServerUrl: "",
            carouselSize: "original", carouselW: 800, carouselH: 800, insertStrategy: "before_h2",
            carouselMode: "append",
            top: false, rec: false, head: false, flagChanges: {}, refreshDate: false,
            checkLinks: false, msg: "", msgClass: "" },
    contentAdmin: { scode: "", mcode: "", records: [], targets: [], revision: "",
                    selected: {}, keyword: "", loading: false, msg: "", msgClass: "" },
    batch: { items: [], running: false, paused: false, pauseRequested: false,
             activeIndex: -1, editingIndex: -1, applyVerified: false,
             skipUnverified: false, stopRequested: false,
             msg: "", _resolve: null },
    query: { products: [], cacheFallbackProducts: [], cacheFallbackHealth: null, cacheLoaded: false, filter: "", healthFilter: "all", mcode: "", models: [], modelsLoading: false,
             sort: "id_desc", page: 1, pageSize: 100, selected: {}, edits: {}, health: null,
             remoteProducts: null, remotePage: 1, remoteHasNext: false, remoteKeyword: "", remoteLoading: false,
             remoteAutoStarted: false, nativeUrl: "",
             continueOnError: false,
             msg: "", msgClass: "", advancedLoading: false },
    mutationReview: {},
    category: { filter: "", selectedId: "", mode: "", form: null, loading: false,
                saving: false, deleting: false, dirty: false, expanded: {}, modelInitialValue: "",
                parentFilter: "", parentExpanded: {}, msg: "", msgClass: "" },
    slide: { records: [], loaded: false, form: null, mode: "", selectedId: "",
             loading: false, saving: false, deleting: false, dirty: false,
             msg: "", msgClass: "" },
    single: { records: [], loaded: false, form: null, selectedId: "", loading: false,
              saving: false, keyword: "", msg: "", msgClass: "" },
    adminModules: { records: [], loaded: false, form: null, view: null, url: "", label: "",
                    revision: "", loading: false, saving: false, dirty: false,
                    msg: "", msgClass: "" },
    msgs: [],
    msgMeta: { complete: false, pages: 0, warning: "", loading: false,
    filter: "", status: "all", selected: {}, limit: 50,
    server_filter: null, server_filter_query: {}, filter_warning: "", native_url: "" },
    audit: { records: [], loading: false, loaded: false, error: "" },
    diagnostic: { msg: "", lastPath: "", pendingOperations: [] },
    log: [],
    _draftChecked: false,
    _draftTimers: {},
    _draftRevision: { publish: 0, edit: 0, batch: 0 },
    _draftRecoveryBase: { publish: 0, edit: 0, batch: 0 },
    _draftRecoveryPending: false,
    _draftRecoveryInFlight: false,
    _draftRecoveryRetries: 0,
    _draftRecoveryRetryTimer: null,
    _draftDeferredSaves: {},
    _draftSaveBlocked: {},
    _draftPersistenceSuspended: false,
    _linkWaiters: {},
  };
}

function activeState() { return TABS.get(ACTIVE); }

function hasPendingUiRequest(st) {
  return !!(st && (st.pub.catLoading || st.pub.htmlLoading ||
    st.edit.articlesLoading || st.edit.formLoading || st.edit.htmlLoading ||
    (st.category && (st.category.loading || st.category.saving || st.category.deleting)) ||
    (st.slide && (st.slide.loading || st.slide.saving || st.slide.deleting)) ||
    (st.single && (st.single.loading || st.single.saving)) ||
    (st.adminModules && (st.adminModules.loading || st.adminModules.saving))));
}

function isLiveState(st) {
  return !!st && TABS.get(st.id) === st;
}

function nextRequest(st, key) {
  const value = Number(st._requests[key] || 0) + 1;
  st._requests[key] = value;
  return value;
}

function requestIsCurrent(st, key, value) {
  return isLiveState(st) && st._requests[key] === value;
}

function loginRequestIsCurrent(st, url, value) {
  return isLiveState(st) && ACTIVE === st.id && _loginSeq === value &&
    $("loginUrl").value.trim() === String(url || "").trim();
}

function setAreaMsg(st, area, elementId, text, cls) {
  if (st && st[area]) {
    st[area].msg = text || "";
    st[area].msgClass = cls || "";
  }
  if (st && st.id === ACTIVE) setMsg(elementId, text, cls);
}

function beginOperation(st, kind) {
  if (!isLiveState(st) || st.busy) return 0;
  const op = ++st._opSeq;
  st.busy = true;
  st.taskKind = kind;
  st.taskStarted = false;
  st._prog = null;
  renderTabStrip();
  if (st.id === ACTIVE) renderActiveTab();
  return op;
}

function operationIsCurrent(st, op) {
  return isLiveState(st) && st.busy && st._opSeq === op;
}

function finishOperation(st, op) {
  if (!isLiveState(st) || (op && st._opSeq !== op)) return;
  st.busy = false;
  st.taskKind = "";
  st.taskStarted = false;
  st._prog = null;
  renderTabStrip();
  if (st.id === ACTIVE) renderActiveTab();
}

/* ── 工具 ── */
function log(text, tabId) {
  const id = tabId || ACTIVE;
  const st = TABS.get(id);
  const ts = new Date().toTimeString().slice(0, 8);
  const line = `[${ts}] ${text}`;
  if (st) st.log.push(line);
  if (id === ACTIVE) {
    const box = $("logBox");
    box.textContent += line + "\n";
    box.scrollTop = box.scrollHeight;
  }
}
function setMsg(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = "msg" + (cls ? " " + cls : "");
}
function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}
function setProg(id, done, total, text, bytesDone=0, bytesTotal=0) {
  const wrap = $(id);
  if (!wrap) return;
  if (done == null) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const pct = total ? Math.round(done * 100 / total) : 0;
  wrap.querySelector(".bar").style.setProperty("--p", pct + "%");
  const byteText = Number(bytesTotal) > 0
    ? ` · ${formatBytes(bytesDone)}/${formatBytes(bytesTotal)}` : "";
  wrap.querySelector("span").textContent = (text || `${done}/${total}`) + byteText;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function shortLabel(url) {
  if (!url) return "新标签";
  try {
    const u = new URL(url.includes("://") ? url : "https://" + url);
    return u.hostname.replace(/^www\./, "");
  } catch (_) { return url.slice(0, 20); }
}

/* ══════════ 标签栏渲染与切换 ══════════ */
function renderTabStrip() {
  const strip = $("tabStrip");
  strip.innerHTML = "";
  TABS.forEach((st) => {
    const el = document.createElement("div");
    el.className = "site-tab" + (st.id === ACTIVE ? " on" : "") +
      (st.loggedIn ? " logged" : "") + (st.busy ? " busy" : "");
    el.innerHTML = `<span class="dot"></span>` +
      `<span class="label">${esc(st.title)}</span>` +
      `<span class="x" title="关闭标签">✕</span>`;
    el.addEventListener("click", (e) => {
      if (e.target.classList.contains("x")) { closeTab(st.id); return; }
      switchTab(st.id);
    });
    strip.appendChild(el);
  });
}
function updateEmptyState() {
  const has = TABS.size > 0;
  $("emptyState").hidden = has;
  document.querySelector(".body").style.display = has ? "flex" : "none";
  $("siteState").style.display = has ? "" : "none";
  $("btnClearAllCache").disabled = !has;
}
function newTab() {
  const st = newTabState();
  TABS.set(st.id, st);
  ACTIVE = st.id;
  _credentialSeq += 1;
  _loginSeq += 1;
  updateEmptyState();
  renderTabStrip();
  renderActiveTab();
  // 新标签立即弹登录浮层，并预填上次用过的地址（省得每次手输）
  $("loginUrl").value = LAST_URL || "";
  syncLoginNetworkControls(st);
  $("loginUser").value = "admin";      // 默认用户名
  $("loginPass").value = ""; $("loginCode").value = "";
  $("loginPass").type = "password"; $("btnTogglePass").textContent = "显示";
  $("loginHistory").value = LAST_URL || "";
  if (LAST_URL) fillSavedCredentials(LAST_URL);
  $("capRow").hidden = true;
  setMsg("loginMsg", LAST_URL ? "已预填上次地址，可修改后点「获取验证码」" : "请填写后台地址");
  $("loginMask").hidden = false;
  $("loginUrl").focus();
}
async function closeTab(tabId) {
  const st = TABS.get(tabId);
  if (st && st.busy) {
    const go = await confirmDialog("关闭后正在执行的任务会被取消。", {
      title: "该标签有任务进行中", kind: "warn", okText: "仍然关闭", cancelText: "不关闭" });
    if (!go) return;
  }
  if (st && st.loggedIn) await flushDrafts(st);
  if (st) clearTimeout(st._draftRecoveryRetryTimer);
  try { await api().close_tab(tabId); } catch (_) {}
  TABS.delete(tabId);
  if (ACTIVE === tabId) {
    ACTIVE = TABS.size ? Array.from(TABS.keys())[TABS.size - 1] : "";
    _credentialSeq += 1;
    _loginSeq += 1;
  }
  updateEmptyState();
  renderTabStrip();
  if (ACTIVE) renderActiveTab();
}
function switchTab(tabId) {
  if (tabId === ACTIVE) return;
  ACTIVE = tabId;
  _credentialSeq += 1;
  _loginSeq += 1;
  renderTabStrip();
  renderActiveTab();
  // 已登录但还没栏目（比如启动恢复时没轮到）→ 切过来自动补上
  const st = TABS.get(tabId);
  if (st && st.loggedIn && !(st.cats || []).length) loadCategoriesFor(st);
  if (st && st.loggedIn && !st.query.cacheLoaded) loadProductCache(st);
}

/* 把当前活动标签的状态渲染回 DOM（切换标签时调用） */
function renderActiveTab() {
  const st = activeState();
  if (!st) return;
  syncLoginNetworkControls(st);
  // 顶栏站点状态
  if (st.busy) { $("siteState").textContent = "忙碌中"; $("siteState").className = "pill busy"; }
  else if (st.loggedIn) { $("siteState").textContent = "已登录 " + st.title; $("siteState").className = "pill on"; }
  else { $("siteState").textContent = "未登录"; $("siteState").className = "pill off"; }
  $("btnLogout").hidden = !st.loggedIn;
  $("btnLogout").disabled = !!st.busy;
  $("btnClearAllCache").disabled = !st.loggedIn || !!st.busy;
  $("btnOpenAdmin").hidden = !st.loggedIn;
  $("btnOpenAdmin").disabled = !st.loggedIn;
  // 日志
  $("logBox").textContent = st.log.join("\n") + (st.log.length ? "\n" : "");
  // 栏目下拉
  fillCatSelects(st);
  // 发布区
  $("pubHtml").value = fileName(st.pub.html);
  $("pubHtml").title = fileName(st.pub.html);
  $("pubCat").value = st.pub.cat;
  $("pubWidth").value = st.pub.width;
  $("pubInsertStrategy").value = st.pub.insertStrategy || "top";
  $("pubIco").value = st.pub.ico;
  $("pubThumbRemember").checked = !!st.pub.rememberThumb;
  $("btnPubThumb").hidden = st.pub.ico !== "file";
  $("pubThumbInfo").textContent = st.pub.thumbPath ? fileName(st.pub.thumbPath) : "";
  renderThumbnailControl(st, "pub");
  void renderThumbnailPreview(st, "pub");
  // The browser's direct uploader owns any server-side image processing.
  // Keep the legacy state fields for draft migration, but never expose a
  // client-side crop/resize mode that would change multipart bytes.
  st.pub.carouselSize = "original";
  $("pubCarouselSize").value = "original";
  $("pubCarouselInfo").textContent = st.pub.carouselImages.length
    ? `已选择 ${st.pub.carouselImages.length} 张` : "未选择";
  $("btnPubCarouselClear").hidden = !st.pub.carouselImages.length;
  $("pubGalleryInfo").textContent = st.pub.galleryPlan != null ? `明确保存 ${st.pub.galleryPlan.length} 张（含标题与顺序）` : "";
  $("pubTop").checked = st.pub.top; $("pubRec").checked = st.pub.rec; $("pubHead").checked = st.pub.head;
  $("pubCheckLinks").checked = st.pub.checkLinks === true;
  $("pubImgInfo").textContent = st.pub.inlineCount || st.pub.remoteCount || st.pub.mediaCount || st.pub.manualImages.length
    ? `内置图片 ${st.pub.inlineCount || 0} 个 + 远程图片 ${st.pub.remoteCount || 0} 个（按编辑器配置处理）+ 媒体 ${st.pub.mediaCount || 0} 个 + 额外手选图 ${st.pub.manualImages.length} 张`
    : "未选择图片";
  renderMapTable("mapTable", fieldsFor(st.pub), st.pub.mapping,
    st.pub.overrides, st.pub.fields);
  renderPublishBackendFields(st);
  renderContentSubmitter(st, "pub");
  setMsg("pubHtmlInfo", st.pub.htmlInfo, st.pub.htmlInfoClass);
  const pubMapped = Object.values(st.pub.mapping || {}).some(Boolean);
  $("btnPublish").disabled = st.busy || !st.loggedIn || !st.pub.cat ||
    !st.pub.htmlReady || st.pub.catLoading || st.pub.htmlLoading || !pubMapped;
  const nativePublish = $("btnPubNative");
  if (nativePublish) {
    nativePublish.disabled = !!st.busy || !st.loggedIn ||
      !String(st.pub.nativeUrl || "").trim();
    nativePublish.title = st.pub.nativeUrl
      ? "在完整浏览器中打开当前栏目真实新增内容页；动态脚本和编辑器由网页执行"
      : "先选择一个可用栏目，读取真实新增内容页后才能打开";
  }
  setMsg("pubMsg", st.pub.msg, st.pub.msgClass);
  const cache = st.pub.preflightCache;
  const cacheValid = preflightCacheFresh(cache, publishFingerprint(publishSnapshot(st)));
  $("btnPreflight").textContent = cacheValid ? "✓ 检查已通过（可复用）" : "发布前检查";
  $("btnPreflight").classList.toggle("checked", cacheValid);
  renderBatchQueue(st);
  // 编辑区
  $("editCat").value = st.edit.cat;
  // 两个栏目选中值都已恢复，此时才能正确显示选择器标题
  closeAllCatPanels();          // 切标签一律收起，默认不展开一级栏目
  updateCatToggle(st, "pub");
  updateCatToggle(st, "edit");
  $("editFilter").value = st.edit.filter || "";
  renderArticleOptions(st.edit.filter || "");
  renderContentAdmin(st);
  $("editArt").value = st.edit.artId;
  const selectedEditArticle = (st.edit.articles || []).find((item) =>
    String(item.id || "") === String(st.edit.artId || ""));
  const nativeEditButton = $("btnEditNative");
  const nativeEditTarget = (selectedEditArticle && selectedEditArticle.edit_url) || st.edit.nativeUrl || "";
  if (nativeEditButton) {
    nativeEditButton.disabled = !!st.busy || !selectedEditArticle ||
      !String(nativeEditTarget).trim();
    nativeEditButton.title = nativeEditTarget
      ? "在完整浏览器中打开当前文章的真实后台编辑页"
      : "当前文章列表没有发现安全的真实后台编辑地址";
  }
  const editPreviewButton = $("btnEditFrontPreview");
  if (editPreviewButton) {
    editPreviewButton.disabled = !!st.busy || !selectedEditArticle ||
      !String(selectedEditArticle.view_url || "").trim();
    editPreviewButton.title = selectedEditArticle && selectedEditArticle.view_url
      ? "打开后台列表明确提供的真实前台/预览页（由网页主题和脚本渲染）"
      : "当前文章列表没有发现明确的同源前台/预览地址；软件不会猜测路由";
  }
  $("editHtml").value = fileName(st.edit.html);
  $("editHtml").title = fileName(st.edit.html);
  $("editCover").value = st.edit.cover;
  $("editInsertStrategy").value = st.edit.insertStrategy || "before_h2";
  st.edit.carouselSize = "original";
  $("editCarouselSize").value = "original";
  $("editCarouselMode").value = st.edit.carouselMode;
  $("editCarouselInfo").textContent = st.edit.carouselImages.length
    ? `已选择 ${st.edit.carouselImages.length} 张` : "未选择";
  $("btnEditCarouselClear").hidden = !st.edit.carouselImages.length;
  $("editGalleryInfo").textContent = st.edit.galleryPlan != null ? `明确保存 ${st.edit.galleryPlan.length} 张（含标题与顺序）` : "";
  $("editCarouselMode").disabled = st.edit.galleryPlan != null || st.busy;
  $("editIco").value = st.edit.ico;
  $("btnEditThumb").hidden = st.edit.ico !== "file";
  $("editThumbInfo").textContent = st.edit.thumbPath ? fileName(st.edit.thumbPath) : "";
  renderThumbnailControl(st, "edit");
  void renderThumbnailPreview(st, "edit");
  $("editTop").checked = st.edit.top; $("editRec").checked = st.edit.rec; $("editHead").checked = st.edit.head;
  $("editCheckLinks").checked = st.edit.checkLinks === true;
  $("editRefreshDate").checked = !!st.edit.refreshDate;
  renderDetailImages(st);
  renderMapTable("editMapTable", fieldsFor(st.edit), st.edit.mapping,
    st.edit.overrides, st.edit.fields);
  renderEditBackendFields(st);
  renderContentSubmitter(st, "edit");
  renderEditLinkReport(st);
  $("btnSubmitEdit").disabled = st.busy || !st.loggedIn || !st.edit.artId ||
    !st.edit.formReady || st.edit.articlesLoading || st.edit.formLoading || st.edit.htmlLoading ||
    (!!st.edit.html && !st.edit.htmlReady);
  setMsg("editMsg", st.edit.msg, st.edit.msgClass);
  // 查改区
  $("qFilter").value = st.query.filter || "";
  $("qHealth").value = st.query.healthFilter || "all";
  $("qSort").value = st.query.sort || "id_desc";
  $("qContinueErrors").checked = !!st.query.continueOnError;
  const modelSelect = $("qModel");
  if (modelSelect) {
    modelSelect.innerHTML = "<option value=\"\">自动识别唯一模型</option>";
    (st.query.models || []).forEach((model) => {
      const option = document.createElement("option");
      option.value = String(model.mcode || "");
      option.textContent = `${(model.labels || []).join(" / ") || "产品模型"}（mcode ${model.mcode}）`;
      modelSelect.appendChild(option);
    });
    modelSelect.value = st.query.mcode || "";
    modelSelect.hidden = !(st.query.models || []).length;
  }
  renderProducts(st.query.filter);
  renderProductHealth(st);
  setMsg("qMsg", st.query.msg, st.query.msgClass);
  // 栏目管理
  renderCategoryManager(st);
  // 独立网站 Slide/轮播管理（与文章图集分开）
  renderSlideManager(st);
  // 独立单页管理（与文章 Content 路由分开）
  renderSingleManager(st);
  // 后台其他模块：只开放当前菜单发现的安全动态表单
  renderAdminModules(st);
  // 留言
  renderMessages(st.msgs, st.msgMeta);
  renderAuditRecords(st);
  renderPendingOperations(st);
  setMsg("diagMsg", st.diagnostic.msg);
  $("btnDiagCopy").hidden = !st.diagnostic.lastPath;
  $("btnDiagOpen").hidden = !st.diagnostic.lastPath;
  // 功能子标签
  document.querySelectorAll(".tabs button").forEach((b) =>
    b.classList.toggle("on", b.dataset.tab === st.activeFuncTab));
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("on", t.id === "tab-" + st.activeFuncTab));
  // 进度条必须按任务类型恢复，不能按当前打开的功能页猜测。
  ["pubProg", "editProg", "qProg", "msgProg", "diagProg"].forEach((id) => setProg(id, null));
  if (st.busy && st._prog) {
    const id = (st._prog.task === "edit" || st.taskKind === "link_check_edit") ? "editProg"
      : (["query", "product_sync", "product_bulk_modify"].includes(st._prog.task) ? "qProg"
      : (st._prog.task === "message_bulk" ? "msgProg"
      : (st._prog.task === "diagnostic" ? "diagProg" : "pubProg")));
    setProg(id, st._prog.done, st._prog.total, st._prog.text,
      st._prog.bytesDone, st._prog.bytesTotal);
  }
  renderTaskControls(st);
}

function renderPendingOperations(st) {
  const panel = $("pendingOpsPanel"), list = $("pendingOpsList");
  if (!panel || !list) return;
  const operations = (st.diagnostic && st.diagnostic.pendingOperations) || [];
  panel.hidden = !operations.length;
  list.innerHTML = "";
  operations.forEach((item) => {
    const row = document.createElement("div"); row.className = "pending-op-row";
    row.innerHTML = `<span>${esc(item.action || "操作")} · ${esc(item.target || "未指定目标")}</span>` +
      `<button type="button" class="ghost compact pending-op-resolve" data-op="${esc(item.operation_id)}">我已在后台核对</button>`;
    list.appendChild(row);
  });
}

function renderTaskControls(st) {
  const locked = !!st.busy || hasPendingUiRequest(st) || !st.loggedIn;
  const publishControls = ["btnPickHtml", "btnPickBatchHtml", "btnPickBatchFolder",
    "pubCatToggle", "btnLoadCats", "btnPickImgs",
    "pubWidth", "pubIco", "pubThumbUrl", "pubThumbRemember", "btnPubThumb", "btnPubCarousel", "btnPubCarouselClear", "pubCarouselSize",
    "pubTop", "pubRec", "pubHead", "pubCheckLinks", "btnAutoMap", "btnPubGalleryEdit",
    "btnPreflight", "btnPubPreview", "batchApplyVerified", "batchSkipUnverified"];
  const editControls = ["editCatToggle", "btnEditLoadArts", "editFilter", "editArt", "btnEditNative", "btnEditFrontPreview",
    "btnEditPickHtml", "editCover", "btnEditCarousel", "btnEditCarouselClear", "editCarouselMode",
    "editCarouselSize", "editIco", "editThumbUrl", "btnEditThumb", "btnEditGalleryEdit",
    "editTop", "editRec", "editHead", "editRefreshDate", "editCheckLinks", "btnEditPreview"];
  const otherControls = ["btnPull", "btnPullFull", "btnRemoteQuery", "btnProductNative", "btnRepairLinks", "btnProductModels", "qModel", "qFilter", "qHealth", "qSort",
    "qSelectAll", "qContinueErrors", "btnBulkRepair", "btnBulkEdit", "btnMsg50", "btnMsgAll",
    "msgFilter", "msgStatus", "msgSelectAll", "btnMsgBulkStatus", "btnMsgExport",
    "btnAuditRefresh", "btnAuditExport",
    "btnBackendDiag", "btnDiag", "btnOpenAdmin", "btnCategoryRefresh", "btnCategoryAddRoot",
    "btnCategoryAddChild", "btnCategoryNative", "btnCategoryBatch", "btnCategorySave", "btnCategoryCancel", "btnCategoryDelete", "categoryFilter",
    "btnSlideRefresh", "btnSlideAdd", "btnSlideSave", "btnSlideCancel", "btnSlideDelete",
    "btnSingleRefresh", "btnSingleSave", "btnSingleCancel", "singleFilter",
    "btnAdminModuleRefresh", "btnAdminModuleSave", "btnAdminModuleCancel"];
  publishControls.concat(editControls, otherControls).forEach((id) => {
    const el = $(id); if (el) el.disabled = locked;
  });
  $("btnRemoteQuery").disabled = locked || !!st.query.remoteLoading;
  $("btnRemoteQuery").textContent = Array.isArray(st.query.remoteProducts)
    ? "返回本地缓存" : "后台实时查询";
  const nativeProduct = $("btnProductNative");
  if (nativeProduct) {
    nativeProduct.hidden = !String(st.query.nativeUrl || "").trim();
    nativeProduct.disabled = locked || !String(st.query.nativeUrl || "").trim();
    nativeProduct.title = st.query.nativeUrl
      ? `打开后台真实列表：${st.query.nativeUrl}`
      : "当前没有已确认的后台列表地址";
  }
  $("batchApplyVerified").disabled = locked || st.pub.checkLinks !== true;
  $("batchSkipUnverified").disabled = locked || st.pub.checkLinks !== true;
  $("editCarouselMode").disabled = locked || st.edit.galleryPlan != null;

  // 主操作按钮还要保留各自的数据完整性判断，不能被上面的通用锁覆盖。
  const pubMapped = Object.values(st.pub.mapping || {}).some(Boolean);
  $("btnPublish").disabled = locked || !st.pub.cat || !st.pub.htmlReady ||
    st.pub.catLoading || st.pub.htmlLoading || !pubMapped;
  $("btnSubmitEdit").disabled = locked || !st.edit.artId || !st.edit.formReady ||
    st.edit.articlesLoading || st.edit.formLoading || st.edit.htmlLoading ||
    (!!st.edit.html && !st.edit.htmlReady);
  document.querySelectorAll("#categoryDynamicFields input, #categoryDynamicFields select, #categoryDynamicFields textarea")
    .forEach((el) => { el.disabled = locked || !!el.dataset.readonly; });
  document.querySelectorAll("#slideDynamicFields input, #slideDynamicFields select, #slideDynamicFields textarea")
    .forEach((el) => { el.disabled = locked || !!el.dataset.readonly; });
  document.querySelectorAll("#singleDynamicFields input, #singleDynamicFields select, #singleDynamicFields textarea")
    .forEach((el) => { el.disabled = locked || !!el.dataset.readonly; });
  document.querySelectorAll("#adminModuleDynamicFields input, #adminModuleDynamicFields select, #adminModuleDynamicFields textarea")
    .forEach((el) => { el.disabled = locked || !!el.dataset.readonly; });

  $("btnCancelTask").hidden = !(st.busy && st.taskStarted &&
    ["publish", "link_check", "batch"].includes(st.taskKind));
  $("btnEditCancel").hidden = !(st.busy && st.taskStarted &&
    ["edit", "link_check_edit"].includes(st.taskKind));
  $("btnEditLinkCancel").hidden = !(st.busy && st.taskStarted && st.taskKind === "link_check_edit");
  $("btnPullCancel").hidden = !(st.busy && st.taskStarted &&
    ["query", "product_sync", "product_bulk_modify"].includes(st.taskKind));
  $("btnBackendDiagCancel").hidden =
    !(st.busy && st.taskStarted && st.taskKind === "diagnostic");
  $("btnRetryImgs").hidden = st.busy || !st.pub.retryable;
  $("btnRetryImgs").disabled = locked;
  const batch = st.batch || {};
  $("btnBatchStart").disabled = !!st.busy || !st.loggedIn || !(batch.items || []).some((x) => batchCanRunItem(x, false));
  $("btnBatchPause").hidden = !batch.running || batch.paused || batch.pauseRequested;
  $("btnBatchPause").disabled = false;
  $("btnBatchResume").hidden = !batch.paused;
  $("btnBatchResume").disabled = false;
  $("btnBatchRetry").hidden = batch.running || !(batch.items || []).some((x) => x.status === "failed");
  $("btnBatchRetry").disabled = !!st.busy;
  $("btnBatchClear").disabled = !!batch.running;
}
function fieldsFor(area) {
  // 把 area.parsedFields（[{key,value}]）转为 renderMapTable 需要的结构
  return area.parsedFields || [];
}

function cmsFieldWide(field) {
  const type = String(field.type || field.kind || "text").toLowerCase();
  const name = String(field.name || "").toLowerCase();
  return type === "textarea" || /description|keywords|title|note|remark/.test(name) ||
    String(field.value ?? "").length > 80;
}

function cmsVerifyIssue(input, field) {
  // Mirror only deterministic Layui/native rules. Unknown rule names remain
  // page-owned JavaScript and are deliberately left to the backend.
  if (!input || !field || input.disabled) return "";
  if (field.form_novalidate) return "";
  const value = String(input.value ?? "");
  const rules = String(field["lay-verify"] || "").split("|")
    .map(item => item.trim().toLowerCase()).filter(Boolean);
  if (rules.includes("required") && !value.trim()) return "不能为空";
  if (!value) return ""; // other rules are skipped for an optional blank.
  if (rules.includes("email") && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value))
    return "必须是有效邮箱地址";
  if (rules.includes("url") && !/^[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s]+$/.test(value))
    return "必须是有效 URL";
  if (rules.includes("number") && !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(value))
    return "必须是有效数值";
  if (rules.includes("phone") && !/^1\d{10}$/.test(value))
    return "手机号格式无效";
  if (rules.includes("identity") && !/^(?:\d{15}|\d{17}[\dXx])$/.test(value))
    return "身份证号格式无效";
  if (rules.includes("password") && !/^\S{6,12}$/.test(value))
    return "密码长度应为 6～12 位";
  if (rules.includes("date") && !/^\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$/.test(value))
    return "必须是有效日期";
  return "";
}

function syncCmsVerifyValidity(input, field) {
  const kind = String(field?.type || field?.kind || "").toLowerCase();
  if (!input || !field || kind === "checkbox" || kind === "radio") return;
  input.setCustomValidity(cmsVerifyIssue(input, field));
}

function _readDroppedFileDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("读取拖放文件失败"));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
}

async function materializeDroppedFiles(fileList, pickerKey, multiple, extensions) {
  const files = Array.from(fileList || []).filter(file => file &&
    (file.type || file.name || file.path));
  if (!files.length) return [];
  const chosen = multiple ? files : files.slice(0, 1);
  const items = [];
  for (const file of chosen) {
    const item = { name: String(file.name || "dropped.bin"),
      type: String(file.type || ""), size: Number(file.size || 0) };
    // Electron-like hosts expose a path; ordinary WebView File objects do
    // not, so use a bounded data URL bridge instead of silently dropping the
    // user's selection.
    if (file.path && typeof file.path === "string") item.path = file.path;
    else item.data_url = await _readDroppedFileDataUrl(file);
    items.push(item);
  }
  const result = await api().save_dropped_files(
    pickerKey || "dropped", items, !!multiple, extensions || []);
  if (!result || !result.ok) throw new Error((result && result.msg) || "保存拖放文件失败");
  if (typeof rememberFileMimeHints === "function")
    rememberFileMimeHints(result.paths || [], result.file_types || []);
  return result.paths || [];
}

function installFileDropTarget(holder, options) {
  if (!holder) return;
  const opts = options || {};
  holder.classList.add("file-drop-target");
  const hasFiles = event => event && event.dataTransfer &&
    Array.from(event.dataTransfer.types || []).some(type => type === "Files");
  holder.addEventListener("dragenter", event => {
    if (!hasFiles(event)) return;
    event.preventDefault(); event.stopPropagation(); holder.classList.add("drag-over");
  });
  holder.addEventListener("dragover", event => {
    if (!hasFiles(event)) return;
    event.preventDefault(); event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    holder.classList.add("drag-over");
  });
  ["dragleave", "dragend"].forEach(name => holder.addEventListener(name, event => {
    if (!hasFiles(event) && name !== "dragend") return;
    holder.classList.remove("drag-over");
  }));
  holder.addEventListener("drop", async event => {
    if (!hasFiles(event)) return;
    event.preventDefault(); event.stopPropagation(); holder.classList.remove("drag-over");
    if (typeof opts.onDrop !== "function") return;
    try { await opts.onDrop(event.dataTransfer.files); }
    catch (error) {
      const st = activeState();
      if (st) log(`拖放文件失败：${error}`, st.id);
    }
  });
}

function createCmsFieldControl(field, value, onChange, prefix) {
  const wrap = document.createElement("div");
  wrap.className = "category-field" + (cmsFieldWide(field) ? " wide" : "");
  const label = document.createElement("label");
  label.textContent = (field.label || field.name || "字段") +
    (field.required ? " *" : "") + " · " + (field.name || "");
  wrap.appendChild(label);
  const type = String(field.type || field.kind || "text").toLowerCase();
  const choices = field.options || [];
  const controls = () => Array.from(wrap.querySelectorAll("[data-cms-control]"));
  const checkGroup = () => {
    if (type !== "checkbox" || !field.required || field.form_novalidate) return;
    const inputs = controls();
    if (inputs[0]) inputs[0].setCustomValidity(inputs.some(x => x.checked) ? "" : "请至少选择一项");
  };
  const emit = () => {
    const inputs = controls();
    let next;
    if (type === "checkbox") next = inputs.filter(x => x.checked).map(x => x.value);
    else if (type === "radio") next = (inputs.find(x => x.checked) || {}).value || "";
    else if (type === "select" && field.multiple) next = Array.from(inputs[0].selectedOptions).filter(x => !x.disabled).map(x => x.value);
    else if (field.multiple) next = inputs.map(x => x.value);
    else {
      next = inputs[0] ? inputs[0].value : "";
      if (field.widget === "datetime" && next) {
        next = next.replace("T", " ");
        if (next.length === 16) next += ":00";
      }
    }
    checkGroup();
    onChange(next);
  };
  const attach = (input) => {
    input.dataset.cmsControl = field.name || "";
    input.disabled = !!(field.disabled || field.readonly);
    for (const attr of ["min","max","step","maxlength","minlength","pattern","accept","placeholder",
      "dirname","autocomplete","inputmode","list","size"]) {
      if (field[attr] !== undefined && field[attr] !== null) input.setAttribute(attr, String(field[attr]));
    }
    if (field.required && type !== "checkbox" && !field.form_novalidate) input.required = true;
    const validate = () => syncCmsVerifyValidity(input, field);
    input.addEventListener("input", () => { validate(); emit(); });
    input.addEventListener("change", () => { validate(); emit(); });
    return input;
  };
  const hasUploadButton = !!field.upload_target;
  if (type === "file" || hasUploadButton) {
    const holder = document.createElement("div");
    holder.className = "media-file-picker";
    if (hasUploadButton && type !== "file") {
      // Stock PbootCMS category controls are text inputs paired with a
      // button.upload[data-des].  Keep the text box (existing server URL or
      // manually entered path remains valid) and add the same local picker;
      // the bridge will upload only an actual local file before POST.
      const text = attach(document.createElement("input"));
      text.type = "text";
      text.value = String(value ?? "");
      holder.appendChild(text);
    }
    const list = document.createElement("div");
    list.className = "hint file-hint";
    let pick = null;
    const render = (selected) => {
      const values = Array.isArray(selected) ? selected : (selected ? [selected] : []);
      list.textContent = values.length ? values.map(fileName).join("、") : "未选择文件";
      if (pick && type === "file" && field.required)
        pick.setCustomValidity(values.length ? "" : "请选择文件");
    };
    render(value);
    pick = document.createElement("button");
    pick.type = "button"; pick.className = "ghost mini";
    // The picker is the visible form-associated control for our safe bridge
    // path.  Custom validity preserves native required-file behavior without
    // exposing a Windows path through a real file input.
    pick.dataset.cmsControl = field.name || "";
    pick.textContent = field.multiple ? "选择文件…" : "选择文件…";
    pick.disabled = !!(field.disabled || field.readonly);
    render(value);
    const applyPicked = (paths) => {
      if (!paths || !paths.length) return false;
      const maxFiles = Number(field.max_files || 0);
      if (field.multiple && Number.isFinite(maxFiles) && maxFiles > 0 &&
          paths.length > maxFiles) {
        const st = activeState();
        if (st) log(`字段 ${field.name || "文件"} 最多选择 ${maxFiles} 个文件；本次选择未应用`, st.id);
        return false;
      }
      const next = field.multiple ? paths : paths[0];
      const text = hasUploadButton && type !== "file"
        ? holder.querySelector('input[type="text"]') : null;
      if (text) text.value = String(next ?? "");
      render(next); onChange(next);
      return true;
    };
    const chooseFiles = async (pathsPromise) => {
      pick.disabled = true;
      try {
        const paths = await pathsPromise();
        applyPicked(paths);
      } catch (error) {
        const st = activeState();
        if (st) log(`选择/拖放文件失败：${error}`, st.id);
      } finally { pick.disabled = !!(field.disabled || field.readonly); }
    };
    const clear = document.createElement("button");
    clear.type = "button"; clear.className = "ghost mini";
    clear.textContent = "清除选择";
    clear.title = type === "file"
      ? "清除本次选择；提交时按浏览器空文件控件语义处理"
      : "清除当前地址或本次选择，并作为明确的空值提交";
    clear.disabled = !!(field.disabled || field.readonly);
    clear.addEventListener("click", () => {
      const text = hasUploadButton && type !== "file"
        ? holder.querySelector('input[type="text"]') : null;
      if (text) {
        text.value = "";
        text.dispatchEvent(new Event("input", {bubbles: true}));
        return;
      }
      const next = field.multiple ? [] : "";
      render(next); onChange(next);
    });
    pick.addEventListener("click", () => chooseFiles(async () => {
      const result = await api().pick_files(
        `media_${prefix}_${field.name}`, !!field.multiple,
        filePickerExtensions(field.accept));
      if (result && result.ok && typeof rememberFileMimeHints === "function")
        rememberFileMimeHints(result.paths || [], result.file_types || []);
      return (result && result.ok) ? (result.paths || []) : [];
    }));
    if (typeof installFileDropTarget === "function") {
      installFileDropTarget(holder, { onDrop: files => chooseFiles(() =>
        materializeDroppedFiles(files, `media_${prefix}_${field.name}`,
          !!field.multiple, filePickerExtensions(field.accept))) });
    }
    holder.appendChild(pick); holder.appendChild(clear); holder.appendChild(list); wrap.appendChild(holder);
    if (field.help) {
      const help = document.createElement("small"); help.className = "hint"; help.textContent = field.help;
      wrap.appendChild(help);
    }
    return wrap;
  } else if (type === "select") {
    const select = attach(document.createElement("select"));
    select.multiple = !!field.multiple;
    const selected = new Set(Array.isArray(value) ? value.map(String) : [String(value ?? "")]);
    choices.forEach(option => {
      const item = document.createElement("option");
      item.value = String(option.value ?? "");
      item.textContent = option.label || option.value || "（空）";
      item.disabled = !!option.disabled;
      item.selected = selected.has(item.value);
      select.appendChild(item);
    });
    if (!field.multiple && !choices.some(x => selected.has(String(x.value ?? "")))) select.selectedIndex = -1;
    syncCmsVerifyValidity(select, field);
    wrap.appendChild(select);
  } else if (type === "radio" || type === "checkbox") {
    const group = document.createElement("div"); group.className = "category-choice-list";
    const selected = new Set(Array.isArray(value) ? value.map(String) : [String(value ?? "")]);
    (choices.length ? choices : [{value:"on",label:field.label || "启用"}]).forEach((option,index) => {
      const choice = document.createElement("label");
      const input = attach(document.createElement("input"));
      input.type = type; input.name = prefix + "_" + field.name;
      input.id = prefix + "_" + field.name + "_" + index;
      input.value = String(option.value ?? "on"); input.checked = selected.has(input.value);
      input.disabled = input.disabled || !!option.disabled;
      choice.appendChild(input);
      const text = document.createElement("span"); text.textContent = option.label || option.value || "选项";
      choice.appendChild(text); group.appendChild(choice);
    });
    wrap.appendChild(group); checkGroup();
  } else {
    const make = (item) => {
      const input = attach(document.createElement(type === "textarea" ? "textarea" : "input"));
      if (type !== "textarea") {
        const nativeTypes = ["number", "url", "email", "date", "time", "datetime-local",
          "color", "range", "month", "week", "tel", "search"];
        input.type = field.widget === "datetime" ? "datetime-local" :
          (["date","time"].includes(field.widget) ? field.widget :
          (nativeTypes.includes(type) ? type : "text"));
      }
      if (field.widget === "datetime") {
        input.step = field.step || "1";
        input.value = String(item ?? "").replace(" ", "T");
      } else input.value = String(item ?? "");
      // Native color/range controls normalize an empty serialized value to
      // their browser default during DOM assignment (#000000 for color and
      // the range midpoint/default step).  Keep the descriptor in sync with
      // that successful-control value even when the user never touches the
      // widget; otherwise the web form would submit the normalized default
      // while the desktop bridge would send the original empty string.
      if (!field.multiple && !String(item ?? "") &&
          (type === "color" || type === "range") && input.value) {
        field.value = input.value;
      }
      syncCmsVerifyValidity(input, field);
      return input;
    };
    if (field.multiple) {
      const rows = document.createElement("div");
      const addRow = (item) => {
        const row = document.createElement("div"); row.className = "row";
        row.appendChild(make(item));
        const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "删除此项";
        remove.disabled = !!(field.disabled || field.readonly);
        remove.addEventListener("click", () => { row.remove(); emit(); });
        row.appendChild(remove); rows.appendChild(row);
      };
      (Array.isArray(value) ? value : [value ?? ""]).forEach(addRow);
      wrap.appendChild(rows);
      const add = document.createElement("button"); add.type = "button"; add.textContent = "添加一项";
      add.disabled = !!(field.disabled || field.readonly);
      add.addEventListener("click", () => { addRow(""); emit(); }); wrap.appendChild(add);
    } else wrap.appendChild(make(value));
  }
  if (field.help) {
    const help = document.createElement("small"); help.className = "hint"; help.textContent = field.help;
    wrap.appendChild(help);
  }
  return wrap;
}

function validateCmsControls(root) {
  if (!root) return true;
  // A native form with novalidate bypasses constraint validation for this
  // submit.  Dynamic CMS sections are rendered into a div, so the descriptor
  // carries the same flag and the bridge mirrors it here.
  if (root.dataset && root.dataset.cmsNoValidate === "1") return true;
  for (const input of root.querySelectorAll("[data-cms-control]")) {
    if (!input.disabled && !input.checkValidity()) { input.reportValidity(); return false; }
  }
  return true;
}

function submitterEqual(a, b) {
  if (!a || !b) return false;
  const keys = ["name", "type", "value", "formaction", "formmethod",
    "formenctype", "formtarget", "formnovalidate"];
  return keys.every((key) => key === "formnovalidate"
    ? !!a[key] === !!b[key]
    : String(a[key] || "") === String(b[key] || ""));
}

function renderContentSubmitter(st, which) {
  const area = st && st[which];
  const select = $(which === "pub" ? "pubSubmitter" : "editSubmitter");
  const row = $(which === "pub" ? "pubSubmitterRow" : "editSubmitterRow");
  if (!area || !select || !row) return;
  const options = Array.isArray(area.submitterOptions) ? area.submitterOptions : [];
  row.hidden = !options.length;
  select.innerHTML = "";
  options.forEach((option, index) => {
    const item = document.createElement("option");
    item.value = String(index);
    const label = String(option.label || option.value || option.name || "提交").trim();
    const method = String(option.formmethod || "POST").toUpperCase();
    const action = String(option.formaction || "").trim();
    const nativeClick = option.requires_native_click ? " · 需原生网页点击" : "";
    item.textContent = `${label || "提交"}（${method}${action ? " · 覆盖地址" : ""}${nativeClick}）`;
    select.appendChild(item);
  });
  let index = options.findIndex((option) => submitterEqual(option, area.submitter));
  if (index < 0 && options.length === 1) index = 0;
  select.value = index >= 0 ? String(index) : "";
  select.disabled = !!st.busy;
  if (options.length > 1 && index < 0) {
    select.classList.add("needs-choice");
  } else {
    select.classList.remove("needs-choice");
  }
}

function renderPublishBackendFields(st) {
  const root = $("pubBackendFields"); if (!root) return;
  root.innerHTML = "";
  root.dataset.cmsNoValidate = (st.pub.fields || []).some(field => field.form_novalidate) ? "1" : "0";
  const mapped = new Set(Object.values(st.pub.mapping || {}).filter(Boolean));
  const excluded = new Set(["title", "content", "pics", "ico", "picstitle[]",
    "scode", "id", "mcode", "formcheck", "istop", "isrecommend", "isheadline"]);
  const fields = (st.pub.fields || []).filter((field) =>
    (field.mappable !== false || String(field.type || field.kind || "").toLowerCase() === "file") && !field.readonly &&
    !excluded.has(String(field.name || "")) && !mapped.has(String(field.name || "")));
  fields.forEach((field) => {
    const name = String(field.name || "");
    const value = Object.prototype.hasOwnProperty.call(st.pub.backendValues || {}, name)
      ? st.pub.backendValues[name] : field.value;
    root.appendChild(createCmsFieldControl(field, value, (next) => {
      st.pub.backendValues[name] = next; notePublishChanged(st);
    }, "pub_backend"));
  });
  $("pubBackendEmpty").hidden = !!fields.length;
  if (!fields.length) $("pubBackendEmpty").textContent = st.pub.cat
    ? "当前可设置字段都已由 HTML 映射或专用控件管理。" : "选择栏目后显示可设置字段。";
}

function renderEditBackendFields(st) {
  const root = $("editBackendFields"); if (!root) return;
  root.innerHTML = "";
  root.dataset.cmsNoValidate = (st.edit.fields || []).some(field => field.form_novalidate) ? "1" : "0";
  const mapped = new Set(Object.values(st.edit.mapping || {}).filter(Boolean));
  const fields = (st.edit.fields || []).filter((field) => {
    const type = String(field.type || field.kind || "").toLowerCase();
    return !field.readonly && !mapped.has(String(field.name || "")) &&
      (type === "file" || (field.mappable !== false && !["title", "content", "pics", "ico", "picstitle[]", "id", "mcode", "formcheck"].includes(String(field.name || ""))));
  });
  fields.forEach((field) => {
    const name = String(field.name || "");
    const value = Object.prototype.hasOwnProperty.call(st.edit.backendValues || {}, name)
      ? st.edit.backendValues[name] : field.value;
    root.appendChild(createCmsFieldControl(field, value, (next) => {
      st.edit.backendValues[name] = next; scheduleDraftSave(st, "edit");
    }, "edit_backend"));
  });
  $("editBackendEmpty").hidden = !!fields.length;
  if (!fields.length) $("editBackendEmpty").textContent = st.edit.artId
    ? "当前没有未映射的可直接设置字段。" : "选择文章后显示可设置字段。";
}

/* ══════════ Python 事件入口（按 tab_id 路由） ══════════ */
function onUploadMetadata(data) {
  const st = TABS.get(data && data.tab_id);
  const entry = data && data.entry;
  if (!st || !entry || typeof entry !== "object") return;
  const area = /缩略图|thumbnail/i.test(String(entry.label || ""))
    ? (data.task === "edit" ? st.edit : st.pub) :
    (data.task === "edit" ? st.edit : st.pub);
  if (!Array.isArray(area.uploadMetadata)) area.uploadMetadata = [];
  // A retry/cache event can repeat the same slot.  Replace that occurrence
  // instead of showing two contradictory server dimensions in the picker.
  const slot = entry.cache_slot != null ? String(entry.cache_slot) : "";
  const index = slot ? area.uploadMetadata.findIndex((item) =>
    String(item && item.cache_slot || "") === slot) : -1;
  if (index >= 0) area.uploadMetadata[index] = entry;
  else area.uploadMetadata.push(entry);
  if (/缩略图|thumbnail/i.test(String(entry.label || ""))) {
    area.thumbServerInfo = thumbnailServerSummary(area.uploadMetadata);
    area.thumbServerUrl = thumbnailServerUrl(area.uploadMetadata);
  }
  if (st.id === ACTIVE) renderActiveTab();
}

function uploadResultNotice(entries) {
  const list = Array.isArray(entries) ? entries : [];
  const parts = [];
  list.forEach((entry) => {
    if (!entry || typeof entry !== "object") return;
    const meta = entry.metadata && typeof entry.metadata === "object" ? entry.metadata : {};
    const data = meta.data && typeof meta.data === "object" && !Array.isArray(meta.data)
      ? meta.data : {};
    const read = (...keys) => {
      for (const key of keys) {
        const value = meta[key] ?? data[key];
        if (value !== undefined && value !== null && String(value) !== "") return value;
      }
      return "";
    };
    const label = String(entry.label || entry.filename || "文件");
    const uploadMode = String(entry.ueditor_upload_mode || meta.ueditor_upload_mode || "").trim();
    const transform = entry.client_transform && typeof entry.client_transform === "object"
      ? entry.client_transform
      : (meta.client_transform && typeof meta.client_transform === "object" ? meta.client_transform : {});
    const width = read("server_width", "width");
    const height = read("server_height", "height");
    const bytes = read("server_bytes", "bytes", "size");
    const mime = read("server_mime", "mime", "mimeType");
    const format = read("server_format", "format");
    const sha256 = read("server_sha256", "sha256");
    const clientBytes = read("client_bytes");
    const clientSha256 = read("client_sha256");
    const clientMime = read("client_mime");
    const serverFilename = read("server_filename", "filename", "fileName");
    const urlBasename = read("server_url_basename");
    const contentType = read("server_content_type");
    const contentLength = read("server_content_length");
    const etag = read("server_etag", "etag");
    const lastModified = read("server_last_modified", "last_modified");
    const rawChangeFields = read("server_change_fields");
    const changeFields = Array.isArray(rawChangeFields)
      ? rawChangeFields.join(",") : rawChangeFields;
    const serverUrl = String(entry.url || "").trim();
    const cacheControl = read("server_cache_control");
    const age = read("server_age");
    const cache = cacheControl || age
      ? `缓存 ${cacheControl || "默认"}${age ? `，Age ${age}` : ""}` : "";
    const contentEncoding = read("server_content_encoding");
    const encoding = contentEncoding ? `编码 ${contentEncoding}` : "";
    const detail = [];
    if (uploadMode) detail.push(`网页上传入口 ${uploadMode}`);
    if (transform.kind === "ueditor-image-compress") {
      const transformParts = [];
      if (transform.border) transformParts.push(`边界 ${transform.border}px`);
      if (transform.quality) transformParts.push(`质量 ${transform.quality}`);
      if (transform.preserve_headers) transformParts.push("保留头部");
      detail.push(`客户端压缩${transformParts.length ? `（${transformParts.join("，")}）` : ""}`);
    }
    if (width && height) detail.push(String(width) + "×" + String(height) + " px");
    if (bytes !== undefined && bytes !== null && String(bytes) !== "") detail.push(String(bytes) + " bytes");
    if (mime) detail.push(String(mime));
    if (contentType && String(contentType).toLowerCase() !== String(mime || "").toLowerCase())
      detail.push(`响应 Content-Type ${String(contentType)}`);
    if (format) detail.push(`格式 ${String(format)}`);
    if (serverFilename) detail.push(`服务器文件名 ${String(serverFilename)}`);
    else if (urlBasename) detail.push(`服务器 URL 文件名 ${String(urlBasename)}`);
    if (sha256) detail.push(`SHA-256 ${String(sha256)}`);
    if (clientBytes !== "" && clientBytes !== undefined && clientBytes !== null &&
        bytes !== "" && bytes !== undefined && bytes !== null &&
        clientBytes !== bytes && String(clientBytes) !== String(bytes))
      detail.push(`客户端→服务器 ${String(clientBytes)}→${String(bytes)} bytes`);
    if (clientSha256 && sha256 && String(clientSha256) !== String(sha256))
      detail.push("客户端/服务器 SHA-256 不同");
    if (clientMime && mime && String(clientMime).toLowerCase() !== String(mime).toLowerCase())
      detail.push(`客户端 MIME ${String(clientMime)}`);
    if (contentLength && String(contentLength) !== String(bytes || ""))
      detail.push(`响应 Content-Length ${String(contentLength)}`);
    if (etag) detail.push(`ETag ${String(etag)}`);
    if (lastModified) detail.push(`Last-Modified ${String(lastModified)}`);
    if (changeFields) detail.push(`变化字段 ${String(changeFields)}`);
    if (cache) detail.push(cache);
    if (encoding) detail.push(encoding);
    // Every upload surface returns the final server object path.  Keep it
    // visible beside dimensions/hash evidence so attachments and media are
    // inspectable just like a browser's upload callback, while refusing
    // non-network schemes that could be a local-path or script injection.
    if (serverUrl && !/^(?:data|blob|file|javascript):/i.test(serverUrl))
      detail.push(`服务器对象 ${serverUrl.slice(0, 512)}`);
    const changed = read("server_changed");
    if (changed === true || String(changed).toLowerCase() === "true")
      detail.push("服务器已处理");
    if (detail.length) parts.push(label + "：" + detail.join("，"));
  });
  return parts.length ? "；上传回读：" + parts.join("；") : "";
}

window.onPyEvent = (evt) => {
  const { event, data } = evt;
  const tabId = data && data.tab_id;
  if (event === "log") { log(data.msg, tabId); return; }
  if (event === "upload_metadata") { onUploadMetadata(data); return; }
  if (event === "progress") {
    const st = TABS.get(tabId);
    if (st) {
      // 批量队列内部每篇仍由 publish 任务执行，但使用者看到的是同一个队列任务。
      st.taskKind = (st.batch && st.batch.running) ? "batch" : (data.task || st.taskKind || "publish");
      st.taskStarted = true;
      st._prog = { done: data.done, total: data.total,
        bytesDone: data.bytes_done || 0, bytesTotal: data.bytes_total || 0,
        text: `${data.done}/${data.total}  ${data.current || ""}`,
        task: (st.batch && st.batch.running) ? "batch" : (data.task || st.taskKind) };
    }
    if (tabId === ACTIVE) {
      const id = (data.task === "edit" || st && st.taskKind === "link_check_edit") ? "editProg"
      : (["query", "product_sync", "product_bulk_modify"].includes(data.task) ? "qProg"
        : (data.task === "message_bulk" ? "msgProg"
        : (data.task === "diagnostic" ? "diagProg" : "pubProg")));
      setProg(id, data.done, data.total, `${data.done}/${data.total}  ${data.current || ""}`,
        data.bytes_done, data.bytes_total);
      if (st) renderTaskControls(st);
    }
    return;
  }
  if (event === "publish_done") return onPublishDone(data);
  if (event === "edit_done") return onEditDone(data);
  if (event === "pull_done") return onPullDone(data);
  if (event === "product_sync_done") return onProductSyncDone(data);
  if (event === "product_bulk_modify_done") return onProductBulkModifyDone(data);
  if (event === "link_check_done") return onLinkCheckDone(data);
  if (event === "diagnostic_done") return onDiagnosticDone(data);
  if (event === "message_bulk_done") return onMessageBulkDone(data);
};

/* ══════════ 登录（针对当前标签） ══════════ */
function syncLoginNetworkControls(st) {
  const mode = (st && ["direct", "system", "custom"].includes(st.networkMode))
    ? st.networkMode : "direct";
  const select = $("loginNetworkMode");
  const proxy = $("loginProxyUrl");
  if (select) select.value = mode;
  if (proxy) {
    proxy.value = st ? String(st.proxyUrl || "") : "";
    proxy.hidden = mode !== "custom";
  }
}

function networkSettingsFromUi(st) {
  const mode = String($("loginNetworkMode")?.value || "direct");
  const proxy = String($("loginProxyUrl")?.value || "").trim();
  if (st) {
    st.networkMode = ["direct", "system", "custom"].includes(mode) ? mode : "direct";
    st.proxyUrl = st.networkMode === "custom" ? proxy : "";
  }
  return { mode: st ? st.networkMode : mode, proxyUrl: st ? st.proxyUrl : proxy };
}

async function reprepareForNetworkChange() {
  const st = activeState();
  if (!st) return;
  const settings = networkSettingsFromUi(st);
  syncLoginNetworkControls(st);
  const url = $("loginUrl").value.trim();
  if (!url) {
    setMsg("loginMsg", settings.mode === "custom" && !settings.proxyUrl
      ? "请填写自定义代理地址" : "网络方式已更新，请填写后台地址", "");
    return;
  }
  if (settings.mode === "custom" && !settings.proxyUrl) {
    $("capRow").hidden = true;
    setMsg("loginMsg", "请填写自定义代理地址", "bad");
    return;
  }
  const seq = ++_loginSeq;
  $("capRow").hidden = true;
  setMsg("loginMsg", "网络方式已改变，正在重新连接站点…");
  await prepareLogin(url, false, false, !!st.insecure, seq);
}

/* 后台地址历史下拉：每次连接/登录后重建，新站点无需重启就能出现 */
function renderSiteList(urls) {
  if (!Array.isArray(urls)) return;
  const dl = $("siteList");
  const history = $("loginHistory");
  const keep = history.value;
  dl.innerHTML = "";
  history.innerHTML = '<option value="">— 选择已保存的后台（最多30个）—</option>';
  urls.forEach((u) => {
    const opt = document.createElement("option");
    opt.value = u;
    dl.appendChild(opt);
    const item = document.createElement("option");
    item.value = u;
    item.textContent = u;
    history.appendChild(item);
  });
  history.value = urls.includes(keep) ? keep : "";
}

async function fillSavedCredentials(url) {
  if (!url) return false;
  const seq = ++_credentialSeq;
  const tabId = ACTIVE;
  const r = await api().saved_credentials(url);
  if (!r || !r.ok || seq !== _credentialSeq || ACTIVE !== tabId ||
      $("loginUrl").value.trim() !== url) return false;
  $("loginUser").value = r.user || "admin";
  $("loginPass").value = r.password || "";
  return true;
}

async function init() {
  const info = await api().app_info();
  _appInfo = info;
  $("ver").textContent = info.version;
  document.title = info.title;
  renderSiteList(info.urls || []);
  if (info.last_url) LAST_URL = info.last_url;
  $("updateBadge").hidden = !(info.update && info.update.update_available);
  updateEmptyState();
  log(`就绪 —— ${info.title}（构建 ${info.build}）`, ACTIVE);
  await autoRestoreSites();
}

/* 启动自动恢复：把会话仍有效的站点直接开成标签，无需再登录。
   各标签后端会话彻底隔离，所以可以并行探测；
   只保留真正免登录成功的，会话已过期的标签直接关掉不碍事。*/
async function autoRestoreSites() {
  let list;
  try {
    const r = await api().restorable_sites();
    list = (r && r.ok) ? (r.sites || []) : [];
  } catch (_) { return; }
  if (!list.length) return;

  log(`正在尝试恢复 ${list.length} 个已登录站点…`, ACTIVE);
  const created = list.map((site) => {
    const st = newTabState();
    st.url = site.admin_url;
    st.title = shortLabel(site.admin_url);
    st.restoring = true;
    TABS.set(st.id, st);
    return { st, site };
  });
  updateEmptyState();
  renderTabStrip();

  const results = await Promise.all(created.map(async ({ st, site }) => {
    try {
      // 启动恢复只验证持久化会话，不应把多个地址并发重排到历史最前，
      // 更不应造成“仅启动软件就改写配置”的副作用。
      const r = await api().prepare_login(
        st.id, site.admin_url, false, false, false);
      // is_login_page=false 说明拿到的是后台页 → cookie 仍有效
      return { st, site, ok: !!(r && r.ok && r.is_login_page === false), r };
    } catch (_) {
      return { st, site, ok: false };
    }
  }));

  for (const { st, site, ok, r } of results) {
    st.restoring = false;
    if (ok) {
      if (r && ["direct", "system", "custom"].includes(r.network_mode))
        st.networkMode = r.network_mode;
      if (r && typeof r.proxy_url === "string") st.proxyUrl = r.proxy_url;
      if (r && r.insecure) st.insecure = true;
      st.loggedIn = true;
      restoreMessageReview(st);
      beginDraftRecovery(st);
      st.title = shortLabel(site.admin_url) + (st.insecure ? " ⚠" : "");
      st.needLogin = false;
      loadPendingOperationsFor(st);
      if (r && Array.isArray(r.urls)) renderSiteList(r.urls);
    } else {
      // 会话已过期：不留空壳标签，用户需要时自己新建
      TABS.delete(st.id);
      try { await api().close_tab(st.id); } catch (_) {}
    }
  }

  const alive = Array.from(TABS.keys());
  ACTIVE = alive.length ? alive[0] : "";
  updateEmptyState();
  renderTabStrip();
  if (ACTIVE) {
    renderActiveTab();
    const okCount = results.filter((x) => x.ok).length;
    log(`已自动恢复 ${okCount} 个站点（共尝试 ${results.length} 个）`, ACTIVE);
    await Promise.allSettled(Array.from(TABS.values()).map(loadProductCache));
    // 每个恢复成功的标签都要加载栏目（会话隔离，可并行）；
    // 只给活动标签加的话，切到其他标签栏目会是空的。
    await Promise.allSettled(Array.from(TABS.values()).map(loadCategoriesFor));
    // 每站点单独询问恢复，统一对话框队列会避免多个弹窗互相覆盖。
    Array.from(TABS.values()).forEach((state) => offerDraftRecovery(state));
  } else {
    log("没有可免登录的站点，请点「＋」新建标签登录", ACTIVE);
  }
}

function setCaptcha(r) {
  const img = $("capImg");
  if (r.captcha) {
    img.src = `data:${r.captcha_mime || "image/png"};base64,` + r.captcha;
    img.alt = "验证码";
  } else {
    img.src = "data:image/svg+xml;utf8," + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="38">' +
      '<rect width="120" height="38" rx="6" fill="#EEF2FF"/>' +
      '<text x="60" y="24" text-anchor="middle" font-size="12" fill="#6366F1">点击重试</text></svg>');
    img.alt = "验证码加载失败，点击重试";
  }
}

async function prepareLogin(url, silent, force, insecure, requestSeq) {
  const st = activeState();
  if (!st) return null;
  const seq = requestSeq == null ? ++_loginSeq : requestSeq;
  setMsg("loginMsg", "正在连接站点…");
  let r;
  try {
    const network = networkSettingsFromUi(st);
    r = await api().prepare_login(st.id, url, !!force, !!insecure, true,
      network.mode, network.proxyUrl);
  } catch (e) {
    if (loginRequestIsCurrent(st, url, seq))
      setMsg("loginMsg", `连接站点失败：${String(e)}`, "bad");
    return null;
  }
  if (!loginRequestIsCurrent(st, url, seq)) return null;
  if (!r.ok) {
    // HTTPS 证书错误：不静默降级，明确向用户确认后再重试
    if (r.ssl_error && !insecure) {
      const go = await confirmDialog("", {
        title: "HTTPS 证书校验失败", kind: "warn",
        okText: "忽略并继续", cancelText: "取消连接",
        lines: [
          "该站点证书校验失败（常见原因：证书已过期）。",
        ].concat(r.detail ? [r.detail] : []).concat([
          "⚠ 忽略后本标签不再校验证书，仅建议对你自己的站点使用。",
          "根治办法：到服务器续签/更新 SSL 证书。",
        ]),
      });
      if (!loginRequestIsCurrent(st, url, seq)) return null;
      if (!go) { setMsg("loginMsg", r.msg + "已取消连接。", "bad"); return null; }
      const again = await prepareLogin(url, silent, force, true, seq);
      if (!loginRequestIsCurrent(st, url, seq)) return null;
      const remember = !!again && await confirmDialog("以后连接该站不再弹询问。", {
        title: "记住该站点？", kind: "confirm", okText: "记住", cancelText: "不记住" });
      if (!loginRequestIsCurrent(st, url, seq)) return null;
      if (remember) {
        await api().remember_insecure_site(st.id, true);
      }
      return again;
    }
    setMsg("loginMsg", r.msg, "bad");
    return null;
  }
  if (["direct", "system", "custom"].includes(r.network_mode))
    st.networkMode = r.network_mode;
  if (typeof r.proxy_url === "string") st.proxyUrl = r.proxy_url;
  syncLoginNetworkControls(st);
  if (r.insecure) {
    st.insecure = true;
    log("⚠ 本标签已忽略 HTTPS 证书校验（站点证书异常）", st.id);
  }
  renderSiteList(r.urls);          // 地址有效已计入历史，同步到下拉
  if (!r.is_login_page) {
    $("loginMask").hidden = true;
    markLoggedIn(st, url, "会话已恢复，无需重新登录");
    return r;
  }
  if (!silent) $("loginMask").hidden = false;
  // 该站存过账密则回填（密码由后端 DPAPI 解密后下发）
  if (r.saved_user) $("loginUser").value = r.saved_user;
  else if (!$("loginUser").value) $("loginUser").value = "admin";
  if (r.saved_pass && !$("loginPass").value) $("loginPass").value = r.saved_pass;
  $("capRow").hidden = !r.has_captcha;
  if (r.has_captcha) setCaptcha(r);
  if (r.has_captcha && !r.captcha) {
    setMsg("loginMsg", `站点已连接，但验证码图片获取失败：${r.captcha_error || "请点击图片或“获取验证码”重试"}`, "bad");
  } else {
    setMsg("loginMsg", r.has_captcha ? "验证码已获取，请填写后登录" : "该站点无验证码，直接登录",
           r.has_captcha ? "ok" : "");
  }
  return r;
}

async function doLogin() {
  const st = activeState();
  if (!st) return;
  const url = $("loginUrl").value.trim();
  if (!url) return setMsg("loginMsg", "请填写后台地址", "bad");
  const seq = ++_loginSeq;
  const username = $("loginUser").value;
  const password = $("loginPass").value;
  const captcha = $("loginCode").value;
  $("btnLogin").disabled = true;
  try {
    setMsg("loginMsg", "登录中…");
    const r = await api().login(st.id, username, password, captcha, url);
    if (!loginRequestIsCurrent(st, url, seq)) return;
    if (!r.ok) {
      if (r.native_url && typeof openNativeLogin === "function") {
        setMsg("loginMsg", r.native_reason || r.msg || "该登录流程需要原生网页点击，正在打开…", "review");
        await openNativeLogin();
        return;
      }
      setMsg("loginMsg", r.msg, "bad");
      $("loginCode").value = "";
      if (r.captcha) { $("capRow").hidden = false; setCaptcha(r); }
      else if (r.need_captcha !== false) { await refreshCaptcha({loginFailure: r.msg}); }
      $("loginCode").focus();
      return;
    }
    $("loginMask").hidden = true;
    $("loginPass").value = ""; $("loginCode").value = "";
    renderSiteList(r.urls);        // 登录成功的地址置顶，同步到下拉
    markLoggedIn(st, url, r.msg);
  } finally {
    $("btnLogin").disabled = false;
  }
}

async function openNativeLogin() {
  const st = activeState();
  const url = String($("loginUrl").value || "").trim();
  if (!st || !url) return setMsg("loginMsg", "请先填写后台地址", "bad");
  const seq = ++_loginSeq;
  setMsg("loginMsg", "正在准备原生网页登录…");
  // Read the real login page first so the bridge has a verified same-origin
  // admin URL and the requests client never receives cookies from a guessed
  // host.  SSO/dynamic pages may still fail this static probe; in that case
  // the user can use the ordinary browser button from the error message.
  const prepared = await prepareLogin(url, true, false, false, seq);
  if (!loginRequestIsCurrent(st, url, seq)) return;
  let state = null;
  try { state = await api().get_site_state(st.id); } catch (_) {}
  const target = String((state && state.admin_url) || url);
  // If the static requests probe cannot parse an SSO/challenge page, still
  // offer the explicitly requested native login route.  The backend validates
  // the sanitized URL and clears a previous site's session on a site switch.
  const result = prepared
    ? await api().open_authenticated_url(st.id, target, "原生网页登录")
    : (typeof api().open_native_login === "function"
      ? await api().open_native_login(st.id, url, "原生网页登录")
      : null);
  if (result && result.ok && result.opened) {
    setMsg("loginMsg", "已打开原生网页；完成 SSO/验证码登录后点击“同步网页登录”。", "ok");
  } else {
    setMsg("loginMsg", (result && result.msg) || "无法打开原生网页登录", "bad");
  }
}

async function syncNativeLogin() {
  const st = activeState();
  const url = String($("loginUrl").value || "").trim();
  if (!st || !url) return setMsg("loginMsg", "请先填写后台地址", "bad");
  setMsg("loginMsg", "正在同步原生网页会话…");
  try {
    if (typeof api().sync_native_session === "function")
      await api().sync_native_session(st.id);
  } catch (_) { /* prepare_login below remains the authoritative probe */ }
  const seq = ++_loginSeq;
  const result = await prepareLogin(url, false, false, false, seq);
  if (!loginRequestIsCurrent(st, url, seq)) return;
  if (result && !result.is_login_page) {
    setMsg("loginMsg", "网页登录会话已同步，桌面功能已解锁。", "ok");
  } else if (result) {
    setMsg("loginMsg", "原生网页仍未确认登录，请完成登录后再同步。", "bad");
  }
}

async function refreshCaptcha({loginFailure = ""} = {}) {
  const st = activeState();
  if (!st) return;
  const url = $("loginUrl").value.trim();
  if (!url) {
    setMsg("loginMsg", "请先填写后台地址", "bad");
    $("loginUrl").focus();
    return;
  }
  const seq = ++_loginSeq;
  const btn = $("btnFetchCap");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "获取中…";
  try {
    const state = await api().get_site_state(st.id);
    if (!loginRequestIsCurrent(st, url, seq)) return;
    if (state.ok && state.login_ready && state.prepared_url === url) {
      const r = await api().reload_captcha(st.id);
      if (!loginRequestIsCurrent(st, url, seq)) return;
      if (r.ok) { $("capRow").hidden = false; setCaptcha(r); setMsg("loginMsg", "验证码已刷新", "ok"); }
      else setMsg("loginMsg", r.msg, "bad");
      return;
    }
    const p = await prepareLogin(url, true, false, false, seq);
    if (!loginRequestIsCurrent(st, url, seq)) return;
    if (p) { $("capRow").hidden = !p.has_captcha; if (p.has_captcha) setCaptcha(p); }
  } catch (e) {
    if (loginRequestIsCurrent(st, url, seq))
      setMsg("loginMsg", `获取验证码失败：${String(e)}`, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    if (loginFailure && loginRequestIsCurrent(st, url, seq)) {
      const refreshMessage = $("loginMsg").textContent;
      setMsg("loginMsg", `${loginFailure}；${refreshMessage}`, "bad");
    }
  }
}

function markLoggedIn(st, url, msg) {
  st.loggedIn = true;
  beginDraftRecovery(st);
  st.url = url;
  restoreMessageReview(st);
  restoreProductEdits(st);
  restoreMutationReview(st);
  st.title = shortLabel(url) + (st.insecure ? " ⚠" : "");
  st.needLogin = false;
  LAST_URL = url;
  renderTabStrip();
  renderActiveTab();
  log(msg, st.id);
  loadPendingOperationsFor(st);
  loadCategoriesFor(st).then(() => offerDraftRecovery(st))
    .catch((e) => {
      log(`栏目加载或草稿恢复异常：${e}`, st.id);
      if (!st._draftChecked) offerDraftRecovery(st);
    }); // 栏目请求失败也不能永久跳过草稿恢复
  loadProductCache(st);
}

async function loadPendingOperationsFor(st) {
  if (!st || !st.loggedIn) return;
  try {
    const result = await api().load_pending_operations(st.id);
    if (!isLiveState(st)) return;
    st.diagnostic.pendingOperations = (result && result.ok) ? (result.operations || []) : [];
    if (st.diagnostic.pendingOperations.length) {
      log(`发现 ${st.diagnostic.pendingOperations.length} 个上次未取得最终结果的操作；已阻止自动重发，请在后台核对后处理。`, st.id);
      st.diagnostic.msg = "存在待核对操作：请打开后台确认结果后，再在日志中处理。";
      if (st.id === ACTIVE) renderActiveTab();
    }
  } catch (_) { /* 旧版本桥接没有该接口时不影响登录 */ }
}

function messageReviewStorageKey(st) {
  return `pboot.message-review:${String(st && st.url || "").trim().toLowerCase()}`;
}

// Product quick edits are deliberately kept separate from content drafts.
// A browser tab may be closed after a request times out, and losing the
// "result may already have been saved" marker would make the next click a
// blind duplicate write.  Persist only the user's pending values and the
// review flag, never credentials, cookies, or the full product cache.
function productEditStorageKey(st) {
  return `pboot.product-edits:${String(st && st.url || "").trim().toLowerCase()}`;
}

function restoreProductEdits(st) {
  if (!st || typeof localStorage === "undefined") return;
  try {
    const raw = localStorage.getItem(productEditStorageKey(st));
    const value = raw ? JSON.parse(raw) : {};
    st.query.edits = value && typeof value === "object" && !Array.isArray(value)
      ? value : {};
  } catch (_) {
    st.query.edits = st.query.edits || {};
  }
}

function persistProductEdits(st) {
  if (!st || typeof localStorage === "undefined") return;
  try {
    const source = st.query && st.query.edits || {};
    const safe = {};
    Object.keys(source).forEach((id) => {
      const item = source[id];
      if (!item || typeof item !== "object") return;
      const next = {};
      ["model", "price", "modelTouched", "priceTouched", "requiresReview"].forEach((key) => {
        if (Object.prototype.hasOwnProperty.call(item, key)) next[key] = item[key];
      });
      if (Object.keys(next).length) safe[String(id)] = next;
    });
    if (Object.keys(safe).length) localStorage.setItem(productEditStorageKey(st), JSON.stringify(safe));
    else localStorage.removeItem(productEditStorageKey(st));
  } catch (_) { /* embedded WebView storage may be disabled */ }
}

// Category and Slide writes are short API calls rather than background
// publish jobs, but a timeout can still happen after the server has accepted
// the request.  Keep one small per-site marker so a restart cannot turn that
// unknown result into an unguarded duplicate save.
function mutationReviewStorageKey(st) {
  return `pboot.mutation-review:${String(st && st.url || "").trim().toLowerCase()}`;
}

function restoreMutationReview(st) {
  if (!st || typeof localStorage === "undefined") return;
  try {
    const raw = localStorage.getItem(mutationReviewStorageKey(st));
    const value = raw ? JSON.parse(raw) : {};
    st.mutationReview = value && typeof value === "object" && !Array.isArray(value)
      ? value : {};
  } catch (_) { st.mutationReview = st.mutationReview || {}; }
}

function persistMutationReview(st) {
  if (!st || typeof localStorage === "undefined") return;
  try {
    const value = st.mutationReview && typeof st.mutationReview === "object"
      ? st.mutationReview : {};
    if (Object.keys(value).length) localStorage.setItem(mutationReviewStorageKey(st), JSON.stringify(value));
    else localStorage.removeItem(mutationReviewStorageKey(st));
  } catch (_) { /* embedded WebView storage may be disabled */ }
}

function mutationReviewKey(kind, id) {
  return `${String(kind || "write")}:${String(id || "new")}`;
}

function resultNeedsMutationReview(result, error) {
  if (result && isWriteReview(result)) return true;
  const text = String((result && result.msg) || error || "");
  return /结果未知|请求未取得响应|连接|超时|断开|未收到结果|提交后任务中断/i.test(text);
}

async function confirmMutationReview(st, key, label) {
  if (!st || !st.mutationReview || !st.mutationReview[key]) return true;
  const item = st.mutationReview[key] || {};
  const yes = await confirmDialog("", {
    title: `${label}上次结果待核对`, kind: "warn",
    okText: "我已核对，继续", cancelText: "取消",
    lines: [item.message || "上次请求可能已经保存，请先在后台核对。继续可能造成重复提交。"],
  });
  if (!yes) return false;
  delete st.mutationReview[key];
  persistMutationReview(st);
  return true;
}

async function confirmGetWrite(form, label) {
  if (!form || String(form.method || "post").toLowerCase() !== "get") return true;
  return await confirmDialog("", {
    title: "网页表单使用 GET 保存", kind: "warn",
    okText: "确认发送", cancelText: "取消", lines: [
      `当前${label || "后台"}保存表单使用 GET 方法。`,
      "软件将按网页声明的查询参数顺序发送，并在保存后重新读取字段核对。",
      "请确认这是后台提供的保存按钮，而不是查询或筛选表单。",
    ],
  });
}

function setMutationReview(st, key, message) {
  if (!st || !key) return;
  st.mutationReview = st.mutationReview || {};
  st.mutationReview[key] = {message: String(message || "上次请求结果待核对"), at: Date.now()};
  persistMutationReview(st);
}

function clearMutationReview(st, key) {
  if (!st || !st.mutationReview || !key) return;
  delete st.mutationReview[key];
  persistMutationReview(st);
}

function restoreMessageReview(st) {
  if (!st || typeof localStorage === "undefined") return;
  try {
    const raw = localStorage.getItem(messageReviewStorageKey(st));
    const value = raw ? JSON.parse(raw) : {};
    st.msgMeta.review = value && typeof value === "object" ? value : {};
  } catch (_) { st.msgMeta.review = st.msgMeta.review || {}; }
}

function persistMessageReview(st) {
  if (!st || typeof localStorage === "undefined") return;
  try {
    const review = st.msgMeta && st.msgMeta.review || {};
    if (Object.keys(review).length) localStorage.setItem(messageReviewStorageKey(st), JSON.stringify(review));
    else localStorage.removeItem(messageReviewStorageKey(st));
  } catch (_) { /* storage may be disabled by the embedded browser */ }
}

function setMessageReview(st, messageId, pending) {
  if (!st || !messageId) return;
  st.msgMeta.review = st.msgMeta.review || {};
  if (pending) st.msgMeta.review[String(messageId)] = true;
  else delete st.msgMeta.review[String(messageId)];
  persistMessageReview(st);
}

function signingSummary(signing) {
  signing = signing || {};
  if (signing.signed) return `✓ 当前 EXE 已通过 Authenticode 验证（${signing.method || "Windows"}）`;
  return signing.message || "当前 EXE 未签名；构建脚本已支持证书指纹或 PFX 签名，正式分发前需配置证书。";
}

function renderAbout(info) {
  const app = info.app || {};
  $("aboutVersion").textContent = app.display_version || (_appInfo && _appInfo.version) || "—";
  $("aboutBuild").textContent = `构建日期 ${app.build_date || (_appInfo && _appInfo.build) || "—"}`;
  const update = info.update || {};
  $("aboutStatus").textContent = update.update_available
    ? `发现新版本 ${update.latest_version}，请在确认发布来源与完整性后手动升级。`
    : (update.configured ? (update.last_status === "ok" ? "当前已是最新版本。" : "尚未检查更新。")
      : "当前未配置正式更新清单地址；软件不会自动联网、下载或安装。 ");
  $("aboutStatus").className = "msg" + (update.update_available ? " bad" : "");
  $("aboutSigning").textContent = signingSummary(info.signing);
  const changelog = (info.changelog || []).map((release) =>
    `${release.version} · ${release.date || ""}  ${release.title || ""}\n` +
    (release.items || []).map((item) => `  • ${item}`).join("\n")).join("\n\n");
  $("aboutChangelog").textContent = changelog || "暂无更新日志";
}

async function openAbout() {
  $("aboutMask").hidden = false;
  $("aboutStatus").textContent = "正在读取版本与签名状态…";
  const info = await api().about_info();
  if (!info || !info.ok) {
    $("aboutStatus").textContent = (info && info.msg) || "版本信息读取失败";
    $("aboutStatus").className = "msg bad";
    return;
  }
  renderAbout(info);
}

async function checkForUpdates() {
  const button = $("btnCheckUpdate"), old = button.textContent;
  button.disabled = true; button.textContent = "检查中…";
  $("aboutStatus").textContent = "正在读取受限的 HTTPS 更新清单（不会下载或安装）…";
  try {
    const result = await api().check_for_updates();
    if (!result || !result.ok) throw new Error((result && result.msg) || "检查失败");
    $("updateBadge").hidden = !result.update_available;
    const messages = {
      not_configured: "未配置正式更新源；本次没有发起网络请求。",
      offline: result.message || "更新服务器暂时无法访问，请稍后再试。",
      invalid: result.message || "更新清单未通过安全校验，已拒绝使用。",
      ok: result.update_available ? `发现新版本 ${result.latest_version}。软件不会自动下载或安装。` : "当前已是最新版本。",
    };
    $("aboutStatus").textContent = messages[result.status] || result.message || "检查完成";
    $("aboutStatus").className = "msg" + (result.status === "invalid" ? " bad" : (result.status === "ok" ? " ok" : ""));
  } catch (e) {
    $("aboutStatus").textContent = `检查更新失败：${e}`;
    $("aboutStatus").className = "msg bad";
  } finally { button.disabled = false; button.textContent = old; }
}

async function createUpdateBackup() {
  const button = $("btnUpdateBackup"), old = button.textContent;
  button.disabled = true; button.textContent = "备份中…";
  try {
    const result = await api().create_update_backup();
    if (!result || !result.ok) throw new Error((result && result.msg) || "备份失败");
    _lastBackupPath = result.path || "";
    $("btnOpenBackup").hidden = !_lastBackupPath;
    $("aboutStatus").textContent = `✓ 升级前备份已完成：${result.files.length} 个文件，SHA-256 ${result.sha256.slice(0, 12)}…`;
    $("aboutStatus").className = "msg ok";
  } catch (e) {
    $("aboutStatus").textContent = `备份失败：${e}`;
    $("aboutStatus").className = "msg bad";
  } finally { button.disabled = false; button.textContent = old; }
}

async function clearAllCache() {
  const st = activeState();
  if (!st || !st.loggedIn || st.busy) return;
  const confirmed = await confirmDialog(
    "将清理当前已登录网站的全部 PbootCMS 运行缓存（包括编译、配置、升级和图片缓存）。\n不会删除网站内容、软件配置、登录信息、草稿或本地产品库；清理后首次访问页面可能稍慢。",
    { title: "清理所有缓存", kind: "warn", okText: "确认清理", cancelText: "取消" });
  if (!confirmed) return;
  const op = beginOperation(st, "cache_clear");
  if (!op) return;
  let success = "";
  try {
    const result = await api().clear_all_cache(st.id);
    if (!operationIsCurrent(st, op)) return;
    if (!result || !result.ok) {
      if (result && result.requires_review) {
        const message = result.msg || "清理缓存结果待核对，请先到后台确认；软件不会自动重发";
        setMutationReview(st, mutationReviewKey("cache_clear", "site"), message);
        log(message, st.id);
        await alertDialog(message, { title: "缓存清理结果待核对", kind: "warn" });
        return;
      }
      throw new Error((result && result.msg) || "清理缓存失败");
    }
    success = result.msg || "当前网站后台的所有缓存已清理";
    log(success, st.id);
  } catch (error) {
    if (operationIsCurrent(st, op)) await alertDialog(`清理缓存失败：${error}`, {
      title: "未能清理缓存", kind: "error" });
  } finally {
    if (operationIsCurrent(st, op)) finishOperation(st, op);
  }
  if (success && isLiveState(st)) await alertDialog(success, {
    title: "缓存清理完成", kind: "success" });
}

function clearSiteUiState(st) {
  const pubPrefs = { width: st.pub.width, insertStrategy: st.pub.insertStrategy || "top",
    carouselSize: "original", carouselW: st.pub.carouselW, carouselH: st.pub.carouselH };
  const editPrefs = { cover: st.edit.cover, insertStrategy: st.edit.insertStrategy || "before_h2",
    carouselSize: "original", carouselW: st.edit.carouselW, carouselH: st.edit.carouselH,
    carouselMode: st.edit.carouselMode };
  Object.assign(st.pub, {
    html: "", htmlReady: false, htmlLoading: false, htmlInfo: "", htmlInfoClass: "",
    cat: "", catLoading: false, catSearch: "", catOpen: {},
    mapping: {}, overrides: {}, backendValues: {}, fields: [], parsedFields: [], inlineCount: 0, remoteCount: 0, mediaCount: 0,
    submitter: null, submitterOptions: [],
    manualImages: [], thumbPath: "", thumbUrl: "", ico: "none", rememberThumb: false, carouselImages: [],
    top: false, rec: false, head: false, flagChanges: {}, checkLinks: false,
    uploadMetadata: [], thumbServerInfo: "", thumbServerUrl: "",
    retryable: false, preflightCache: null,
    msg: "", msgClass: "",
  }, pubPrefs);
  Object.assign(st.edit, {
    cat: "", catSearch: "", catOpen: {}, articles: [], articlesLoading: false,
    artId: "", html: "",
    htmlReady: false, htmlLoading: false, mapping: {}, overrides: {}, fields: [], parsedFields: [],
    submitter: null, submitterOptions: [],
    formLoading: false, formReady: false, inlineCount: 0, remoteCount: 0, mediaCount: 0, filter: "", linkReport: null,
    contentImages: [], contentHash: "", imageReplacements: {},
    thumbPath: "", thumbUrl: "", ico: "none", carouselImages: [],
    uploadMetadata: [], thumbServerInfo: "", thumbServerUrl: "", top: false, rec: false,
    head: false, flagChanges: {}, refreshDate: false, checkLinks: false, msg: "", msgClass: "",
  }, editPrefs);
  st.batch = { items: [], running: false, paused: false, pauseRequested: false,
    activeIndex: -1, editingIndex: -1, applyVerified: false,
    skipUnverified: false, stopRequested: false,
    msg: "", _resolve: null };
  st.query = { products: [], cacheFallbackProducts: [], cacheFallbackHealth: null, cacheLoaded: false, filter: "", healthFilter: "all", mcode: "", models: [], modelsLoading: false,
    sort: "id_desc", page: 1, pageSize: 100, selected: {}, edits: {}, health: null,
    remoteProducts: null, remotePage: 1, remoteHasNext: false, remoteKeyword: "", remoteLoading: false,
    remoteAutoStarted: false,
    continueOnError: false, msg: "", msgClass: "", advancedLoading: false };
  st.mutationReview = {};
  st.category = { filter: "", selectedId: "", mode: "", form: null, loading: false,
    saving: false, deleting: false, dirty: false, expanded: {}, modelInitialValue: "",
    parentFilter: "", parentExpanded: {}, msg: "", msgClass: "" };
  st.slide = { records: [], loaded: false, form: null, mode: "", selectedId: "",
    loading: false, saving: false, deleting: false, dirty: false, msg: "", msgClass: "" };
  st.msgs = [];
  st.msgMeta = { complete: false, pages: 0, warning: "", loading: false,
    filter: "", status: "all", selected: {}, limit: 50,
    server_filter: null, server_filter_query: {}, filter_warning: "", native_url: "" };
  st.audit = { records: [], loading: false, loaded: false, error: "" };
  st.diagnostic = { msg: "", lastPath: "" };
  st.cats = [];
  st.busy = false;
  st.taskKind = "";
  st.taskStarted = false;
  st._prog = null;
  st._requests = {};
  st._draftChecked = false;
  Object.values(st._draftTimers || {}).forEach(clearTimeout);
  st._draftTimers = {};
  clearTimeout(st._draftRecoveryRetryTimer);
  st._draftRevision = { publish: 0, edit: 0, batch: 0 };
  st._draftRecoveryBase = { publish: 0, edit: 0, batch: 0 };
  st._draftRecoveryPending = false;
  st._draftRecoveryInFlight = false;
  st._draftRecoveryRetries = 0;
  st._draftRecoveryRetryTimer = null;
  st._draftDeferredSaves = {};
  st._draftSaveBlocked = {};
  st._draftPersistenceSuspended = false;
  st._linkWaiters = {};
  st._opSeq += 1;
}

async function doLogout() {
  const st = activeState();
  if (!st || st.busy) {
    if (st) setAreaMsg(st, "pub", "pubMsg", "任务执行期间不能退出，请先等待或取消任务", "bad");
    return;
  }
  await flushDrafts(st);
  const r = await api().logout(st.id);
  if (!isLiveState(st)) return;
  if (!r || !r.ok || r.logged_out === false) {
    setAreaMsg(st, "pub", "pubMsg", (r && r.msg) || "退出失败", "bad");
    return;
  }
  st.loggedIn = false;
  st.needLogin = true;
  clearSiteUiState(st);
  renderTabStrip();
  log(r.msg || "已退出", st.id);
  if (ACTIVE !== st.id) {
    renderActiveTab();
    return;
  }
  renderActiveTab();
  _credentialSeq += 1;
  _loginSeq += 1;
  $("loginUrl").value = st.url || LAST_URL || "";
  $("loginHistory").value = st.url || "";
  $("loginUser").value = "admin";
  $("loginPass").value = "";
  $("loginPass").type = "password"; $("btnTogglePass").textContent = "显示";
  $("loginCode").value = "";
  $("capRow").hidden = true;
  $("loginMask").hidden = false;
  setMsg("loginMsg", "已退出，可重新获取验证码登录；取消将关闭该标签");
  if ($("loginUrl").value) fillSavedCredentials($("loginUrl").value.trim());
}

/* ══════════ 剪贴板 ══════════
   【重要】不得拦截 Ctrl+C/V/X/A。
   keydown 里一旦 await，再调 preventDefault() 已经太迟（原生粘贴早已执行），
   结果“原生 + 自己”插两次，AABB 变 AABBAABB。
   本 WebView 的原生快捷键本就可用，交给它即可；
   只额外提供“右键粘贴”（contextmenu 默认行为只是弹菜单，拦住不会重复）。*/
async function clipRead() {
  try {
    const t = await navigator.clipboard.readText();
    if (typeof t === "string") return t;
  } catch (_) { /* 回退到 Python 读系统剪贴板 */ }
  try { const r = await api().clipboard_get(); return (r && r.ok) ? (r.text || "") : ""; }
  catch (_e) { return ""; }
}
function isEditable(el) {
  if (!el) return false;
  const tag = el.tagName;
  return (tag === "INPUT" && !/^(checkbox|radio|button|submit|file)$/i.test(el.type)) ||
         tag === "TEXTAREA";
}
function replaceSelection(el, text) {
  const s = el.selectionStart ?? el.value.length;
  const e = el.selectionEnd ?? el.value.length;
  el.value = el.value.slice(0, s) + text + el.value.slice(e);
  const pos = s + text.length;
  el.setSelectionRange(pos, pos);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}
/* 右键粘贴：在输入框上右键直接粘贴剪贴板内容 */
// A browser drops a file onto the document by navigating to it unless a
// handler cancels the default.  Keep the app stable for drops outside an
// explicit media target; target handlers stop propagation after accepting the
// files, while unsupported URL/text drops retain normal browser behavior.
document.addEventListener("dragover", (e) => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files"))
    e.preventDefault();
});
document.addEventListener("drop", (e) => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files"))
    e.preventDefault();
});

document.addEventListener("contextmenu", async (e) => {
  const el = e.target;
  if (!isEditable(el)) return;
  e.preventDefault();
  const text = await clipRead();
  if (text) { el.focus(); replaceSelection(el, text); }
});

/* ══════════ 栏目 ══════════ */
function flattenCats(nodes, depth, out) {
  (nodes || []).forEach((n) => {
    out.push({ scode: n.scode || n.id || "", name: "　".repeat(depth) + (n.name || "") });
    flattenCats(n.children, depth + 1, out);
  });
  return out;
}
function fillCatSelects(st) {
  const flat = flattenCats(st.cats, 0, []);
  for (const sel of [$("pubCat"), $("editCat")]) {
    const keep = sel.value;
    sel.innerHTML = flat.length
      ? '<option value="">— 请选择栏目 —</option>'
      : '<option value="">— 请先加载栏目 —</option>';
    flat.forEach((c) => {
      if (!c.scode) return;
      const opt = document.createElement("option");
      opt.value = c.scode; opt.textContent = `${c.name}（${c.scode}）`;
      sel.appendChild(opt);
    });
    sel.value = keep;
  }
  renderCatTree(st, "pub");
  renderCatTree(st, "edit");
}

/* ══════════ 可折叠栏目树 ══════════
   <select> 本身无法折叠，所以它只做隐藏的取值载体，
   可见部分用这棵树；选中时写回 select 并派 change，旧逻辑无需改动。
   展开态每个标签、每个选择器各自保存，默认全部折叠。*/
function catNodeId(n) { return String(n.scode || n.id || ""); }

function renderCatTree(st, which) {
  const box = $(which + "CatTree");
  if (!box) return;
  const sel = $(which + "Cat");
  const scope = st[which];
  if (!scope.catOpen) scope.catOpen = {};
  box.innerHTML = "";
  const nodes = st.cats || [];
  const search = document.createElement("input");
  search.className = "cat-search";
  search.placeholder = "搜索栏目名称或编号…";
  search.value = scope.catSearch || "";
  search.addEventListener("click", (e) => e.stopPropagation());
  search.addEventListener("input", (e) => {
    scope.catSearch = e.target.value;
    renderCatTree(st, which);
    const next = box.querySelector(".cat-search");
    if (next) { next.focus(); next.setSelectionRange(next.value.length, next.value.length); }
  });
  box.appendChild(search);
  if (!nodes.length) {
    box.insertAdjacentHTML("beforeend", '<div class="cat-empty">— 请先加载栏目 —</div>');
    return;
  }
  const query = String(scope.catSearch || "").trim().toLowerCase();
  const ownMatches = (n) =>
    `${n.name || ""} ${catNodeId(n)}`.toLowerCase().includes(query);
  const matches = (n) => ownMatches(n) || (n.children || []).some(matches);
  let shown = 0;
  const build = (list, depth, parent, showAll) => {
    list.forEach((n) => {
      if (query && !showAll && !matches(n)) return;
      shown += 1;
      const id = catNodeId(n);
      const kids = n.children || [];
      const revealChildren = !!showAll || (!!query && ownMatches(n));
      const row = document.createElement("div");
      row.className = "cat-row";
      row.style.paddingLeft = (depth * 16 + 4) + "px";
      if (id && id === sel.value) row.classList.add("on");

      const caret = document.createElement("span");
      caret.className = "cat-caret" + (kids.length ? "" : " leaf");
      caret.textContent = kids.length ? ((query || scope.catOpen[id]) ? "▾" : "▸") : "";
      if (kids.length && !query) {
        caret.addEventListener("click", (e) => {
          e.stopPropagation();
          scope.catOpen[id] = !scope.catOpen[id];
          renderCatTree(st, which);
        });
      }
      row.appendChild(caret);

      const name = document.createElement("span");
      name.className = "cat-name";
      name.textContent = n.name || "";
      row.appendChild(name);

      const code = document.createElement("span");
      code.className = "cat-code";
      code.textContent = id;
      row.appendChild(code);

      row.addEventListener("click", () => {
        if (!id) return;
        sel.value = id;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        updateCatToggle(st, which);
        closeCatPanel(which);          // 选完就收起，不占地方
      });
      parent.appendChild(row);

      if (kids.length && (query || scope.catOpen[id]))
        build(kids, depth + 1, parent, revealChildren);
    });
  };
  build(nodes, 0, box, false);
  if (!shown) box.insertAdjacentHTML("beforeend", '<div class="cat-empty">没有匹配的栏目</div>');
}

/* 展开到选中项（恢复状态/切标签时用，否则选中项藏在折叠里看不见）*/
function expandCatPath(st, which, scode) {
  if (!scode) return;
  const scope = st[which];
  if (!scope.catOpen) scope.catOpen = {};
  const walk = (list, chain) => {
    for (const n of (list || [])) {
      const id = catNodeId(n);
      if (id === String(scode)) { chain.forEach((c) => { scope.catOpen[c] = true; }); return true; }
      if (walk(n.children, chain.concat(id))) return true;
    }
    return false;
  };
  walk(st.cats || [], []);
}

/* 折叠式栏目选择器：默认只显示一行当前选中，点一下才弹出树 */
function findCatNode(nodes, scode) {
  for (const n of (nodes || [])) {
    if (catNodeId(n) === String(scode)) return n;
    const hit = findCatNode(n.children, scode);
    if (hit) return hit;
  }
  return null;
}
function updateCatToggle(st, which) {
  const btn = $(which + "CatToggle");
  if (!btn) return;
  const scode = $(which + "Cat").value;
  if (!(st.cats || []).length) { btn.textContent = "— 请先加载栏目 —"; return; }
  const node = findCatNode(st.cats, scode);
  btn.textContent = node ? `${node.name}（${scode}）` : "— 请选择栏目 —";
}
function closeCatPanel(which) {
  const box = $(which + "CatTree");
  if (box) box.hidden = true;
  const btn = $(which + "CatToggle");
  if (btn) btn.classList.remove("open");
}
function closeAllCatPanels() { closeCatPanel("pub"); closeCatPanel("edit"); }
function toggleCatPanel(which) {
  const st = activeState();
  if (!st) return;
  const box = $(which + "CatTree");
  const willOpen = box.hidden;
  closeAllCatPanels();
  if (!willOpen) return;
  // 开展时展开到当前选中项，其余保持折叠
  expandCatPath(st, which, $(which + "Cat").value);
  renderCatTree(st, which);
  box.hidden = false;
  $(which + "CatToggle").classList.add("open");
}
/* 点面板之外关闭（与原生下拉行为一致）*/
document.addEventListener("click", (e) => {
  if (!e.target.closest(".cat-picker")) closeAllCatPanels();
});
/* 栏目加载：指定标签版（自动恢复时需要给非活动标签也加载）*/
async function loadCategoriesFor(st) {
  if (!st || !st.loggedIn || st.busy) return false;
  const req = nextRequest(st, "categories");
  const expectedUrl = st.url;
  log("加载栏目…", st.id);
  let r;
  try { r = await api().load_categories(st.id); }
  catch (e) { log("栏目加载异常: " + e, st.id); return false; }
  if (!requestIsCurrent(st, "categories", req) || !st.loggedIn ||
      st.url !== expectedUrl) return false;
  if (!r || !r.ok) { log("栏目加载失败: " + (r && r.msg), st.id); return false; }
  st.cats = r.tree;
  if (st.id === ACTIVE) { fillCatSelects(st); updateCatToggle(st, "pub"); updateCatToggle(st, "edit"); }
  log(`栏目加载完成，共 ${r.count} 个`, st.id);
  return true;
}
/* 手动点“加载栏目”：强制刷新当前标签 */
async function loadCategories() {
  const st = activeState();
  if (!st) return;
  if (!st.loggedIn) { log("请先登录再加载栏目", st.id); return; }
  const btn = $("btnLoadCats");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "加载中…";
  try { await loadCategoriesFor(st); }
  finally { btn.disabled = false; btn.textContent = old; }
}

/* ══════════ 栏目管理（安全 CRUD） ══════════ */
function categoryNodeId(node) { return String((node && (node.scode || node.id)) || ""); }
function countCategoryNodes(nodes) {
  return (nodes || []).reduce((total, node) => total + 1 + countCategoryNodes(node.children), 0);
}
function setCategoryMsg(st, text, cls) {
  if (!st || !st.category) return;
  st.category.msg = text || "";
  st.category.msgClass = cls || "";
  if (st.id === ACTIVE) setMsg("categoryMsg", text, cls);
}
function resetCategoryEditor(st) {
  if (!st || !st.category) return;
  st.category.selectedId = "";
  st.category.mode = "";
  st.category.form = null;
  st.category.dirty = false;
  st.category.modelInitialValue = "";
}
function clearWorkflowCategory(st, scode) {
  scode = String(scode || "");
  if (st.pub.cat === scode) {
    st.pub.cat = ""; st.pub.nativeUrl = ""; st.pub.fields = []; st.pub.mapping = {}; st.pub.overrides = {}; st.pub.submitter = null; st.pub.submitterOptions = []; st.pub.catLoading = false;
    st.pub.htmlInfo = "栏目已变更，请重新选择栏目后再发布。"; st.pub.htmlInfoClass = "";
  }
  if (st.edit.cat === scode) {
    st.edit.cat = ""; st.edit.articles = []; st.edit.artId = ""; st.edit.fields = [];
    st.edit.mapping = {}; st.edit.overrides = {}; st.edit.backendValues = {}; st.edit.submitter = null; st.edit.submitterOptions = [];
    st.edit.formReady = false; st.edit.formLoading = false;
  }
}
function categoryFormIsOpen(st) { return !!(st && st.category && st.category.form); }
async function confirmDiscardCategory(st) {
  if (!categoryFormIsOpen(st) || !st.category.dirty) return true;
  return confirmDialog("当前栏目表单有未保存的修改，继续将丢失这些内容。", {
    title: "放弃未保存的栏目修改？", kind: "warn", okText: "继续", cancelText: "留在当前表单" });
}
function categoryFieldWide(field) {
  const name = String(field.name || "").toLowerCase();
  return field.kind === "textarea" || /description|keywords|content|template|title|pcode|parent/.test(name) ||
    String(field.value || "").length > 80;
}
function isCategoryParentField(field) {
  const name = String((field && field.name) || "").toLowerCase();
  return field && field.kind === "select" &&
    ["pcode", "parent", "parent_id", "parent_scode", "pid"].includes(name);
}
function isCategoryModelField(field) {
  const name = String((field && field.name) || "").toLowerCase();
  return field && field.kind === "select" && ["mcode", "model", "model_id", "modelid"].includes(name);
}
function categoryTemplatePreset(form) {
  if (!form) return null;
  const presets = Array.isArray(form.template_presets) ? form.template_presets : [];
  return presets.find((preset) => {
    const model = (form.fields || []).find((field) => field.name === preset.model_field);
    return model && String(model.value || "") === String(preset.model_value || "");
  }) || null;
}
function categoryTemplateOptionExists(field, value) {
  return !!field && (field.options || []).some((option) =>
    String(option.value || "") === String(value || ""));
}
function applyCategoryTemplateDefaults(st, force) {
  const form = st && st.category && st.category.form;
  if (!form || (st.category.mode !== "create" && !force)) return [];
  const preset = categoryTemplatePreset(form);
  if (!preset) return [];
  const applied = [];
  [[preset.list_field, preset.list_value, "列表页模板"],
   [preset.detail_field, preset.detail_value, "详情页模板"]].forEach(([name, value, label]) => {
    const field = (form.fields || []).find((item) => item.name === name);
    // The server remains the source of truth: do not set a template which is
    // not actually listed in this site's own category form.
    if (!categoryTemplateOptionExists(field, value)) return;
    if (String(field.value || "") !== String(value || "")) {
      field.value = String(value || ""); applied.push(`${label}：${value || "（留空）"}`);
    }
  });
  return applied;
}
function categoryTemplateHint(form) {
  const preset = categoryTemplatePreset(form);
  if (!preset) return "";
  return `“${preset.model_label}”默认：列表页 ${preset.list_value || "（留空）"}；详情页 ${preset.detail_value || "（留空）"}。`;
}
function categoryDescendantIds(node, result) {
  result = result || new Set();
  (node && node.children || []).forEach((child) => {
    const id = categoryNodeId(child);
    if (id) result.add(id);
    categoryDescendantIds(child, result);
  });
  return result;
}
function categoryParentEntries(nodes) {
  const entries = [];
  const walk = (items, depth, pathNames, ancestorIds) => {
    (items || []).forEach((node) => {
      const id = categoryNodeId(node); if (!id) return;
      const name = String(node.name || "（未命名栏目）");
      const names = pathNames.concat(name);
      entries.push({ id, name, depth, path: names.join(" / "), ancestors: Array.from(ancestorIds) });
      walk(node.children || [], depth + 1, names, ancestorIds.concat(id));
    });
  };
  walk(nodes || [], 0, [], []);
  return entries;
}
function renderCategoryParentField(st, field, wrap, attach, readonly) {
  const picker = document.createElement("div"); picker.className = "category-parent-picker";
  const search = document.createElement("input");
  search.type = "search"; search.className = "category-parent-search";
  search.placeholder = "搜索栏目名称、路径或编号…";
  search.setAttribute("aria-label", "搜索父栏目");
  search.value = String(st.category.parentFilter || "");
  if (readonly) { search.disabled = true; search.dataset.readonly = "1"; }
  const select = attach(document.createElement("select"));
  select.className = "category-parent-native";
  const sourceOptions = Array.from(field.options || []).map((option) => ({
    value: String(option.value || ""), label: String(option.label || option.value || "（空）"),
  }));
  sourceOptions.forEach((option) => {
    const item = document.createElement("option"); item.value = option.value;
    item.textContent = option.label; select.appendChild(item);
  });
  const allowed = new Set(sourceOptions.map((option) => option.value));
  const rootOption = sourceOptions.find((option) => !option.value) || null;
  const tree = (st.category.form && st.category.form.category_tree) || st.cats || [];
  const allTreeEntries = categoryParentEntries(tree);
  const byId = new Map(allTreeEntries.map((entry) => [entry.id, entry]));
  const entries = allTreeEntries.filter((entry) => allowed.has(entry.id));
  const fallbackOptions = sourceOptions.filter((option) => option.value && !byId.has(option.value));
  const editingId = st.category.mode === "edit" ? String(st.category.selectedId || "") : "";
  const editingNode = editingId ? findCatNode(tree, editingId) : null;
  const blocked = categoryDescendantIds(editingNode);
  if (editingId) blocked.add(editingId);
  const originalValue = String(field.value || "");
  select.value = sourceOptions.some((option) => option.value === originalValue) ? originalValue : "";
  const treeBox = document.createElement("div"); treeBox.className = "category-parent-tree";
  const eligibleCache = new Map();
  const hasAllowed = (node) => {
    const id = categoryNodeId(node);
    if (eligibleCache.has(id)) return eligibleCache.get(id);
    const result = allowed.has(id) || (node.children || []).some(hasAllowed);
    eligibleCache.set(id, result); return result;
  };
  const expanded = st.category.parentExpanded || (st.category.parentExpanded = {});
  const choose = (value) => {
    if (select.disabled || !allowed.has(String(value))) return;
    select.value = String(value); field.value = String(value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
    renderTree();
  };
  const renderTree = () => {
    treeBox.innerHTML = "";
    const query = String(st.category.parentFilter || "").trim().toLowerCase();
    const current = String(select.value || "");
    const ownMatch = (node, path) => {
      const id = categoryNodeId(node), name = String(node.name || "");
      return `${name} ${path.concat(name).join(" / ")} ${id}`.toLowerCase().includes(query);
    };
    const branchMatches = (node, path) => !query || ownMatch(node, path) ||
      (node.children || []).some((child) => branchMatches(child, path.concat(String(node.name || ""))));
    const rootShown = !!(rootOption && (!query ||
      `顶级 无父栏目 ${rootOption.label}`.toLowerCase().includes(query)));
    if (rootShown) {
      const rootRow = document.createElement("button"); rootRow.type = "button";
      rootRow.className = "category-parent-row root" + (!current ? " selected" : "");
      rootRow.innerHTML = '<span class="category-parent-caret blank"></span>' +
        `<span class="category-parent-name">${esc(rootOption.label || "顶级栏目（无父栏目）")}</span>` +
        '<span class="category-parent-code">TOP</span>';
      rootRow.addEventListener("click", () => choose("")); treeBox.appendChild(rootRow);
    }
    let shown = 0;
    const build = (nodes, parent, depth, path, forceSubtree) => {
      (nodes || []).forEach((node) => {
        if (!hasAllowed(node)) return;
        const id = categoryNodeId(node), name = String(node.name || "（未命名栏目）");
        const thisMatch = !query || ownMatch(node, path);
        const visible = !query || forceSubtree || branchMatches(node, path);
        if (!visible) return;
        const eligibleChildren = (node.children || []).filter(hasAllowed);
        const currentPath = byId.get(current);
        const containsCurrent = !!(currentPath && currentPath.ancestors.includes(id));
        const opened = query ? true : (Object.prototype.hasOwnProperty.call(expanded, id)
          ? !!expanded[id]
          : (st.category.mode === "create" ? containsCurrent : (depth < 2 || containsCurrent)));
        const item = document.createElement("div"); item.className = "category-parent-node";
        const row = document.createElement("button"); row.type = "button";
        const selectable = allowed.has(id) && !blocked.has(id);
        row.className = "category-parent-row" + (id === current ? " selected" : "") +
          (!selectable ? " disabled" : "");
        row.title = path.concat(name).join(" / ");
        const caret = document.createElement("span");
        caret.className = "category-parent-caret" + (eligibleChildren.length ? "" : " blank");
        caret.textContent = eligibleChildren.length ? (opened ? "▾" : "▸") : "";
        if (eligibleChildren.length) caret.addEventListener("click", (event) => {
          event.preventDefault(); event.stopPropagation(); expanded[id] = !opened; renderTree();
        });
        row.appendChild(caret);
        const label = document.createElement("span"); label.className = "category-parent-name";
        label.textContent = name; row.appendChild(label);
        const code = document.createElement("span"); code.className = "category-parent-code";
        code.textContent = id; row.appendChild(code);
        if (selectable) row.addEventListener("click", () => choose(id));
        item.appendChild(row); shown += 1;
        if (eligibleChildren.length && opened) {
          const children = document.createElement("div"); children.className = "category-parent-children";
          build(eligibleChildren, children, depth + 1, path.concat(name), forceSubtree || (!!query && thisMatch));
          item.appendChild(children);
        }
        parent.appendChild(item);
      });
    };
    build(tree, treeBox, 0, [], false);
    fallbackOptions.forEach((option) => {
      if (query && !`${option.label} ${option.value}`.toLowerCase().includes(query)) return;
      const row = document.createElement("button"); row.type = "button";
      row.className = "category-parent-row fallback" + (option.value === current ? " selected" : "");
      row.innerHTML = '<span class="category-parent-caret blank"></span>' +
        `<span class="category-parent-name">${esc(option.label)}</span><span class="category-parent-code">${esc(option.value)}</span>`;
      row.addEventListener("click", () => choose(option.value)); treeBox.appendChild(row); shown += 1;
    });
    if (!shown && !rootShown) {
      const empty = document.createElement("div"); empty.className = "category-parent-empty";
      empty.textContent = "没有匹配的栏目"; treeBox.appendChild(empty);
    }
  };
  search.addEventListener("input", () => {
    st.category.parentFilter = search.value; renderTree();
  });
  const help = document.createElement("small"); help.className = "category-parent-help";
  help.textContent = `按层级显示 ${entries.length} 个后台允许的父栏目` +
    (editingId ? "；当前栏目及其子栏目不可选。" : "。");
  picker.appendChild(search); picker.appendChild(select); picker.appendChild(treeBox); picker.appendChild(help);
  wrap.appendChild(picker);
  renderTree();
  return wrap;
}
function renderCategoryField(st, field) {
  if (isCategoryParentField(field)) {
    const wrap = document.createElement("div"); wrap.className = "category-field wide";
    const label = document.createElement("label"); label.textContent = field.label || field.name;
    wrap.appendChild(label);
    const attach = control => {
      control.dataset.catField = field.name;
      control.disabled = !!field.readonly;
      control.addEventListener("change", () => { field.value = control.value; st.category.dirty = true; });
      return control;
    };
    return renderCategoryParentField(st, field, wrap, attach, !!field.readonly);
  }
  return createCmsFieldControl(field, field.value, next => {
    field.value = next; st.category.dirty = true;
    if (isCategoryModelField(field)) {
      const modelChanged = st.category.mode === "create" ||
        String(st.category.modelInitialValue || "") !== String(field.value || "");
      const applied = applyCategoryTemplateDefaults(st, modelChanged);
      if (applied.length) {
        setCategoryMsg(st, "已按内容模型自动设置" + applied.join("；"), "");
        renderCategoryManager(st);
      }
    }
  }, "category");
}
function renderCategoryAdminTree(st) {
  const box = $("categoryAdminTree");
  if (!box) return;
  box.innerHTML = "";
  const query = String(st.category.filter || "").trim().toLowerCase();
  const match = (node) => `${node.name || ""} ${categoryNodeId(node)}`.toLowerCase().includes(query);
  const visible = (node) => match(node) || (node.children || []).some(visible);
  const build = (nodes, parent, forceOpen) => {
    (nodes || []).forEach((node) => {
      if (query && !visible(node)) return;
      const id = categoryNodeId(node), children = node.children || [];
      const opened = !!query || !!forceOpen || !!st.category.expanded[id];
      const item = document.createElement("div"); item.className = "category-node";
      const row = document.createElement("div");
      row.className = "category-node-row" + (id === st.category.selectedId ? " on" : "");
      const caret = document.createElement("button"); caret.type = "button";
      caret.className = "caret" + (children.length ? "" : " blank");
      caret.textContent = children.length ? (opened ? "▾" : "▸") : "";
      if (children.length) caret.addEventListener("click", (event) => {
        event.stopPropagation(); st.category.expanded[id] = !opened; renderCategoryAdminTree(st);
      });
      row.appendChild(caret);
      const name = document.createElement("span"); name.className = "category-node-name"; name.textContent = node.name || "（未命名栏目）";
      row.appendChild(name);
      const code = document.createElement("span"); code.className = "category-node-code"; code.textContent = id;
      row.appendChild(code);
      row.addEventListener("click", () => openCategoryEdit(id));
      item.appendChild(row);
      if (children.length && opened) {
        const childBox = document.createElement("div"); childBox.className = "category-node-children";
        build(children, childBox, !!query && match(node)); item.appendChild(childBox);
      }
      parent.appendChild(item);
    });
  };
  if (!(st.cats || []).length) {
    box.innerHTML = '<div class="category-empty">请先登录并加载栏目</div>';
    return;
  }
  build(st.cats, box, false);
  if (!box.children.length) box.innerHTML = '<div class="category-empty">没有匹配的栏目</div>';
}
function renderCategoryManager(st) {
  if (!st || !st.category) return;
  $("categoryFilter").value = st.category.filter || "";
  $("categoryCount").textContent = (st.cats || []).length ? `共 ${countCategoryNodes(st.cats)} 个栏目` : "";
  renderCategoryAdminTree(st);
  const form = st.category.form;
  $("categoryEditorEmpty").hidden = !!form;
  $("categoryEditorForm").hidden = !form;
  $("categoryEditorTitle").textContent = form
    ? (st.category.mode === "create" ? "新增栏目" : "修改栏目") : "栏目编辑";
  $("categoryEditorHint").textContent = form
    ? (st.category.mode === "batch"
      ? "使用当前后台原生 multiplename 控件一次新增多个栏目；没有该控件时不会退化为猜测式循环创建。"
      : st.category.mode === "create"
      ? "字段来自当前网站后台的新增栏目表单；保存栏目只创建栏目，不会发布文章。"
      : `正在编辑栏目编号 ${form.scode || st.category.selectedId}。`)
    : "从左侧选择栏目，或新增一个顶级栏目。";
  const fields = $("categoryDynamicFields"); fields.innerHTML = "";
  fields.dataset.cmsNoValidate = form && (form.fields || []).some(field => field.form_novalidate) ? "1" : "0";
  if (form) (form.fields || []).forEach((field) => fields.appendChild(renderCategoryField(st, field)));
  const warnings = form && form.warnings || [];
  $("categoryWarnings").hidden = !warnings.length;
  $("categoryWarnings").textContent = warnings.join("\n");
  $("btnCategoryAddChild").hidden = !form || st.category.mode !== "edit";
  const categoryNative = $("btnCategoryNative");
  const selectedCategory = findCatNode(st.cats, st.category.selectedId);
  if (categoryNative) {
    categoryNative.hidden = !form || st.category.mode !== "edit" ||
      !String(selectedCategory && selectedCategory.edit_url || "").trim();
    categoryNative.disabled = !!st.busy || !String(selectedCategory && selectedCategory.edit_url || "").trim();
    categoryNative.title = selectedCategory && selectedCategory.edit_url
      ? "在完整浏览器中打开当前栏目的真实后台编辑页"
      : "当前栏目没有发现安全的真实后台编辑地址";
  }
  $("btnCategoryBatch").hidden = !!form || st.busy || !st.loggedIn;
  $("btnCategoryDelete").hidden = !form || st.category.mode !== "edit";
  $("btnCategorySave").disabled = !form || st.busy || st.category.loading;
  setMsg("categoryMsg", st.category.msg, st.category.msgClass);
}
function collectCategoryValues() {
  const st = activeState();
  const fields = st && st.category && st.category.form ? st.category.form.fields : [];
  return Object.fromEntries((fields || []).filter(x => !x.readonly && !x.disabled)
    .map(x => [x.name, x.value]));
}
function applyCategoryTree(st, tree) {
  st.cats = Array.isArray(tree) ? tree : [];
  if (!findCatNode(st.cats, st.pub.cat)) st.pub.cat = "";
  if (!findCatNode(st.cats, st.edit.cat)) st.edit.cat = "";
  fillCatSelects(st);
  updateCatToggle(st, "pub"); updateCatToggle(st, "edit");
}
async function openCategoryCreate(parentScode) {
  const st = activeState();
  if (!st || st.busy || !st.loggedIn) return;
  if (!await confirmDiscardCategory(st)) return;
  const req = nextRequest(st, "categoryForm");
  st.category.loading = true; setCategoryMsg(st, "正在读取后台栏目表单…", "");
  if (st.id === ACTIVE) renderCategoryManager(st);
  try {
    const r = await api().prepare_category_create(st.id, parentScode || "");
    if (!requestIsCurrent(st, "categoryForm", req)) return;
    if (!r || !r.ok) {
      setCategoryMsg(st, (r && r.msg) || "读取新增栏目表单失败", r && r.native_url ? "review" : "bad");
      if (r && r.native_url && typeof openNativeRecordUrl === "function")
        await openNativeRecordUrl(st, r.native_url, "动态栏目新增页", {allowBusy: true});
      return;
    }
    st.category.selectedId = ""; st.category.mode = "create"; st.category.form = r;
    const createModel = (r.fields || []).find(isCategoryModelField);
    st.category.modelInitialValue = createModel ? String(createModel.value || "") : "";
    const templateChanges = applyCategoryTemplateDefaults(st);
    st.category.dirty = false;
    st.category.parentFilter = ""; st.category.parentExpanded = {};
    const templateHint = categoryTemplateHint(r);
    setCategoryMsg(st, templateChanges.length
      ? `已按内容模型设置${templateChanges.join("；")}。请填写栏目字段后保存。`
      : (templateHint ? `${templateHint} 请填写栏目字段后保存。` : "请填写栏目字段后保存。"), "");
  } catch (error) { if (requestIsCurrent(st, "categoryForm", req)) setCategoryMsg(st, "读取栏目表单异常: " + error, "bad"); }
  finally { if (requestIsCurrent(st, "categoryForm", req)) { st.category.loading = false; if (st.id === ACTIVE) renderCategoryManager(st); } }
}
async function openCategoryEdit(scode) {
  const st = activeState();
  if (!st || st.busy || !st.loggedIn || !scode) return;
  if (String(scode) === st.category.selectedId && st.category.form && st.category.mode === "edit") return;
  if (!await confirmDiscardCategory(st)) return;
  const req = nextRequest(st, "categoryForm");
  st.category.loading = true; setCategoryMsg(st, "正在读取后台栏目表单…", "");
  if (st.id === ACTIVE) renderCategoryManager(st);
  try {
    const r = await api().prepare_category_edit(st.id, String(scode));
    if (!requestIsCurrent(st, "categoryForm", req)) return;
    if (!r || !r.ok) {
      setCategoryMsg(st, (r && r.msg) || "读取栏目表单失败", r && r.native_url ? "review" : "bad");
      if (r && r.native_url && typeof openNativeRecordUrl === "function")
        await openNativeRecordUrl(st, r.native_url, "动态栏目编辑页", {allowBusy: true});
      return;
    }
    st.category.selectedId = String(scode); st.category.mode = "edit"; st.category.form = r; st.category.dirty = false;
    const editModel = (r.fields || []).find(isCategoryModelField);
    st.category.modelInitialValue = editModel ? String(editModel.value || "") : "";
    st.category.parentFilter = ""; st.category.parentExpanded = {};
    setCategoryMsg(st, "", "");
  } catch (error) { if (requestIsCurrent(st, "categoryForm", req)) setCategoryMsg(st, "读取栏目表单异常: " + error, "bad"); }
  finally { if (requestIsCurrent(st, "categoryForm", req)) { st.category.loading = false; if (st.id === ACTIVE) renderCategoryManager(st); } }
}
async function refreshCategoryManager() {
  const st = activeState();
  if (!st || st.busy || !st.loggedIn) return;
  if (!await confirmDiscardCategory(st)) return;
  const btn = $("btnCategoryRefresh"); btn.disabled = true;
  try {
    const ok = await loadCategoriesFor(st);
    if (ok) { resetCategoryEditor(st); setCategoryMsg(st, "栏目列表已刷新。", "ok"); }
    else setCategoryMsg(st, "刷新栏目列表失败，请查看运行日志。", "bad");
  } finally { btn.disabled = false; if (st.id === ACTIVE) renderCategoryManager(st); }
}
async function saveCategory() {
  const st = activeState(), form = st && st.category.form;
  if (!st || !form || st.busy) return;
  if (!validateCmsControls($("categoryDynamicFields"))) return;
  const reviewKey = mutationReviewKey("category", st.category.mode === "edit"
    ? st.category.selectedId : st.category.mode === "batch" ? "batch" : "new");
  if (!await confirmMutationReview(st, reviewKey, "栏目保存")) return;
  // Keep the handler usable when embedded/test harnesses extract only the
  // save function.  The full app always provides confirmGetWrite; a missing
  // helper must not turn an otherwise POST-compatible save into a runtime
  // error.
  if (typeof confirmGetWrite === "function" && !await confirmGetWrite(form, "栏目")) return;
  const values = collectCategoryValues();
  const op = beginOperation(st, "category");
  if (!op) return;
  st.category.saving = true;
  let reopenScode = "";
  const editingExisting = st.category.mode === "edit";
  try {
    const r = st.category.mode === "batch"
      ? await api().create_categories_batch(st.id, values, form.revision || "")
      : st.category.mode === "create"
      ? await api().create_category(st.id, values, form.revision || "")
      : await api().update_category(st.id, st.category.selectedId, values, form.revision || "");
    if (!operationIsCurrent(st, op)) return;
    if (!r || !r.ok) {
      if (r && r.native_url && typeof openNativeRecordUrl === "function") {
        setCategoryMsg(st, r.msg || "栏目保存表单已改为动态网页，已切换原生网页。", "review");
        await openNativeRecordUrl(st, r.native_url, "动态栏目保存页", {
          allowBusy: true,
          handoff: nativeHandoffFromForm("category", values,
            [`栏目：${st.category.selectedId || "新增"}`], r.msg || "动态栏目保存已转网页。"),
        });
        return;
      }
      if (resultNeedsMutationReview(r)) setMutationReview(st, reviewKey, (r && r.msg) || "栏目保存结果待核对，请先到后台确认。");
      setCategoryMsg(st, (r && r.msg) || "保存栏目失败", resultNeedsMutationReview(r) ? "review" : "bad"); return;
    }
    clearMutationReview(st, reviewKey);
    applyCategoryTree(st, r.tree);
    const saved = st.category.mode === "create" ? String(r.scode || "") : st.category.selectedId;
    if (editingExisting) clearWorkflowCategory(st, saved);
    resetCategoryEditor(st);
    const uploadNote = uploadResultNotice(r.upload_metadata);
    setCategoryMsg(st, (r.msg || "栏目已保存") + uploadNote, "ok");
    log((r.msg || "栏目已保存") + uploadNote +
      (r.front_url_changed ? "；栏目 URL 可能影响产品前台链接，请按需重新同步产品。" : ""), st.id);
    reopenScode = saved;
  } catch (error) {
    if (operationIsCurrent(st, op)) {
      const review = resultNeedsMutationReview(null, error);
      if (review) setMutationReview(st, reviewKey, "栏目保存请求可能已到达后台，请先核对后再重试。");
      setCategoryMsg(st, "保存栏目异常: " + error, review ? "review" : "bad");
    }
  }
  finally {
    st.category.saving = false; finishOperation(st, op);
    if (reopenScode && isLiveState(st) && !st.busy) await openCategoryEdit(reopenScode);
  }
}
async function openCategoryBatchCreate() {
  const st = activeState();
  if (!st || st.busy || !st.loggedIn || !await confirmDiscardCategory(st)) return;
  const req = nextRequest(st, "categoryForm"); st.category.loading = true; setCategoryMsg(st, "正在检查后台 multiplename 批量表单…", "");
  if (st.id === ACTIVE) renderCategoryManager(st);
  try {
    const r = await api().prepare_category_batch_create(st.id, "");
    if (!requestIsCurrent(st, "categoryForm", req)) return;
    if (!r || !r.ok) {
      setCategoryMsg(st, (r && r.msg) || "当前后台没有批量新增栏目控件", r && r.native_url ? "review" : "bad");
      if (r && r.native_url && typeof openNativeRecordUrl === "function")
        await openNativeRecordUrl(st, r.native_url, "动态栏目批量新增页", {allowBusy: true});
      return;
    }
    st.category.selectedId = ""; st.category.mode = "batch"; st.category.form = r; st.category.dirty = false;
    st.category.parentFilter = ""; st.category.parentExpanded = {};
    setCategoryMsg(st, "请在 multiplename 字段中填写多个栏目名称后保存。", "");
  } catch (error) { if (requestIsCurrent(st, "categoryForm", req)) setCategoryMsg(st, "读取批量新增表单异常：" + error, "bad"); }
  finally { if (requestIsCurrent(st, "categoryForm", req)) { st.category.loading = false; if (st.id === ACTIVE) renderCategoryManager(st); } }
}
async function removeCategory() {
  const st = activeState(), form = st && st.category.form;
  if (!st || !form || st.category.mode !== "edit" || st.busy) return;
  const node = findCatNode(st.cats, st.category.selectedId);
  const childCount = node ? (node.children || []).length : 0;
  const name = node ? node.name : "当前栏目";
  const childWarning = childCount
    ? `\n该栏目下有 ${childCount} 个子栏目，最终是否允许删除由后台网页规则决定。`
    : "";
  const yes = await confirmDialog(`确定删除栏目“${name}”（编号 ${st.category.selectedId}）吗？${childWarning}\n该操作会修改网站后台，且不能由本工具自动撤销。`, {
    title: "删除栏目", kind: "warn", okText: "确认删除", cancelText: "取消" });
  if (!yes) return;
  const reviewKey = mutationReviewKey("category", st.category.selectedId);
  if (!await confirmMutationReview(st, reviewKey, "栏目删除")) return;
  const op = beginOperation(st, "category");
  if (!op) return;
  st.category.deleting = true;
  try {
    const r = await api().delete_category(st.id, st.category.selectedId, name, childCount > 0);
    if (!operationIsCurrent(st, op)) return;
    if (!r || !r.ok) {
      if (resultNeedsMutationReview(r)) setMutationReview(st, reviewKey, (r && r.msg) || "栏目删除结果待核对，请先到后台确认。");
      setCategoryMsg(st, (r && r.msg) || "删除栏目失败", resultNeedsMutationReview(r) ? "review" : "bad"); return;
    }
    clearMutationReview(st, reviewKey);
    applyCategoryTree(st, r.tree); clearWorkflowCategory(st, st.category.selectedId); resetCategoryEditor(st);
    setCategoryMsg(st, r.msg || "栏目已删除", "ok"); log(r.msg || "栏目已删除", st.id);
  } catch (error) {
    if (operationIsCurrent(st, op)) {
      const review = resultNeedsMutationReview(null, error);
      if (review) setMutationReview(st, reviewKey, "栏目删除请求可能已到达后台，请先核对后再重试。");
      setCategoryMsg(st, "删除栏目异常: " + error, review ? "review" : "bad");
    }
  }
  finally { st.category.deleting = false; finishOperation(st, op); }
}

/* ══════════ 独立网站 Slide/轮播管理 ══════════ */
function setSlideMsg(st, text, cls) {
  if (!st || !st.slide) return;
  st.slide.msg = text || ""; st.slide.msgClass = cls || "";
  if (st.id === ACTIVE) setMsg("slideMsg", text, cls);
}
function slideFieldValue(form, name) {
  const field = (form && form.fields || []).find((item) => item.name === name);
  return field ? field.value : "";
}
function renderSlideManager(st) {
  if (!st || !st.slide) return;
  const tbody = $("slideTable") && $("slideTable").querySelector("tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const records = st.slide.records || [];
  records.forEach((item) => {
    const tr = document.createElement("tr");
    const pic = String(item.pic || "");
    const preview = /^https?:\/\//i.test(pic)
      ? `<img src="${esc(pic)}" alt="" loading="lazy">` : esc(pic || "—");
    tr.innerHTML = `<td>${esc(item.id || "—")}</td><td>${preview}</td>` +
      `<td>${esc(item.title || "—")}</td><td>${esc(item.subtitle || "—")}</td>` +
      `<td>${esc(item.link || "—")}</td><td>${esc(item.gid || "—")}</td>` +
      `<td>${esc(item.sorting || "—")}</td><td class="row-actions">` +
      `<button type="button" class="ghost mini slide-edit">编辑</button>` +
      `${item.edit_url ? '<button type="button" class="ghost mini slide-native" title="在完整浏览器中打开真实后台编辑页">原生网页</button>' : ""}` +
      `<button type="button" class="ghost mini danger-action slide-delete">删除</button></td>`;
    tr.querySelector(".slide-edit").addEventListener("click", () => openSlideEdit(st, item.id));
    const native = tr.querySelector(".slide-native");
    if (native) native.addEventListener("click", () => openNativeRecordUrl(st, item.edit_url, "轮播后台编辑页"));
    tr.querySelector(".slide-delete").addEventListener("click", () => deleteSlide(st, item));
    tbody.appendChild(tr);
  });
  $("slideCount").textContent = st.slide.loading ? "加载中…" : `共 ${records.length} 条`;
  $("slideEditorEmpty").hidden = !!st.slide.form;
  $("slideEditorForm").hidden = !st.slide.form;
  $("slideEditorTitle").textContent = st.slide.form
    ? (st.slide.mode === "create" ? "新增轮播" : `编辑轮播 #${st.slide.selectedId}`) : "轮播编辑";
  $("slideEditorHint").textContent = st.slide.form
    ? "字段来自当前网站 Slide 表单；本地图会先按后台上传策略上传。"
    : "点击新增或选择一条记录进行编辑。";
  const root = $("slideDynamicFields"); root.innerHTML = "";
  root.dataset.cmsNoValidate = st.slide.form && (st.slide.form.fields || []).some(field => field.form_novalidate) ? "1" : "0";
  if (st.slide.form) {
    (st.slide.form.fields || []).forEach((field) => {
      root.appendChild(createCmsFieldControl(field, field.value, (value) => {
        field.value = value; st.slide.dirty = true;
      }, "slide"));
    });
  }
  setMsg("slideMsg", st.slide.msg, st.slide.msgClass);
  $("btnSlideDelete").hidden = !st.slide.form || st.slide.mode !== "edit";
  $("btnSlideSave").disabled = !st.slide.form || st.busy || st.slide.loading || st.slide.saving;
}
async function loadSlides(st, force = false) {
  st = st || activeState();
  if (!st || !st.loggedIn || st.busy || (st.slide.loaded && !force)) return false;
  st.slide.loading = true; if (st.id === ACTIVE) renderSlideManager(st);
  const req = nextRequest(st, "slides");
  try {
    const result = await api().load_slides(st.id);
    if (!requestIsCurrent(st, "slides", req)) return false;
    if (!result || !result.ok) { setSlideMsg(st, (result && result.msg) || "读取网站轮播失败", "bad"); return false; }
    st.slide.records = result.slides || []; st.slide.loaded = true;
    setSlideMsg(st, `已读取 ${st.slide.records.length} 条网站轮播。`, "ok");
    return true;
  } catch (error) { if (requestIsCurrent(st, "slides", req)) setSlideMsg(st, "读取网站轮播异常：" + error, "bad"); return false; }
  finally { if (requestIsCurrent(st, "slides", req)) { st.slide.loading = false; if (st.id === ACTIVE) renderSlideManager(st); } }
}
async function openSlideCreate(st) {
  st = st || activeState(); if (!st || st.busy || !st.loggedIn) return;
  st.slide.loading = true; st.slide.form = null; if (st.id === ACTIVE) renderSlideManager(st);
  try {
    const result = await api().prepare_slide_create(st.id);
    if (!result || !result.ok) {
      setSlideMsg(st, (result && result.msg) || "读取新增轮播表单失败", result && result.native_url ? "review" : "bad");
      if (result && result.native_url && typeof openNativeRecordUrl === "function")
        await openNativeRecordUrl(st, result.native_url, "动态轮播新增页", {allowBusy: true});
      return;
    }
    st.slide.mode = "create"; st.slide.selectedId = ""; st.slide.form = result; st.slide.dirty = false; setSlideMsg(st, "请填写轮播字段后保存。", "");
  } catch (error) { setSlideMsg(st, "读取新增轮播表单异常：" + error, "bad"); }
  finally { st.slide.loading = false; if (st.id === ACTIVE) renderSlideManager(st); }
}
async function openSlideEdit(st, id) {
  st = st || activeState(); if (!st || st.busy || !st.loggedIn || !id) return;
  st.slide.loading = true; st.slide.form = null; st.slide.selectedId = String(id); if (st.id === ACTIVE) renderSlideManager(st);
  try {
    const result = await api().prepare_slide_edit(st.id, String(id));
    if (!result || !result.ok) {
      setSlideMsg(st, (result && result.msg) || "读取轮播表单失败", result && result.native_url ? "review" : "bad");
      if (result && result.native_url && typeof openNativeRecordUrl === "function")
        await openNativeRecordUrl(st, result.native_url, "动态轮播编辑页", {allowBusy: true});
      return;
    }
    st.slide.mode = "edit"; st.slide.form = result; st.slide.dirty = false; setSlideMsg(st, "", "");
  } catch (error) { setSlideMsg(st, "读取轮播表单异常：" + error, "bad"); }
  finally { st.slide.loading = false; if (st.id === ACTIVE) renderSlideManager(st); }
}
async function saveSlide() {
  const st = activeState(), form = st && st.slide.form;
  if (!st || !form || st.busy || !validateCmsControls($("slideDynamicFields"))) return;
  const reviewKey = mutationReviewKey("slide", st.slide.mode === "edit" ? st.slide.selectedId : "new");
  if (!await confirmMutationReview(st, reviewKey, "轮播保存")) return;
  if (typeof confirmGetWrite === "function" && !await confirmGetWrite(form, "轮播")) return;
  const values = Object.fromEntries((form.fields || []).filter((field) => !field.readonly && !field.disabled)
    .map((field) => [field.name, field.value]));
  st.slide.saving = true; if (st.id === ACTIVE) renderSlideManager(st);
  try {
    const result = st.slide.mode === "create"
      ? await api().create_slide(st.id, values, form.revision || "")
      : await api().update_slide(st.id, st.slide.selectedId, values, form.revision || "");
    if (!result || !result.ok) {
      if (result && result.native_url && typeof openNativeRecordUrl === "function") {
        setSlideMsg(st, result.msg || "轮播表单已改为动态网页，已切换原生网页。", "review");
        await openNativeRecordUrl(st, result.native_url, "动态轮播保存页", {
          allowBusy: true,
          handoff: nativeHandoffFromForm("slide", values,
            [`轮播：${st.slide.selectedId || "新增"}`], result.msg || "动态轮播保存已转网页。"),
        });
        return;
      }
      const review = resultNeedsMutationReview(result);
      if (review) setMutationReview(st, reviewKey, (result && result.msg) || "轮播保存结果待核对，请先到后台确认。");
      return setSlideMsg(st, (result && result.msg) || "保存轮播失败", review ? "review" : "bad");
    }
    clearMutationReview(st, reviewKey);
    st.slide.records = result.slides || st.slide.records; st.slide.loaded = true; st.slide.form = null; st.slide.dirty = false;
    setSlideMsg(st, (result.msg || "轮播已保存") + uploadResultNotice(result.upload_metadata), "ok");
    if (st.id === ACTIVE) renderSlideManager(st);
  } catch (error) {
    const review = resultNeedsMutationReview(null, error);
    if (review) setMutationReview(st, reviewKey, "轮播保存请求可能已到达后台，请先核对后再重试。");
    setSlideMsg(st, "保存轮播异常：" + error, review ? "review" : "bad");
  }
  finally { st.slide.saving = false; if (st.id === ACTIVE) renderSlideManager(st); }
}
async function deleteSlide(st, item) {
  st = st || activeState(); if (!st || st.busy || !item) return;
  const yes = await confirmDialog(`确定删除轮播“${item.title || item.id}”（编号 ${item.id}）吗？\n该操作会修改网站后台，且不会删除文章图集。`, {
    title: "删除网站轮播", kind: "warn", okText: "确认删除", cancelText: "取消" });
  if (!yes) return;
  const reviewKey = mutationReviewKey("slide", item.id);
  if (!await confirmMutationReview(st, reviewKey, "轮播删除")) return;
  st.slide.deleting = true; if (st.id === ACTIVE) renderSlideManager(st);
  try {
    const result = await api().delete_slide(st.id, item.id, item.title || "");
    if (!result || !result.ok) {
      const review = resultNeedsMutationReview(result);
      if (review) setMutationReview(st, reviewKey, (result && result.msg) || "轮播删除结果待核对，请先到后台确认。");
      return setSlideMsg(st, (result && result.msg) || "删除轮播失败", review ? "review" : "bad");
    }
    clearMutationReview(st, reviewKey);
    st.slide.records = result.slides || []; st.slide.form = null; st.slide.loaded = true;
    setSlideMsg(st, result.msg || "轮播已删除", "ok");
  } catch (error) {
    const review = resultNeedsMutationReview(null, error);
    if (review) setMutationReview(st, reviewKey, "轮播删除请求可能已到达后台，请先核对后再重试。");
    setSlideMsg(st, "删除轮播异常：" + error, review ? "review" : "bad");
  }
  finally { st.slide.deleting = false; if (st.id === ACTIVE) renderSlideManager(st); }
}

/* ══════════ 独立网站单页管理 ══════════ */
function setSingleMsg(st, text, cls) {
  if (!st || !st.single) return;
  st.single.msg = text || ""; st.single.msgClass = cls || "";
  if (st.id === ACTIVE) setMsg("singleMsg", text, cls);
}

function renderSingleManager(st) {
  if (!st || !st.single) return;
  const tbody = $("singleTable") && $("singleTable").querySelector("tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const filter = String(st.single.keyword || "").trim().toLowerCase();
  const records = (st.single.records || []).filter((item) => !filter ||
    `${item.id || ""} ${item.title || ""} ${item.scode || ""}`.toLowerCase().includes(filter));
  records.forEach((item) => {
    const tr = document.createElement("tr");
    const toggle = item.toggles && item.toggles.status;
    const status = toggle
      ? `<button type="button" class="text-btn single-status" data-id="${esc(item.id)}" ` +
        `data-value="${esc(toggle.target)}" data-url="${esc(toggle.url)}"` +
        `${st.single.loading ? " disabled" : ""}>` +
        `${Number(toggle.target) === 1 ? "开启" : "关闭"}</button>`
      : `<span class="hint">未发现状态入口</span>`;
    tr.innerHTML = `<td>${esc(item.id || "—")}</td><td>${esc(item.scode || "—")}</td>` +
      `<td title="${esc(item.title || "")}">${esc(item.title || "—")}</td>` +
      `<td>${esc(item.date || "—")}</td><td>${status}</td>` +
      `<td><button type="button" class="ghost mini single-edit">编辑</button>` +
      `${item.edit_url ? '<button type="button" class="ghost mini single-native" title="在完整浏览器中打开真实后台编辑页">原生网页</button>' : ""}</td>`;
    const edit = tr.querySelector(".single-edit");
    if (edit) edit.addEventListener("click", () => openSingleEdit(st, item.id));
    const native = tr.querySelector(".single-native");
    if (native) native.addEventListener("click", () => openNativeRecordUrl(st, item.edit_url, "单页后台编辑页"));
    const statusButton = tr.querySelector(".single-status");
    if (statusButton) statusButton.addEventListener("click", () => toggleSingleStatus(st, item));
    tbody.appendChild(tr);
  });
  $("singleFilter").value = st.single.keyword || "";
  $("singleCount").textContent = st.single.loading ? "加载中…" :
    `共 ${records.length}${records.length !== (st.single.records || []).length ? ` / ${st.single.records.length}` : ""} 条`;
  $("singleEditorEmpty").hidden = !!st.single.form;
  $("singleEditorForm").hidden = !st.single.form;
  $("singleEditorTitle").textContent = st.single.form
    ? `编辑单页 #${st.single.selectedId}` : "单页编辑";
  $("singleEditorHint").textContent = st.single.form
    ? "字段来自当前网站 Single/mod 表单；保存前会校验字段并回读确认。"
    : "选择一条单页记录后编辑。";
  const root = $("singleDynamicFields"); root.innerHTML = "";
  root.dataset.cmsNoValidate = st.single.form && (st.single.form.fields || []).some(field => field.form_novalidate) ? "1" : "0";
  if (st.single.form) {
    (st.single.form.fields || []).forEach((field) => {
      root.appendChild(createCmsFieldControl(field, field.value, (value) => {
        field.value = value; st.single.dirty = true;
      }, "single"));
    });
  }
  setMsg("singleMsg", st.single.msg, st.single.msgClass);
  $("btnSingleSave").disabled = !st.single.form || st.busy || st.single.loading || st.single.saving;
  $("btnSingleCancel").disabled = !st.single.form || st.single.saving;
}

async function loadSinglePages(st, force = false) {
  st = st || activeState();
  if (!st || !st.loggedIn || st.busy || (st.single.loaded && !force)) return false;
  st.single.loading = true;
  if (st.id === ACTIVE) renderSingleManager(st);
  const req = nextRequest(st, "single");
  try {
    const result = await api().load_single_pages(st.id, st.single.keyword || "");
    if (!requestIsCurrent(st, "single", req)) return false;
    if (!result || !result.ok) {
      st.single.records = []; st.single.loaded = false;
      setSingleMsg(st, (result && result.msg) || "读取单页列表失败", "bad");
      return false;
    }
    st.single.records = result.records || []; st.single.loaded = true;
    setSingleMsg(st, `已读取 ${st.single.records.length} 个单页。`, "ok");
    return true;
  } catch (error) {
    if (requestIsCurrent(st, "single", req)) setSingleMsg(st, "读取单页列表异常：" + error, "bad");
    return false;
  } finally {
    if (requestIsCurrent(st, "single", req)) {
      st.single.loading = false;
      if (st.id === ACTIVE) renderSingleManager(st);
    }
  }
}

async function openSingleEdit(st, id) {
  st = st || activeState();
  if (!st || st.busy || !st.loggedIn || !id) return;
  st.single.loading = true; st.single.form = null; st.single.selectedId = String(id);
  if (st.id === ACTIVE) renderSingleManager(st);
  try {
    const result = await api().prepare_single_edit(st.id, String(id));
    if (!result || !result.ok) {
      setSingleMsg(st, (result && result.msg) || "读取单页表单失败", result && result.native_url ? "review" : "bad");
      if (result && result.native_url && typeof openNativeRecordUrl === "function")
        await openNativeRecordUrl(st, result.native_url, "动态单页编辑页", {allowBusy: true});
      return;
    }
    st.single.form = result; st.single.dirty = false;
    setSingleMsg(st, "单页表单已载入，可编辑后保存。", "ok");
  } catch (error) {
    setSingleMsg(st, "读取单页表单异常：" + error, "bad");
  } finally {
    st.single.loading = false;
    if (st.id === ACTIVE) renderSingleManager(st);
  }
}

async function saveSingle() {
  const st = activeState(), form = st && st.single.form;
  if (!st || !form || st.busy || !validateCmsControls($("singleDynamicFields"))) return;
  const id = String(st.single.selectedId || form.id || "");
  const reviewKey = mutationReviewKey("single", id);
  if (!await confirmMutationReview(st, reviewKey, "单页保存")) return;
  if (typeof confirmGetWrite === "function" && !await confirmGetWrite(form, "单页")) return;
  const values = Object.fromEntries((form.fields || [])
    .filter((field) => !field.readonly && !field.disabled && field.name)
    .map((field) => [field.name, field.value]));
  st.single.saving = true; if (st.id === ACTIVE) renderSingleManager(st);
  try {
    const result = await api().update_single(st.id, id, values, form.revision || "");
    if (!result || !result.ok) {
      if (result && result.native_url && typeof openNativeRecordUrl === "function") {
        setSingleMsg(st, result.msg || "单页表单已改为动态网页，已切换原生网页。", "review");
        await openNativeRecordUrl(st, result.native_url, "动态单页保存页", {
          allowBusy: true,
          handoff: nativeHandoffFromForm("single", values,
            [`单页：${id}`], result.msg || "动态单页保存已转网页。"),
        });
        return;
      }
      const review = resultNeedsMutationReview(result);
      if (review) setMutationReview(st, reviewKey, (result && result.msg) || "单页保存结果待核对，请先到后台确认。 ");
      return setSingleMsg(st, (result && result.msg) || "保存单页失败", review ? "review" : "bad");
    }
    clearMutationReview(st, reviewKey);
    st.single.records = result.records || st.single.records;
    st.single.loaded = true; st.single.form = null; st.single.dirty = false;
    setSingleMsg(st, (result.msg || "单页已保存") + uploadResultNotice(result.upload_metadata), "ok");
  } catch (error) {
    const review = resultNeedsMutationReview(null, error);
    if (review) setMutationReview(st, reviewKey, "单页保存请求可能已到达后台，请先核对后再重试。");
    setSingleMsg(st, "保存单页异常：" + error, review ? "review" : "bad");
  } finally {
    st.single.saving = false;
    if (st.id === ACTIVE) renderSingleManager(st);
  }
}

async function toggleSingleStatus(st, item) {
  st = st || activeState();
  const toggle = item && item.toggles && item.toggles.status;
  if (!st || st.busy || !item || !toggle) return;
  const id = String(item.id || ""), value = String(toggle.target || "");
  const reviewKey = mutationReviewKey("single_status", id);
  if (!await confirmMutationReview(st, reviewKey, "单页状态切换")) return;
  st.single.loading = true;
  if (st.id === ACTIVE) renderSingleManager(st);
  try {
    const result = await api().toggle_single_status(st.id, id, value, toggle.url || "");
    if (!result || !result.ok) {
      const review = resultNeedsMutationReview(result);
      if (review) setMutationReview(st, reviewKey, (result && result.msg) || "单页状态切换结果待核对，请先到后台确认。");
      return setSingleMsg(st, (result && result.msg) || "状态切换失败", review ? "review" : "bad");
    }
    clearMutationReview(st, reviewKey);
    setSingleMsg(st, result.msg || "单页状态已切换，正在刷新列表…", "ok");
    st.single.loaded = false;
    await loadSinglePages(st, true);
  } catch (error) {
    const review = resultNeedsMutationReview(null, error);
    if (review) setMutationReview(st, reviewKey, "单页状态切换请求可能已到达后台，请先核对后再重试。");
    setSingleMsg(st, "状态切换异常：" + error, review ? "review" : "bad");
    if (st.id === ACTIVE) renderSingleManager(st);
  } finally {
    // A successful path hands ownership to loadSinglePages(), which clears
    // loading after the verified list refresh. Error paths clear it here.
    if (st.single.loading && (!st.single.loaded || st.single.msgClass === "bad" || st.single.msgClass === "review")) {
      st.single.loading = false;
      if (st.id === ACTIVE) renderSingleManager(st);
    }
  }
}

/* ══════════ 其他后台模块：菜单发现 + 动态表单 ══════════ */
function setAdminModuleMsg(st, text, cls) {
  if (!st || !st.adminModules) return;
  st.adminModules.msg = text || ""; st.adminModules.msgClass = cls || "";
  if (st.id === ACTIVE) setMsg("adminModuleMsg", text, cls);
}

function renderAdminModules(st) {
  if (!st || !st.adminModules) return;
  const tbody = $("adminModuleTable") && $("adminModuleTable").querySelector("tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  (st.adminModules.records || []).forEach((item) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(item.label || item.route || "—")}</td>` +
      `<td><code>${esc(item.route || "")}</code></td>` +
      `<td><button type="button" class="ghost mini admin-module-open">查看页面</button>` +
      ` <button type="button" class="ghost mini admin-module-native" title="在完整浏览器中打开当前后台页面">原生网页</button></td>`;
    const button = tr.querySelector(".admin-module-open");
    if (button) button.addEventListener("click", () => openAdminModule(st, item));
    const native = tr.querySelector(".admin-module-native");
    if (native) native.addEventListener("click", () => openAdminModuleNative(st, item));
    tbody.appendChild(tr);
  });
  $("adminModuleCount").textContent = st.adminModules.loading ? "加载中…" :
    `共 ${(st.adminModules.records || []).length} 个可发现模块`;
  $("adminModuleEditorEmpty").hidden = !!(st.adminModules.form || st.adminModules.view);
  $("adminModuleEditorForm").hidden = !st.adminModules.form;
  const readonly = $("adminModuleReadOnly");
  if (readonly) readonly.hidden = !st.adminModules.view || !!st.adminModules.form;
  $("adminModuleEditorTitle").textContent = st.adminModules.form
    ? `编辑：${st.adminModules.label || st.adminModules.url}`
    : st.adminModules.view ? `查看：${st.adminModules.label || st.adminModules.url}` : "后台模块查看";
  $("adminModuleEditorHint").textContent = st.adminModules.form
    ? ((String(st.adminModules.form.method || "post").toLowerCase() === "get"
        ? "此网页保存表单使用 GET；每次保存前仍会要求确认。"
        : "此网页保存表单使用 POST。") +
       "字段来自当前后台菜单页面；只提交可写控件，并使用 revision 防止覆盖并发修改。")
    : st.adminModules.view ? "以下内容来自当前登录会话的只读 GET；脚本、样式和副作用链接均未执行。"
      : "选择一个当前菜单中发现的模块。删除、状态和清缓存等副作用链接不会在此通用入口开放。";
  const view = st.adminModules.view;
  const viewTitle = $("adminModuleReadOnlyTitle");
  const viewText = $("adminModuleReadOnlyText");
  const viewTables = $("adminModuleReadOnlyTables");
  const viewLinks = $("adminModuleReadOnlyLinks");
  if (viewTitle) viewTitle.textContent = view ? (view.title || view.label || view.route || "后台页面") : "";
  if (viewText) viewText.textContent = view ? (view.text || "页面没有可显示的文本内容。") : "";
  if (viewTables) viewTables.innerHTML = view ? (view.tables || []).map((table) => {
    const head = (table.headers || []).length
      ? `<thead><tr>${table.headers.map((cell) => `<th>${esc(cell)}</th>`).join("")}</tr></thead>` : "";
    const rows = (table.rows || []).map((row) => `<tr>${(row || []).map((cell) => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("");
    return `<table>${head}<tbody>${rows}</tbody></table>`;
  }).join("") : "";
  if (viewLinks) {
    viewLinks.innerHTML = view && (view.links || []).length
      ? `<details><summary>页面内只读链接（${view.links.length}）</summary><ul>${view.links.map((link, index) =>
        `<li><code>${esc(link.route || "")}</code> ${esc(link.label || "")} ` +
        `<button type="button" class="ghost mini admin-readonly-link" data-link-index="${index}">打开网页</button></li>`
      ).join("")}</ul></details>` : "";
    // The inspection response contains only links that passed the backend
    // same-origin and destructive-route filters.  Still route the click
    // through the authenticated native bridge, which validates the current
    // session origin again and synchronizes cookies before opening it.
    if (view && typeof viewLinks.querySelectorAll === "function") {
      viewLinks.querySelectorAll(".admin-readonly-link").forEach((button) => {
        button.addEventListener("click", () => {
          const index = Number(button.dataset.linkIndex);
          const link = Number.isInteger(index) && view.links ? view.links[index] : null;
          if (link && link.url) openNativeRecordUrl(st, link.url,
            link.label || link.route || "后台只读页面");
        });
      });
    }
  }
  const root = $("adminModuleDynamicFields"); root.innerHTML = "";
  const submitterSelect = $("adminModuleSubmitter");
  if (submitterSelect) {
    const options = Array.isArray(st.adminModules.form && st.adminModules.form.submitter_options)
      ? st.adminModules.form.submitter_options : [];
    submitterSelect.innerHTML = options.length
      ? options.map((item, index) => {
          const suffix = item.requires_native_click ? "（需原生网页点击）" : "";
          return `<option value="${index}">${esc((item.label || item.name || "提交") + suffix)}</option>`;
        }).join("")
      : '<option value="">表单没有可点击提交按钮</option>';
    submitterSelect.disabled = !options.length || st.busy || st.adminModules.saving;
    const selected = st.adminModules.form && st.adminModules.form.submitter;
    const selectedIndex = selected && options.findIndex((item) =>
      ["name", "type", "value", "formaction", "formmethod", "formenctype"]
        .every((key) => String(item[key] || "") === String(selected[key] || "")));
    submitterSelect.value = String(selectedIndex >= 0 ? selectedIndex : (options.length === 1 ? 0 : ""));
  }
  root.dataset.cmsNoValidate = st.adminModules.form && (st.adminModules.form.fields || []).some(field => field.form_novalidate) ? "1" : "0";
  if (st.adminModules.form) {
    (st.adminModules.form.fields || []).forEach((field) => {
      root.appendChild(createCmsFieldControl(field, field.value, (value) => {
        field.value = value; st.adminModules.dirty = true;
      }, "admin_module"));
    });
  }
  setMsg("adminModuleMsg", st.adminModules.msg, st.adminModules.msgClass);
  $("btnAdminModuleSave").disabled = !st.adminModules.form || st.busy ||
    st.adminModules.loading || st.adminModules.saving;
  $("btnAdminModuleCancel").disabled = !st.adminModules.form || st.adminModules.saving;
}

async function openAdminModuleNative(st, item, options) {
  options = options || {};
  st = st || activeState();
  if (!st || st.busy || !st.loggedIn || !item || !item.url) return false;
  let opened;
  // Keep the small UI function usable in isolated DOM tests and in older
  // embedded shells that do not expose the shared logger; the direct bridge
  // fallback still opens the same verified URL.
  if (typeof openNativeRecordUrl === "function" && typeof log === "function") {
    opened = await openNativeRecordUrl(st, item.url, item.label || item.route || "原生后台页面", options);
  } else {
    const fallback = await api().open_external_url(String(item.url));
    opened = !!(fallback && fallback.ok && fallback.opened);
  }
  setAdminModuleMsg(st, opened
    ? "已打开原生后台页面；动态脚本、完整编辑器和站点主题由网页执行。"
    : "无法打开原生网页；请确认系统浏览器和登录会话。",
    opened ? "ok" : "bad");
  return opened;
}

function nativeHandoffFields(values) {
  const result = [];
  const seen = new Set();
  const secret = /(?:pass(?:word)?|pwd|token|secret|cookie|csrf|captcha|checkcode|formcheck|nonce|auth|api[_-]?key|signature|sign(?:ature)?|session(?:id)?|credential|private|bearer|jwt|salt)/i;
  const add = (name, value) => {
    name = String(name || "").trim();
    if (!/^[A-Za-z_][A-Za-z0-9_.:\[\]-]{0,159}$/.test(name) ||
        secret.test(name) || seen.has(name)) return;
    if (value == null || typeof value === "function" || typeof value === "object" && !Array.isArray(value)) return;
    if (Array.isArray(value)) {
      value = value.slice(0, 50).filter((item) =>
        item == null || ["string", "number", "boolean"].includes(typeof item))
        .map((item) => typeof item === "boolean" ? item : String(item ?? ""));
    } else if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") return;
    if (typeof value === "string") value = value.slice(0, 65536);
    result.push({name, value}); seen.add(name);
  };
  Object.entries(values || {}).forEach(([name, value]) => add(name, value));
  return result.slice(0, 300);
}

function nativeHandoffSnapshot(st, workflow, snap, extra = {}) {
  snap = snap || {};
  const values = Object.assign({}, snap.backendFields || {});
  // `overrides` are the user's explicit values.  Map HTML keys back to the
  // discovered CMS field names where possible; do not leak local paths.
  const mapping = snap.mapping || {};
  Object.entries(snap.overrides || {}).forEach(([key, value]) => {
    const name = mapping[key] || key;
    values[name] = value;
  });
  (snap.parsedFields || []).forEach((field) => {
    const name = mapping[field.key];
    if (name && !Object.prototype.hasOwnProperty.call(values, name)) values[name] = field.value;
  });
  const notes = [];
  const assetName = (path) => path ? `素材：${fileName(path)}` : "";
  if (snap.html) notes.push(`正文文件：${fileName(snap.html)}（请在网页编辑器中确认）`);
  [snap.thumbPath, ...(snap.manualImages || []), ...(snap.carouselImages || [])]
    .map(assetName).filter(Boolean).forEach((note) => notes.push(note));
  if (snap.imageReplacements && Object.keys(snap.imageReplacements).length)
    notes.push(`正文替图：${Object.keys(snap.imageReplacements).length} 项（请在网页中确认）`);
  if (snap.ico === "first") notes.push("缩略图：首图需由网页缩略图控件再次上传");
  if (snap.articleId) notes.unshift(`文章 ID：${snap.articleId}`);
  if (snap.scode) notes.unshift(`栏目：${snap.scode}`);
  return {
    workflow: String(workflow || "native"),
    message: extra.message || "软件已保留当前草稿；文件控件和动态编辑器不会被自动提交。",
    fields: nativeHandoffFields(values),
    notes: notes.slice(0, 30),
  };
}

function nativeHandoffFromForm(workflow, values, notes = [], message = "") {
  return {
    workflow: String(workflow || "native"),
    message: message || "软件已保留当前表单字段；文件控件和动态脚本不会被自动提交。",
    fields: nativeHandoffFields(values || {}),
    notes: (Array.isArray(notes) ? notes : [notes]).filter(Boolean).slice(0, 30),
  };
}

// Open a URL discovered from the authenticated backend itself.  These
// links are deliberately passed through the same explicit user-click bridge
// as the generic admin fallback; the desktop form never guesses a route or
// replays a write request on the user's behalf.  An optional handoff only
// fills ordinary, explicitly captured controls and never submits the page.
async function openNativeRecordUrl(st, url, label = "后台页面", options = {}) {
  st = st || activeState();
  const target = String(url || "").trim();
  const allowBusy = !!(options && options.allowBusy);
  if (!st || (!allowBusy && st.busy) || !st.loggedIn || !target) return false;
  try {
    let result = null;
    if (typeof api().open_authenticated_url === "function")
      result = await api().open_authenticated_url(st.id, target, label,
        options && options.handoff ? options.handoff : null);
    if (!result || !result.ok || !result.opened)
      result = await api().open_external_url(target);
    if (!result || !result.ok || !result.opened) {
      setAreaMsg(st, "edit", "editMsg",
        (result && result.msg) || ("无法打开" + label + "；请确认系统浏览器和登录会话。"), "bad");
      return false;
    }
    const mode = result.mode === "embedded" ? "已在嵌入式浏览器打开" : "已在系统浏览器打开";
    log(mode + label + "；动态脚本和完整编辑器由网页执行。", st.id);
    return true;
  } catch (error) {
    try { await api().open_external_url(target); } catch (_) {}
    log("打开" + label + "失败，已尝试系统浏览器：" + error, st.id);
    return false;
  }
}

async function openEditArticleNative() {
  const st = activeState();
  if (!st || st.busy || !st.loggedIn) return;
  const item = (st.edit.articles || []).find((row) =>
    String(row.id || "") === String(st.edit.artId || ""));
  const target = item && (item.edit_url || st.edit.nativeUrl);
  if (!item || !target)
    return setAreaMsg(st, "edit", "editMsg", "当前文章没有发现安全的真实后台编辑地址", "bad");
  await openNativeRecordUrl(st, target, "文章后台编辑页");
}

async function openEditArticlePreview() {
  const st = activeState();
  if (!st || st.busy || !st.loggedIn) return;
  const item = (st.edit.articles || []).find((row) =>
    String(row.id || "") === String(st.edit.artId || ""));
  if (!item || !item.view_url)
    return setAreaMsg(st, "edit", "editMsg",
      "当前文章列表没有发现明确的同源前台/预览地址；不会猜测 URL", "bad");
  await openNativeRecordUrl(st, item.view_url, "文章前台预览页");
}

async function loadAdminModules(st, force = false) {
  st = st || activeState();
  if (!st || !st.loggedIn || st.busy || (st.adminModules.loaded && !force)) return false;
  st.adminModules.loading = true;
  if (st.id === ACTIVE) renderAdminModules(st);
  const req = nextRequest(st, "adminModules");
  try {
    const result = await api().load_admin_modules(st.id);
    if (!requestIsCurrent(st, "adminModules", req)) return false;
    if (!result || !result.ok) {
      st.adminModules.loaded = false; st.adminModules.records = [];
      setAdminModuleMsg(st, (result && result.msg) || "读取后台模块失败", "bad");
      return false;
    }
    st.adminModules.records = result.modules || []; st.adminModules.loaded = true;
    setAdminModuleMsg(st, `已发现 ${st.adminModules.records.length} 个可安全打开的后台模块。`, "ok");
    return true;
  } catch (error) {
    if (requestIsCurrent(st, "adminModules", req)) setAdminModuleMsg(st, "读取后台模块异常：" + error, "bad");
    return false;
  } finally {
    if (requestIsCurrent(st, "adminModules", req)) {
      st.adminModules.loading = false;
      if (st.id === ACTIVE) renderAdminModules(st);
    }
  }
}

async function openAdminModule(st, item) {
  st = st || activeState();
  if (!st || st.busy || !st.loggedIn || !item || !item.url) return;
  st.adminModules.loading = true; st.adminModules.form = null; st.adminModules.view = null;
  st.adminModules.url = String(item.url); st.adminModules.label = String(item.label || item.route || "");
  if (st.id === ACTIVE) renderAdminModules(st);
  try {
    const bridge = api();
    let inspection = null;
    if (typeof bridge.inspect_admin_module === "function") {
      inspection = await bridge.inspect_admin_module(st.id, st.adminModules.url);
      if (!inspection || !inspection.ok)
        return setAdminModuleMsg(st, (inspection && inspection.msg) || "读取后台模块失败", "bad");
      st.adminModules.view = inspection;
    }
    if (inspection && !inspection.has_form) {
      st.adminModules.form = null;
      st.adminModules.dirty = false;
      setAdminModuleMsg(st, "该后台模块没有可安全提交的表单，已切换为只读查看。", "ok");
    } else {
      const result = await bridge.prepare_admin_module(st.id, st.adminModules.url);
      if (!result || !result.ok) {
        // A runtime-owned form must never be approximated by a guessed POST.
        // The backend returns the server-resolved page URL so the same click
        // can continue in the authenticated browser instead.
        if (result && result.native_url) {
          st.adminModules.form = null;
          st.adminModules.dirty = false;
          setAdminModuleMsg(st, (result.msg || "该表单由网页脚本控制，已切换原生网页。"), "review");
          await openAdminModuleNative(st, {
            url: result.native_url,
            label: st.adminModules.label || st.adminModules.url,
            route: item.route,
          });
          return;
        }
        return setAdminModuleMsg(st, (result && result.msg) || "读取后台模块表单失败", "bad");
      }
      if (result.native_only || result.native_url) {
        st.adminModules.form = null;
        st.adminModules.dirty = false;
        setAdminModuleMsg(st, result.native_reason ||
          "该表单由网页脚本控制，已切换原生网页完成操作。", "review");
        await openAdminModuleNative(st, {
          url: result.native_url,
          label: st.adminModules.label || st.adminModules.url,
          route: item.route,
        });
        return;
      }
      st.adminModules.form = result; st.adminModules.revision = result.revision || "";
      st.adminModules.dirty = false;
      setAdminModuleMsg(st, "后台模块表单已载入；同时保留只读页面快照。", "ok");
    }
  } catch (error) {
    setAdminModuleMsg(st, "读取后台模块表单异常：" + error, "bad");
  } finally {
    st.adminModules.loading = false;
    if (st.id === ACTIVE) renderAdminModules(st);
  }
}

async function saveAdminModule() {
  const st = activeState(), form = st && st.adminModules.form;
  if (!st || !form || st.busy || !validateCmsControls($("adminModuleDynamicFields"))) return;
  const reviewKey = mutationReviewKey("admin_module", st.adminModules.url);
  if (!await confirmMutationReview(st, reviewKey, "后台模块保存")) return;
  if (String(form.method || "post").toLowerCase() === "get" &&
      !await confirmDialog("", {title: "网页表单使用 GET 保存", kind: "warn",
        okText: "确认发送", cancelText: "取消", lines: [
          "当前后台的实际保存表单使用 GET 方法。",
          "软件将按网页的查询参数顺序发送，并在保存后重新读取字段核对。",
          "请确认这是后台提供的保存按钮，而不是查询或筛选表单。",
        ]})) return;
  const values = Object.fromEntries((form.fields || [])
    .filter((field) => !field.readonly && !field.disabled && field.name)
    .map((field) => [field.name, field.value]));
  const submitterOptions = Array.isArray(form.submitter_options) ? form.submitter_options : [];
  const submitterSelect = $("adminModuleSubmitter");
  const submitterIndex = submitterSelect ? Number(submitterSelect.value) : NaN;
  const submitter = Number.isInteger(submitterIndex) && submitterIndex >= 0
    ? submitterOptions[submitterIndex] : null;
  if (submitterOptions.length > 1 && !submitter)
    return setAdminModuleMsg(st, "请选择网页中实际点击的提交按钮（保存/提交，而不是预览/取消）", "bad");
  st.adminModules.saving = true; if (st.id === ACTIVE) renderAdminModules(st);
  try {
    const result = await api().update_admin_module(st.id, st.adminModules.url, values, form.revision || "", submitter);
    if (!result || !result.ok) {
      if (result && result.native_url) {
        st.adminModules.form = null;
        st.adminModules.dirty = false;
        setAdminModuleMsg(st, result.msg || result.native_reason ||
          "后台页面已改为动态表单，已切换原生网页。", "review");
        await openAdminModuleNative(st, {
          url: result.native_url,
          label: st.adminModules.label || st.adminModules.url,
          route: st.adminModules.url,
        }, {handoff: nativeHandoffFromForm("admin_module", values,
          [`模块：${st.adminModules.label || st.adminModules.url}`],
          result.msg || "动态后台模块保存已转网页。")});
        return;
      }
      const review = resultNeedsMutationReview(result);
      if (review) setMutationReview(st, reviewKey, (result && result.msg) || "后台模块保存结果待核对，请先到后台确认。");
      return setAdminModuleMsg(st, (result && result.msg) || "保存后台模块失败", review ? "review" : "bad");
    }
    clearMutationReview(st, reviewKey);
    st.adminModules.form = result; st.adminModules.revision = result.revision || "";
    st.adminModules.dirty = false;
    setAdminModuleMsg(st, (result.msg || "后台模块已保存并回读") +
      uploadResultNotice(result.upload_metadata), "ok");
  } catch (error) {
    const review = resultNeedsMutationReview(null, error);
    if (review) setMutationReview(st, reviewKey, "后台模块保存请求可能已到达后台，请先核对后再重试。");
    setAdminModuleMsg(st, "保存后台模块异常：" + error, review ? "review" : "bad");
  } finally {
    st.adminModules.saving = false;
    if (st.id === ACTIVE) renderAdminModules(st);
  }
}

/* ══════════ 通用映射表 ══════════ */
function invalidateEditLinkReport(st) {
  // 新 HTML 的正文或其映射发生改变后，旧的内链结论已不可靠。
  st.edit.linkReport = null;
  if (st.id === ACTIVE) renderEditLinkReport(st);
  clearTimeout(st._editLinkTimer);
  if (st.edit.checkLinks === true && st.edit.artId && !st.busy && !st.edit.formLoading && !st.edit.htmlLoading) {
    const articleId = st.edit.artId;
    st._editLinkTimer = setTimeout(() => {
      if (isLiveState(st) && !st.busy && !st.edit.formLoading &&
          !st.edit.htmlLoading && st.edit.artId === articleId)
        refreshEditLinks(st, !st.edit.html, articleId);
    }, 350);
  }
}

function stableStringify(value) {
  if (Array.isArray(value)) return "[" + value.map(stableStringify).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((key) =>
      JSON.stringify(key) + ":" + stableStringify(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function invalidatePreflight(st) {
  if (!st || !st.pub) return;
  st.pub.preflightCache = null;
}

function parsedSourceObject(area) {
  const result = {};
  (area.parsedFields || []).forEach((item) => { result[item.key] = String(item.value ?? ""); });
  return result;
}

const CMS_FLAG_PROPERTIES = { istop: "top", isrecommend: "rec", isheadline: "head" };

function cmsFlagChanges(area) {
  const values = {};
  for (const name of Object.keys(CMS_FLAG_PROPERTIES)) {
    if (Object.prototype.hasOwnProperty.call(area.flagChanges || {}, name))
      values[name] = !!area.flagChanges[name];
  }
  return values;
}

function initializeCmsFlags(area, fields, explicit = {}) {
  area.flagChanges = {};
  for (const [name, property] of Object.entries(CMS_FLAG_PROPERTIES)) {
    const raw = (fields || []).find((field) => field.name === name)?.value;
    const selected = Array.isArray(raw) ? raw.some((v) => /^(1|on|true)$/i.test(String(v))) :
      /^(1|on|true)$/i.test(String(raw ?? ""));
    area[property] = selected;
    if (Object.prototype.hasOwnProperty.call(explicit, name)) {
      area.flagChanges[name] = !!explicit[name];
      area[property] = !!explicit[name];
    }
  }
}

function publishDraftPayload(st) {
  const payload = {
    category: st.pub.cat, html_path: st.pub.html,
    mapping: Object.assign({}, st.pub.mapping || {}),
    overrides: Object.assign({}, st.pub.overrides || {}),
    backend_fields: Object.assign({}, st.pub.backendValues || {}),
    media: { manual_images: Array.from(st.pub.manualImages || []),
      carousel_images: Array.from(st.pub.carouselImages || []),
      thumbnail_path: st.pub.thumbPath || "", thumbnail_url: st.pub.thumbUrl || "",
      ...gallerySubmission(st.pub) },
    flags: cmsFlagChanges(st.pub),
    submitter: st.pub.submitter ? Object.assign({}, st.pub.submitter) : null,
    preferences: { write_review_required: !!st.pub.requiresReview, media_processing_version: 2, flag_intent: "explicit", link_check_enabled: st.pub.checkLinks === true, width_mode: st.pub.width, thumbnail_source: st.pub.ico,
      remember_thumbnail: !!st.pub.rememberThumb, carousel_size: "original",
      insert_strategy: st.pub.insertStrategy || "top",
      carousel_width: st.pub.carouselW, carousel_height: st.pub.carouselH },
  };
  return typeof attachFileMimeHints === "function" ? attachFileMimeHints(payload) : payload;
}

function editDraftPayload(st) {
  const payload = {
    category: st.edit.cat, article_id: st.edit.artId, html_path: st.edit.html,
    mapping: Object.assign({}, st.edit.mapping || {}),
    overrides: Object.assign({}, st.edit.overrides || {}),
    backend_fields: Object.assign({}, st.edit.backendValues || {}),
    media: { carousel_images: Array.from(st.edit.carouselImages || []),
      thumbnail_path: st.edit.thumbPath || "", thumbnail_url: st.edit.thumbUrl || "",
      image_replacements: Object.values(st.edit.imageReplacements || {}).map((item) => ({...item})),
      image_content_hash: String(st.edit.contentHash || ""),
      ...gallerySubmission(st.edit) },
    flags: cmsFlagChanges(st.edit),
    submitter: st.edit.submitter ? Object.assign({}, st.edit.submitter) : null,
    preferences: { write_review_required: !!st.edit.requiresReview, media_processing_version: 2, flag_intent: "explicit", link_check_enabled: st.edit.checkLinks === true, refresh_publish_date: !!st.edit.refreshDate,
      cover_mode: "part", thumbnail_source: st.edit.ico,
      carousel_size: "original", insert_strategy: st.edit.insertStrategy || "before_h2", carousel_width: st.edit.carouselW,
      carousel_height: st.edit.carouselH, carousel_mode: st.edit.carouselMode },
  };
  return typeof attachFileMimeHints === "function" ? attachFileMimeHints(payload) : payload;
}

function batchDraftPayload(st) {
  const payload = Object.assign({}, publishDraftPayload(st), {
    html_paths: (st.batch.items || []).map((item) => item.path),
    batch_items: (st.batch.items || []).map((item, index) => ({
      id: String(item.id || index + 1), html_path: item.path,
      status: item.status || "pending", title: item.title || "",
      error: item.message || "", attempts: item.attempts || 0,
      mapping: Object.assign({}, item.mapping || {}),
      overrides: Object.assign({}, item.overrides || {}),
      settings: item.settings && typeof captureBatchSettings === "function" ? captureBatchSettings({pub: {
        cat: item.settings.scode, mapping: item.settings.mapping || {},
        backendValues: item.settings.backendFields || {}, width: item.settings.width,
        manualImages: item.settings.manualImages || [], ico: item.settings.ico,
        thumbPath: item.settings.thumbPath, thumbUrl: item.settings.thumbUrl,
        carouselImages: item.settings.carouselImages || [], galleryPlan: item.settings.galleryPlan,
        carouselSize: "original", carouselW: item.settings.carouselW,
        insertStrategy: item.settings.insertStrategy || "top",
        carouselH: item.settings.carouselH, top: item.settings.top, rec: item.settings.rec,
        head: item.settings.head, flagChanges: item.settings.flagChanges || {},
        checkLinks: item.settings.checkLinks,
        // A batch item is submitted as its own browser form.  Preserve the
        // selected submitter in the durable draft instead of silently falling
        // back to the queue-wide/current article button after a restart.
        submitter: item.settings.submitter || null }}) : (item.settings || null),
    })),
    preferences: Object.assign({}, publishDraftPayload(st).preferences, {
      link_policy_version: 1,
      apply_verified_links: st.batch.applyVerified === true,
      skip_unverified_links: st.batch.skipUnverified === true,
    }),
  });
  return typeof attachFileMimeHints === "function" ? attachFileMimeHints(payload) : payload;
}

function draftMeaningful(st, workflow) {
  if (workflow === "publish") return !!(st.pub.html || st.pub.cat ||
    Object.keys(st.pub.overrides || {}).length || Object.keys(st.pub.backendValues || {}).length ||
    st.pub.manualImages.length ||
    st.pub.carouselImages.length || st.pub.galleryPlan != null || st.pub.thumbPath || st.pub.thumbUrl || st.pub.ico === "clear");
  if (workflow === "edit") return !!(st.edit.artId || st.edit.html ||
    Object.keys(st.edit.overrides || {}).length || st.edit.carouselImages.length || st.edit.galleryPlan != null ||
    Object.keys(st.edit.imageReplacements || {}).length || st.edit.thumbPath || st.edit.thumbUrl || st.edit.ico === "clear");
  return !!(st.batch.items || []).length;
}

const DRAFT_WORKFLOWS = ["publish", "edit", "batch"];

function draftRevisionSnapshot(st) {
  const revisions = st._draftRevision || {};
  return Object.fromEntries(DRAFT_WORKFLOWS.map((workflow) =>
    [workflow, Number(revisions[workflow] || 0)]));
}

function beginDraftRecovery(st) {
  if (!st) return;
  clearTimeout(st._draftRecoveryRetryTimer);
  st._draftChecked = false;
  st._draftRecoveryPending = true;
  st._draftRecoveryInFlight = false;
  st._draftRecoveryRetries = 0;
  st._draftRecoveryRetryTimer = null;
  st._draftRecoveryBase = draftRevisionSnapshot(st);
  st._draftDeferredSaves = {};
  st._draftSaveBlocked = {};
  st._draftPersistenceSuspended = false;
}

function queueDraftSave(st, workflow) {
  clearTimeout(st._draftTimers[workflow]);
  st._draftTimers[workflow] = setTimeout(async () => {
    if (!isLiveState(st) || !st.loggedIn || st._restoringDraft ||
        st._draftRecoveryPending || st._draftPersistenceSuspended ||
        (st._draftSaveBlocked || {})[workflow]) return;
    try {
      const result = await persistDraftNow(st, workflow);
      if (!result || !result.ok)
        log(`草稿自动保存失败（${workflow}）：${(result && result.msg) || "未知错误"}`, st.id);
    } catch (e) { log(`草稿自动保存失败（${workflow}）：${e}`, st.id); }
  }, 650);
}

function scheduleDraftSave(st, workflow) {
  if (!st || !st.loggedIn || st._restoringDraft || !DRAFT_WORKFLOWS.includes(workflow)) return;
  st._draftRevision = st._draftRevision || { publish: 0, edit: 0, batch: 0 };
  st._draftRevision[workflow] = Number(st._draftRevision[workflow] || 0) + 1;
  clearTimeout(st._draftTimers[workflow]);
  if (st._draftRecoveryPending || st._draftPersistenceSuspended ||
      (st._draftSaveBlocked || {})[workflow]) {
    st._draftDeferredSaves = st._draftDeferredSaves || {};
    st._draftDeferredSaves[workflow] = true;
    return;
  }
  queueDraftSave(st, workflow);
}

async function persistDraftNow(st, workflow) {
  if (!st || !st.loggedIn) return { ok: true };
  clearTimeout(st._draftTimers[workflow]);
  if (st._draftRecoveryPending || st._draftPersistenceSuspended ||
      (st._draftSaveBlocked || {})[workflow]) {
    st._draftDeferredSaves = st._draftDeferredSaves || {};
    st._draftDeferredSaves[workflow] = true;
    return { ok: true, deferred: true };
  }
  if (!draftMeaningful(st, workflow)) return api().delete_draft(st.id, workflow);
  const payload = workflow === "publish" ? publishDraftPayload(st)
    : (workflow === "edit" ? editDraftPayload(st) : batchDraftPayload(st));
  const result = await api().save_draft(st.id, workflow, payload);
  if (result && result.ok && Array.isArray(result.asset_warnings) && result.asset_warnings.length)
    log(`草稿已保存，但 ${result.asset_warnings.length} 个素材未能创建快照；恢复时将继续核对原路径。`, st.id);
  return result;
}

async function flushDrafts(st) {
  if (!st || !st.loggedIn || st._draftRecoveryPending || st._draftPersistenceSuspended) return;
  await Promise.all(DRAFT_WORKFLOWS.filter((workflow) =>
    !(st._draftSaveBlocked || {})[workflow]).map((workflow) =>
    persistDraftNow(st, workflow).catch(() => null)));
}

function finishDraftRecovery(st, skipWorkflows) {
  const skip = new Set(skipWorkflows || []);
  const deferred = Object.keys(st._draftDeferredSaves || {});
  st._draftRecoveryPending = false;
  st._draftRecoveryInFlight = false;
  st._draftRecoveryRetries = 0;
  clearTimeout(st._draftRecoveryRetryTimer);
  st._draftRecoveryRetryTimer = null;
  st._draftDeferredSaves = {};
  deferred.forEach((workflow) => {
    if (!skip.has(workflow) && !(st._draftSaveBlocked || {})[workflow])
      queueDraftSave(st, workflow);
  });
}

function notePublishChanged(st) {
  invalidatePreflight(st);
  if (st.batch && st.batch.editingIndex >= 0) {
    const item = st.batch.items[st.batch.editingIndex];
    if (item && item.path === st.pub.html) {
      item.mapping = Object.assign({}, st.pub.mapping || {});
      item.overrides = Object.assign({}, st.pub.overrides || {});
      // Any control change while editing a queue row (thumbnail, gallery,
      // dimensions, flags, link policy, or submitter) must be durable too.
      // Otherwise the next batch draft save would silently retain the row's
      // previous settings even though the visible form changed.
      item.settings = typeof captureBatchSettings === "function"
        ? captureBatchSettings(st) : item.settings;
    }
  }
  scheduleDraftSave(st, "publish");
  if ((st.batch.items || []).length) scheduleDraftSave(st, "batch");
}

async function deleteDraftNow(st, workflow) {
  if (!st) return;
  clearTimeout(st._draftTimers[workflow]);
  try {
    const result = await api().delete_draft(st.id, workflow);
    if (result && result.ok) {
      delete (st._draftSaveBlocked || {})[workflow];
      delete (st._draftDeferredSaves || {})[workflow];
    }
  } catch (_) {}
}

function applyDraftPreferences(area, prefs) {
  prefs = prefs || {};
  if (Object.prototype.hasOwnProperty.call(prefs, "write_review_required"))
    area.requiresReview = !!prefs.write_review_required;
  if (prefs.width_mode) area.width = prefs.width_mode;
  if (["top", "before_h2", "after_first_paragraph"].includes(String(prefs.insert_strategy || "")))
    area.insertStrategy = String(prefs.insert_strategy);
  if (prefs.thumbnail_source) area.ico = prefs.thumbnail_source;
  if (Object.prototype.hasOwnProperty.call(prefs, "remember_thumbnail"))
    area.rememberThumb = !!prefs.remember_thumbnail;
  if (prefs.cover_mode) area.cover = "part";
  if (Object.prototype.hasOwnProperty.call(prefs, "refresh_publish_date"))
    area.refreshDate = !!prefs.refresh_publish_date;
  area.checkLinks = prefs.link_check_enabled === true;
  if (prefs.carousel_size && prefs.carousel_size !== "original")
    area._legacyCarouselSize = String(prefs.carousel_size);
  // Version 2 deliberately has no client-side image transform.  Old drafts
  // are normalized to the same raw-upload behavior rather than replaying a
  // stale crop request after restart.
  area.carouselSize = "original";
  if (prefs.carousel_width != null) area.carouselW = Number(prefs.carousel_width) || 800;
  if (prefs.carousel_height != null) area.carouselH = Number(prefs.carousel_height) || 800;
  if (prefs.carousel_mode) area.carouselMode = prefs.carousel_mode;
}

async function restorePublishDraft(st, draft) {
  if (typeof restoreFileMimeHints === "function") restoreFileMimeHints(draft);
  const savedMapping = Object.assign({}, draft.mapping || {});
  const savedOverrides = Object.assign({}, draft.overrides || {});
  const savedBackendFields = Object.assign({}, draft.backend_fields || {});
  const savedSubmitter = draft.submitter && typeof draft.submitter === "object"
    ? Object.assign({}, draft.submitter) : null;
  st.pub.cat = String(draft.category || "");
  st.pub.html = String(draft.html_path || "");
  const media = draft.media || {}, flags = draft.flags || {};
  st.pub.manualImages = Array.from(media.manual_images || []);
  st.pub.carouselImages = Array.from(media.carousel_images || []);
  st.pub.galleryPlan = media.gallery_plan != null ? media.gallery_plan.map(item => ({...item})) : null;
  st.pub.thumbPath = String(media.thumbnail_path || "");
  st.pub.thumbUrl = String(media.thumbnail_url || "");
  st.pub.flagChanges = {};
  applyDraftPreferences(st.pub, draft.preferences);
  if (draft.preferences?.carousel_size && draft.preferences.carousel_size !== "original")
    log("旧草稿包含客户端裁切设置；为保持与后台直接上传一致，已改为上传原文件。需要尺寸处理请使用原生网页。", st.id);
  if (st.pub.cat) {
    const category = await api().select_category(st.id, st.pub.cat);
    if (category && category.ok) {
      st.pub.fields = category.fields || [];
      st.pub.nativeUrl = String(category.page_url || "");
      st.pub.submitterOptions = Array.isArray(category.submitter_options)
        ? category.submitter_options : [];
      const matchedSubmitter = savedSubmitter && st.pub.submitterOptions.find((option) =>
        submitterEqual(option, savedSubmitter));
      st.pub.submitter = matchedSubmitter || (savedSubmitter ? null : (category.submitter || null));
      if (savedSubmitter && !matchedSubmitter && st.pub.submitterOptions.length)
        log("草稿中的提交按钮已变化，请重新选择当前网页的实际保存按钮。", st.id);
    }
    else if (category && category.native_only && (category.native_url || category.page_url)) {
      // Keep the draft/category intact and expose the exact dynamic add page;
      // clearing the category here would silently discard a valid draft.
      st.pub.nativeUrl = String(category.native_url || category.page_url || "");
      st.pub.htmlInfo = (category.msg || "该栏目表单由网页脚本控制") +
        "；可使用原生网页发布";
      st.pub.htmlInfoClass = "review";
    } else st.pub.cat = "";
  }
  if (st.pub.html) {
    st.pub.htmlLoading = true;
    const parsed = await api().parse_html(st.id, st.pub.html, "publish");
    st.pub.htmlLoading = false;
    if (parsed && parsed.ok) applyPubParse(st, parsed);
    else {
      st.pub.htmlReady = false;
      st.pub.htmlInfo = `草稿中的 HTML 无法恢复：${(parsed && parsed.msg) || "文件可能已移动"}`;
      st.pub.htmlInfoClass = "bad";
    }
  }
  st.pub.mapping = savedMapping;
  st.pub.overrides = savedOverrides;
  st.pub.backendValues = savedBackendFields;
  initializeCmsFlags(st.pub, st.pub.fields, draft.preferences?.flag_intent === "explicit" ? flags : {});
  if (!draft.preferences?.flag_intent && Object.keys(flags).length)
    log("旧草稿未记录文章属性是否修改，已保留后台属性；如需改变请重新勾选。", st.id);
}

async function restoreEditDraft(st, draft) {
  if (typeof restoreFileMimeHints === "function") restoreFileMimeHints(draft);
  const savedMapping = Object.assign({}, draft.mapping || {});
  const savedOverrides = Object.assign({}, draft.overrides || {});
  const savedBackendFields = Object.assign({}, draft.backend_fields || {});
  const savedSubmitter = draft.submitter && typeof draft.submitter === "object"
    ? Object.assign({}, draft.submitter) : null;
  st.edit.cat = String(draft.category || "");
  st.edit.artId = String(draft.article_id || "");
  st.edit.html = String(draft.html_path || "");
  const media = draft.media || {}, flags = draft.flags || {};
  st.edit.carouselImages = Array.from(media.carousel_images || []);
  st.edit.galleryPlan = media.gallery_plan != null ? media.gallery_plan.map(item => ({...item})) : null;
  st.edit.thumbPath = String(media.thumbnail_path || "");
  st.edit.thumbUrl = String(media.thumbnail_url || "");
  const savedImageReplacements = Array.isArray(media.image_replacements)
    ? media.image_replacements.map((item) => ({...item})) : [];
  const savedImageHash = String(media.image_content_hash || "");
  st.edit.imageReplacements = {};
  st.edit.flagChanges = {};
  applyDraftPreferences(st.edit, draft.preferences);
  if (draft.preferences?.carousel_size && draft.preferences.carousel_size !== "original")
    log("旧草稿包含客户端裁切设置；为保持与后台直接上传一致，已改为上传原文件。需要尺寸处理请使用原生网页。", st.id);
  if (st.edit.cat) {
    const articles = await api().load_articles(st.id, st.edit.cat);
    if (articles && articles.ok) st.edit.articles = articles.articles || [];
  }
  if (st.edit.artId && (st.edit.articles || []).some((item) => String(item.id) === st.edit.artId)) {
    const form = await api().load_edit_form(st.id, st.edit.artId, false,
      (typeof responsiveContext === "function" ? responsiveContext() : {}));
    if (form && form.ok) {
      st.edit.fields = form.fields || []; st.edit.formReady = true;
      st.edit.submitterOptions = Array.isArray(form.submitter_options)
        ? form.submitter_options : [];
      const matchedSubmitter = savedSubmitter && st.edit.submitterOptions.find((option) =>
        submitterEqual(option, savedSubmitter));
      st.edit.submitter = matchedSubmitter || (savedSubmitter ? null : (form.submitter || null));
      if (savedSubmitter && !matchedSubmitter && st.edit.submitterOptions.length)
        log("草稿中的提交按钮已变化，请重新选择当前网页的实际保存按钮。", st.id);
    }
  }
  if (st.edit.html) {
    const parsed = await api().parse_html(st.id, st.edit.html, "edit");
    if (parsed && parsed.ok) applyEditParse(st, parsed, false);
    else setAreaMsg(st, "edit", "editMsg", `草稿中的 HTML 无法恢复：${(parsed && parsed.msg) || "文件可能已移动"}`, "bad");
  }
  if (savedImageReplacements.length) {
    if (savedImageHash && savedImageHash === String(st.edit.contentHash || "")) {
      st.edit.imageReplacements = Object.fromEntries(savedImageReplacements.map((item) => [String(item.index), item]));
      log(`已恢复 ${savedImageReplacements.length} 项正文图片替图/尺寸修改；提交前仍会重新核对正文版本。`, st.id);
    } else {
      log("草稿中的正文图片替图未恢复：当前正文版本已变化，请重新打开详情图片后选择。", st.id);
    }
  }
  st.edit.mapping = savedMapping;
  st.edit.overrides = savedOverrides;
  st.edit.backendValues = savedBackendFields;
  initializeCmsFlags(st.edit, st.edit.fields, draft.preferences?.flag_intent === "explicit" ? flags : {});
  if (!draft.preferences?.flag_intent && Object.keys(flags).length)
    log("旧草稿未记录文章属性是否修改，已保留后台属性；如需改变请重新勾选。", st.id);
}

function restoreBatchDraft(st, draft) {
  if (typeof restoreFileMimeHints === "function") restoreFileMimeHints(draft);
  if (!st.pub || typeof st.pub !== "object") st.pub = {};
  st.pub.submitter = draft.submitter && typeof draft.submitter === "object"
    ? Object.assign({}, draft.submitter) : null;
  const rows = draft.batch_items || [];
  const paths = draft.html_paths || [];
  const byPath = new Map(rows.map((item) => [String(item.html_path || ""), item]));
  st.batch.items = paths.map((path, index) => {
    const saved = byPath.get(String(path)) || {};
    const oldStatus = String(saved.status || "pending");
    return { id: saved.id || String(index + 1), path: String(path),
      status: restoredBatchStatus(oldStatus),
      title: saved.title || "", message: saved.error || "", attempts: Number(saved.attempts || 0),
      mapping: Object.assign({}, saved.mapping || {}),
      overrides: Object.assign({}, saved.overrides || {}),
      settings: saved.settings || null };
  });
  const preferences = draft.preferences || {};
  const explicit = preferences.link_policy_version === 1;
  st.batch.applyVerified = explicit && preferences.apply_verified_links === true;
  st.batch.skipUnverified = explicit && preferences.skip_unverified_links === true;
  if (!explicit && (preferences.apply_verified_links || preferences.skip_unverified_links))
    log("旧草稿未区分默认与主动选择的链接处理，已关闭自动换链/去链；需要时请重新勾选并确认。", st.id);
}

async function offerDraftRecovery(st) {
  if (!st || !st.loggedIn || st._draftChecked || st._draftRecoveryInFlight) return;
  if (!st._draftRecoveryPending) beginDraftRecovery(st);
  st._draftRecoveryInFlight = true;
  let listed;
  try { listed = await api().list_drafts(st.id, ""); }
  catch (_) { listed = null; }
  if (!isLiveState(st) || !st.loggedIn) return;
  if (!listed || !listed.ok) {
    st._draftRecoveryInFlight = false;
    st._draftRecoveryRetries = Number(st._draftRecoveryRetries || 0) + 1;
    if (st._draftRecoveryRetries < 3) {
      const delay = st._draftRecoveryRetries * 1200;
      clearTimeout(st._draftRecoveryRetryTimer);
      st._draftRecoveryRetryTimer = setTimeout(() => offerDraftRecovery(st), delay);
    } else {
      // 不确定服务器上是否已有旧草稿时，宁可暂停本次自动保存，也不能用
      // 当前空白/半成品静默覆盖它。重新登录后会重新尝试恢复。
      st._draftRecoveryPending = false;
      st._draftPersistenceSuspended = true;
      setAreaMsg(st, "pub", "pubMsg",
        "草稿恢复暂时不可用；为保护旧草稿，本次自动保存已暂停，重新登录后会重试", "bad");
      log("草稿列表连续读取失败，本次自动保存已保护性暂停", st.id);
    }
    return;
  }
  st._draftChecked = true;
  st._draftRecoveryInFlight = false;
  if (!(listed.drafts || []).length) {
    finishDraftRecovery(st);
    return;
  }
  const summaries = listed.drafts || [];
  const release = await acquirePromptSlot();
  let resume = false;
  try {
    if (!isLiveState(st) || !st.loggedIn) return;
    const base = st._draftRecoveryBase || {};
    const changed = summaries.filter((item) =>
      Number((st._draftRevision || {})[item.workflow] || 0) !== Number(base[item.workflow] || 0));
    resume = await confirmDialog("", {
      title: `发现 ${st.title} 的未完成工作`, kind: "confirm",
      okText: "恢复草稿", cancelText: "丢弃草稿",
      lines: summaries.map((item) => {
        const label = { publish: "单篇发布", edit: "文章修改", batch: "批量队列" }[item.workflow] || item.workflow;
        return `${label} · ${item.html_path ? fileName(item.html_path) : (item.batch_count + " 个文件")} · ${String(item.saved_at || "").replace("T", " ").slice(0, 19)}`;
      }).concat(changed.length ? [
        `⚠ 等待恢复期间，“${changed.map((item) => ({ publish: "单篇发布", edit: "文章修改", batch: "批量队列" }[item.workflow] || item.workflow)).join("、")}”已有新修改；选择恢复会明确覆盖这些当前修改。`,
      ] : []).concat(["恢复后会重新读取本地 HTML 和后台表单，确保不是旧解析结果。"]),
    });
  } finally { release(); }
  if (!isLiveState(st)) return;
  if (!resume) {
    const deleted = await Promise.all(summaries.map(async (item) => {
      try {
        const result = await api().delete_draft(st.id, item.workflow);
        return { workflow: item.workflow, ok: !!(result && result.ok) };
      } catch (_) { return { workflow: item.workflow, ok: false }; }
    }));
    const failed = deleted.filter((item) => !item.ok).map((item) => item.workflow);
    failed.forEach((workflow) => { st._draftSaveBlocked[workflow] = true; });
    finishDraftRecovery(st, failed);
    if (failed.length) {
      setAreaMsg(st, "pub", "pubMsg",
        `有 ${failed.length} 份旧草稿未能删除；对应流程的自动保存已暂停，避免覆盖旧内容`, "bad");
      log("部分旧草稿删除失败，已保护性暂停对应流程的自动保存", st.id);
    } else log("已按用户选择丢弃本站点的未完成草稿", st.id);
    return;
  }
  st._restoringDraft = true;
  const failed = [];
  try {
    for (const summary of summaries) {
      try {
        const loaded = await api().load_draft(st.id, summary.workflow);
        if (!loaded || !loaded.ok || !loaded.found) {
          failed.push(summary.workflow);
          continue;
        }
        const draft = loaded.record.draft || {};
        if (summary.workflow === "publish") await restorePublishDraft(st, draft);
        else if (summary.workflow === "edit") await restoreEditDraft(st, draft);
        else restoreBatchDraft(st, draft);
      } catch (e) {
        failed.push(summary.workflow);
        log(`草稿恢复失败（${summary.workflow}）：${e}`, st.id);
      }
    }
    failed.forEach((workflow) => { st._draftSaveBlocked[workflow] = true; });
    if (failed.length)
      setAreaMsg(st, "pub", "pubMsg",
        `有 ${failed.length} 份草稿未能完整恢复；对应流程的自动保存已暂停，旧草稿仍保留`, "bad");
    else log("未完成草稿已恢复", st.id);
  } finally {
    st._restoringDraft = false;
    // 恢复选择已明确取代提示期间对同一流程的临时修改，不能再把延迟保存
    // 重新写回去；没有服务器草稿的其他流程仍可正常补存。
    finishDraftRecovery(st, summaries.map((item) => item.workflow));
    if (st.id === ACTIVE) renderActiveTab();
  }
}

function renderMapTable(tableId, fields, mapping, overrides, cmsFields) {
  const tbody = $(tableId).querySelector("tbody");
  tbody.innerHTML = "";
  const cmsOptions = (cmsFields || []).filter((field) => field.mappable !== false && !field.readonly);
  const values = overrides || {};
  (fields || []).forEach((f) => {
    const tr = document.createElement("tr");
    const originalValue = String(f.value ?? "");
    const isRequired = ["title", "content"].includes(String(f.key || "").toLowerCase());
    const options = ['<option value="">（不提交）</option>']
      .concat(cmsOptions.map((c) => {
        const kind = c.type === "select" ? "下拉" : (c.type === "radio" ? "单选" :
          (c.type === "checkbox" ? "复选" : ""));
        const marks = [c.required ? "必填" : "", kind,
          Number(c.maximum_words) > 0 ? `最多${Number(c.maximum_words)}字` : ""].filter(Boolean);
        const choices = (c.options || []).map((item) => `${item.label}=${item.value}`).join("；");
        const title = [c.help || "", choices ? `选项：${choices}` : ""].filter(Boolean).join("；");
        const text = `${c.label || c.name} · ${c.name}${marks.length ? " · " + marks.join("/") : ""}`;
        return `<option value="${esc(c.name)}" title="${esc(title)}"${mapping[f.key] === c.name ? " selected" : ""}>${esc(text)}</option>`;
      })).join("");
    tr.innerHTML =
      `<td><div class="map-field-name">${esc(f.key)}${isRequired ? '<span class="required-badge" title="发布必填来源字段">必填</span>' : ""}</div>` +
      `<span class="modified-badge" hidden>● 已修改</span></td>` +
      `<td></td>` +
      `<td><select data-key="${esc(f.key)}">${options}</select></td>` +
      `<td><div class="map-row-actions"><button class="ghost expand" type="button">完整编辑</button>` +
      `<button class="restore" type="button">还原</button></div></td>`;
    // 预览值是本次稿件的一部分：使用独立 overrides 保存人工修改，避免
    // 因重绘/切换标签而丢失，也避免改写后端保留的原始解析结果。
    const preview = document.createElement("textarea");
    preview.className = "cell-preview-input";
    preview.rows = f.key === "content" ? 4 : 1;
    preview.dataset.key = f.key;
    preview.setAttribute("aria-label", `${f.key} 预览值`);
    preview.title = "可直接修改；提交时将使用此处的最新内容";
    preview.value = Object.prototype.hasOwnProperty.call(values, f.key)
      ? String(values[f.key] ?? "") : originalValue;
    const modifiedBadge = tr.querySelector(".modified-badge");
    const restoreButton = tr.querySelector(".restore");
    const updateModified = () => {
      const changed = Object.prototype.hasOwnProperty.call(values, f.key) &&
        String(values[f.key] ?? "") !== originalValue;
      modifiedBadge.hidden = !changed;
      restoreButton.disabled = !Object.prototype.hasOwnProperty.call(values, f.key);
      tr.classList.toggle("modified", changed);
    };
    preview.addEventListener("input", (e) => {
      values[f.key] = e.target.value;
      const st = activeState();
      updateModified();
      if (st) {
        if (tableId === "mapTable") notePublishChanged(st);
        else scheduleDraftSave(st, "edit");
      }
      if (tableId === "editMapTable" && st && mapping[f.key] === "content")
        invalidateEditLinkReport(st);
    });
    tr.querySelector(".expand").addEventListener("click", () =>
      openFieldEditor({ tableId, key: f.key, originalValue, preview, values, mapping }));
    restoreButton.addEventListener("click", () => {
      delete values[f.key];
      preview.value = originalValue;
      updateModified();
      const st = activeState();
      if (st) {
        if (tableId === "mapTable") notePublishChanged(st);
        else scheduleDraftSave(st, "edit");
        if (tableId === "editMapTable" && mapping[f.key] === "content") invalidateEditLinkReport(st);
      }
    });
    tr.children[1].appendChild(preview);
    tr.querySelector("select").addEventListener("change", (e) => {
      mapping[f.key] = e.target.value;
      const st = activeState();
      if (!st) return;
      if (tableId === "mapTable") notePublishChanged(st);
      else scheduleDraftSave(st, "edit");
      renderTaskControls(st);
      if (tableId === "mapTable") renderPublishBackendFields(st);
      if (tableId === "editMapTable") invalidateEditLinkReport(st);
    });
    updateModified();
    tbody.appendChild(tr);
  });
}

let _fieldEditorContext = null;
function openFieldEditor(context) {
  _fieldEditorContext = context;
  $("fieldEditorTitle").textContent = `完整编辑：${context.key}`;
  $("fieldEditorHint").textContent =
    "保存后，本次发布/修改会使用这里的值；清空后保存也会被视为一次明确修改。";
  $("fieldEditorValue").value = context.preview.value;
  $("fieldEditorMask").hidden = false;
  $("fieldEditorValue").focus();
}
function closeFieldEditor(save) {
  const context = _fieldEditorContext;
  if (save && context) {
    context.values[context.key] = $("fieldEditorValue").value;
    context.preview.value = $("fieldEditorValue").value;
    context.preview.dispatchEvent(new Event("input", { bubbles: true }));
  }
  $("fieldEditorMask").hidden = true;
  _fieldEditorContext = null;
}

/*
 * Render the same mapped body the worker will submit in a browser-like,
 * read-only surface.  This intentionally uses a sandboxed srcdoc instead of
 * assigning HTML to the main application DOM: source scripts, forms and
 * inline event handlers must never execute in the publisher window.  A base
 * element lets ordinary relative image/style URLs resolve like a page loaded
 * from the current backend origin, while the CSP keeps this preview from
 * becoming a second write-capable browser tab.
 */
function previewBodyFor(st, which) {
  const area = st && st[which];
  if (!area) return "";
  const fields = Array.isArray(area.parsedFields) ? area.parsedFields : [];
  const mapping = area.mapping || {};
  const overrides = area.overrides || {};
  const contentField = fields.find((field) => mapping[field.key] === "content");
  if (contentField) {
    return Object.prototype.hasOwnProperty.call(overrides, contentField.key)
      ? String(overrides[contentField.key] ?? "")
      : String(contentField.value ?? "");
  }
  // Editing an existing article can be previewed before a new HTML file is
  // chosen.  Its backend field descriptors use name rather than parsed key.
  const backend = (area.fields || []).find((field) => String(field.name || "") === "content");
  return backend ? String(backend.value ?? "") : "";
}

// Let the embedded browser perform the non-script part of the native image
// selection algorithm before a publish/edit task starts.  This covers
// picture/source media queries, sizes and DPR cases that a static parser
// cannot evaluate exactly.  The sandbox deliberately omits allow-scripts,
// allow-forms and navigation permissions; it is a read-only image probe, not
// another write-capable page.  Python still validates the returned URL as
// same-origin before using it for a thumbnail.
async function resolveBrowserFirstImage(body, baseUrl) {
  const html = String(body || "");
  if (!html || typeof document === "undefined" || !document.body) return null;
  let base = String(baseUrl || "").trim();
  try {
    const parsed = new URL(base.includes("://") ? base : `https://${base}`);
    if (!/^https?:$/.test(parsed.protocol)) return null;
    base = parsed.href;
  } catch (_) { return null; }
  // A fragment imported from another page can carry its own browser base.
  // Put the generated base before the body so the iframe follows the same
  // first-<base> rule as a real document, but only accept an HTTP(S) value.
  try {
    if (typeof DOMParser === "function") {
      const parsedBody = new DOMParser().parseFromString(html, "text/html");
      const declared = parsedBody && parsedBody.querySelector("base[href]");
      if (declared) {
        const resolved = new URL(String(declared.getAttribute("href") || ""), base);
        if (/^https?:$/.test(resolved.protocol)) base = resolved.href;
      }
    }
  } catch (_) { /* malformed/unsupported base: keep the verified page base */ }
  let imageSource = "'self' data:";
  try {
    imageSource += ` ${new URL(base).origin}`;
  } catch (_) { return null; }
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-same-origin");
  frame.setAttribute("aria-hidden", "true");
  frame.referrerPolicy = "no-referrer";
  frame.style.cssText = `position:fixed;left:-100000px;top:-100000px;` +
    `width:${Math.max(1, Number(window.innerWidth || 1))}px;` +
    `height:${Math.max(1, Number(window.innerHeight || 1))}px;` +
    "visibility:hidden;pointer-events:none;border:0";
  const source = `<!doctype html><html><head><meta charset="utf-8">` +
    `<base href="${esc(base)}">` +
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${imageSource}; style-src 'unsafe-inline'">` +
    `</head><body>${html}</body></html>`;
  const loaded = new Promise((resolve) => {
    let finished = false;
    const done = () => { if (!finished) { finished = true; resolve(); } };
    frame.addEventListener("load", done, { once: true });
    setTimeout(done, 1500);
  });
  document.body.appendChild(frame);
  try {
    frame.srcdoc = source;
    await loaded;
    const doc = frame.contentDocument;
    const image = doc && doc.querySelector("img");
    if (!image) return null;
    const candidate = String(image.currentSrc || image.src || "").trim();
    if (!/^https?:\/\//i.test(candidate)) return null;
    return {
      url: candidate,
      width: Number(image.naturalWidth || 0) || 0,
      height: Number(image.naturalHeight || 0) || 0,
      current_src: candidate,
    };
  } catch (_) {
    return null;
  } finally {
    frame.remove();
  }
}

function openHtmlPreview(which) {
  const st = activeState();
  if (!st || !st.loggedIn) return;
  const body = previewBodyFor(st, which);
  if (!body) {
    const msgId = which === "pub" ? "pubMsg" : "editMsg";
    return setMsg(msgId, "当前还没有可预览的正文内容。", "bad");
  }
  let base = String(st.url || "").trim();
  try {
    const parsed = new URL(base.includes("://") ? base : "https://" + base);
    if (!/^https?:$/.test(parsed.protocol)) throw new Error("unsafe");
    base = parsed.href;
  } catch (_) { base = ""; }
  // A source document may declare its own browser base URL.  The worker and
  // the native page parser already honour it; doing the same here prevents a
  // relative image/media in an imported fragment from silently resolving
  // against the admin root.  DOMParser is used only as a non-executing HTML
  // reader; the original body still enters the sandboxed srcdoc below.
  try {
    if (base && typeof DOMParser === "function") {
      const parsedBody = new DOMParser().parseFromString(body, "text/html");
      const declared = parsedBody && parsedBody.querySelector("base[href]");
      const href = declared ? String(declared.getAttribute("href") || "").trim() : "";
      if (href) {
        const resolved = new URL(href, base);
        if (/^https?:$/.test(resolved.protocol)) base = resolved.href;
      }
    }
  } catch (_) { /* malformed/unsupported base: keep the verified admin base */ }
  const baseTag = base ? `<base href="${esc(base)}">` : "";
  // Keep the isolated preview's network surface identical to the browser
  // candidate reader: only resources from the parsed HTTP(S) base origin are
  // allowed.  The previous broad ``http: https:`` CSP let an imported local
  // fragment pull arbitrary cross-site images/styles during preview, while
  // the upload/readback paths correctly rejected those candidates.  Inline
  // CSS/data URLs remain available for the fragment itself; media gets an
  // explicit directive so audio/video previews follow the same safe origin
  // rule instead of being silently blocked by ``default-src 'none'``.
  let previewOrigin = "";
  try { previewOrigin = base ? new URL(base).origin : ""; } catch (_) { previewOrigin = ""; }
  const previewResourceOrigin = previewOrigin || "'none'";
  const previewCsp = `default-src 'none'; img-src ${previewResourceOrigin} data:; ` +
    `media-src ${previewResourceOrigin} data:; ` +
    `style-src 'unsafe-inline' ${previewResourceOrigin} data:; ` +
    `font-src ${previewResourceOrigin} data:`;
  const source = `<!doctype html><html><head><meta charset="utf-8">${baseTag}` +
    `<meta http-equiv="Content-Security-Policy" content="${previewCsp}">` +
    `<style>html,body{margin:0;padding:0;background:#fff;color:#202938;font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}body{padding:24px;overflow-wrap:anywhere}img,video,iframe{max-width:100%;height:auto}table{max-width:100%;border-collapse:collapse}td,th{border:1px solid #d9dee8;padding:6px 9px}a{color:#315fd1}pre{white-space:pre-wrap;overflow:auto}blockquote{margin:12px 0;padding:8px 14px;border-left:3px solid #ccd5e4;background:#f7f9fc}</style>` +
    `</head><body>${body}</body></html>`;
  $("htmlPreviewTitle").textContent = which === "pub" ? "发布正文浏览器预览" : "编辑正文浏览器预览";
  $("htmlPreviewHint").textContent = base
    ? "当前映射后的正文，按后台地址解析相对资源。脚本、表单提交和写操作已禁用；主题 CSS、服务器处理和前台最终尺寸仍需实站核对。"
    : "当前映射后的正文。脚本、表单提交和写操作已禁用；未能确定后台基准地址，复杂相对资源可能无法显示。";
  const frame = $("htmlPreviewFrame");
  frame.srcdoc = source;
  $("htmlPreviewMask").hidden = false;
}

function closeHtmlPreview() {
  const frame = $("htmlPreviewFrame");
  if (frame) frame.srcdoc = "";
  $("htmlPreviewMask").hidden = true;
}

/* ══════════ 发布 ══════════ */
function clearPublishArticleMedia(st) {
  st.pub.manualImages = [];
  if (!st.pub.rememberThumb) {
    st.pub.thumbPath = "";
    st.pub.thumbUrl = "";
    st.pub.ico = "none";
  }
  st.pub.carouselImages = [];
  st.pub.galleryPlan = null;
}

function clearEditArticleDraft(st, clearHtml) {
  if (clearHtml) {
    st.edit.html = "";
    st.edit.htmlReady = false;
    st.edit.htmlLoading = false;
    st.edit.parsedFields = [];
    st.edit.mapping = {};
    st.edit.overrides = {};
    st.edit.backendValues = {};
    st.edit.inlineCount = 0; st.edit.remoteCount = 0; st.edit.mediaCount = 0;
  }
  st.edit.thumbPath = "";
  st.edit.thumbUrl = "";
  st.edit.ico = "none";
  st.edit.carouselImages = [];
  st.edit.galleryPlan = null;
  st.edit.contentImages = [];
  st.edit.contentHash = "";
  st.edit.imageReplacements = {};
  st.edit.linkReport = null;
  clearTimeout(st._editLinkTimer);
}

async function pickHtml() {
  const st = activeState();
  if (!st || st.busy) return;
  const r = await api().pick_html("publish_html");
  if (!isLiveState(st) || !r.ok || r.cancelled) return;
  await loadPublishHtmlPath(st, r.path);
}

async function loadPublishHtmlPath(st, path) {
  if (!isLiveState(st) || st.busy || !path) return false;
  const req = nextRequest(st, "pubHtml");
  // 只有“已有稿件被另一份稿件替换”时才清文章专属素材。允许用户先选
  // 独立缩略图/轮播图、再首次选择 HTML；重复选择同一稿也不应静默丢图。
  if (st.pub.html && st.pub.html !== path) clearPublishArticleMedia(st);
  st.pub.html = path;
  st.pub.htmlReady = false;
  st.pub.htmlLoading = true;
  st.pub.parsedFields = [];
  st.pub.mapping = {};
  st.pub.overrides = {};
  st.pub.inlineCount = 0; st.pub.remoteCount = 0; st.pub.mediaCount = 0;
  st.pub.htmlInfo = "正在解析 HTML…";
  st.pub.htmlInfoClass = "";
  notePublishChanged(st);
  if (st.id === ACTIVE) renderActiveTab();
  const p = await api().parse_html(st.id, path, "publish");
  if (!requestIsCurrent(st, "pubHtml", req) || st.pub.html !== path) return false;
  st.pub.htmlLoading = false;
  if (!p.ok) {
    st.pub.htmlReady = false;
    st.pub.htmlInfo = `HTML 解析失败：${p.msg || "未知错误"}`;
    st.pub.htmlInfoClass = "bad";
    if (st.id === ACTIVE) renderActiveTab();
    return false;
  }
  applyPubParse(st, p);
  return true;
}
function applyPubParse(st, p) {
  st.pub.parsedFields = p.fields;
  st.pub.mapping = p.suggest || {};
  st.pub.inlineCount = (p.inline_images || []).length;
  st.pub.remoteCount = (p.remote_images || []).length;
  st.pub.mediaCount = (p.media_assets || []).length;
  st.pub.htmlReady = true;
  st.pub.htmlLoading = false;
  const missing = p.missing || [];
  st.pub.htmlInfo = `解析出 ${p.fields.length} 个字段` +
    (missing.length ? `；⚠ 未识别: ${missing.join(", ")}` : "");
  st.pub.htmlInfoClass = missing.length ? "bad" : "ok";
  (p.image_problems || []).forEach((x) => log("内置图跳过: " + x.reason + " ← " + x.src, st.id));
  if (st.pub.remoteCount) log(`发现 ${st.pub.remoteCount} 个远程图片；只有当前 UEditor 明确启用远程抓取时才会上传，否则保留原地址`, st.id);
  (p.media_problems || []).forEach((x) => log("媒体资源保留原样: " + x.reason + " ← " + x.src, st.id));
  if (!st.pub.fields.length) {
    st.pub.htmlInfo = "⚠ 请先选择发布栏目，才能载入 CMS 字段列表并做映射";
    st.pub.htmlInfoClass = "bad";
  }
  notePublishChanged(st);
  if (st.id === ACTIVE) renderActiveTab();
}

function renderDetailImages(st) {
  const root = $("editDetailImages");
  if (!root) return;
  root.innerHTML = "";
  const images = st.edit.contentImages || [];
  if (!st.edit.artId) { root.textContent = "选择文章后可直接替换正文中的单张图片；保留原图 alt 属性。"; return; }
  if (!images.length) { root.textContent = "当前正文没有可替换的图片。"; return; }
  images.forEach((image) => {
    const replacement = (st.edit.imageReplacements || {})[image.index];
    const row = document.createElement("div");
    row.className = "detail-image-row";
    const preview = image.preview_url ? `<img src="${esc(image.preview_url)}" alt="${esc(image.alt || "")}">` : "<span class=\"detail-image-no-preview\">无同源预览</span>";
    const currentAlt = replacement && Object.prototype.hasOwnProperty.call(replacement, "new_alt")
      ? String(replacement.new_alt || "") : String(image.alt || "");
    const currentWidth = replacement && Object.prototype.hasOwnProperty.call(replacement, "new_width")
      ? String(replacement.new_width || "") : String(image.width || "");
    const currentHeight = replacement && Object.prototype.hasOwnProperty.call(replacement, "new_height")
      ? String(replacement.new_height || "") : String(image.height || "");
    row.innerHTML = `${preview}<div class="detail-image-meta"><strong>第 ${Number(image.index) + 1} 张</strong>` +
      `<span title="${esc(image.alt || "")}">原 alt：${esc(image.alt || "（无）")}</span>` +
      `<label class="detail-image-alt-label">替换后 alt <input class="detail-image-alt" type="text" maxlength="4096" value="${esc(currentAlt)}" aria-label="第 ${Number(image.index) + 1} 张图片替换后 alt"></label>` +
      `<div class="detail-image-dimensions"><label>宽 <input class="detail-image-dim" data-dim="width" type="text" maxlength="32" value="${esc(currentWidth)}" placeholder="原尺寸" aria-label="第 ${Number(image.index) + 1} 张图片宽度"></label>` +
      `<label>高 <input class="detail-image-dim" data-dim="height" type="text" maxlength="32" value="${esc(currentHeight)}" placeholder="原尺寸" aria-label="第 ${Number(image.index) + 1} 张图片高度"></label></div>` +
      `<small>${replacement ? (replacement.local_path ? `替换为：${esc(fileName(replacement.local_path))}` : "仅修改图片属性") : "未替换"}</small></div>` +
      `<div class="detail-image-actions"><button class="ghost mini-btn" type="button">${replacement && replacement.local_path ? "重新选择" : "选择替换图"}</button>` +
      `${replacement ? '<button class="text-btn" type="button">取消</button>' : ""}</div>`;
    const buttons = row.querySelectorAll("button");
    const actions = row.querySelector(".detail-image-actions");
    const altInput = row.querySelector(".detail-image-alt");
    const ensurePropertyEdit = () => {
      st.edit.imageReplacements = st.edit.imageReplacements || {};
      return st.edit.imageReplacements[image.index] ||
        (st.edit.imageReplacements[image.index] = {
          index: image.index, expected_src: image.src,
          tag_fingerprint: image.tag_fingerprint, local_path: ""});
    };
    const prunePropertyEdit = () => {
      const current = (st.edit.imageReplacements || {})[image.index];
      if (!current || current.local_path) return;
      const unchanged = (!Object.prototype.hasOwnProperty.call(current, "new_alt") ||
          String(current.new_alt) === String(image.alt || "")) &&
        (!Object.prototype.hasOwnProperty.call(current, "new_width") ||
          String(current.new_width) === String(image.width || "")) &&
        (!Object.prototype.hasOwnProperty.call(current, "new_height") ||
          String(current.new_height) === String(image.height || ""));
      if (unchanged) delete st.edit.imageReplacements[image.index];
    };
    const ensurePropertyCancel = () => {
      if (!actions || actions.querySelector(".detail-property-cancel")) return;
      const cancel = document.createElement("button");
      cancel.type = "button"; cancel.className = "text-btn detail-property-cancel";
      cancel.textContent = "取消属性修改";
      cancel.addEventListener("click", () => {
        delete st.edit.imageReplacements[image.index];
        scheduleDraftSave(st, "edit"); renderActiveTab();
      });
      actions.appendChild(cancel);
    };
    if (altInput) {
      altInput.disabled = st.busy || !!st.edit.html;
      altInput.addEventListener("input", () => {
        if (st.edit.html) return;
        const current = ensurePropertyEdit(); current.new_alt = altInput.value;
        ensurePropertyCancel();
        prunePropertyEdit(); scheduleDraftSave(st, "edit");
      });
    }
    row.querySelectorAll(".detail-image-dim").forEach((input) => {
      input.disabled = st.busy || !!st.edit.html;
      input.addEventListener("input", () => {
        if (st.edit.html) return;
        const current = ensurePropertyEdit();
        current[`new_${input.dataset.dim}`] = input.value;
        ensurePropertyCancel();
        prunePropertyEdit(); scheduleDraftSave(st, "edit");
      });
    });
    buttons[0].disabled = st.busy || !!st.edit.html;
    buttons[0].addEventListener("click", () => chooseDetailImageReplacement(st, image));
    if (replacement) buttons[1].addEventListener("click", () => {
      delete st.edit.imageReplacements[image.index];
      scheduleDraftSave(st, "edit"); renderActiveTab();
    });
    root.appendChild(row);
  });
}

async function chooseDetailImageReplacement(st, image) {
  if (!isLiveState(st) || st.busy) return;
  if (st.edit.html) {
    setAreaMsg(st, "edit", "editMsg", "已选择新 HTML，不能同时替换原正文图片", "bad");
    return;
  }
  const picked = await api().pick_images("edit_detail_image");
  if (!picked || !picked.ok || !(picked.paths || []).length) return;
  if (typeof rememberFileMimeHints === "function")
    rememberFileMimeHints(picked.paths || [], picked.file_types || []);
  const path = picked.paths[0];
  const current = st.edit.imageReplacements[image.index] || {
    index: image.index, expected_src: image.src,
    tag_fingerprint: image.tag_fingerprint,
  };
  current.local_path = path;
  if (!Object.prototype.hasOwnProperty.call(current, "new_alt"))
    current.new_alt = String(image.alt || "");
  st.edit.imageReplacements[image.index] = current;
  scheduleDraftSave(st, "edit");
  if (st.id === ACTIVE) renderActiveTab();
}
async function onSelectCategory() {
  const st = activeState();
  if (!st || st.busy) return;
  const scode = $("pubCat").value;
  if (st.pub.cat && st.pub.cat !== scode) {
    clearPublishArticleMedia(st);
    st.pub.backendValues = {};
  }
  st.pub.cat = scode;
  notePublishChanged(st);
  const req = nextRequest(st, "pubCat");
  st.pub.catLoading = !!scode;
  st.pub.fields = [];
  st.pub.nativeUrl = "";
  st.pub.submitter = null;
  st.pub.submitterOptions = [];
  st.pub.mapping = {};
  if (!scode) {
    st.pub.catLoading = false;
    if (st.id === ACTIVE) renderActiveTab();
    return;
  }
  if (st.id === ACTIVE) renderActiveTab();
  const r = await api().select_category(st.id, scode);
  if (!requestIsCurrent(st, "pubCat", req) || st.pub.cat !== scode) return;
  if (!r.ok) {
    st.pub.catLoading = false;
    st.pub.fields = [];
    st.pub.mapping = {};
    st.pub.backendValues = {};
    st.pub.nativeUrl = String(r.page_url || "");
    st.pub.htmlInfo = `栏目表单加载失败：${r.msg || "未知错误"}` +
      (st.pub.nativeUrl ? "；可点击“原生网页发布”由完整网页脚本处理" : "");
    st.pub.htmlInfoClass = "bad";
    log("获取栏目表单失败: " + r.msg, st.id);
    if (st.id === ACTIVE) renderActiveTab();
    if (r.native_only && st.pub.nativeUrl && typeof openNativeRecordUrl === "function")
      await openNativeRecordUrl(st, st.pub.nativeUrl, "动态栏目发布页", {
        handoff: nativeHandoffSnapshot(st, "publish", publishSnapshot(st), {
          message: r.native_reason || r.msg || "栏目表单由网页脚本接管；当前草稿字段已带入，未自动提交。",
        }),
      });
    return;
  }
  st.pub.fields = r.fields || [];
  st.pub.nativeUrl = String(r.page_url || "");
  st.pub.submitterOptions = Array.isArray(r.submitter_options) ? r.submitter_options : [];
  st.pub.submitter = r.submitter || null;
  initializeCmsFlags(st.pub, st.pub.fields);
  log(`栏目 ${scode} 表单字段 ${r.fields.length} 个（mcode=${r.mcode}）`, st.id);
  if (st.pub.html) {
    const htmlPath = st.pub.html;
    const p = await api().parse_html(st.id, htmlPath, "publish");
    if (!requestIsCurrent(st, "pubCat", req) || st.pub.cat !== scode ||
        st.pub.html !== htmlPath) return;
    st.pub.catLoading = false;
    if (p.ok) applyPubParse(st, p);
    else {
      st.pub.htmlReady = false;
      st.pub.mapping = {};
      st.pub.htmlInfo = `HTML 重新解析失败：${p.msg || "未知错误"}`;
      st.pub.htmlInfoClass = "bad";
      if (st.id === ACTIVE) renderActiveTab();
    }
  } else {
    st.pub.catLoading = false;
    if (st.id === ACTIVE) renderActiveTab();
  }
}
function renderThumbnailControl(st, which) {
  const area = st[which];
  const input = $(which + "ThumbUrl");
  input.hidden = area.ico !== "url";
  input.value = area.thumbUrl || "";
  $(which + "ThumbClearHint").hidden = area.ico !== "clear";
  const info = $(which + "ThumbInfo");
  if (info) {
    const local = area.thumbPath ? fileName(area.thumbPath) : "";
    const server = String(area.thumbServerInfo || "").trim();
    info.textContent = [local, server].filter(Boolean).join(" · ");
  }
}

function localImageSummary(path, result) {
  const name = fileName(path || "");
  const bits = [name || "本地图片"];
  if (result && Number.isFinite(Number(result.width)) && Number.isFinite(Number(result.height)))
    bits.push(`${Number(result.width)}×${Number(result.height)} px`);
  if (result && Number.isFinite(Number(result.bytes)))
    bits.push(`${Number(result.bytes)} bytes`);
  return bits.join(" · ");
}

// Keep the server-side result beside the thumbnail picker.  The completion
// message contains the same metadata, but it is easy to miss after a long
// batch; showing the final server dimensions here makes the desktop result
// inspectable in the same place as the browser's upload preview.  We only use
// values returned by the server/read-back layer and never infer a resize.
function thumbnailServerSummary(entries) {
  const list = Array.isArray(entries) ? entries : [];
  const entry = [...list].reverse().find((item) =>
    item && /缩略图|thumbnail/i.test(String(item.label || "")));
  if (!entry) return "";
  const meta = entry.metadata && typeof entry.metadata === "object" ? entry.metadata : {};
  const data = meta.data && typeof meta.data === "object" ? meta.data : {};
  const read = (...keys) => {
    for (const key of keys) {
      const value = meta[key] ?? data[key];
      if (value !== undefined && value !== null && String(value) !== "") return value;
    }
    return "";
  };
  const width = read("server_width", "width");
  const height = read("server_height", "height");
  const bytes = read("server_bytes", "bytes", "size", "filesize", "fileSize");
  const mime = read("server_mime", "mime", "mimeType");
  const clientBytes = read("client_bytes");
  const clientSha256 = read("client_sha256");
  const clientMime = read("client_mime");
  const format = read("server_format", "format");
  const filename = read("server_filename", "filename", "fileName");
  const urlBasename = read("server_url_basename");
  const contentType = read("server_content_type");
  const contentLength = read("server_content_length");
  const sha256 = read("server_sha256", "sha256");
  const cacheControl = read("server_cache_control");
  const age = read("server_age");
  const etag = read("server_etag");
  const lastModified = read("server_last_modified", "last_modified");
  const encoding = read("server_content_encoding");
  const changeFields = read("server_change_fields");
  const changed = read("server_changed") === true ||
    String(read("server_changed")).toLowerCase() === "true";
  const uploadMode = String(entry.ueditor_upload_mode || read("ueditor_upload_mode") || "").trim();
  const transform = entry.client_transform && typeof entry.client_transform === "object"
    ? entry.client_transform : (meta.client_transform && typeof meta.client_transform === "object"
      ? meta.client_transform : {});
  const parts = [];
  if (uploadMode) parts.push(`网页上传入口 ${uploadMode}`);
  if (transform.kind === "ueditor-image-compress") {
    const transformParts = [];
    if (transform.border) transformParts.push(`边界 ${transform.border}px`);
    if (transform.quality) transformParts.push(`质量 ${transform.quality}`);
    if (transform.preserve_headers) transformParts.push("保留头部");
    parts.push(`客户端压缩${transformParts.length ? `（${transformParts.join("，")}）` : ""}`);
  }
  if (width && height) parts.push(`服务器 ${width}×${height} px`);
  if (bytes) parts.push(`${bytes} bytes`);
  if (mime) parts.push(String(mime));
  if (contentType && String(contentType).toLowerCase() !== String(mime || "").toLowerCase())
    parts.push(`响应 Content-Type ${contentType}`);
  if (format) parts.push(`格式 ${format}`);
  if (filename) parts.push(`服务器文件名 ${filename}`);
  else if (urlBasename) parts.push(`服务器 URL 文件名 ${urlBasename}`);
  if (sha256) parts.push(`SHA-256 ${sha256}`);
  if (clientBytes && bytes && String(clientBytes) !== String(bytes))
    parts.push(`客户端→服务器 ${clientBytes}→${bytes} bytes`);
  if (clientSha256 && sha256 && String(clientSha256) !== String(sha256))
    parts.push("客户端/服务器 SHA-256 不同");
  if (clientMime && mime && String(clientMime).toLowerCase() !== String(mime).toLowerCase())
    parts.push(`客户端 MIME ${clientMime}`);
  if (contentLength && String(contentLength) !== String(bytes || ""))
    parts.push(`响应 Content-Length ${contentLength}`);
  if (cacheControl || age) parts.push(`缓存 ${cacheControl || "默认"}${age ? ` · Age ${age}` : ""}`);
  if (encoding) parts.push(`编码 ${encoding}`);
  if (etag) parts.push(`ETag ${etag}`);
  if (lastModified) parts.push(`Last-Modified ${lastModified}`);
  if (changeFields) parts.push(`变化字段 ${Array.isArray(changeFields) ? changeFields.join(",") : changeFields}`);
  if (changed) parts.push("服务器已处理");
  return parts.join(" · ");
}

// The upload callback carries the server's final URL even when the response
// has no optional metadata envelope.  Keep that URL separately from the
// user's next thumbnail intent so the preview can immediately switch from
// the local source to the actual object returned by the backend.
function thumbnailServerUrl(entries) {
  const list = Array.isArray(entries) ? entries : [];
  const entry = [...list].reverse().find((item) =>
    item && /缩略图|thumbnail/i.test(String(item.label || "")) &&
    String(item.url || "").trim());
  return entry ? String(entry.url || "").trim() : "";
}

function setThumbnailInfo(st, which, localText) {
  const info = $(which + "ThumbInfo");
  if (!info || !st || !st[which]) return;
  const local = String(localText || (st[which].thumbPath ? fileName(st[which].thumbPath) : "")).trim();
  const server = String(st[which].thumbServerInfo || "").trim();
  info.textContent = [local, server].filter(Boolean).join(" · ");
}

async function renderThumbnailPreview(st, which) {
  const image = $(which + "ThumbPreview");
  const area = st && st[which];
  if (!image || !area) return;
  const key = `${area.ico || "none"}|${area.thumbPath || ""}|${area.thumbUrl || ""}|${area.thumbServerUrl || ""}|${st.url || ""}`;
  if (image._previewKey === key) return;
  image._previewKey = key;
  image.hidden = true;
  if (typeof image.removeAttribute === "function") image.removeAttribute("src");
  let src = "";
  let fallbackSrc = "";
  // Prefer the final server object after a confirmed callback.  The helper
  // rejects unsafe schemes and preserves the same URL handling used by
  // existing gallery/URL previews; if it cannot be rendered, fall back to
  // the local source below rather than hiding a usable preview.
  if (area.thumbServerUrl && typeof galleryRemotePreviewUrl === "function") {
    src = galleryRemotePreviewUrl(st, area.thumbServerUrl);
  }
  // Resolve the local fallback even when the server URL exists.  The browser
  // can receive a successful callback before the CDN/object is readable; an
  // image error must then return to the selected local file (or explicit URL)
  // instead of leaving a blank preview.
  if (area.ico === "file" && area.thumbPath) {
    try {
      const bridge = api();
      if (bridge && typeof bridge.preview_local_image === "function") {
        const result = await bridge.preview_local_image(area.thumbPath);
        if (result && result.ok) {
          fallbackSrc = result.data_url ? String(result.data_url) : "";
          const info = $(which + "ThumbInfo");
          setThumbnailInfo(st, which, localImageSummary(area.thumbPath, result));
        }
      }
    } catch (_) { /* optional visual feedback */ }
  } else if (area.ico === "url" && typeof galleryRemotePreviewUrl === "function") {
    fallbackSrc = galleryRemotePreviewUrl(st, area.thumbUrl);
  }
  if (!isLiveState(st) || image._previewKey !== key) return;
  if (!src) src = fallbackSrc;
  if (!src && area.thumbServerInfo) setThumbnailInfo(st, which);
  if (src) {
    image.hidden = false;
    // The browser's image decoder is the final authority for formats that
    // the bounded Python container parser cannot inspect (for example some
    // JXL/HEIC variants) and for CDN representations that omit metadata.
    // Record naturalWidth/naturalHeight from the actual rendered server
    // object without replacing the server byte/hash evidence.  This mirrors
    // the dimensions a user sees in the backend preview.
    const onRendered = () => {
      if (!isLiveState(st) || image._previewKey !== key) return;
      const width = Number(image.naturalWidth || 0);
      const height = Number(image.naturalHeight || 0);
      if (!(width > 0 && height > 0)) return;
      const current = String(area.thumbServerInfo || "").trim();
      if (/服务器\s+\d+×\d+\s+px/.test(current) ||
          /浏览器渲染\s+\d+×\d+\s+px/.test(current)) return;
      area.thumbServerInfo = [current, `浏览器渲染 ${width}×${height} px`]
        .filter(Boolean).join(" · ");
      setThumbnailInfo(st, which);
    };
    if (typeof image.addEventListener === "function")
      image.addEventListener("load", onRendered, {once: true});
    // A server object can be temporarily unavailable or return an expired
    // signed URL.  Bind one guarded fallback handler only for that first
    // server attempt; local data URLs are not network probes and should not
    // recursively retry themselves.
    if (fallbackSrc && src !== fallbackSrc && typeof image.addEventListener === "function") {
      const onServerError = () => {
        if (!isLiveState(st) || image._previewKey !== key) return;
        image.removeEventListener("error", onServerError);
        image.src = fallbackSrc; image.hidden = false;
      };
      image.addEventListener("error", onServerError, {once: true});
    }
    image.src = src;
  }
}

function thumbnailSubmission(snap) {
  return { thumbnail_mode: snap.ico || "none",
    thumbnail_path: snap.ico === "file" ? snap.thumbPath || "" : "",
    thumbnail_url: snap.ico === "url" ? snap.thumbUrl || "" : "",
    thumbnail_from_first: snap.ico === "first" };
}

function thumbnailSummary(snap) {
  if (snap.ico === "clear") return ["缩略图：明确提交空值（不删除服务器文件，后台可能自动取图；保存后核对）"];
  if (snap.ico === "url") return [`缩略图地址：${snap.thumbUrl || ""}（直接填值，不上传文件）`];
  if (snap.ico === "file") return [`独立缩略图：${fileName(snap.thumbPath)}`];
  if (snap.ico === "first") return ["缩略图：第一张同站正文图独立经过缩略图上传控件，不复用正文地址",
    "来源：本次本地图使用原文件；已有站内图使用服务器当前文件，不能恢复此前的裁切/水印。动态地址不支持时停止，请另选文件。"];
  return ["缩略图：不主动修改，保留后台默认或已明确映射的值"];
}

function validateThumbnailSnapshot(snap) {
  if (!["none", "file", "url", "clear", "first"].includes(snap.ico || "none"))
    return "缩略图操作无效，请重新选择";
  if (snap.ico === "file" && !snap.thumbPath) return "请选择独立图片缩略图";
  if (snap.ico === "url" && !String(snap.thumbUrl || "").trim())
    return "请填写缩略图地址；需要清空时请选择明确清空";
  return "";
}

function publishSnapshot(st) {
  const snapshot = {
    scode: String(st.pub.cat || ""), html: String(st.pub.html || ""),
    htmlReady: !!st.pub.htmlReady, mapping: Object.assign({}, st.pub.mapping || {}),
    overrides: Object.assign({}, st.pub.overrides || {}),
    backendFields: Object.assign({}, st.pub.backendValues || {}),
    manualImages: Array.from(st.pub.manualImages || []), width: st.pub.width,
    ico: st.pub.ico, thumbPath: st.pub.thumbPath, thumbUrl: st.pub.thumbUrl || "",
    carouselImages: Array.from(st.pub.carouselImages || []),
    galleryPlan: st.pub.galleryPlan != null ? st.pub.galleryPlan.map(item => ({...item})) : null,
    insertStrategy: st.pub.insertStrategy || "top",
    carouselSize: "original", carouselW: st.pub.carouselW,
    carouselH: st.pub.carouselH, inlineCount: st.pub.inlineCount,
    remoteCount: st.pub.remoteCount,
    top: !!st.pub.top, rec: !!st.pub.rec, head: !!st.pub.head,
    flagChanges: cmsFlagChanges(st.pub), checkLinks: st.pub.checkLinks === true,
    submitter: st.pub.submitter ? Object.assign({}, st.pub.submitter) : null,
    submitterOptions: Array.isArray(st.pub.submitterOptions) ? st.pub.submitterOptions.map(item => ({...item})) : [],
    parsedFields: Array.isArray(st.pub.parsedFields)
      ? st.pub.parsedFields.map(item => ({ key: item.key, value: item.value })) : [],
  };
  snapshot.assetMimes = typeof snapshotFileMimeHints === "function"
    ? snapshotFileMimeHints(snapshot) : {};
  return snapshot;
}

function publishFingerprint(snap) {
  return stableStringify({
    scode: snap.scode, html: snap.html, mapping: snap.mapping, overrides: snap.overrides,
    backendFields: snap.backendFields,
    manualImages: snap.manualImages, remoteCount: snap.remoteCount, width: snap.width, ico: snap.ico,
    thumbPath: snap.thumbPath, thumbUrl: snap.thumbUrl || "", carouselImages: snap.carouselImages, galleryPlan: snap.galleryPlan,
    carouselSize: "original", carouselW: snap.carouselW, carouselH: snap.carouselH,
    insertStrategy: snap.insertStrategy || "top",
    top: snap.top, rec: snap.rec, head: snap.head, flagChanges: snap.flagChanges,
    checkLinks: snap.checkLinks === true, submitter: snap.submitter,
    assetMimes: snap.assetMimes || {},
  });
}

function onLinkCheckDone(data) {
  const st = TABS.get(data.tab_id);
  if (!st) return;
  const key = String(data.request_id || "");
  const waiter = st._linkWaiters && st._linkWaiters[key];
  if (waiter) {
    delete st._linkWaiters[key];
    waiter.resolve(data);
  }
}

async function startLinkCheck(st, args) {
  const requestId = `${st.id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const resultPromise = new Promise((resolve) => {
    st._linkWaiters[requestId] = { resolve };
  });
  st.taskStarted = true;
  st._prog = { done: 0, total: 5, text: "1/5  提取正文链接…", task: "link_check" };
  if (st.id === ACTIVE) renderActiveTab();
  let started;
  try {
    started = await api().start_link_check(
      st.id, args.mapping || {}, args.overrides || {}, !!args.useCurrent,
      args.workflow || "publish", args.expectedArticleId || "", requestId);
  } catch (e) {
    started = { ok: false, msg: String(e) };
  }
  if (!started || !started.ok) {
    delete st._linkWaiters[requestId];
    return started || { ok: false, msg: "内链检查启动失败" };
  }
  return resultPromise;
}

async function reviewLinkProblems(st, lk, actionName) {
  const uncertain = (lk.links || []).filter((item) =>
    item.state === "unknown" || item.state === "unchecked");
  let uncertaintyAccepted = false;
  const releasePrompt = await acquirePromptSlot();
  try {
    if (uncertain.length) {
      const accepted = await confirmDialog("", {
        title: "部分内链暂时无法确认", kind: "warn",
        okText: "标记已知风险并继续", cancelText: `停止${actionName}`,
        lines: [`有 ${uncertain.length} 条内链因超时、限流、服务器异常或取消而无法确认。`,
          "它们不会被当作正确链接，也不会自动删除；继续表示你已看到该风险。"],
      });
      if (!accepted) return { ok: false, cancelled: true, message: `内链结果不确定，已停止${actionName}` };
      uncertaintyAccepted = true;
    }
    let linkActions = [];
    if ((lk.dead || []).length) {
      const resolved = await showDeadLinkPanel(lk.dead);
      if (resolved === null)
        return { ok: false, cancelled: true, message: `已取消${actionName}` };
      linkActions = resolved;
    }
    return { ok: true, linkActions, uncertaintyAccepted };
  } finally { releasePrompt(); }
}

async function performPublishChecks(st, snap, op) {
  st.taskKind = "link_check";
  setAreaMsg(st, "pub", "pubMsg", "第 1/5 步：恢复栏目表单与 HTML 快照…", "");
  const restored = await restorePublishBackend(st, snap, op);
  if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
  if (!restored.ok) return { ok: false, msg: restored.msg || "发布状态恢复失败" };

  st._prog = { done: 1, total: 5, text: "2/5  校验字段与图片…", task: "link_check" };
  if (st.id === ACTIVE) renderActiveTab();
  const check = await api().preflight(st.id, snap.mapping, snap.overrides, snap.backendFields);
  if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
  if (!check || !check.ok) return { ok: false, msg: (check && check.msg) || "发布前检查失败" };
  if ((check.errors || []).length)
    return { ok: false, msg: "❌ " + check.errors.join("\n❌ "), check };

  let links = {ok:true,links:[],dead:[],internal_count:0,skipped:true};
  let reviewed = {ok:true,linkActions:[],uncertaintyAccepted:false};
  if (snap.checkLinks === true) {
    setAreaMsg(st, "pub", "pubMsg", "第 2/5 步完成；正在分阶段检查正文内链（可取消）…", "");
    links = await startLinkCheck(st, {
      mapping: snap.mapping, overrides: snap.overrides, useCurrent: false, workflow: "publish",
    });
    if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
    if (!links || !links.ok)
      return { ok: false, cancelled: !!(links && links.cancelled),
        msg: (links && links.msg) || "内链检查失败" };
    reviewed = await reviewLinkProblems(st, links, "检查");
    if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
    if (!reviewed.ok) return { ok: false, cancelled: true, msg: reviewed.message };
  } else {
    setAreaMsg(st, "pub", "pubMsg", "字段与图片检查完成；未启用软件额外的内链联网检查。", "");
  }

  const prepared = {
    fingerprint: publishFingerprint(snap), snapshotToken: check.snapshot_token || "",
    check, links, linkActions: reviewed.linkActions,
    uncertaintyAccepted: reviewed.uncertaintyAccepted, checkedAt: Date.now(),
  };
  st.pub.preflightCache = prepared;
  return Object.assign({ ok: true }, prepared);
}

function snapshotCarouselDimensions(snap) {
  // Client-side crop/resize was removed to preserve the exact native
  // uploader bytes.  Keep this helper for old integrations; it now always
  // means "let the backend process the original file".
  return null;
}

function validateCarouselSnapshot(snap) {
  if (!(snap.carouselImages || []).length) return "";
  // Legacy drafts may carry a removed size preference.  It is normalized to
  // raw upload and must not block a browser-equivalent submission.
  return "";
}

function maximumWordsIssue(snap, cmsFields) {
  const fields = Array.isArray(cmsFields) ? cmsFields : [];
  const byName = new Map(fields.map((field) => [String(field.name || ""), field]));
  const parsed = new Map((snap.parsedFields || []).map((field) =>
    [String(field.key || ""), String(field.value ?? "")]));
  for (const [htmlKey, cmsName] of Object.entries(snap.mapping || {})) {
    const field = byName.get(String(cmsName || ""));
    const limit = Number(field && field.maximum_words);
    if (!field || !Number.isInteger(limit) || limit <= 0) continue;
    const raw = Object.prototype.hasOwnProperty.call(snap.overrides || {}, htmlKey)
      ? String(snap.overrides[htmlKey] ?? "") : String(parsed.get(htmlKey) || "");
    let text = raw;
    if (typeof document !== "undefined" && document.createElement) {
      const holder = document.createElement("div");
      holder.innerHTML = raw;
      text = holder.textContent || holder.innerText || "";
    } else {
      text = raw.replace(/<[^>]*>/g, "");
    }
    // JS string length is UTF-16 code units, which is also UEditor's count.
    if (text.length > limit)
      return `${field.label || cmsName}超过当前网页编辑器字数上限（${limit} 字符）`;
  }
  return "";
}

function validatePublishSnapshot(snap, cmsFields) {
  if (!snap.scode) return "请选择发布栏目";
  if (!snap.html || !snap.htmlReady) return "请重新选择并成功解析 HTML 文件";
  if (!Object.values(snap.mapping || {}).some(Boolean)) return "没有可提交的字段映射";
  if (snap.submitterOptions.length > 1 && !snap.submitter)
    return "当前新增表单有多个提交按钮，请先选择实际发布按钮";
  const thumbnailError = validateThumbnailSnapshot(snap);
  if (thumbnailError) return thumbnailError;
  // Keep the validator usable in the small isolated handler harnesses too;
  // the real WebView always defines maximumWordsIssue above this function.
  const wordIssue = typeof maximumWordsIssue === "function"
    ? maximumWordsIssue(snap, cmsFields) : "";
  if (wordIssue) return wordIssue;
  return validateCarouselSnapshot(snap);
}

async function restorePublishBackend(st, snap, op) {
  const category = await api().select_category(st.id, snap.scode, true);
  if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
  if (!category || !category.ok)
    return { ok: false, msg: (category && category.msg) || "发布栏目表单恢复失败" };
  const parsed = await api().parse_html(st.id, snap.html, "publish");
  if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
  if (!parsed || !parsed.ok)
    return { ok: false, msg: (parsed && parsed.msg) || "发布 HTML 重新解析失败" };
  return { ok: true };
}

async function preflight() {
  const st = activeState();
  if (!st || st.busy || hasPendingUiRequest(st)) return;
  const snap = publishSnapshot(st);
  const invalid = validatePublishSnapshot(snap, st.pub.fields);
  if (invalid) return setAreaMsg(st, "pub", "pubMsg", invalid, "bad");
  const op = beginOperation(st, "link_check");
  if (!op) return;
  const currentFingerprint = publishFingerprint(snap);
  const cached = st.pub.preflightCache;
  if (preflightCacheFresh(cached, currentFingerprint)) {
    const valid = await api().validate_preflight_snapshot(
      st.id, cached.snapshotToken, snap.mapping, snap.overrides, snap.backendFields);
    if (operationIsCurrent(st, op) && valid && valid.ok && valid.valid) {
      const age = Math.max(0, Math.round((Date.now() - cached.checkedAt) / 1000));
      setAreaMsg(st, "pub", "pubMsg", `✓ 完整检查仍有效（${age} 秒前），发布时将直接复用`, "ok");
      finishOperation(st, op);
      return cached.check;
    }
  }
  const prepared = await performPublishChecks(st, snap, op);
  if (!operationIsCurrent(st, op)) return null;
  if (!prepared.ok) {
    setAreaMsg(st, "pub", "pubMsg", prepared.msg || "发布前检查未完成",
      prepared.cancelled ? "" : "bad");
    finishOperation(st, op);
    return null;
  }
  const r = prepared.check;
  const lines = [];
  if (r.errors.length) lines.push("❌ " + r.errors.join("\n❌ "));
  if (r.warnings.length) lines.push("⚠ " + r.warnings.join("\n⚠ "));
  const issueCount = (prepared.links.dead || []).length;
  lines.push(`提交字段 ${r.field_count} 个；内置图 ${r.inline_count} 张` +
    (snap.checkLinks ? `；站内链接 ${prepared.links.internal_count || 0} 条` : "；内链检查未启用"));
  if (issueCount) lines.push(`已记录 ${issueCount} 条内链处理方案`);
  lines.push(snap.checkLinks
    ? "✓ 本次完整检查结果已缓存；内容不变时点击发布不会重复联网检查"
    : "✓ 本次字段与图片检查已缓存；正文链接将保持原样");
  setAreaMsg(st, "pub", "pubMsg", lines.join("\n"), r.errors.length ? "bad" : "ok");
  finishOperation(st, op);
  return r;
}
async function publish() {
  const st = activeState();
  if (!st || st.busy || hasPendingUiRequest(st)) return;
  if (!await confirmPreviousWriteReview(st, "pub")) return;
  if (!validateCmsControls($("pubBackendFields"))) return;
  const snap = publishSnapshot(st);
  const invalid = validatePublishSnapshot(snap, st.pub.fields);
  if (invalid) return setAreaMsg(st, "pub", "pubMsg", invalid, "bad");
  const op = beginOperation(st, "publish");
  if (!op) return;
  st.pub.retryable = false;
  let prepared = null;
  let reusedPreflight = false;
  const cached = st.pub.preflightCache;
  // Keep the handler self-contained for embedded/legacy WebViews that load
  // individual handlers before the top-level helper has been evaluated.
  const cacheFresh = typeof preflightCacheFresh === "function"
    ? preflightCacheFresh(cached, publishFingerprint(snap))
    : !!(cached && cached.fingerprint === publishFingerprint(snap) && cached.snapshotToken);
  if (cacheFresh) {
    setAreaMsg(st, "pub", "pubMsg", "正在确认缓存的完整检查仍对应当前文件…", "");
    const valid = await api().validate_preflight_snapshot(
      st.id, cached.snapshotToken, snap.mapping, snap.overrides, snap.backendFields);
    if (!operationIsCurrent(st, op)) return;
    if (valid && valid.ok && valid.valid) {
      prepared = Object.assign({ ok: true }, cached);
      reusedPreflight = true;
      const age = Math.max(0, Math.round((Date.now() - cached.checkedAt) / 1000));
      setAreaMsg(st, "pub", "pubMsg", `✓ 已复用 ${age} 秒前的完整发布检查，无需重复联网`, "ok");
    } else {
      st.pub.preflightCache = null;
    }
  }
  if (!prepared) prepared = await performPublishChecks(st, snap, op);
  if (!operationIsCurrent(st, op)) return;
  if (!prepared || !prepared.ok) {
    setAreaMsg(st, "pub", "pubMsg", (prepared && prepared.msg) || "发布前检查未完成",
      prepared && prepared.cancelled ? "" : "bad");
    finishOperation(st, op);
    return;
  }
  const check = prepared.check;
  const lk = prepared.links;
  const linkActions = Array.from(prepared.linkActions || []);
  st.taskKind = "publish";
  const releasePrompt = await acquirePromptSlot();
  if (!operationIsCurrent(st, op)) { releasePrompt(); return; }
  let okGo = false;
  try {
  okGo = await confirmDialog("", {
    title: "确认发布？", kind: "confirm", okText: "立即发布",
    lines: [
      `站点：${st.title}`,
      `栏目：${snap.scode}`,
      `HTML：${fileName(snap.html)}`,
      `提交字段：${check.field_count} 个`,
    ].concat([`检查：已完成并锁定当前快照${reusedPreflight ? "（复用）" : ""}`,
      snap.checkLinks ? "内链：已执行软件额外联网检查" : "内链：未启用额外检查，正文链接保持原样"])
     .concat(linkActions.length ? [`内链处理：${linkActions.length} 条`] : [])
     .concat(snap.inlineCount ? [`内置图：${snap.inlineCount} 张（自动上传）`] : [])
     .concat(snap.remoteCount ? [`远程图：${snap.remoteCount} 个（按当前编辑器抓取配置处理）`] : [])
     .concat(thumbnailSummary(snap))
     .concat(gallerySummary(snap))
     .concat(snap.carouselImages.length
       ? [`轮播图：${snap.carouselImages.map(fileName).join("、")}`] : []),
  });
  } finally {
    releasePrompt();
  }
  if (!operationIsCurrent(st, op)) return;
  if (!okGo) {
    setAreaMsg(st, "pub", "pubMsg", "已取消发布", "");
    finishOperation(st, op);
    return;
  }
  try {
    const saved = await api().save_mapping(st.id, snap.mapping, "publish");
    if (saved && !saved.ok) log("发布映射保存失败（不影响本次发布）: " + saved.msg, st.id);
  } catch (e) { log("发布映射保存失败（不影响本次发布）: " + e, st.id); }
  if (!operationIsCurrent(st, op)) return;
  let browserFirstImage = null;
  if (snap.ico === "first" && typeof resolveBrowserFirstImage === "function") {
    browserFirstImage = await resolveBrowserFirstImage(
      previewBodyFor(st, "pub"), st.url);
    if (!operationIsCurrent(st, op)) return;
  }
  st.taskStarted = true;
  st._prog = { done: 0, total: 1, text: "启动…", task: "publish" };
  if (st.id === ACTIVE) renderActiveTab();
  const r = await api().publish(st.id, {
    scode: snap.scode, mapping: snap.mapping, overrides: snap.overrides,
    backend_fields: snap.backendFields,
    asset_mimes: snap.assetMimes || {},
    preflight_token: prepared.snapshotToken || "",
    html_path: snap.html,
    image_paths: snap.manualImages, width_mode: snap.width,
    ...thumbnailSubmission(snap),
    carousel_paths: snap.carouselImages,
    ...gallerySubmission(snap),
    carousel_size: snapshotCarouselDimensions(snap),
    responsive_context: (typeof responsiveContext === "function" ? responsiveContext() : {}),
    browser_first_image: browserFirstImage,
    submitter: snap.submitter,
    ...snap.flagChanges,
    strategy: snap.insertStrategy || "top", link_actions: linkActions,
  });
  if (!operationIsCurrent(st, op)) return;
  if (!r || !r.ok) {
    setAreaMsg(st, "pub", "pubMsg", (r && r.msg) || "发布任务启动失败", "bad");
    finishOperation(st, op);
  }
}
function isWriteReview(result) {
  return !!result?.requires_review || ["unknown", "reported_unverified", "different"].includes(result?.outcome);
}

async function confirmPreviousWriteReview(st, areaName) {
  const area = st[areaName];
  if (!area?.requiresReview) return true;
  const yes = await confirmDialog("", {
    title: "上次保存结果仍待核对", kind: "warn", okText: "已核对，继续本次操作", cancelText: "先去后台核对",
    lines: ["请先确认上次是否已经保存，以及正文、图片和字段是否符合预期。",
      "再次提交不会撤销上次操作；新增可能产生重复文章。只有核对后仍需要提交，才继续。"],
  });
  if (!yes || !isLiveState(st) || st.busy || st[areaName] !== area) return false;
  area.requiresReview = false;
  scheduleDraftSave(st, areaName === "pub" ? "publish" : "edit");
  return true;
}

function batchCanRunItem(item, failedOnly) {
  return failedOnly ? item.status === "failed" : ["pending", "failed", "paused"].includes(item.status || "pending");
}

function restoredBatchStatus(status) {
  if (["review", "running", "unknown", "reported_unverified", "different"].includes(status)) return "review";
  return status === "success" ? "success" : "pending";
}

function batchResultStatus(result) {
  return isWriteReview(result) ? "review" : (result.ok ? "success" : "failed");
}

function onPublishDone(d) {
  const st = TABS.get(d.tab_id);
  if (st && st.batch && st.batch._resolve) {
    const resolve = st.batch._resolve;
    st.batch._resolve = null;
    resolve(d);
    return;
  }
  if (st) {
    st.busy = false;
    st.taskKind = "";
    st.taskStarted = false;
    st._prog = null;
    st.pub.retryable = !isWriteReview(d) && !!d.has_failed;
    st.pub.uploadMetadata = Array.isArray(d.upload_metadata) ? d.upload_metadata : [];
    st.pub.thumbServerInfo = typeof thumbnailServerSummary === "function"
      ? thumbnailServerSummary(st.pub.uploadMetadata) : "";
    st.pub.thumbServerUrl = typeof thumbnailServerUrl === "function"
      ? thumbnailServerUrl(st.pub.uploadMetadata) : "";
  }
  renderTabStrip();
  const review = isWriteReview(d);
  const summary = completionSummary(d, "发布");
  const line = d.cancelled ? d.msg : (review ? "⚠️ " : (d.ok ? "✅ " : "❌ ")) + summary;
  if (st) {
    st.pub.msg = line;
    st.pub.msgClass = d.cancelled ? "" : (review ? "review" : (d.ok ? "ok" : "bad"));
    if (d.native_url) st.pub.nativeUrl = String(d.native_url);
    if (review || d.ok) st.pub.requiresReview = review;
    if (review) scheduleDraftSave(st, "publish");
  }
  if (d.ok && st) {
    // 成功后只保留站点、栏目和尺寸偏好；内容及文章专属素材全部失效。
    st.pub.html = "";
    st.pub.htmlReady = false;
    st.pub.htmlLoading = false;
    st.pub.htmlInfo = "";
    st.pub.htmlInfoClass = "";
    st.pub.parsedFields = [];
    st.pub.mapping = {};
    st.pub.overrides = {};
    st.pub.inlineCount = 0; st.pub.remoteCount = 0;
    st.pub.manualImages = [];
    if (!st.pub.rememberThumb) {
      st.pub.thumbPath = "";
      st.pub.thumbUrl = "";
      st.pub.ico = "none";
    }
    st.pub.carouselImages = [];
    st.pub.galleryPlan = null;
    st.pub.preflightCache = null;
    deleteDraftNow(st, "publish");
  }
  log((d.ok ? "发布成功: " : "发布结束: ") + d.msg, d.tab_id);
  if (d.tab_id === ACTIVE) renderActiveTab();
  if (st && d.native_only && d.native_url && d.tab_id === ACTIVE &&
      typeof openNativeRecordUrl === "function")
    setTimeout(() => openNativeRecordUrl(st, d.native_url, "动态内容发布页", {
      handoff: nativeHandoffSnapshot(st, "publish", publishSnapshot(st), {
        message: d.native_reason || d.msg || "网页脚本接管发布；当前草稿字段已带入，未自动提交。",
      }),
    }), 0);
  if (d.ok && d.tab_id === ACTIVE) alertDialog(summary, { title: "发布成功", kind: "success" });
  if (review && d.tab_id === ACTIVE) alertDialog(d.msg, { title: "发布结果待核对", kind: "warn" });
}
async function retryImages() {
  const st = activeState();
  if (!st || st.busy || !st.pub.retryable) return;
  const op = beginOperation(st, "publish");
  if (!op) return;
  st.taskStarted = true;
  st._prog = { done: 0, total: 1, text: "准备重试…", task: "publish" };
  if (st.id === ACTIVE) renderActiveTab();
  const r = await api().retry_failed_images(st.id, {
    // 后端使用首次失败任务的完整快照；这里不再读取可能已经变化的当前 DOM。
  });
  if (!operationIsCurrent(st, op)) return;
  if (!r || !r.ok) {
    setAreaMsg(st, "pub", "pubMsg", (r && r.msg) || "重试启动失败", "bad");
    finishOperation(st, op);
  }
}
async function pickManualImages() {
  const st = activeState();
  if (!st || st.busy) return;
  const r = await api().pick_images("publish_content_images");
  if (isLiveState(st) && r.ok && r.paths.length) {
    if (typeof rememberFileMimeHints === "function")
      rememberFileMimeHints(r.paths || [], r.file_types || []);
    if (typeof appendManualImagePaths === "function") appendManualImagePaths(st, r.paths, true);
    else {
      // Keep isolated integrations that load this picker handler on its own
      // compatible; the full app uses the shared append helper above.
      st.pub.manualImages = r.paths;
      notePublishChanged(st);
      if (st.id === ACTIVE) renderActiveTab();
      log(`将处理 ${st.pub.inlineCount || 0} 个内置图片资源，并额外插入 ${r.paths.length} 张手选图`, st.id);
    }
  }
}

function appendManualImagePaths(st, paths, replace = false) {
  if (!isLiveState(st) || !Array.isArray(paths)) return false;
  const incoming = paths.map(value => String(value || "")).filter(Boolean);
  if (!incoming.length) return false;
  const current = replace ? [] : Array.from(st.pub.manualImages || []);
  st.pub.manualImages = current.concat(incoming);
  notePublishChanged(st);
  if (st.id === ACTIVE) renderActiveTab();
  log(`将处理 ${st.pub.inlineCount || 0} 个内置图片资源，并额外插入 ${st.pub.manualImages.length} 张手选图`, st.id);
  return true;
}

function carouselDimensions(area) {
  return null;
}

function gallerySubmission(area) {
  return area.galleryPlan == null ? {} : {gallery_plan: area.galleryPlan.map(item => ({...item}))};
}

function gallerySummary(area) {
  return area.galleryPlan == null ? [] : [area.galleryPlan.length
    ? `图集：按编辑后的 ${area.galleryPlan.length} 张及逐图标题、顺序完整保存`
    : "图集：明确清空全部图片引用及标题（不删除服务器文件）"];
}

function galleryInitialPlan(area) {
  if (area.galleryPlan != null) return area.galleryPlan.map(item => ({...item}));
  const value = (area.fields || []).find(field => field.name === "pics")?.value || "";
  let urls;
  if (Array.isArray(value)) urls = value.slice();
  else if (String(value).trim().startsWith("[")) {
    urls = JSON.parse(value);
    if (!Array.isArray(urls)) throw new Error("图集格式不是数组");
  } else {
    const text = String(value).trim();
    const separator = ["\r\n", "\n", "|", ";", ","].find(sep => text.includes(sep)) || ",";
    urls = text ? text.split(separator).map(url => url.trim()).filter(Boolean) : [];
  }
  if (urls.some(url => typeof url !== "string" || !url.trim())) throw new Error("图集图片地址格式不明确，请核对后台");
  const rawTitles = (area.fields || []).find(field => field.name === "picstitle[]")?.value ?? [];
  const titles = Array.isArray(rawTitles) ? rawTitles : [String(rawTitles)];
  if (titles.slice(urls.length).some(title => String(title))) throw new Error("图集有未对应图片的标题，请先核对后台");
  const existing = area.carouselMode === "replace" && (area.carouselImages || []).length ? []
    : urls.map((value, index) => ({kind:"url", value, title:String(titles[index] ?? "")}));
  return existing.concat((area.carouselImages || []).map(value => ({kind:"file", value, title:""})));
}

let _galleryEditorContext = null;
function galleryContextCurrent(ctx) {
  return ctx && isLiveState(ctx.st) && !ctx.st.busy && ctx.st[ctx.which] === ctx.area &&
    ctx.target === JSON.stringify([ctx.area.cat, ctx.area.artId, ctx.area.html]);
}

function openGalleryEditor(which) {
  const st = activeState();
  if (!st || st.busy) return;
  const area = st[which];
  if (!(area.fields || []).some(field => field.name === "pics" && !field.disabled))
    return alertDialog("请先加载含 pics 图集字段的后台表单", {kind:"warn"});
  let items;
  try { items = galleryInitialPlan(area); }
  catch (error) { return alertDialog(String(error.message || error), {kind:"warn"}); }
  _galleryEditorContext = {st, which, area, items, original:JSON.stringify(items),
    target:JSON.stringify([area.cat, area.artId, area.html])};
  $("galleryEditorMask").hidden = false;
  renderGalleryEditor();
}

function moveGalleryItem(from, to) {
  const ctx = _galleryEditorContext;
  if (!galleryContextCurrent(ctx) || !Number.isInteger(from) || !Number.isInteger(to) ||
      from < 0 || to < 0 || from >= ctx.items.length || to >= ctx.items.length) return;
  ctx.items.splice(to, 0, ctx.items.splice(from, 1)[0]);
  renderGalleryEditor();
}

function moveGalleryItemWithFocus(from, to) {
  const ctx = _galleryEditorContext;
  if (!galleryContextCurrent(ctx) || !Number.isInteger(from) || !Number.isInteger(to) ||
      from < 0 || to < 0 || from >= ctx.items.length || to >= ctx.items.length) return;
  ctx.items.splice(to, 0, ctx.items.splice(from, 1)[0]);
  renderGalleryEditor();
  const host = $("galleryEditorRows");
  const row = host && host.children ? host.children[to] : null;
  if (row && typeof row.focus === "function") row.focus();
}

function moveGalleryItemToBoundary(index, boundary) {
  const ctx = _galleryEditorContext;
  if (!galleryContextCurrent(ctx) || !Number.isInteger(index) ||
      index < 0 || index >= ctx.items.length || !ctx.items.length) return;
  const target = boundary === "end" ? ctx.items.length - 1 : 0;
  if (index === target) return;
  moveGalleryItemWithFocus(index, target);
}

function galleryRemotePreviewUrl(st, value) {
  const text = String(value || "").trim();
  if (!text || /^(?:data|blob|file|javascript):/i.test(text)) return "";
  try {
    const url = new URL(text, String(st?.url || ""));
    return /^https?:$/i.test(url.protocol) ? url.href : "";
  } catch (_) { return ""; }
}

async function fillGalleryPreview(ctx, row, item, preview, index) {
  if (!ctx || !row || !item || !preview || !galleryContextCurrent(ctx)) return;
  let src = "";
  if (item.kind === "file") {
    try {
      const bridge = api();
      if (bridge && typeof bridge.preview_local_image === "function") {
        const result = await bridge.preview_local_image(item.value);
        if (result && result.ok && result.data_url) src = String(result.data_url);
      }
    } catch (_) { /* preview is optional; upload remains available */ }
  } else {
    src = galleryRemotePreviewUrl(ctx.st, item.value);
  }
  if (!galleryContextCurrent(ctx) || row.isConnected === false ||
      row.dataset.previewIndex !== String(index)) return;
  if (!src) {
    preview.textContent = "无预览";
    return;
  }
  const image = document.createElement("img");
  image.alt = `第 ${index + 1} 张图片预览`;
  image.loading = "lazy";
  image.src = src;
  image.addEventListener("error", () => { preview.textContent = "无预览"; });
  preview.replaceChildren(image);
}

function renderGalleryEditor() {
  const ctx = _galleryEditorContext, host = $("galleryEditorRows");
  host.replaceChildren();
  if (!ctx) return;
  if (!host.dataset) host.dataset = {};
  if (typeof installFileDropTarget === "function" && !host.dataset.dropInstalled) {
    installFileDropTarget(host, { onDrop: async files => {
      const current = _galleryEditorContext;
      if (!galleryContextCurrent(current)) return;
      const extensions = typeof imageDropExtensions === "function"
        ? imageDropExtensions(current.st, "pics", current.which) : [];
      const paths = await materializeDroppedFiles(
        files, `gallery_drop_${current.which}`, true, extensions);
      if (_galleryEditorContext !== current || !galleryContextCurrent(current)) return;
      current.items.push(...paths.map(value => ({kind: "file", value, title: ""})));
      renderGalleryEditor();
    }});
    host.dataset.dropInstalled = "1";
  }
  if (!ctx.items.length) { host.textContent = "图集为空；可添加图片。删除全部后应用并提交文章将清空图集引用。"; return; }
  ctx.items.forEach((item, index) => {
    const row = document.createElement("div"); row.className = "gallery-row"; row.draggable = true;
    row.setAttribute("tabindex", "0");
    row.setAttribute("role", "listitem");
    row.setAttribute("aria-label", `第 ${index + 1} 张图集图片，可用上下方向键排序`);
    row.setAttribute("aria-grabbed", ctx.keyboardDragIndex === index ? "true" : "false");
    if (ctx.keyboardDragIndex === index) row.classList.add("drag-source");
    row.dataset = row.dataset || {};
    row.dataset.previewIndex = String(index);
    const preview = document.createElement("div"); preview.className = "gallery-thumb";
    preview.textContent = "加载预览…";
    const label = document.createElement("div"); label.textContent = `${index + 1}. ${item.kind === "file" ? "待上传" : "已有地址"}：${item.value}`;
    const title = document.createElement("input"); title.type = "text"; title.value = item.title;
    title.placeholder = "图片标题（可留空）"; title.setAttribute("aria-label", `第 ${index + 1} 张图片标题`);
    title.addEventListener("input", () => { if (galleryContextCurrent(ctx)) item.title = title.value; });
    const buttons = document.createElement("div");
    for (const [text, action, disabled] of [["上移", () => moveGalleryItem(index, index-1), index===0],
        ["下移", () => moveGalleryItem(index, index+1), index===ctx.items.length-1],
        ["移除", () => { if(galleryContextCurrent(ctx)){ctx.items.splice(index,1);renderGalleryEditor();} }, false]]) {
      const button=document.createElement("button"); button.type="button"; button.className="ghost mini-btn";
      button.textContent=text;button.disabled=disabled;button.addEventListener("click",action);buttons.appendChild(button);
    }
    row.addEventListener("dragstart", e => { if(e.target === title){e.preventDefault();return;} ctx.dragIndex=index; row.classList.add("drag-source"); });
    row.addEventListener("dragover", e => { e.preventDefault(); row.classList.add("drag-target"); });
    row.addEventListener("dragleave", () => row.classList.remove("drag-target"));
    row.addEventListener("drop", e => {e.preventDefault();row.classList.remove("drag-target");moveGalleryItem(ctx.dragIndex,index);ctx.dragIndex=null;});
    row.addEventListener("dragend", () => {ctx.dragIndex=null;row.classList.remove("drag-source","drag-target");});
    // Native HTML5 drag is unavailable or inconsistent on touch WebViews.
    // Pointer capture gives the same reorder operation to pen/touch input
    // without treating a title edit or a button press as a drag gesture.
    let pointerStart = null;
    row.addEventListener("pointerdown", e => {
      if (e.pointerType === "mouse" || e.target === title ||
          (e.target && typeof e.target.closest === "function" && e.target.closest("button,input"))) return;
      pointerStart = {id: e.pointerId, y: e.clientY, moved: false};
      if (typeof row.setPointerCapture === "function") {
        try { row.setPointerCapture(e.pointerId); } catch (_) { /* optional WebView API */ }
      }
    });
    row.addEventListener("pointermove", e => {
      if (!pointerStart || pointerStart.id !== e.pointerId) return;
      if (Math.abs(Number(e.clientY || 0) - Number(pointerStart.y || 0)) < 10) return;
      pointerStart.moved = true;
      row.classList.add("drag-source");
      const rows = Array.from(host.children || []);
      const target = rows.findIndex(candidate => {
        if (candidate === row || typeof candidate.getBoundingClientRect !== "function") return false;
        const box = candidate.getBoundingClientRect();
        return Number(e.clientY || 0) >= box.top && Number(e.clientY || 0) <= box.bottom;
      });
      if (target >= 0 && target !== index) {
        row.classList.add("drag-target");
        ctx.dragIndex = index;
        moveGalleryItem(index, target);
        pointerStart = {id: e.pointerId, y: e.clientY, moved: true};
      }
    });
    row.addEventListener("pointerup", e => {
      if (!pointerStart || pointerStart.id !== e.pointerId) return;
      row.classList.remove("drag-source","drag-target");
      if (typeof row.releasePointerCapture === "function") {
        try { row.releasePointerCapture(e.pointerId); } catch (_) { /* optional WebView API */ }
      }
      pointerStart = null; ctx.dragIndex = null;
    });
    row.addEventListener("pointercancel", () => {
      pointerStart = null; ctx.dragIndex = null;
      row.classList.remove("drag-source","drag-target");
    });
    // Keep keyboard users on the same reorder path as drag-and-drop.  The
    // row itself is focusable so title inputs keep their normal arrow-key
    // editing behavior; only an explicitly focused row consumes arrows.
    row.addEventListener("keydown", e => {
      if (!galleryContextCurrent(ctx) || e.target !== row) return;
      const picked = Number.isInteger(ctx.keyboardDragIndex);
      const activeIndex = picked ? ctx.keyboardDragIndex : index;
      if (e.key === "ArrowUp" && activeIndex > 0) {
        e.preventDefault(); if (picked) ctx.keyboardDragIndex = activeIndex - 1;
        moveGalleryItemWithFocus(activeIndex, activeIndex - 1);
      } else if (e.key === "ArrowDown" && activeIndex < ctx.items.length - 1) {
        e.preventDefault(); if (picked) ctx.keyboardDragIndex = activeIndex + 1;
        moveGalleryItemWithFocus(activeIndex, activeIndex + 1);
      } else if (e.key === "Home") {
        e.preventDefault(); if (picked) ctx.keyboardDragIndex = 0;
        moveGalleryItemToBoundary(activeIndex, "start");
      } else if (e.key === "End") {
        e.preventDefault(); if (picked) ctx.keyboardDragIndex = ctx.items.length - 1;
        moveGalleryItemToBoundary(activeIndex, "end");
      } else if (e.key === " " || e.key === "Spacebar") {
        // Space is the conventional accessible "pick up/drop" gesture. A
        // second press drops at the current position; while picked up, the
        // arrow keys continue to use the same reorder path as before.
        e.preventDefault();
        if (ctx.keyboardDragIndex == null) {
          ctx.keyboardDragIndex = index;
          row.classList.add("drag-source");
        } else {
          ctx.keyboardDragIndex = null;
          row.classList.remove("drag-source");
        }
      }
    });
    // Keep the existing label/title/button order stable for keyboard users;
    // the visual thumbnail is an additional trailing cell.
    row.append(label,title,buttons,preview);host.appendChild(row);
    if (typeof fillGalleryPreview === "function")
      void fillGalleryPreview(ctx, row, item, preview, index);
  });
}

async function addGalleryFiles() {
  const ctx = _galleryEditorContext;
  if (!galleryContextCurrent(ctx)) return;
  const result = await api().pick_images(
    ctx.which === "pub" ? "publish_carousel" : "edit_carousel",
    typeof imagePickerExtensions === "function" ?
      imagePickerExtensions(ctx.st, "pics", ctx.which) : []);
  if (_galleryEditorContext !== ctx || !galleryContextCurrent(ctx) || !result?.ok) return;
  if (typeof rememberFileMimeHints === "function")
    rememberFileMimeHints(result.paths || [], result.file_types || []);
  ctx.items.push(...(result.paths || []).map(value => ({kind:"file",value,title:""})));
  renderGalleryEditor();
}

async function closeGalleryEditor(apply) {
  const ctx = _galleryEditorContext;
  if (!ctx) return;
  const changed = JSON.stringify(ctx.items) !== ctx.original;
  if (apply && !galleryContextCurrent(ctx)) return alertDialog("文章或栏目状态已变化，请取消后重新打开图集", {kind:"warn"});
  if (!apply && changed && !await confirmDialog("放弃本次图集编辑？", {kind:"confirm"})) return;
  if (_galleryEditorContext !== ctx) return;
  if (apply && changed) {
    ctx.area.galleryPlan = ctx.items.map(item => ({...item}));
    ctx.area.carouselImages = ctx.items.filter(item => item.kind === "file").map(item => item.value);
    if (ctx.which === "pub") notePublishChanged(ctx.st); else scheduleDraftSave(ctx.st,"edit");
  }
  _galleryEditorContext = null;$("galleryEditorMask").hidden = true;
  if (ctx.st.id === ACTIVE) renderActiveTab();
}

function appendCarouselPaths(st, which, paths) {
  if (!st || !isLiveState(st) || !Array.isArray(paths) || !paths.length) return false;
  const area = st[which];
  if (area.galleryPlan != null) {
    area.galleryPlan = area.galleryPlan.concat(paths.map(value => ({kind:"file", value, title:""})));
    area.carouselImages = area.galleryPlan.filter(item => item.kind === "file").map(item => item.value);
  } else area.carouselImages = paths;
  if (which === "pub") notePublishChanged(st);
  else scheduleDraftSave(st, "edit");
  const id = which === "pub" ? "pubCarouselInfo" : "editCarouselInfo";
  const clearId = which === "pub" ? "btnPubCarouselClear" : "btnEditCarouselClear";
  if (st.id === ACTIVE) {
    $(id).textContent = `已选择 ${area.carouselImages.length} 张`;
    $(clearId).hidden = false;
    if (area.galleryPlan != null) renderActiveTab();
  }
  return true;
}
async function pickCarousel(which) {
  const st = activeState();
  if (!st || st.busy) return;
  const r = await api().pick_images(
    which === "pub" ? "publish_carousel" : "edit_carousel",
    typeof imagePickerExtensions === "function" ?
      imagePickerExtensions(st, "pics", which) : []);
  if (!isLiveState(st) || !r.ok || !r.paths.length) return;
  if (typeof rememberFileMimeHints === "function")
    rememberFileMimeHints(r.paths || [], r.file_types || []);
  if (typeof appendCarouselPaths === "function") {
    appendCarouselPaths(st, which, r.paths);
    return;
  }
  // Keep the standalone legacy/test invocation self-contained when only
  // this function is loaded by an embedded integration.
  const area = st[which];
  if (area.galleryPlan != null) {
    area.galleryPlan = area.galleryPlan.concat(r.paths.map(value => ({kind:"file", value, title:""})));
    area.carouselImages = area.galleryPlan.filter(item => item.kind === "file").map(item => item.value);
  } else area.carouselImages = r.paths;
  if (which === "pub") notePublishChanged(st); else scheduleDraftSave(st, "edit");
}
function applyThumbnailPath(st, which, path) {
  if (!st || !path || !isLiveState(st)) return false;
  st[which].thumbPath = String(path);
  st[which].thumbServerInfo = "";
  st[which].thumbServerUrl = "";
  if (which === "pub") notePublishChanged(st);
  else scheduleDraftSave(st, "edit");
  const buttonId = which === "pub" ? "btnPubThumb" : "btnEditThumb";
  const infoId = which === "pub" ? "pubThumbInfo" : "editThumbInfo";
  if (st.id === ACTIVE) {
    $(buttonId).hidden = false;
    $(infoId).textContent = fileName(path);
    if (typeof renderThumbnailPreview === "function") void renderThumbnailPreview(st, which);
  }
  return true;
}
async function pickThumbnail(which) {
  const st = activeState();
  if (!st || st.busy) return false;
  const r = await api().pick_image(
    which === "pub" ? "publish_thumbnail" : "edit_thumbnail",
    typeof imagePickerExtensions === "function" ?
      imagePickerExtensions(st, "ico", which) : []);
  if (!isLiveState(st)) return false;
  if (!r.ok) {
    const msgId = which === "pub" ? "pubMsg" : "editMsg";
    if (st.id === ACTIVE) setMsg(msgId, r.msg || "请选择缩略图文件", "bad");
    return false;
  }
  if (r.cancelled) return false;
  if (typeof rememberFileMimeHints === "function")
    rememberFileMimeHints(r.path ? [r.path] : [], r.file_types || []);
  if (typeof applyThumbnailPath === "function") return applyThumbnailPath(st, which, r.path);
  // Standalone integrations may load this legacy picker function without the
  // surrounding renderer; retain the same state update in that case.
  st[which].thumbPath = r.path;
  st[which].thumbServerInfo = "";
  st[which].thumbServerUrl = "";
  if (which === "pub") notePublishChanged(st); else scheduleDraftSave(st, "edit");
  return true;
}

function fileName(path) {
  return String(path || "").split(/[\\/]/).pop();
}

function imagePickerExtensions(st, fieldName, areaName) {
  const area = areaName && st ? st[areaName] : null;
  const fields = (area && area.fields) || (st && st.pub && st.pub.fields) ||
    (st && st.edit && st.edit.fields) || [];
  const field = fields.find(item => String(item.name || "") === String(fieldName || ""));
  const accept = String((field && field.accept) || "");
  if (!accept || accept.includes("image/*")) return [];
  // ``accept`` may name a concrete image MIME (for example image/heic or
  // image/svg+xml), not only a suffix.  Reuse the MIME map used by ordinary
  // file controls so the native image picker does not silently fall back to
  // the broad default list for those fields.
  const mapped = filePickerExtensions(accept);
  const imageOnly = mapped.filter(value =>
    /^\.(?:jpe?g|jpe|png|gif|webp|bmp|tiff?|svg|avif|heic|heif|ico|jxl|jp2|j2k|jpf|jpx|jpm|psd)$/i.test(value));
  return imageOnly.length ? imageOnly :
    (mapped.includes(".pboot-no-match") ? mapped : []);
}

function imageDropExtensions(st, fieldName, areaName) {
  const mapped = imagePickerExtensions(st, fieldName, areaName);
  // ``image/*`` intentionally returns [] for the native dialog because
  // pick_image already supplies the platform's standard image filter.  A
  // data-URL drop has no such dialog, so make the same filter explicit and do
  // not accidentally accept a PDF renamed as an image.
  return mapped.length ? mapped :
    [".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
      ".svg", ".avif", ".heic", ".heif", ".ico", ".jxl",
      ".jp2", ".j2k", ".jpf", ".jpx", ".jpm", ".psd"];
}

// Native Windows dialogs accept suffixes, while browser ``accept`` also
// permits MIME types and wildcards.  Map the common MIME forms explicitly so
// a file field with ``image/*``/``application/pdf`` does not accidentally open
// an unrestricted all-files picker.  Unknown MIME types remain unrestricted
// only when the browser itself would also leave them to the OS.
function filePickerExtensions(accept) {
  const map = {
    "image/*": [".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".avif", ".heic", ".heif", ".ico", ".jxl", ".jp2", ".j2k", ".jpf", ".jpx", ".jpm", ".psd"],
    "image/jpeg": [".jpg", ".jpeg", ".jpe"],
    "image/png": [".png"],
    "image/gif": [".gif"],
    "image/webp": [".webp"],
    "image/bmp": [".bmp"],
    "image/tiff": [".tif", ".tiff"],
    "image/svg+xml": [".svg"],
    "image/avif": [".avif"],
    "image/heic": [".heic"],
    "image/heif": [".heif"],
    "image/x-icon": [".ico"],
    "image/vnd.microsoft.icon": [".ico"],
    "image/jxl": [".jxl"],
    "image/jp2": [".jp2", ".j2k", ".jpf", ".jpx", ".jpm"],
    "image/vnd.adobe.photoshop": [".psd"],
    "audio/*": [".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".amr", ".ape", ".mid", ".midi", ".mka", ".wma", ".caf", ".ac3"],
    "audio/mpeg": [".mp3"],
    "audio/mp4": [".m4a", ".mp4"],
    "audio/aac": [".aac"],
    "audio/flac": [".flac"],
    "audio/opus": [".opus"],
    "audio/amr": [".amr"],
    "audio/ape": [".ape"],
    "audio/midi": [".mid", ".midi"],
    "audio/x-matroska": [".mka"],
    "audio/x-ms-wma": [".wma"],
    "audio/x-caf": [".caf"],
    "audio/vnd.dolby.dd-raw": [".ac3"],
    "audio/wav": [".wav"],
    "audio/ogg": [".ogg", ".oga"],
    "audio/x-mpegurl": [".m3u"],
    "application/vnd.apple.mpegurl": [".m3u8"],
    "video/*": [".mp4", ".m4v", ".webm", ".ogv", ".mov", ".avi", ".mkv", ".3gp", ".3g2", ".flv", ".wmv", ".asf", ".rm", ".rmvb", ".ts", ".mts", ".m2ts"],
    "video/mp4": [".mp4"],
    "video/x-m4v": [".m4v"],
    "video/webm": [".webm"],
    "video/x-matroska": [".mkv"],
    "video/quicktime": [".mov"],
    "video/x-msvideo": [".avi"],
    "video/x-pn-realvideo": [".rm"],
    "video/vnd.rn-realvideo": [".rmvb"],
    "video/mp2t": [".ts", ".mts", ".m2ts"],
    "application/pdf": [".pdf"],
    "application/zip": [".zip"],
    "application/x-7z-compressed": [".7z"],
    "application/x-rar-compressed": [".rar"],
    "application/gzip": [".gz"],
    "application/x-tar": [".tar"],
    "application/msword": [".doc"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.ms-powerpoint": [".ppt"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": [".pptx"],
    "font/otf": [".otf"],
    "font/ttf": [".ttf"],
    "font/woff": [".woff"],
    "font/woff2": [".woff2"],
    "application/vnd.ms-fontobject": [".eot"],
    "application/json": [".json"],
    "text/plain": [".txt", ".log"],
    "text/csv": [".csv"],
    "text/html": [".html", ".htm"],
    "text/xml": [".xml"],
    "application/xml": [".xml"],
    "application/javascript": [".js"],
    "text/javascript": [".js"]
  };
  const result = [];
  let unknownConcreteMime = false;
  String(accept || "").split(/[,;\s]+/).forEach(raw => {
    const token = String(raw || "").trim().toLowerCase();
    if (!token) return;
    if (/^\.[a-z0-9]{1,10}$/.test(token)) {
      if (!result.includes(token)) result.push(token); return;
    }
    if (map[token]) {
      map[token].forEach(ext => { if (!result.includes(ext)) result.push(ext); });
    } else if (token.includes("/") && !token.endsWith("/*")) {
      unknownConcreteMime = true;
    }
  });
  if (!result.length && unknownConcreteMime) result.push(".pboot-no-match");
  return result;
}

/* ══════════ 多 HTML 批量发布队列 ══════════ */
function saveActiveBatchItem(st) {
  const index = st && st.batch ? st.batch.editingIndex : -1;
  const item = index >= 0 ? st.batch.items[index] : null;
  if (!item || item.path !== st.pub.html) return;
  item.mapping = Object.assign({}, st.pub.mapping || {});
  item.overrides = Object.assign({}, st.pub.overrides || {});
  // Keep media, backend fields and explicit flags with the individual queue
  // item.  The web page lets an operator submit each HTML form separately;
  // sharing these settings across the whole queue silently changes that
  // behavior when one item needs a different thumbnail or attachment.
  item.settings = typeof captureBatchSettings === "function"
    ? captureBatchSettings(st) : null;
}

async function pickBatchHtml(fromFolder) {
  const st = activeState();
  if (!st || st.busy) return;
  const result = await api().pick_html_files("batch_html", !!fromFolder);
  if (!isLiveState(st) || !result || !result.ok || result.cancelled) return;
  await appendBatchHtmlPaths(st, result.paths || []);
}

async function appendBatchHtmlPaths(st, paths) {
  if (!isLiveState(st) || !Array.isArray(paths)) return false;
  const existing = new Set((st.batch.items || []).map((item) => item.path.toLowerCase()));
  let added = 0;
  // Keep the complete native picker result.  Unlike the old desktop-only
  // slice(0, 1000), this matches a browser file input/folder workflow; draft
  // serialization and task resource guards report explicit limits instead
  // of silently discarding later selections.
  paths.forEach((path) => {
    const key = String(path || "").toLowerCase();
    if (!key || existing.has(key)) return;
    existing.add(key);
    st.batch.items.push({ id: String(st.batch.items.length + 1), path,
      title: fileName(path), status: "pending", message: "等待发布", attempts: 0,
      mapping: {}, overrides: {}, settings: typeof captureBatchSettings === "function"
        ? captureBatchSettings(st) : null });
    added += 1;
  });
  if (!added) return false;
  scheduleDraftSave(st, "batch");
  if (st.batch.editingIndex < 0) await loadBatchItem(st, 0);
  if (st.id === ACTIVE) renderActiveTab();
  return true;
}

async function loadBatchItem(st, index) {
  if (!st || st.busy || !st.batch.items[index]) return;
  saveActiveBatchItem(st);
  const item = st.batch.items[index];
  if (typeof applyBatchSettings === "function")
    applyBatchSettings(st, item.settings ||
      (typeof captureBatchSettings === "function" ? captureBatchSettings(st) : null));
  st.batch.editingIndex = index;
  st.pub.html = item.path;
  st.pub.htmlReady = false;
  st.pub.htmlLoading = true;
  st.pub.parsedFields = [];
  st.pub.overrides = {};
  st.pub.htmlInfo = `正在解析队列第 ${index + 1} 项…`;
  invalidatePreflight(st);
  if (st.id === ACTIVE) renderActiveTab();
  const request = nextRequest(st, "pubHtml");
  const parsed = await api().parse_html(st.id, item.path, "publish");
  if (!requestIsCurrent(st, "pubHtml", request) || st.batch.editingIndex !== index) return;
  st.pub.htmlLoading = false;
  if (!parsed || !parsed.ok) {
    if (!["review", "success"].includes(item.status)) item.status = "failed";
    item.message = (parsed && parsed.msg) || "HTML 解析失败";
    st.pub.htmlInfo = item.message; st.pub.htmlInfoClass = "bad";
    renderActiveTab();
    return;
  }
  applyPubParse(st, parsed);
  if (Object.keys(item.mapping || {}).length) st.pub.mapping = Object.assign({}, item.mapping);
  if (Object.keys(item.overrides || {}).length) st.pub.overrides = Object.assign({}, item.overrides);
  item.mapping = Object.assign({}, st.pub.mapping);
  item.overrides = Object.assign({}, st.pub.overrides);
  scheduleDraftSave(st, "batch");
  if (st.id === ACTIVE) renderActiveTab();
}

function batchStatusLabel(status) {
  return { pending: "等待", running: "发布中", success: "成功", failed: "失败",
    paused: "已暂停", review: "待核对" }[status] || "等待";
}

async function resolveBatchReview(st, item, saved) {
  if (st.busy || item.status !== "review") return;
  const yes = await confirmDialog("", {
    title: saved ? "确认后台已保存这篇？" : "确认后台没有保存这篇？", kind: "warn",
    okText: saved ? "已核对，标为完成" : "已核对，允许重试", cancelText: "保持待核对",
    lines: [fileName(item.path), "请先检查后台内容、图片和本次字段。此按钮只更新本地队列，不修改后台。",
      saved ? "标为完成后，队列不会再次发布此篇。" : "只有确认未保存才允许重试，否则可能重复发布。"],
  });
  if (!yes || !isLiveState(st) || st.busy || item.status !== "review") return;
  item.status = saved ? "success" : "pending";
  item.message = saved ? "用户核对后台后标记完成" : "用户确认未保存，允许重新提交";
  scheduleDraftSave(st, "batch");
  if (st.id === ACTIVE) renderActiveTab();
}

function renderBatchQueue(st) {
  const card = $("batchCard"), tbody = $("batchTable").querySelector("tbody");
  const items = st.batch.items || [];
  card.hidden = !items.length;
  tbody.innerHTML = "";
  $("batchApplyVerified").checked = st.batch.applyVerified === true;
  $("batchSkipUnverified").checked = st.batch.skipUnverified === true;
  items.forEach((item, index) => {
    const tr = document.createElement("tr");
    tr.classList.toggle("active", index === st.batch.activeIndex || index === st.batch.editingIndex);
    tr.innerHTML = `<td>${index + 1}</td>` +
      `<td><span class="batch-file" title="${esc(item.path)}">${esc(fileName(item.path))}</span></td>` +
      `<td><span class="batch-status ${esc(item.status || "pending")}">${batchStatusLabel(item.status)}</span></td>` +
      `<td><span class="hint" title="${esc(item.message || "")}">${esc(item.message || "")}</span></td>` +
      `<td><div class="product-link-actions"><button class="ghost mini edit-item">编辑</button>` +
      (item.status === "review" ? `<button class="ghost mini review-saved">已保存</button><button class="ghost mini review-unsaved">未保存</button>` : "") +
      `<button class="text-btn mini remove-item">移除</button></div></td>`;
    tr.querySelector(".edit-item").disabled = !!st.busy;
    tr.querySelector(".remove-item").disabled = !!st.busy;
    tr.querySelector(".edit-item").addEventListener("click", () => loadBatchItem(st, index));
    if (item.status === "review") {
      for (const [selector, saved] of [[".review-saved", true], [".review-unsaved", false]]) {
        tr.querySelector(selector).disabled = !!st.busy;
        tr.querySelector(selector).addEventListener("click", () => resolveBatchReview(st, item, saved));
      }
    }
    tr.querySelector(".remove-item").addEventListener("click", () => {
      if (st.busy) return;
      st.batch.items.splice(index, 1);
      if (st.batch.editingIndex === index) st.batch.editingIndex = -1;
      else if (st.batch.editingIndex > index) st.batch.editingIndex -= 1;
      scheduleDraftSave(st, "batch");
      renderActiveTab();
    });
    tbody.appendChild(tr);
  });
  const counts = { success: 0, failed: 0, pending: 0, running: 0, review: 0 };
  items.forEach((item) => { counts[item.status] = (counts[item.status] || 0) + 1; });
  $("batchSummary").textContent = `共 ${items.length} 篇 · 成功 ${counts.success} · 失败 ${counts.failed} · 待核对 ${counts.review} · 待处理 ${counts.pending}` +
    (st.batch.pauseRequested ? " · 将在当前篇完成后暂停" : (st.batch.paused ? " · 已暂停" : ""));
}

function captureBatchSettings(st) {
  const settings = {
    scode: String(st.pub.cat || ""), mapping: Object.assign({}, st.pub.mapping || {}),
    backendFields: Object.assign({}, st.pub.backendValues || {}),
    width: st.pub.width, manualImages: Array.from(st.pub.manualImages || []),
    ico: st.pub.ico, thumbPath: st.pub.thumbPath, thumbUrl: st.pub.thumbUrl || "",
    carouselImages: Array.from(st.pub.carouselImages || []),
    galleryPlan: st.pub.galleryPlan != null ? st.pub.galleryPlan.map(item => ({...item})) : null,
    carouselSize: "original", carouselW: st.pub.carouselW, carouselH: st.pub.carouselH,
    insertStrategy: st.pub.insertStrategy || "top",
    top: !!st.pub.top, rec: !!st.pub.rec, head: !!st.pub.head,
    flagChanges: cmsFlagChanges(st.pub),
    checkLinks: st.pub.checkLinks === true,
    submitter: st.pub.submitter ? Object.assign({}, st.pub.submitter) : null,
  };
  settings.assetMimes = typeof snapshotFileMimeHints === "function"
    ? snapshotFileMimeHints(settings) : {};
  return settings;
}

function applyBatchSettings(st, settings) {
  if (!st || !settings) return;
  st.pub.cat = String(settings.scode || "");
  st.pub.width = settings.width || "preserve";
  if (["top", "before_h2", "after_first_paragraph"].includes(String(settings.insertStrategy || "")))
    st.pub.insertStrategy = String(settings.insertStrategy);
  st.pub.backendValues = Object.assign({}, settings.backendFields || {});
  st.pub.manualImages = Array.from(settings.manualImages || []);
  st.pub.ico = settings.ico || "none";
  st.pub.thumbPath = String(settings.thumbPath || "");
  st.pub.thumbUrl = String(settings.thumbUrl || "");
  st.pub.carouselImages = Array.from(settings.carouselImages || []);
  st.pub.galleryPlan = settings.galleryPlan == null ? null : settings.galleryPlan.map(item => ({...item}));
  if (settings.carouselSize && settings.carouselSize !== "original")
    log("批量草稿中的客户端裁切设置已取消；将上传原文件并交由后台处理。", st.id);
  st.pub.carouselSize = "original";
  st.pub.carouselW = Number(settings.carouselW || 800);
  st.pub.carouselH = Number(settings.carouselH || 800);
  st.pub.top = !!settings.top; st.pub.rec = !!settings.rec; st.pub.head = !!settings.head;
  st.pub.flagChanges = Object.assign({}, settings.flagChanges || {});
  st.pub.checkLinks = settings.checkLinks === true;
  // A deliberately empty submitter is meaningful: this row has no selected
  // button and must not inherit the previous row's clicked action.
  st.pub.submitter = settings.submitter ? Object.assign({}, settings.submitter) : null;
  if (typeof restoreFileMimeHints === "function")
    Object.entries(settings.assetMimes || {}).forEach(([path, mime]) => rememberFileMime(path, mime));
}

function batchSharedSettings(st) {
  saveActiveBatchItem(st);
  const shared = typeof captureBatchSettings === "function" ? captureBatchSettings(st) : {
    scode: String(st.pub.cat || ""), mapping: Object.assign({}, st.pub.mapping || {}),
    backendFields: Object.assign({}, st.pub.backendValues || {}), width: st.pub.width,
    manualImages: Array.from(st.pub.manualImages || []), ico: st.pub.ico,
    thumbPath: st.pub.thumbPath, thumbUrl: st.pub.thumbUrl || "",
    carouselImages: Array.from(st.pub.carouselImages || []), galleryPlan: st.pub.galleryPlan,
    carouselSize: "original", carouselW: st.pub.carouselW, carouselH: st.pub.carouselH,
    top: !!st.pub.top, rec: !!st.pub.rec, head: !!st.pub.head,
    flagChanges: Object.assign({}, st.pub.flagChanges || {}), checkLinks: st.pub.checkLinks === true,
    submitter: st.pub.submitter ? Object.assign({}, st.pub.submitter) : null,
  };
  // The active item is the source of truth for its own settings.  The object
  // remains the queue-wide fallback for legacy items without a saved bundle.
  shared.linkPolicy = { applyVerified: st.batch.applyVerified === true,
    removeUnmatched: st.batch.skipUnverified === true };
  return shared;
}

function waitForBatchPublish(st) {
  return new Promise((resolve) => { st.batch._resolve = resolve; });
}

function batchShouldStop(st, op) {
  return !operationIsCurrent(st, op) || !!st.batch.stopRequested;
}

function batchStoppedResult() {
  return { ok: false, cancelled: true, msg: "已取消，等待重试" };
}

async function processBatchItem(st, item, index, settings, op) {
  const itemSettings = Object.assign({}, settings, item.settings || {});
  itemSettings.backendFields = Object.assign({}, itemSettings.backendFields || {});
  itemSettings.manualImages = Array.from(itemSettings.manualImages || []);
  itemSettings.carouselImages = Array.from(itemSettings.carouselImages || []);
  itemSettings.galleryPlan = itemSettings.galleryPlan == null ? null
    : itemSettings.galleryPlan.map(entry => ({...entry}));
  item.submissionStarted = false;
  item.status = "running"; item.message = "1/4 读取栏目表单"; item.attempts = (item.attempts || 0) + 1;
  st.batch.activeIndex = index;
  if (st.id === ACTIVE) renderActiveTab();
  const category = await api().select_category(st.id, itemSettings.scode);
  if (batchShouldStop(st, op)) return batchStoppedResult();
  if (!category || !category.ok) return { ok: false, msg: (category && category.msg) || "栏目表单加载失败" };
  st.pub.nativeUrl = String(category.page_url || "");
  item.message = "2/4 解析并校验 HTML"; if (st.id === ACTIVE) renderBatchQueue(st);
  const parsed = await api().parse_html(st.id, item.path, "publish");
  if (batchShouldStop(st, op)) return batchStoppedResult();
  if (!parsed || !parsed.ok) return { ok: false, msg: (parsed && parsed.msg) || "HTML 解析失败" };
  const available = new Set((parsed.fields || []).map((field) => field.key));
  const sourceMapping = Object.keys(item.mapping || {}).length
    ? item.mapping : (itemSettings.mapping || settings.mapping);
  const mapping = {};
  Object.keys(parsed.suggest || {}).forEach((key) => { mapping[key] = parsed.suggest[key]; });
  Object.entries(sourceMapping || {}).forEach(([key, value]) => { if (available.has(key)) mapping[key] = value; });
  const overrides = Object.assign({}, item.overrides || {});
  const check = await api().preflight(st.id, mapping, overrides, itemSettings.backendFields);
  if (batchShouldStop(st, op)) return batchStoppedResult();
  if (!check || !check.ok || (check.errors || []).length)
    return { ok: false, msg: !check || !check.ok ? ((check && check.msg) || "发布前检查失败") : check.errors.join("；") };

  item.message = itemSettings.checkLinks === true ? "3/4 检查内链" : "3/4 保留原稿链接";
  if (st.id === ACTIVE) renderBatchQueue(st);
  let links = {ok:true,links:[],dead:[],internal_count:0,skipped:true};
  if (itemSettings.checkLinks === true) {
    links = await startLinkCheck(st, { mapping, overrides, workflow: "publish" });
    if (batchShouldStop(st, op)) return batchStoppedResult();
    if (!links || !links.ok) return { ok: false, cancelled: !!(links && links.cancelled), msg: (links && links.msg) || "内链检查失败" };
  }
  const linkActions = [];
  const manualIssues = [];
  const policy = itemSettings.linkPolicy || settings.linkPolicy || {};
  for (const issue of (links.dead || [])) {
    if (policy.applyVerified === true && issue.suggest) {
      linkActions.push(linkActionFor(issue, issue.suggest));
    } else if (!issue.suggest && policy.removeUnmatched === true) {
      linkActions.push(linkActionFor(issue, ""));
    } else {
      manualIssues.push(issue);
    }
  }
  const uncertain = (links.links || []).filter((link) => ["unknown", "unchecked"].includes(link.state));
  let uncertaintyAccepted = false;
  if (manualIssues.length || uncertain.length) {
    item.message = `3/4 等待确认链接：${fileName(item.path)}`;
    if (st.id === ACTIVE) renderBatchQueue(st);
    const reviewed = await reviewLinkProblems(st, {...links, dead: manualIssues},
      `发布 ${fileName(item.path)}`);
    if (batchShouldStop(st, op)) return batchStoppedResult();
    if (!reviewed.ok) return { ok: false, cancelled: true, msg: reviewed.message };
    linkActions.push(...reviewed.linkActions);
    uncertaintyAccepted = reviewed.uncertaintyAccepted;
  }

  // Batch items are submitted one by one through the same backend endpoint.
  // Resolve the first image against this item's parsed body (rather than the
  // currently visible queue item) so thumbnail=first follows the same native
  // picture/srcset selection as single-article publishing.
  let browserFirstImage = null;
  if (itemSettings.ico === "first" && typeof resolveBrowserFirstImage === "function") {
    const bodyField = (parsed.fields || []).find((field) =>
      String(mapping[field.key] || "") === "content");
    const body = bodyField
      ? (Object.prototype.hasOwnProperty.call(overrides, bodyField.key)
        ? String(overrides[bodyField.key] ?? "") : String(bodyField.value ?? ""))
      : "";
    browserFirstImage = await resolveBrowserFirstImage(body, st.url);
    if (batchShouldStop(st, op)) return batchStoppedResult();
  }

  // 这是进入真实写入前的最后一道取消闸门。此前任一只读检查期间点取消，
  // 都必须停在这里，不能因为检查响应稍晚到达而继续发布。
  if (batchShouldStop(st, op)) return batchStoppedResult();
  item.message = "4/4 正在发布"; if (st.id === ACTIVE) renderBatchQueue(st);
  const donePromise = waitForBatchPublish(st);
  let started;
  try {
    item.submissionStarted = true;
    scheduleDraftSave(st, "batch");
    started = await api().publish(st.id, {
      scode: itemSettings.scode, mapping, overrides, html_path: item.path,
      backend_fields: itemSettings.backendFields,
      asset_mimes: itemSettings.assetMimes || {},
      preflight_token: check.snapshot_token || "",
      image_paths: itemSettings.manualImages, width_mode: itemSettings.width,
      ...thumbnailSubmission(itemSettings),
      carousel_paths: itemSettings.carouselImages,
      ...gallerySubmission(itemSettings),
      carousel_size: snapshotCarouselDimensions(itemSettings),
      responsive_context: (typeof responsiveContext === "function" ? responsiveContext() : {}),
      browser_first_image: browserFirstImage,
      submitter: itemSettings.submitter || null,
      // Keep the legacy spread spelling documented for queue snapshots:
      // ...settings.flagChanges
      ...itemSettings.flagChanges,
      strategy: itemSettings.insertStrategy || "top", link_actions: linkActions,
    });
  } catch (e) {
    st.batch._resolve = null;
    throw e;
  }
  if (!started || !started.ok) {
    st.batch._resolve = null;
    item.submissionStarted = !started;
    if (!started) return { ok: false, outcome: "unknown", requires_review: true, msg: "未收到任务启动确认，请先核对后台" };
    return { ok: false, msg: (started && started.msg) || "发布任务启动失败" };
  }
  const done = await donePromise;
  if (done && done.ok && (linkActions.length || uncertaintyAccepted)) {
    const removed = linkActions.filter((action) => !action.new_href).length;
    const replaced = linkActions.length - removed;
    return Object.assign({}, done, {
      msg: `${done.msg || "发布成功"}；按确认方案替换 ${replaced} 条、去链 ${removed} 条（保留内部内容）` +
        (uncertaintyAccepted ? "；未确认链接按原样保留，风险已确认" : ""),
      removed_links: removed, replaced_links: replaced,
      uncertainty_accepted: uncertaintyAccepted,
    });
  }
  return done || { ok: false, outcome: "unknown", requires_review: true, msg: "未收到发布完成状态，请先核对后台" };
}

async function startBatchQueue(retryFailedOnly) {
  const st = activeState();
  if (!st || st.busy || !(st.batch.items || []).length) return;
  const settings = batchSharedSettings(st);
  const invalid = validatePublishSnapshot(Object.assign({ html: "batch", htmlReady: true }, settings), st.pub.fields);
  if (invalid && invalid !== "请重新选择并成功解析 HTML 文件")
    return setAreaMsg(st, "pub", "pubMsg", `批量队列：${invalid}`, "bad");
  const target = st.batch.items.map((item, index) => ({ item, index })).filter(({ item }) =>
    batchCanRunItem(item, retryFailedOnly));
  if (!target.length) return;
  for (const {item} of target) {
    const itemSettings = Object.assign({}, settings, item.settings || {});
    const itemInvalid = validatePublishSnapshot(Object.assign(
      { html: "batch", htmlReady: true }, itemSettings), st.pub.fields);
    if (itemInvalid && itemInvalid !== "请重新选择并成功解析 HTML 文件")
      return setAreaMsg(st, "pub", "pubMsg",
        `批量队列 ${fileName(item.path)}：${itemInvalid}`, "bad");
  }
  const confirmed = await confirmDialog("", {
    title: retryFailedOnly ? "仅重试失败项？" : `开始发布 ${target.length} 篇？`,
    kind: "confirm", okText: "开始队列", cancelText: "取消",
    lines: [`站点：${st.title}`, `栏目：${settings.scode}`, `文件：${target.length} 个`,
      settings.checkLinks === true && settings.linkPolicy?.applyVerified === true
        ? "已主动启用：有已验证候选的链接将自动替换（验证可访问不保证内容相关）。" : "未启用自动换链。",
      settings.checkLinks === true && settings.linkPolicy?.removeUnmatched === true
        ? "已主动启用：无已验证建议的待处理链接将去掉链接外壳，保留内部文字、图片和格式。" : "未启用自动去链。",
      settings.checkLinks === true
        ? "其他待处理链接逐篇确认，默认保留原链接；无法确认的链接可确认风险后原样继续，或取消该篇。"
        : "内链检查未启用：不额外联网，不自动换链或去链，正文链接保持原样。"
    ].concat(thumbnailSummary(settings), gallerySummary(settings)),
  });
  if (!confirmed || !isLiveState(st) || st.busy) return;
  target.forEach(({ item }) => { item.status = "pending"; item.message = "等待发布"; });
  const op = beginOperation(st, "batch");
  if (!op) return;
  st.batch.running = true; st.batch.paused = false; st.batch.pauseRequested = false;
  st.batch.stopRequested = false;
  st.taskStarted = true;
  let terminal = "complete";
  let currentItem = null;
  try {
    for (const { item, index } of target) {
      if (!operationIsCurrent(st, op) || st.batch.stopRequested) {
        terminal = "stopped";
        break;
      }
      if (st.batch.pauseRequested) {
        terminal = "paused";
        break;
      }
      currentItem = item;
      let result;
      try {
        result = await processBatchItem(st, item, index, settings, op);
      } catch (e) {
        result = { ok: false, outcome: item.submissionStarted ? "unknown" : "not_sent",
          requires_review: !!item.submissionStarted, msg: `执行异常：${e}` };
        log(`批量发布 ${fileName(item.path)} 异常：${e}`, st.id);
      }
      if (!isLiveState(st)) return;
      if (!operationIsCurrent(st, op)) {
        if (item.status === "running") {
          item.status = result.ok || isWriteReview(result) ? batchResultStatus(result)
            : (item.submissionStarted ? "review" : "pending");
          item.message = result.msg || (item.status === "review" ? "提交后任务中断，请先核对" : "任务已停止，等待重试");
        }
        terminal = "stopped";
        break;
      }
      if (st.batch.stopRequested && result.cancelled) {
        item.status = "pending";
        item.message = result.msg || "已取消，等待重试";
        terminal = "stopped";
      } else {
        item.status = batchResultStatus(result);
        item.message = result.ok ? (result.msg || "发布成功") : (result.msg || "发布失败");
      }
      currentItem = null;
      scheduleDraftSave(st, "batch");
      if (st.id === ACTIVE) renderActiveTab();
      if (item.status === "review") {
        terminal = "review";
        break;
      }
      if (st.batch.stopRequested) {
        terminal = "stopped";
        break;
      }
      if (st.batch.pauseRequested) {
        terminal = "paused";
        break;
      }
    }
  } catch (e) {
    terminal = "error";
    if (currentItem && currentItem.status === "running") {
      currentItem.status = st.batch.stopRequested ? "pending" : "failed";
      currentItem.message = st.batch.stopRequested ? "已取消，等待重试" : `执行异常：${e}`;
    }
    log(`批量队列异常终止：${e}`, st.id);
  } finally {
    if (!isLiveState(st)) return;
    if (currentItem && currentItem.status === "running") {
      currentItem.status = (terminal === "stopped" || terminal === "paused") ? "pending" : "failed";
      currentItem.message = terminal === "stopped" ? "已取消，等待重试"
        : (terminal === "paused" ? "已暂停，等待继续" : "队列异常，等待重试");
    }
    const stopped = terminal === "stopped" || !!st.batch.stopRequested;
    const paused = !stopped && (terminal === "paused" || !!st.batch.pauseRequested);
    st.batch._resolve = null;
    st.batch.running = false;
    st.batch.paused = paused;
    st.batch.pauseRequested = false;
    st.batch.stopRequested = false;
    st.batch.activeIndex = -1;
    const failures = st.batch.items.filter((item) => item.status === "failed").length;
    const successes = st.batch.items.filter((item) => item.status === "success").length;
    const pending = st.batch.items.filter((item) => item.status === "pending").length;
    const reviews = st.batch.items.filter((item) => item.status === "review").length;
    const prefix = stopped ? "批量队列已停止" : (paused ? "批量队列已暂停"
      : (terminal === "error" ? "批量队列异常终止" : (terminal === "review" ? "批量队列等待核对" : "批量队列完成")));
    setAreaMsg(st, "pub", "pubMsg",
      `${prefix}：成功 ${successes}，失败 ${failures}，待核对 ${reviews}，待处理 ${pending}`,
      reviews ? "review" : (stopped || paused ? "" : (failures || terminal === "error" ? "bad" : "ok")));
    finishOperation(st, op);
    if (!failures && !pending && !reviews) deleteDraftNow(st, "batch");
    else scheduleDraftSave(st, "batch");
  }
}

function pauseBatchQueue() {
  const st = activeState(); if (!st || !st.batch.running) return;
  st.batch.pauseRequested = true;
  setAreaMsg(st, "pub", "pubMsg", "已请求暂停：当前篇完成后暂停队列", "");
  renderActiveTab();
}

function resumeBatchQueue() {
  const st = activeState(); if (!st || !st.batch.paused) return;
  startBatchQueue(false);
}

async function clearBatchQueue() {
  const st = activeState(); if (!st || st.batch.running || !st.batch.items.length) return;
  const yes = await confirmDialog("队列状态和每篇手动预览修改会一并清除。", {
    title: "清空批量队列？", kind: "warn", okText: "清空", cancelText: "保留" });
  if (!yes) return;
  st.batch.items = []; st.batch.editingIndex = -1; st.batch.activeIndex = -1;
  await deleteDraftNow(st, "batch");
  renderActiveTab();
}

/* ══════════ 编辑修改 ══════════ */
async function editLoadArticles() {
  const st = activeState();
  if (!st || st.busy) return;
  const scode = $("editCat").value;
  st.edit.cat = scode;
  scheduleDraftSave(st, "edit");
  if (!scode) return setAreaMsg(st, "edit", "editMsg", "请选择栏目", "bad");
  const req = nextRequest(st, "editArticles");
  // 重新载入会取消当前文章选择；同步清掉该文章的 HTML/图片，避免随后
  // 选择另一篇时因 previousId 已为空而把旧素材带过去。
  if (st.edit.artId) clearEditArticleDraft(st, true);
  st.edit.articlesLoading = true;
  st.edit.articles = [];
  st.edit.artId = "";
  st.edit.fields = [];
  st.edit.submitter = null;
  st.edit.submitterOptions = [];
  st.edit.mapping = {};
  st.edit.backendValues = {};
  st.edit.formLoading = false;
  st.edit.formReady = false;
  st.edit.linkReport = null;
  st.contentAdmin = { scode: scode, mcode: "", records: [], targets: [], revision: "",
    selected: {}, keyword: st.contentAdmin.keyword || "", loading: false, msg: "", msgClass: "" };
  st.edit.top = false; st.edit.rec = false; st.edit.head = false;
  setAreaMsg(st, "edit", "editMsg", "载入文章…", "");
  if (st.id === ACTIVE) renderActiveTab();
  const r = await api().load_articles(st.id, scode);
  if (!requestIsCurrent(st, "editArticles", req) || st.edit.cat !== scode) return;
  st.edit.articlesLoading = false;
  if (!r || !r.ok) {
    setAreaMsg(st, "edit", "editMsg", (r && r.msg) || "文章载入失败", "bad");
    if (st.id === ACTIVE) renderActiveTab();
    return;
  }
  st.edit.articles = r.articles || [];
  setAreaMsg(st, "edit", "editMsg", `共 ${st.edit.articles.length} 篇文章`, "ok");
  if (st.id === ACTIVE) renderActiveTab();
}
function renderArticleOptions(keyword) {
  const st = activeState();
  if (!st) return;
  const sel = $("editArt");
  sel.innerHTML = '<option value="">— 请选择文章 —</option>';
  const kw = (keyword || "").trim().toLowerCase();
  const articles = st.edit.articles || [];
  const visible = articles.filter((a) => !kw ||
    String(a.title || "").toLowerCase().includes(kw) || String(a.id || "").includes(kw));
  // 搜索只负责缩小候选范围，不应让 DOM 显示“未选择”而状态仍指向旧文章。
  // 当前文章即使不匹配关键词也保留在下拉中，确保可见目标与提交目标一致。
  const selected = articles.find((a) => String(a.id) === String(st.edit.artId));
  if (selected && !visible.includes(selected)) visible.unshift(selected);
  visible
    .forEach((a) => {
      const opt = document.createElement("option");
      opt.value = a.id;
      opt.textContent = `ID:${a.id} | ${a.title.slice(0, 60)}`;
      sel.appendChild(opt);
    });
  sel.value = st.edit.artId;
}

function contentAdminSelectedIds(st) {
  return Object.keys(st.contentAdmin.selected || {}).filter((id) =>
    st.contentAdmin.selected[id]);
}

function renderContentAdmin(st) {
  renderBulkLinks(st);
  if (!st.contentAdmin) st.contentAdmin = { scode: "", mcode: "", records: [], targets: [],
    revision: "", selected: {}, keyword: "", loading: false, msg: "", msgClass: "" };
  const state = st.contentAdmin;
  const filter = String(state.keyword || "").trim().toLowerCase();
  const rows = (state.records || []).filter((row) => !filter ||
    String(row.title || "").toLowerCase().includes(filter) ||
    String(row.id || "").includes(filter));
  const table = $("contentAdminTable");
  if (!table) return;
  const body = table.querySelector("tbody");
  body.innerHTML = "";
  const labels = { status: "状态", istop: "置顶", isrecommend: "推荐", isheadline: "头条" };
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const checked = !!state.selected[String(row.id)];
    const toggles = row.toggles || {};
    const toggleCell = (field) => {
      const item = toggles[field];
      if (!item) return "<span class=\"hint\">—</span>";
      const target = Number(item.target) === 1 ? "开" : "关";
      return `<button type=\"button\" class=\"text-btn content-toggle\" ` +
        `data-id=\"${esc(row.id)}\" data-field=\"${field}\" ` +
        `data-value=\"${item.target}\" data-url=\"${esc(item.url)}\" ` +
        `title=\"点击${Number(item.target) === 1 ? "关闭" : "开启"}${labels[field]}\">${target}</button>`;
    };
    tr.innerHTML = `<td><input type=\"checkbox\" class=\"content-admin-select\" data-id=\"${esc(row.id)}\" ${checked ? "checked" : ""}></td>` +
      `<td>${esc(row.id)}</td><td title=\"${esc(row.title)}\">${esc(row.title)}</td>` +
      `<td><input class=\"content-admin-sort\" data-id=\"${esc(row.id)}\" value=\"${esc(row.sorting || "")}\" aria-label=\"文章 ${esc(row.id)} 排序\"></td>` +
      `<td>${toggleCell("status")}</td><td>${toggleCell("istop")}</td>` +
      `<td>${toggleCell("isrecommend")}</td><td>${toggleCell("isheadline")}</td>` +
      `<td>${row.edit_url ? '<button type="button" class="ghost mini content-native-edit" title="在完整浏览器中打开真实文章编辑页">原生网页</button>' : ""}` +
      `${row.view_url ? '<button type="button" class="ghost mini content-native-view" title="打开后台列表明确提供的真实前台/预览页">前台</button>' : ""}</td>`;
    const nativeEdit = tr.querySelector(".content-native-edit");
    if (nativeEdit) nativeEdit.addEventListener("click", () =>
      openNativeRecordUrl(st, row.edit_url, "文章后台编辑页"));
    const nativeView = tr.querySelector(".content-native-view");
    if (nativeView) nativeView.addEventListener("click", () =>
      openNativeRecordUrl(st, row.view_url, "文章前台预览页"));
    body.appendChild(tr);
  });
  $("contentAdminFilter").value = state.keyword || "";
  const target = $("contentAdminTarget");
  target.innerHTML = '<option value="">目标栏目（复制/移动）</option>';
  (state.targets || []).forEach((item) => {
    if (String(item.value) === String(state.scode)) return;
    const opt = document.createElement("option"); opt.value = item.value; opt.textContent = item.label || item.value;
    target.appendChild(opt);
  });
  const selected = contentAdminSelectedIds(st);
  $("contentAdminSelected").textContent = `已选 ${selected.length} 项`;
  ["btnContentAdminCopy", "btnContentAdminMove", "btnContentAdminDelete", "btnContentAdminSort"]
    .forEach((id) => { $(id).disabled = !!st.busy || !selected.length || !state.records.length; });
  $("contentAdminSelectAll").checked = rows.length > 0 && rows.every((row) => state.selected[String(row.id)]);
  body.querySelectorAll(".content-toggle").forEach((button) =>
    button.addEventListener("click", toggleContentAdmin));
  setMsg("contentAdminMsg", state.msg, state.msgClass);
}

async function loadContentAdmin() {
  const st = activeState();
  if (!st || st.busy) return;
  const scode = String(st.edit.cat || $("editCat").value || "");
  if (!scode) return setAreaMsg(st, "edit", "editMsg", "请先选择栏目", "bad");
  st.contentAdmin.loading = true; st.contentAdmin.scode = scode;
  st.contentAdmin.selected = {}; st.contentAdmin.msg = "正在读取后台内容列表…"; st.contentAdmin.msgClass = "";
  renderActiveTab();
  const r = await api().prepare_content_admin(st.id, scode, null, 1, st.contentAdmin.keyword || "");
  st.contentAdmin.loading = false;
  if (!r || !r.ok) {
    st.contentAdmin.records = []; st.contentAdmin.msg = (r && r.msg) || "内容列表读取失败"; st.contentAdmin.msgClass = "bad";
  } else {
    st.contentAdmin.mcode = r.mcode || ""; st.contentAdmin.records = r.records || [];
    st.contentAdmin.targets = r.targets || []; st.contentAdmin.revision = r.revision || "";
    st.contentAdmin.msg = `已读取 ${st.contentAdmin.records.length} 篇内容`; st.contentAdmin.msgClass = "ok";
  }
  renderActiveTab();
}

async function contentAdminAction(operation) {
  const st = activeState(); if (!st || st.busy) return;
  const selected = contentAdminSelectedIds(st);
  if (!selected.length) return;
  if (["copy", "move"].includes(operation) && !$("contentAdminTarget").value)
    return setMsg("contentAdminMsg", "请选择目标栏目", "bad");
  const title = operation === "delete" ? "删除内容" : (operation === "move" ? "移动内容" : operation === "copy" ? "复制内容" : "保存排序");
  if (operation === "delete" && !await confirmDialog(`确定删除选中的 ${selected.length} 篇内容吗？后台已删除的内容通常无法恢复。`, { title, kind: "warn", okText: "确认删除", cancelText: "取消" })) return;
  if (operation === "move" && !await confirmDialog(`确定移动选中的 ${selected.length} 篇内容吗？`, { title, kind: "warn", okText: "确认移动", cancelText: "取消" })) return;
  const sorting = {};
  document.querySelectorAll(".content-admin-sort").forEach((input) => {
    if (selected.includes(String(input.dataset.id))) sorting[String(input.dataset.id)] = input.value;
  });
  const r = operation === "delete"
    ? await api().delete_content(st.id, st.contentAdmin.scode, selected, st.contentAdmin.revision, st.contentAdmin.mcode)
    : await api().content_bulk_action(st.id, st.contentAdmin.scode, selected, operation,
        st.contentAdmin.revision, $("contentAdminTarget").value, sorting, st.contentAdmin.mcode);
  if (!r || !r.ok) {
    st.contentAdmin.msg = (r && r.msg) || `${title}失败`; st.contentAdmin.msgClass = "bad";
  } else {
    st.contentAdmin.records = r.records || []; st.contentAdmin.revision = r.revision || "";
    st.contentAdmin.selected = {}; st.contentAdmin.msg = r.msg || `${title}成功`; st.contentAdmin.msgClass = "ok";
  }
  renderActiveTab();
}

async function toggleContentAdmin(event) {
  const st = activeState(); const button = event.currentTarget;
  if (!st || st.busy || !button) return;
  button.disabled = true;
  const r = await api().toggle_content_field(st.id, button.dataset.id, button.dataset.field,
    button.dataset.value, button.dataset.url,
    st.contentAdmin.scode || "", st.contentAdmin.mcode || null);
  st.contentAdmin.msg = r && r.ok ? (r.msg || "状态已切换") : ((r && r.msg) || "状态切换失败");
  st.contentAdmin.msgClass = r && r.ok ? "ok" : "bad";
  if (r && r.ok) await loadContentAdmin(); else renderActiveTab();
}
async function editOnArticleChange() {
  const st = activeState();
  if (!st || st.busy) return;
  const id = $("editArt").value;
  const previousId = st.edit.artId;
  if (previousId && previousId !== id) clearEditArticleDraft(st, true);
  st.edit.artId = id;
  scheduleDraftSave(st, "edit");
  const req = nextRequest(st, "editForm");
  st.edit.fields = [];
  st.edit.mapping = {};
  st.edit.backendValues = {};
  st.edit.formReady = false;
  st.edit.nativeUrl = "";
  st.edit.linkReport = null;
  st.contentAdmin = { scode: st.edit.cat || "", mcode: "", records: [], targets: [], revision: "",
    selected: {}, keyword: st.contentAdmin.keyword || "", loading: false, msg: "", msgClass: "" };
  if (!id) {
    st.edit.formLoading = false;
    st.edit.top = false; st.edit.rec = false; st.edit.head = false;
    if (st.id === ACTIVE) renderActiveTab();
    return;
  }
  st.edit.formLoading = true;
  setAreaMsg(st, "edit", "editMsg", "加载编辑表单…", "");
  if (st.id === ACTIVE) renderActiveTab();
  const r = await api().load_edit_form(st.id, id, false,
    (typeof responsiveContext === "function" ? responsiveContext() : {}));
  if (!requestIsCurrent(st, "editForm", req) || st.edit.artId !== id) return;
  if (!r || !r.ok) {
    st.edit.formLoading = false;
    st.edit.formReady = false;
    st.edit.submitter = null;
    st.edit.submitterOptions = [];
    st.edit.nativeUrl = String(r && r.native_url || "");
    st.edit.top = false; st.edit.rec = false; st.edit.head = false;
    setAreaMsg(st, "edit", "editMsg", (r && r.msg) || "编辑表单加载失败",
      r && r.native_url ? "review" : "bad");
    if (st.id === ACTIVE) renderActiveTab();
    if (r && r.native_url && typeof openNativeRecordUrl === "function")
      await openNativeRecordUrl(st, r.native_url, "动态文章编辑页", {
        allowBusy: true,
        handoff: nativeHandoffSnapshot(st, "edit", editSnapshot(st), {
          message: r.native_reason || r.msg || "文章编辑表单由网页脚本接管；当前草稿字段已带入，未自动提交。",
        }),
      });
    return;
  }
  st.edit.fields = r.fields || [];
  st.edit.nativeUrl = String(r.native_url || "");
  st.edit.submitterOptions = Array.isArray(r.submitter_options) ? r.submitter_options : [];
  st.edit.submitter = r.submitter || null;
  st.edit.backendValues = st.edit.backendValues || {};
  st.edit.contentImages = r.content_images || [];
  st.edit.contentHash = r.content_hash || "";
  st.edit.imageReplacements = {};
  st.edit.formReady = true;
  initializeCmsFlags(st.edit, st.edit.fields);
  st.edit.refreshDate = false;
  st.edit.formLoading = false;
  setAreaMsg(st, "edit", "editMsg", `表单字段 ${r.fields.length} 个，可提交修改`, "ok");
  if (st.edit.html) {
    await editParseHtml(st, st.edit.html, req);
  } else {
    if (st.id === ACTIVE) renderActiveTab();
    if (st.edit.checkLinks === true) await refreshEditLinks(st, true, id);
  }
}
async function editPickHtml() {
  const st = activeState();
  if (!st || st.busy) return;
  if (Object.keys(st.edit.imageReplacements || {}).length) {
    const clear = await confirmDialog("选择新 HTML 会清除已选择的原正文图片替换项。", {
      title: "切换到 HTML 修改？", kind: "warn", okText: "清除并选择", cancelText: "取消" });
    if (!clear || !isLiveState(st)) return;
    st.edit.imageReplacements = {};
  }
  const r = await api().pick_html("edit_html");
  if (!isLiveState(st) || !r.ok || r.cancelled) return;
  await loadEditHtmlPath(st, r.path);
}

async function loadEditHtmlPath(st, path) {
  if (!isLiveState(st) || st.busy || !path) return false;
  const req = nextRequest(st, "editHtml");
  // 与发布页一致：首次选 HTML 时保留用户提前选择的媒体；真正换稿才清空。
  if (st.edit.html && st.edit.html !== path) clearEditArticleDraft(st, false);
  st.edit.html = path;
  scheduleDraftSave(st, "edit");
  st.edit.htmlReady = false;
  st.edit.htmlLoading = true;
  st.edit.parsedFields = [];
  st.edit.mapping = {};
  st.edit.overrides = {};
  st.edit.inlineCount = 0; st.edit.remoteCount = 0;
  st.edit.linkReport = null;
  setAreaMsg(st, "edit", "editMsg", "正在解析新 HTML…", "");
  if (st.id === ACTIVE) renderActiveTab();
  const p = await api().parse_html(st.id, path, "edit");
  if (!requestIsCurrent(st, "editHtml", req) || st.edit.html !== path) return false;
  st.edit.htmlLoading = false;
  if (!p || !p.ok) {
    st.edit.htmlReady = false;
    st.edit.mapping = {};
    setAreaMsg(st, "edit", "editMsg", `HTML 解析失败：${(p && p.msg) || "未知错误"}`, "bad");
    if (st.id === ACTIVE) renderActiveTab();
    return false;
  }
  applyEditParse(st, p, true);
  return true;
}
async function editParseHtml(st, path, formReq) {
  if (!isLiveState(st) || !path) return false;
  const htmlReq = Number(st._requests.editHtml || 0);
  st.edit.htmlLoading = true;
  if (st.id === ACTIVE) renderActiveTab();
  const p = await api().parse_html(st.id, path, "edit");
  if (!isLiveState(st) || st.edit.html !== path ||
      Number(st._requests.editHtml || 0) !== htmlReq ||
      (formReq != null && st._requests.editForm !== formReq)) return false;
  st.edit.htmlLoading = false;
  if (!p || !p.ok) {
    st.edit.htmlReady = false;
    st.edit.mapping = {};
    setAreaMsg(st, "edit", "editMsg", `HTML 重新解析失败：${(p && p.msg) || "未知错误"}`, "bad");
    if (st.id === ACTIVE) renderActiveTab();
    return false;
  }
  applyEditParse(st, p, true);
  return true;
}
function applyEditParse(st, p, refreshLinks) {
  st.edit.parsedFields = p.fields;
  st.edit.mapping = p.suggest || {};
  st.edit.inlineCount = (p.inline_images || []).length;
  st.edit.remoteCount = (p.remote_images || []).length;
  st.edit.mediaCount = (p.media_assets || []).length;
  st.edit.htmlReady = true;
  st.edit.htmlLoading = false;
  if (!st.edit.fields.length) {
    setAreaMsg(st, "edit", "editMsg",
      "⚠ 已解析 HTML，但还未载入 CMS 字段：请先选栏目 → 载入文章 → 选一篇文章", "bad");
  }
  log(`编辑：解析 HTML ${p.fields.length} 字段，内置图 ${st.edit.inlineCount} 张，远程图 ${st.edit.remoteCount} 个，媒体 ${st.edit.mediaCount} 个`, st.id);
  scheduleDraftSave(st, "edit");
  if (st.id === ACTIVE) renderActiveTab();
  if (refreshLinks !== false && st.edit.checkLinks === true && st.edit.artId)
    refreshEditLinks(st, false, st.edit.artId);
}

function renderEditLinkReport(st) {
  const panel = $("editLinkPanel");
  const report = st && st.edit ? st.edit.linkReport : null;
  if (!report) { panel.hidden = true; return; }
  panel.hidden = false;
  const list = $("editLinkList");
  list.innerHTML = "";
  if (report.scanning) {
    $("editLinkSummary").textContent = "正在检测…";
    return;
  }
  if (report.error) {
    $("editLinkSummary").textContent = "检测失败";
    list.innerHTML = `<div class="edit-link-item hint">内链检测失败：${esc(report.error)}</div>`;
    return;
  }
  const links = report.links || [];
  const okCount = links.filter((x) => x.state === "ok").length;
  const issueCount = links.filter((x) =>
    ["dead", "pending", "unknown", "unchecked"].includes(x.state)).length;
  $("editLinkSummary").textContent =
    `站内 ${links.length} 条：正确 ${okCount}，待处理 ${issueCount}；外链 ${report.external_count || 0} 条`;
  if (!links.length) {
    list.innerHTML = '<div class="edit-link-item hint">正文中没有站内链接</div>';
    return;
  }
  const labels = { ok: "✓ 正确", dead: "✕ 死链", pending: "待填充",
                   unknown: "暂无法确认", unchecked: "未检测" };
  const visibleLinks = $("editIssuesOnly").checked
    ? links.filter((item) => item.state !== "ok") : links;
  visibleLinks.forEach((item) => {
    const row = document.createElement("div");
    row.className = "edit-link-item";
    const state = item.state || "unknown";
    const url = item.abs || item.suggest || item.href || "";
    const model = item.model ? ` · 型号 ${esc(item.model)}` : "";
    row.innerHTML = `<div><span class="anchor">${esc(item.text || "(无锚文字)")}</span>${model}` +
      `<span class="edit-link-state ${esc(state)}">${labels[state] || labels.unknown}</span></div>` +
      `<div class="url-line"><div class="url">${esc(url)}${item.status ? ` [${esc(String(item.status))}]` : ""}</div>` +
      `${/^https?:\/\//i.test(url) ? '<button class="open-link" type="button">打开</button>' : ""}</div>`;
    const open = row.querySelector(".open-link");
    if (open) open.addEventListener("click", () => {
      const st = activeState();
      if (st && typeof openNativeRecordUrl === "function")
        return openNativeRecordUrl(st, url, "正文链接", {allowBusy: true});
      return api().open_external_url(url);
    });
    list.appendChild(row);
  });
  if (!visibleLinks.length && links.length)
    list.innerHTML = '<div class="edit-link-item hint">当前没有需要处理的链接</div>';
}

async function refreshEditLinks(st, useCurrent, expectedArticleId) {
  if (!isLiveState(st) || st.busy || st.edit.formLoading || st.edit.htmlLoading) return;
  const req = nextRequest(st, "editLinks");
  const articleId = expectedArticleId == null ? st.edit.artId : String(expectedArticleId);
  const htmlPath = st.edit.html;
  st.edit.linkReport = { scanning: true, links: [] };
  if (st.id === ACTIVE) renderEditLinkReport(st);
  const op = beginOperation(st, "link_check_edit");
  if (!op) return;
  const r = await startLinkCheck(st, {
    mapping: Object.assign({}, st.edit.mapping || {}),
    overrides: Object.assign({}, st.edit.overrides || {}), useCurrent: !!useCurrent,
    workflow: "edit", expectedArticleId: articleId,
  });
  if (!requestIsCurrent(st, "editLinks", req) || st.edit.artId !== articleId ||
      st.edit.html !== htmlPath) { if (operationIsCurrent(st, op)) finishOperation(st, op); return; }
  st.edit.linkReport = r && r.ok ? r : {
    links: [], external_count: 0, error: (r && r.msg) || "未知错误",
  };
  finishOperation(st, op);
  if (st.id === ACTIVE) renderEditLinkReport(st);
}

function editSnapshot(st) {
  const snapshot = {
    cat: String(st.edit.cat || ""), articleId: String(st.edit.artId || ""),
    formReady: !!st.edit.formReady,
    html: String(st.edit.html || ""), htmlReady: !!st.edit.htmlReady,
    mapping: Object.assign({}, st.edit.mapping || {}), cover: st.edit.cover,
    overrides: Object.assign({}, st.edit.overrides || {}),
    backendFields: Object.assign({}, st.edit.backendValues || {}),
    expectedContentHash: String(st.edit.contentHash || ""),
    imageReplacements: Object.values(st.edit.imageReplacements || {}).map((item) => Object.assign({}, item)),
    carouselImages: Array.from(st.edit.carouselImages || []),
    galleryPlan: st.edit.galleryPlan != null ? st.edit.galleryPlan.map(item => ({...item})) : null,
    carouselSize: "original", carouselW: st.edit.carouselW,
    insertStrategy: st.edit.insertStrategy || "before_h2",
    carouselH: st.edit.carouselH, carouselMode: st.edit.carouselMode,
    ico: st.edit.ico, thumbPath: st.edit.thumbPath, thumbUrl: st.edit.thumbUrl || "", inlineCount: st.edit.inlineCount,
    remoteCount: st.edit.remoteCount,
    top: !!st.edit.top, rec: !!st.edit.rec, head: !!st.edit.head,
    flagChanges: cmsFlagChanges(st.edit), refreshDate: !!st.edit.refreshDate,
    checkLinks: st.edit.checkLinks === true,
    submitter: st.edit.submitter ? Object.assign({}, st.edit.submitter) : null,
    submitterOptions: Array.isArray(st.edit.submitterOptions) ? st.edit.submitterOptions.map(item => ({...item})) : [],
  };
  snapshot.assetMimes = typeof snapshotFileMimeHints === "function"
    ? snapshotFileMimeHints(snapshot) : {};
  return snapshot;
}

function validateEditSnapshot(snap) {
  if (!snap.cat) return "请先选择栏目并载入文章";
  if (!snap.articleId) return "请先选择要修改的文章";
  if (!snap.formReady) return "文章表单尚未成功加载，请重新选择文章";
  if (snap.submitterOptions.length > 1 && !snap.submitter)
    return "当前编辑表单有多个提交按钮，请先选择实际保存按钮";
  if (snap.html && !snap.htmlReady) return "新 HTML 尚未成功解析，请重新选择";
  if (snap.html && snap.imageReplacements.length) return "新 HTML 与原正文图片直接替换不能同时提交";
  if (snap.imageReplacements.length && !snap.expectedContentHash) return "详情图片状态已失效，请重新加载文章";
  const thumbnailError = validateThumbnailSnapshot(snap);
  if (thumbnailError) return thumbnailError;
  return validateCarouselSnapshot(snap);
}

async function restoreEditBackend(st, snap, op) {
  const articles = await api().load_articles(st.id, snap.cat);
  if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
  if (!articles || !articles.ok)
    return { ok: false, msg: (articles && articles.msg) || "编辑栏目文章列表恢复失败" };
  if (!(articles.articles || []).some((item) => String(item.id) === snap.articleId))
    return { ok: false, msg: "所选文章已不在该栏目，请重新载入文章" };
  const formArgs = [st.id, snap.articleId, true];
  if (typeof responsiveContext === "function") formArgs.push(responsiveContext());
  const form = await api().load_edit_form(...formArgs);
  if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
  if (!form || !form.ok)
    return { ok: false, msg: (form && form.msg) || "编辑表单恢复失败" };
  if (snap.html) {
    const parsed = await api().parse_html(st.id, snap.html, "edit");
    if (!operationIsCurrent(st, op)) return { ok: false, stale: true };
    if (!parsed || !parsed.ok)
      return { ok: false, msg: (parsed && parsed.msg) || "编辑 HTML 重新解析失败" };
  }
  return { ok: true };
}

async function submitEdit() {
  const st = activeState();
  if (!st || st.busy || hasPendingUiRequest(st)) return;
  if (!await confirmPreviousWriteReview(st, "edit")) return;
  const snap = editSnapshot(st);
  const invalid = validateEditSnapshot(snap);
  if (invalid) return setAreaMsg(st, "edit", "editMsg", invalid, "bad");
  clearTimeout(st._editLinkTimer);
  const op = beginOperation(st, "edit");
  if (!op) return;
  setAreaMsg(st, "edit", "editMsg", snap.checkLinks
    ? "正在恢复文章表单并复核内链…" : "正在恢复文章表单；正文链接将保持原样…", "");
  const restored = await restoreEditBackend(st, snap, op);
  if (!operationIsCurrent(st, op)) return;
  if (!restored.ok) {
    setAreaMsg(st, "edit", "editMsg", restored.msg || "编辑状态恢复失败", "bad");
    finishOperation(st, op);
    return;
  }
  let linkActions = [];
  let lk = {ok:true,links:[],dead:[],internal_count:0,skipped:true};
  if (snap.checkLinks === true) {
    st.taskKind = "link_check_edit";
    lk = await startLinkCheck(st, {
      mapping: snap.mapping, overrides: snap.overrides, useCurrent: !snap.html,
      workflow: "edit", expectedArticleId: snap.articleId,
    });
    if (!operationIsCurrent(st, op)) return;
    if (!lk || !lk.ok) {
      setAreaMsg(st, "edit", "editMsg",
        `内链检测失败，已停止修改：${(lk && lk.msg) || "未知错误"}`, "bad");
      finishOperation(st, op);
      return;
    }
  }
  st.taskKind = "edit";
  const releasePrompt = await acquirePromptSlot();
  if (!operationIsCurrent(st, op)) { releasePrompt(); return; }
  let okGo = false;
  try {
  const uncertain = (lk.links || []).filter((item) =>
    item.state === "unknown" || item.state === "unchecked");
  if (uncertain.length) {
    const skipUnknown = await confirmDialog("", {
      title: "内链检测结果不确定", kind: "warn",
      okText: "明确跳过并继续", cancelText: "停止修改",
      lines: [`有 ${uncertain.length} 条内链因超时、限流或服务器异常暂时无法确认。`,
        "默认不会把它们当作正确链接。只有你明确确认，才会继续本次修改。"],
    });
    if (!skipUnknown) {
      setAreaMsg(st, "edit", "editMsg", "内链检测不确定，已停止修改", "bad");
      finishOperation(st, op);
      return;
    }
    setAreaMsg(st, "edit", "editMsg", `已明确跳过 ${uncertain.length} 条不确定内链`, "");
  }
  if (lk.ok) {
    st.edit.linkReport = lk;
    if (st.id === ACTIVE) renderEditLinkReport(st);
    if (lk.dead && lk.dead.length) {
      const resolved = await showDeadLinkPanel(lk.dead);
      if (resolved === null) {
        setAreaMsg(st, "edit", "editMsg", "已取消修改", "");
        finishOperation(st, op);
        return;
      }
      linkActions = resolved;
    }
  }
  okGo = await confirmDialog("", {
    title: "确认修改？", kind: "confirm", okText: "提交修改",
    lines: [`站点：${st.title}`, `文章 ID：${snap.articleId}`,
      snap.checkLinks ? "内链：已执行软件额外联网检查" : "内链：未启用额外检查，正文链接保持原样"]
      .concat((() => {
        const category = (st.edit.fields || []).find((field) =>
          String(field.name || "") === "scode" && field.edit_category);
        const selected = Object.prototype.hasOwnProperty.call(snap.backendFields || {}, "scode")
          ? String(snap.backendFields.scode ?? "") : "";
        if (!category || !selected || String(category.value ?? "") === selected) return [];
        const option = (category.options || []).find((item) => String(item.value ?? "") === selected);
        return [`栏目：${option?.label || selected}（将文章从当前栏目移动）`];
      })())
      .concat([snap.refreshDate ? "发布时间：明确刷新为电脑当前时间" : "发布时间：保留原值或使用明确映射的日期"])
      .concat(snap.html ? [`HTML：${fileName(snap.html)}`] : ["正文：保留后台当前内容"])
      .concat(snap.inlineCount ? [`内置图：${snap.inlineCount} 张（自动上传）`] : [])
      .concat(snap.remoteCount ? [`远程图：${snap.remoteCount} 个（按当前编辑器抓取配置处理）`] : [])
      .concat(snap.imageReplacements.length ? [`原正文图片替换：${snap.imageReplacements.length} 张`] : [])
      .concat(thumbnailSummary(snap))
      .concat(gallerySummary(snap))
      .concat(snap.carouselImages.length
        ? [`轮播图：${snap.carouselImages.map(fileName).join("、")}`] : []),
  });
  } finally {
    releasePrompt();
  }
  if (!operationIsCurrent(st, op)) return;
  if (!okGo) {
    setAreaMsg(st, "edit", "editMsg", "已取消修改", "");
    finishOperation(st, op);
    return;
  }
  if (Object.values(snap.mapping).some(Boolean)) {
    try {
      const saved = await api().save_mapping(st.id, snap.mapping, "edit");
      if (saved && !saved.ok) log("编辑映射保存失败（不影响本次修改）: " + saved.msg, st.id);
    } catch (e) { log("编辑映射保存失败（不影响本次修改）: " + e, st.id); }
  }
  if (!operationIsCurrent(st, op)) return;
  let browserFirstImage = null;
  if (snap.ico === "first" && typeof resolveBrowserFirstImage === "function") {
    browserFirstImage = await resolveBrowserFirstImage(
      previewBodyFor(st, "edit"), st.url);
    if (!operationIsCurrent(st, op)) return;
  }
  st.taskStarted = true;
  st._prog = { done: 0, total: 1, text: "启动…", task: "edit" };
  if (st.id === ACTIVE) renderActiveTab();
  const r = await api().submit_edit(st.id, {
    article_id: snap.articleId, mapping: snap.mapping,
    overrides: snap.overrides,
    backend_fields: snap.backendFields,
    asset_mimes: snap.assetMimes || {},
    html_path: snap.html,
    expected_content_hash: snap.expectedContentHash,
    image_replacements: snap.imageReplacements,
    cover_all: false, refresh_publish_date: snap.refreshDate,
    image_paths: [], width_mode: "responsive",
    carousel_paths: snap.carouselImages,
    ...gallerySubmission(snap),
    carousel_size: snapshotCarouselDimensions(snap),
    responsive_context: (typeof responsiveContext === "function" ? responsiveContext() : {}),
    browser_first_image: browserFirstImage,
    carousel_mode: snap.carouselMode,
    ...thumbnailSubmission(snap),
    submitter: snap.submitter,
    ...snap.flagChanges,
    link_actions: linkActions,
    strategy: snap.insertStrategy || "before_h2",
  });
  if (!operationIsCurrent(st, op)) return;
  if (!r || !r.ok) {
    setAreaMsg(st, "edit", "editMsg", (r && r.msg) || "修改任务启动失败", "bad");
    finishOperation(st, op);
  }
}
function completionSummary(d, verb) {
  if (!d.ok || isWriteReview(d)) return String(d.msg || "结果待核对");
  const count = Array.isArray(d.upload_metadata) ? d.upload_metadata.length : 0;
  const warnings = Array.isArray(d.upload_warnings) ? d.upload_warnings.length : 0;
  return `${verb}成功。${count ? `已处理 ${count} 个上传文件。` : ""}${warnings ? `有 ${warnings} 项上传警告，请查看日志详情。` : ""}`;
}

function onEditDone(d) {
  const st = TABS.get(d.tab_id);
  if (st) {
    st.busy = false;
    st.taskKind = "";
    st.taskStarted = false;
    st._prog = null;
    st.edit.uploadMetadata = Array.isArray(d.upload_metadata) ? d.upload_metadata : [];
    st.edit.thumbServerInfo = typeof thumbnailServerSummary === "function"
      ? thumbnailServerSummary(st.edit.uploadMetadata) : "";
    st.edit.thumbServerUrl = typeof thumbnailServerUrl === "function"
      ? thumbnailServerUrl(st.edit.uploadMetadata) : "";
  }
  renderTabStrip();
  const review = isWriteReview(d);
  const summary = completionSummary(d, "修改");
  const line = d.cancelled ? d.msg : (review ? "⚠️ " : (d.ok ? "✅ " : "❌ ")) + summary;
  if (st) {
    st.edit.msg = line;
    st.edit.msgClass = d.cancelled ? "" : (review ? "review" : (d.ok ? "ok" : "bad"));
    if (d.native_url) st.edit.nativeUrl = String(d.native_url);
    if (review || d.ok) st.edit.requiresReview = review;
    if (review) scheduleDraftSave(st, "edit");
  }
  if (d.ok && st) {
    st.edit.html = "";
    st.edit.htmlReady = false;
    st.edit.htmlLoading = false;
    st.edit.parsedFields = [];
    st.edit.mapping = {};
    st.edit.overrides = {};
    st.edit.fields = [];
    st.edit.formReady = false;
    st.edit.inlineCount = 0; st.edit.remoteCount = 0; st.edit.mediaCount = 0;
    st.edit.contentImages = [];
    st.edit.contentHash = "";
    st.edit.imageReplacements = {};
    st.edit.linkReport = null;
    st.edit.thumbPath = "";
    st.edit.thumbUrl = "";
    st.edit.ico = "none";
    st.edit.carouselImages = [];
    st.edit.galleryPlan = null;
    st.edit.top = false; st.edit.rec = false; st.edit.head = false;
    deleteDraftNow(st, "edit");
  }
  log((d.ok ? "修改成功: " : "修改结束: ") + d.msg, d.tab_id);
  if (d.tab_id === ACTIVE) renderActiveTab();
  if (st && d.native_only && d.native_url && d.tab_id === ACTIVE &&
      typeof openNativeRecordUrl === "function")
    setTimeout(() => openNativeRecordUrl(st, d.native_url, "动态文章编辑页", {
      handoff: nativeHandoffSnapshot(st, "edit", editSnapshot(st), {
        message: d.native_reason || d.msg || "网页脚本接管编辑；当前草稿字段已带入，未自动提交。",
      }),
    }), 0);
  if (d.ok && d.tab_id === ACTIVE) alertDialog(summary, { title: "修改成功", kind: "success" });
  if (review && d.tab_id === ACTIVE) alertDialog(d.msg, { title: "修改结果待核对", kind: "warn" });
}

/* ══════════ 查询修改 ══════════ */
function reconcileProductSelection(st) {
  if (!st || !st.query) return;
  const validIds = new Set((st.query.products || []).map((product) => String(product.id)));
  st.query.selected = st.query.selected || {};
  st.query.edits = st.query.edits || {};
  Object.keys(st.query.selected).forEach((id) => {
    if (!validIds.has(String(id))) delete st.query.selected[id];
  });
  Object.keys(st.query.edits).forEach((id) => {
    if (!validIds.has(String(id))) delete st.query.edits[id];
  });
}

async function loadProductCache(targetState) {
  const st = targetState || activeState();
  if (!st || !st.loggedIn) return;
  const req = nextRequest(st, "productCache");
  const expectedUrl = st.url;
  const r = await api().load_products_cache(st.id);
  if (requestIsCurrent(st, "productCache", req) && st.loggedIn &&
      st.url === expectedUrl && r.ok) {
    // The browser list is authoritative server data.  Keep the cache as an
    // explicit fallback, but do not paint it first and then visibly reorder
    // the table when the read-only backend query completes.  This makes the
    // normal login path match the webpage's server-first list while retaining
    // offline/restricted-site recovery.
    const cachedProducts = r.products || [];
    st.query.cacheFallbackProducts = cachedProducts;
    st.query.cacheFallbackHealth = r.health || null;
    st.query.products = [];
    st.query.health = r.health || null;
    st.query.cacheLoaded = true;
    if (st.id === ACTIVE) {
      setAreaMsg(st, "query", "qMsg", "正在读取后台真实产品列表…", "");
      renderProducts(st.query.filter); renderProductHealth(st);
    }
    // The browser product list is server-backed.  Warm the same read-only
    // view once after login, while retaining the local cache as the immediate
    // fallback if the site's custom search/pagination cannot be discovered.
    // This never writes or reconciles the cache and is guarded per session so
    // tab switches/restored sessions do not issue duplicate requests.
    void autoQueryProductsRemote(st);
  }
}

async function autoQueryProductsRemote(st) {
  if (!st || !st.loggedIn || !st.query || st.query.remoteAutoStarted ||
      st.query.remoteLoading || st.busy) return;
  st.query.remoteAutoStarted = true;
  const cached = Array.isArray(st.query.cacheFallbackProducts)
    ? st.query.cacheFallbackProducts
    : (Array.isArray(st.query.products) ? st.query.products : []);
  const cachedHealth = st.query.cacheFallbackHealth || st.query.health || null;
  const previousMessage = st.query.msg;
  const previousClass = st.query.msgClass;
  const fallbackMessage = previousMessage &&
    !/^正在读取后台真实产品列表/.test(previousMessage)
    ? previousMessage
    : "后台实时查询失败，当前继续使用本地产品缓存。";
  try {
    const result = await queryProductsRemote(st, 1);
    if (!result || !result.ok) {
      // Keep the already loaded cache visible.  A read-only network failure
      // must not look like an empty remote catalogue.
      st.query.remoteProducts = null;
      st.query.remotePage = 1;
      st.query.remoteHasNext = false;
      st.query.products = cached;
      st.query.health = cachedHealth;
      reconcileProductSelection(st);
      st.query.msg = fallbackMessage;
      st.query.msgClass = previousClass || "review";
      if (st.id === ACTIVE) renderActiveTab();
    }
  } catch (_) {
    st.query.remoteProducts = null;
    st.query.remotePage = 1;
    st.query.remoteHasNext = false;
    st.query.products = cached;
    st.query.health = cachedHealth;
    reconcileProductSelection(st);
    st.query.msg = fallbackMessage;
    st.query.msgClass = previousClass || "review";
    if (st.id === ACTIVE) renderActiveTab();
  }
}

function formatHealthTime(value) {
  if (!value) return "尚未同步";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "尚未同步";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function productIssueCount(product) {
  return (!String(product.xinghao ?? "").trim() ? 1 : 0) +
    (productMissingPrice(product) ? 1 : 0) +
    (!String(product.front_url || "").trim() ? 1 : 0);
}

function productMissingPrice(product) {
  return !!String(product.jiage_field ?? "").trim() && !String(product.jiage ?? "").trim();
}

function renderProductHealth(st) {
  const health = (st && st.query && st.query.health) || {};
  $("healthTotal").textContent = Number(health.total || 0);
  $("healthModel").textContent = Number(health.missing_model || 0);
  $("healthPrice").textContent = Number(health.missing_supported_price || 0);
  $("healthUrl").textContent = Number(health.missing_front_url || 0);
  $("healthSync").textContent = formatHealthTime(health.last_sync_at || health.cache_updated_at);
  document.querySelectorAll(".health-card[data-health]").forEach((card) =>
    card.classList.toggle("on", card.dataset.health === (st.query.healthFilter || "all")));
}

function productMatchesHealth(product, filter) {
  const missingModel = !String(product.xinghao ?? "").trim();
  const missingPrice = productMissingPrice(product);
  const missingUrl = !String(product.front_url || "").trim();
  if (filter === "healthy") return !(missingModel || missingPrice || missingUrl);
  if (filter === "missing_model") return missingModel;
  if (filter === "missing_price") return missingPrice;
  if (filter === "missing_front_url") return missingUrl;
  return true;
}

async function queryProductsRemote(targetState, page) {
  const st = targetState || activeState();
  if (!st || !st.loggedIn || st.query.remoteLoading || st.busy) return;
  const keyword = String(st.query.filter || "").trim();
  const requestedPage = Math.max(1, Number(page || st.query.remotePage || 1));
  st.query.remoteLoading = true;
  setAreaMsg(st, "query", "qMsg", `正在按后台真实搜索控件查询${keyword ? `“${keyword}”` : "全部产品"}…`, "");
  if (st.id === ACTIVE) renderActiveTab();
  try {
    const result = await api().query_products_remote(
      st.id, keyword, requestedPage, st.query.mcode || "");
    if (!result || !result.ok) {
      if (result && result.native_url) st.query.nativeUrl = String(result.native_url);
      setAreaMsg(st, "query", "qMsg", (result && result.msg) || "后台实时查询失败",
        result && result.native_url ? "review" : "bad");
      if (result && result.native_url && typeof openNativeRecordUrl === "function")
        await openNativeRecordUrl(st, result.native_url, "动态产品后台查询页", {allowBusy: true});
      return result || { ok: false, msg: "后台实时查询失败" };
    }
    if (result.native_url) st.query.nativeUrl = String(result.native_url);
    st.query.remoteProducts = result.products || [];
    st.query.remotePage = Number(result.page || requestedPage) || requestedPage;
    st.query.remoteHasNext = !!result.has_next;
    st.query.remoteKeyword = keyword;
    st.query.page = 1;
    setAreaMsg(st, "query", "qMsg",
      `后台实时查询完成：第 ${st.query.remotePage} 页，${st.query.remoteProducts.length} 条` +
      (result.keyword_field ? `（字段 ${result.keyword_field}）` : ""), "ok");
    return result;
  } catch (error) {
    setAreaMsg(st, "query", "qMsg", `后台实时查询异常：${error}`, "bad");
    return { ok: false, msg: String(error) };
  } finally {
    st.query.remoteLoading = false;
    if (st.id === ACTIVE) renderActiveTab();
  }
}

function sortProducts(rows, mode) {
  const numericId = (value) => Number.parseInt(value, 10) || 0;
  const copy = Array.from(rows);
  copy.sort((a, b) => {
    if (mode === "id_asc") return numericId(a.id) - numericId(b.id);
    if (mode === "model_asc") return String(a.xinghao || "").localeCompare(String(b.xinghao || ""), "zh-CN", { numeric: true });
    if (mode === "issues_first") return productIssueCount(b) - productIssueCount(a) || numericId(b.id) - numericId(a.id);
    return numericId(b.id) - numericId(a.id);
  });
  return copy;
}

function renderProducts(keyword) {
  const st = activeState();
  if (!st) return;
  const tbody = $("qTable").querySelector("tbody");
  tbody.innerHTML = "";
  const kw = (keyword || "").trim().toLowerCase();
  const remoteMode = Array.isArray(st.query.remoteProducts);
  const all = remoteMode ? st.query.remoteProducts : (st.query.products || []);
  const filtered = sortProducts(all.filter((p) => (!kw ||
    (p.title || "").toLowerCase().includes(kw) ||
    (p.xinghao || "").toLowerCase().includes(kw) || String(p.id).includes(kw)) &&
    productMatchesHealth(p, st.query.healthFilter || "all")), st.query.sort || "id_desc");
  const pages = remoteMode
    ? Math.max(1, Number(st.query.remotePage || 1) + (st.query.remoteHasNext ? 1 : 0))
    : Math.max(1, Math.ceil(filtered.length / st.query.pageSize));
  st.query.page = Math.max(1, Math.min(st.query.page || 1, pages));
  const start = (st.query.page - 1) * st.query.pageSize;
  const rows = filtered.slice(start, start + st.query.pageSize);
  st.query.edits = st.query.edits || {};
  rows.forEach((p) => {
    const tr = document.createElement("tr");
    const pid = String(p.id), edit = st.query.edits[pid] || {};
    const healthParts = [];
    if (!String(p.xinghao ?? "").trim()) healthParts.push('<span class="health-badge">缺型号</span>');
    if (productMissingPrice(p)) healthParts.push('<span class="health-badge">缺价格</span>');
    if (!String(p.front_url || "").trim()) healthParts.push('<span class="health-badge">缺链接</span>');
    if (!healthParts.length) healthParts.push('<span class="health-badge ok">完整</span>');
    tr.innerHTML =
      `<td><input class="product-select" type="checkbox" ${st.query.selected[pid] ? "checked" : ""} aria-label="选择产品 ${esc(pid)}"></td>` +
      `<td>${esc(p.id)}</td><td>${esc(p.cat_name)}</td>` +
      `<td><span class="cell-preview" title="${esc(p.title)}">${esc(p.title)}</span></td>` +
      `<td>${esc(p.xinghao)}</td><td>${esc(p.jiage)}</td>` +
      `<td><div class="product-link-actions">${healthParts.join("")}` +
      `${p.front_url ? '<button class="open-link product-open" type="button">打开</button>' : ""}</div></td>` +
      `<td><input data-f="m" value="${esc(edit.model ?? "")}" placeholder="新型号" title="未操作保留原值；输入后清空表示清除型号"></td>` +
      `<td><input data-f="p" value="${esc(edit.price ?? "")}" placeholder="新价格" title="未操作保留原值；输入后清空表示清除价格"></td>` +
      `<td><div class="product-link-actions"><button class="mini cta product-quick-edit">修改</button>` +
      `${p.edit_url ? '<button class="mini ghost product-native-edit" title="在完整浏览器中打开真实产品编辑页">原生网页</button>' : ""}` +
      `<button class="mini ghost product-advanced-edit">高级编辑</button></div></td>`;
    const selector = tr.querySelector(".product-select");
    selector.dataset.id = pid;
    selector.addEventListener("change", () => {
      if (selector.checked) st.query.selected[pid] = true; else delete st.query.selected[pid];
      renderProductSelection(st);
    });
    const open = tr.querySelector(".product-open");
    if (open) open.addEventListener("click", () =>
      openNativeRecordUrl(st, p.front_url, "产品前台页面", {allowBusy: true}));
    const nativeEdit = tr.querySelector(".product-native-edit");
    if (nativeEdit) nativeEdit.addEventListener("click", () =>
      openNativeRecordUrl(st, p.edit_url, "产品后台编辑页"));
    const modelInput = tr.querySelector('input[data-f="m"]');
    const priceInput = tr.querySelector('input[data-f="p"]');
    modelInput.addEventListener("input", () => {
      const value = st.query.edits[pid] || (st.query.edits[pid] = {});
      value.model = modelInput.value; value.modelTouched = true;
      if (typeof persistProductEdits === "function") persistProductEdits(st);
    });
    priceInput.addEventListener("input", () => {
      const value = st.query.edits[pid] || (st.query.edits[pid] = {});
      value.price = priceInput.value; value.priceTouched = true;
      if (typeof persistProductEdits === "function") persistProductEdits(st);
    });
    const modifyButton = tr.querySelector(".product-quick-edit");
    modifyButton.disabled = st.busy || !st.loggedIn || remoteMode;
    modifyButton.addEventListener("click", async () => {
      if (!isLiveState(st) || st.busy) return;
      const pending = st.query.edits[pid] || {};
      if (pending.requiresReview && !await confirmDialog("", {
        title: "上次保存结果待核对", kind: "warn", okText: "已核对，继续", cancelText: "取消",
        lines: ["上次请求可能已经保存。请先在后台核对该产品；继续提交可能再次覆盖字段。"],
      })) return;
      const nm = pending.modelTouched ? modelInput.value : null;
      const np = pending.priceTouched ? priceInput.value : null;
      if (nm === null && np === null) return alertDialog("尚未修改。输入后再清空表示清除该字段；未操作的输入框保持原值。", { title: "无改动", kind: "warn" });
      const okGo = await confirmDialog("", {
        title: "确认修改产品？", kind: "confirm", okText: "提交修改",
        lines: [`ID：${p.id}`, `新型号：${nm === null ? "(不改)" : nm === "" ? "(清空)" : nm}`, `新价格：${np === null ? "(不改)" : np === "" ? "(清空)" : np}`],
      });
      if (!okGo) return;
      const op = beginOperation(st, "modify");
      if (!op) return;
      let rr;
      try { rr = await api().modify_product(st.id, p.id, nm, np); }
      catch (error) { rr = {ok: false, requires_review: true, outcome: "unknown", msg: `调用中断，保存结果待核对：${error}`}; }
      if (!rr) rr = {ok: false, requires_review: true, outcome: "unknown", msg: "未收到结果，请核对后台"};
      if (!operationIsCurrent(st, op)) return;
      if (rr.ok) {
        setAreaMsg(st, "query", "qMsg", `ID=${p.id} 修改成功`, "ok");
        if (nm !== null) p.xinghao = nm;
        if (np !== null) p.jiage = np;
        delete st.query.edits[pid];
        if (typeof persistProductEdits === "function") persistProductEdits(st);
        if (rr.health) st.query.health = rr.health;
        else {
          const health = await api().product_health(st.id);
          if (health && health.ok) st.query.health = health.health;
        }
      } else {
        if (isWriteReview(rr)) pending.requiresReview = true;
        if (typeof persistProductEdits === "function") persistProductEdits(st);
        setAreaMsg(st, "query", "qMsg", `ID=${p.id} ${isWriteReview(rr) ? "结果待核对" : "修改失败"}: ${rr.msg}`, isWriteReview(rr) ? "review" : "bad");
      }
      finishOperation(st, op);
      if (rr.ok && st.id === ACTIVE) renderActiveTab();
    });
    const advancedButton = tr.querySelector(".product-advanced-edit");
    advancedButton.disabled = st.busy || !st.loggedIn || remoteMode;
    advancedButton.addEventListener("click", () => openProductAdvancedEditor(st, p));
    tbody.appendChild(tr);
  });
  $("qCount").textContent = remoteMode
    ? `后台实时结果 ${filtered.length} 条（不改本地缓存）`
    : `筛选 ${filtered.length}/${all.length} 条`;
  $("qPage").textContent = remoteMode
    ? `后台第 ${st.query.remotePage || 1} 页`
    : `第 ${st.query.page}/${pages} 页`;
  $("qPrev").disabled = remoteMode ? (st.query.remotePage <= 1 || st.query.remoteLoading) : st.query.page <= 1;
  $("qNext").disabled = remoteMode ? (!st.query.remoteHasNext || st.query.remoteLoading) : st.query.page >= pages;
  renderProductSelection(st);
}

function renderProductSelection(st) {
  const remoteMode = Array.isArray(st.query.remoteProducts);
  const count = Object.keys(st.query.selected || {}).length;
  const pageInputs = Array.from($("qTable").querySelectorAll(".product-select"));
  const pageSelected = pageInputs.filter((input) => !!st.query.selected[input.dataset.id]).length;
  const selectAll = $("qSelectAll");
  selectAll.checked = !remoteMode && !!pageInputs.length && pageSelected === pageInputs.length;
  selectAll.indeterminate = !remoteMode && pageSelected > 0 && pageSelected < pageInputs.length;
  $("qSelected").textContent = remoteMode ? "实时结果只读，不可批量修改" : (count === pageSelected
    ? `已选 ${count} 项` : `已选 ${count} 项（当前页 ${pageSelected} 项）`);
  selectAll.disabled = remoteMode || !!st.busy;
  $("btnBulkRepair").disabled = remoteMode || !!st.busy || !count;
  $("btnBulkEdit").disabled = remoteMode || !!st.busy || !count;
}

let _productEditorContext = null;
async function openProductAdvancedEditor(st, product) {
  if (!isLiveState(st) || st.busy || st.query.advancedLoading) return;
  st.query.advancedLoading = true;
  setAreaMsg(st, "query", "qMsg", `正在读取产品 ${product.id} 的真实后台字段…`, "");
  const request = nextRequest(st, "productAdvanced");
  const result = await api().prepare_product_advanced_edit(st.id, product.id);
  st.query.advancedLoading = false;
  if (!requestIsCurrent(st, "productAdvanced", request)) return;
  if (!result || !result.ok) {
    if (result && result.native_url && typeof openNativeRecordUrl === "function") {
      setAreaMsg(st, "query", "qMsg",
        result.msg || result.native_reason ||
        "产品高级字段由网页脚本控制，已切换原生网页。", "review");
      await openNativeRecordUrl(st, result.native_url, "产品后台动态编辑页");
      return;
    }
    setAreaMsg(st, "query", "qMsg", (result && result.msg) || "高级字段读取失败", "bad");
    return;
  }
  const values = {};
  (result.fields || []).forEach((field) => { values[field.name] = field.value; });
  _productEditorContext = { st, product, form: result, values, saving: false,
    reviewKey: mutationReviewKey("product-advanced", product.id) };
  $("productEditorTitle").textContent = `产品高级编辑 #${product.id}`;
  $("productEditorHint").textContent = `${product.title || "（未命名产品）"}。字段来自真实后台表单；正文、图片和栏目请到“编辑修改”处理。`;
  $("productEditorMsg").textContent = (result.warnings || []).join("\n");
  $("productEditorMsg").className = "msg";
  const root = $("productEditorFields"); root.innerHTML = "";
  (result.fields || []).forEach((field) => {
    root.appendChild(createCmsFieldControl(field, values[field.name],
      (next) => { if (_productEditorContext) _productEditorContext.values[field.name] = next; },
      "product_advanced"));
  });
  $("productEditorSave").disabled = !(result.fields || []).length;
  $("productEditorMask").hidden = false;
}

function closeProductAdvancedEditor() {
  if (_productEditorContext && _productEditorContext.saving) return;
  _productEditorContext = null;
  $("productEditorMask").hidden = true;
  $("productEditorFields").innerHTML = "";
}

async function saveProductAdvancedEditor() {
  const context = _productEditorContext;
  if (!context || context.saving || !isLiveState(context.st)) return;
  if (!await confirmMutationReview(context.st, context.reviewKey, "产品高级保存")) return;
  context.saving = true; $("productEditorSave").disabled = true;
  setMsg("productEditorMsg", "正在重新读取后台字段并检查冲突…", "");
  let result;
  try {
    result = await api().update_product_advanced(
      context.st.id, context.product.id, context.values, context.form.revision);
  } catch (error) {
    result = { ok: false, msg: String(error) };
  }
  context.saving = false; $("productEditorSave").disabled = false;
  if (!result || !result.ok) {
    if (result && result.native_url && typeof openNativeRecordUrl === "function") {
      const nativeUrl = String(result.native_url || "");
      const nativeMessage = result.msg || result.native_reason ||
        "产品字段上传策略无法安全复刻，已切换原生网页。";
      closeProductAdvancedEditor();
      setAreaMsg(context.st, "query", "qMsg", nativeMessage, "review");
      const handoffFields = typeof nativeHandoffFields === "function"
        ? nativeHandoffFields(context.values || {}) : Object.entries(context.values || {})
          .filter(([name, value]) => /^[A-Za-z_][A-Za-z0-9_.:\[\]-]{0,159}$/.test(String(name)) &&
            value != null && typeof value !== "object")
          .slice(0, 300).map(([name, value]) => ({name, value}));
      await openNativeRecordUrl(context.st, nativeUrl, "产品后台动态编辑页", {
        handoff: {
          workflow: "product",
          message: nativeMessage + "；当前字段已带入，未自动提交。",
          fields: handoffFields,
          notes: [`产品 ID：${context.product && context.product.id || ""}`],
        },
      });
      return;
    }
    const review = resultNeedsMutationReview(result);
    if (review) setMutationReview(context.st, context.reviewKey,
      (result && result.msg) || "产品高级保存结果待核对，请先到后台确认。");
    setMsg("productEditorMsg", (result && result.msg) || "高级字段保存失败", review ? "review" : "bad");
    return;
  }
  clearMutationReview(context.st, context.reviewKey);
  if (result.products) context.st.query.products = result.products;
  if (result.health) context.st.query.health = result.health;
  setAreaMsg(context.st, "query", "qMsg",
    (result.msg || "产品字段修改成功") + uploadResultNotice(result.upload_metadata), "ok");
  closeProductAdvancedEditor();
  if (context.st.id === ACTIVE) renderActiveTab();
}

async function startProductSync(mode, productIds) {
  const st = activeState();
  if (!st || st.busy) return;
  st.query.remoteProducts = null;
  st.query.remotePage = 1;
  st.query.remoteHasNext = false;
  const op = beginOperation(st, "product_sync");
  if (!op) return;
  st.taskStarted = true;
  const labels = { incremental: "准备同步最新数据（逐条核对详情）…", full: "准备完整同步…", repair: "准备修复缺失链接…" };
  st._prog = { done: 0, total: 1, text: labels[mode] || "准备同步…", task: "product_sync" };
  st.query.msg = ""; st.query.msgClass = "";
  if (st.id === ACTIVE) renderActiveTab();
  const options = { mode };
  if (st.query.mcode) options.mcode = st.query.mcode;
  if (productIds && productIds.length) options.product_ids = productIds;
  if (mode === "repair") options.fields = ["front_url"];
  const r = await api().product_sync(st.id, options);
  if (!operationIsCurrent(st, op)) return;
  if (!r || !r.ok) {
    setAreaMsg(st, "query", "qMsg", (r && r.msg) || "同步任务启动失败", "bad");
    finishOperation(st, op);
  }
}
async function pullProducts() { return startProductSync("incremental"); }

async function loadProductModels() {
  const st = activeState();
  if (!st || st.busy || !st.loggedIn || st.query.modelsLoading) return;
  st.query.modelsLoading = true; if (st.id === ACTIVE) renderActiveTab();
  try {
    const result = await api().list_product_models(st.id);
    if (!result || !result.ok) return setAreaMsg(st, "query", "qMsg", (result && result.msg) || "读取产品模型失败", "bad");
    st.query.models = result.models || [];
    if (st.query.mcode && !(st.query.models || []).some((item) => String(item.mcode) === st.query.mcode)) st.query.mcode = "";
    if ((st.query.models || []).length === 1) st.query.mcode = String(st.query.models[0].mcode || "");
    setAreaMsg(st, "query", "qMsg", st.query.models.length > 1
      ? "发现多个产品模型，请选择后再同步。" : "已确认产品模型，可直接同步。", "ok");
  } catch (error) { setAreaMsg(st, "query", "qMsg", "读取产品模型异常：" + error, "bad"); }
  finally { st.query.modelsLoading = false; if (st.id === ACTIVE) renderActiveTab(); }
}

function applyProductDone(st, d) {
  st.busy = false; st.taskKind = ""; st.taskStarted = false; st._prog = null;
  if (d.ok) {
    st.query.products = d.products || st.query.products || [];
    st.query.health = d.health || st.query.health;
    st.query.cacheLoaded = true;
    reconcileProductSelection(st);
  }
  if (!d.ok) {
    st.query.msg = d.msg || "同步失败";
    st.query.msgClass = d.cancelled ? "" : "bad";
  } else {
    const s = d.stats || {};
    const modeLabel = { incremental: "最新数据同步", full: "完整同步", repair: "缺失链接修复" }[d.mode] || "同步";
    st.query.msg = `✅ ${modeLabel}完成：共 ${d.count} 条（新增 ${s.added ?? 0}、更新 ${s.updated ?? 0}、` +
      `未变 ${s.unchanged ?? 0}、删除 ${s.deleted ?? 0}` +
      (d.mode === "repair" ? `、恢复链接 ${d.repaired_front_url ?? 0}` : "") + "）";
    st.query.msgClass = "ok";
  }
}

function onProductSyncDone(d) {
  const st = TABS.get(d.tab_id); if (!st) return;
  applyProductDone(st, d);
  renderTabStrip();
  if (d.tab_id === ACTIVE) renderActiveTab();
}

function onPullDone(d) {
  const st = TABS.get(d.tab_id);
  if (st) applyProductDone(st, Object.assign({ mode: "full" }, d));
  renderTabStrip();
  if (d.tab_id === ACTIVE) renderActiveTab();
}

async function repairSelectedProducts() {
  const st = activeState(); if (!st || st.busy) return;
  if (Array.isArray(st.query.remoteProducts))
    return setAreaMsg(st, "query", "qMsg", "后台实时结果只读，请先退出实时查询后再修复本地缓存。", "");
  reconcileProductSelection(st);
  const ids = Object.keys(st.query.selected || {});
  if (!ids.length) return;
  return startProductSync("repair", ids);
}

async function bulkModifySelectedProducts() {
  const st = activeState(); if (!st || st.busy) return;
  if (Array.isArray(st.query.remoteProducts))
    return setAreaMsg(st, "query", "qMsg", "后台实时结果只读，不能直接批量修改。请先同步到本地缓存。", "");
  reconcileProductSelection(st);
  const ids = Object.keys(st.query.selected || {});
  const byId = new Map((st.query.products || []).map((product) => [String(product.id), product]));
  if (ids.some((id) => (st.query.edits[id] || {}).requiresReview))
    return alertDialog("所选产品含保存结果待核对的条目。请先在后台核对，再通过该行快捷修改入口处理；未核对前不进行批量提交。", { title: "先核对后台", kind: "warn" });
  const changes = [];
  ids.forEach((id) => {
    const edit = (st.query.edits || {})[id] || {}, product = byId.get(id);
    if (!product || (!edit.modelTouched && !edit.priceTouched)) return;
    const change = { id, expected_model: String(product.xinghao || ""), expected_price: String(product.jiage || "") };
    if (edit.modelTouched) change.model = String(edit.model ?? "");
    if (edit.priceTouched) change.price = String(edit.price ?? "");
    changes.push(change);
  });
  if (!changes.length)
    return alertDialog("请先在所选产品的“新型号”或“新价格”输入框中填写内容。输入后再清空，表示明确清空该字段。", { title: "没有待提交值", kind: "warn" });
  const go = await confirmDialog("", {
    title: `批量修改 ${changes.length} 个产品？`, kind: "warn", okText: "逐项安全修改", cancelText: "取消",
    lines: ["整批会先校验；每项提交前还会回读后台最新值，避免覆盖他人的新改动。",
      st.query.continueOnError ? "已选择失败后继续：某项失败后仍会处理后续项目，已成功项目不会回滚。" : "首项失败即停止，尚未执行的项目不会被写入。"],
  });
  if (!go) return;
  const op = beginOperation(st, "product_bulk_modify"); if (!op) return;
  st.taskStarted = true;
  st._prog = { done: 0, total: changes.length, text: "准备批量修改…", task: "product_bulk_modify" };
  renderActiveTab();
  const result = await api().bulk_modify_products(st.id, changes, !!st.query.continueOnError);
  if (!operationIsCurrent(st, op)) return;
  if (!result || !result.ok || !result.started) {
    if (result && result.ok && result.started === false)
      setAreaMsg(st, "query", "qMsg", result.msg || "所选值没有变化", "");
    else setAreaMsg(st, "query", "qMsg", (result && result.msg) || "批量修改启动失败", "bad");
    finishOperation(st, op);
  }
}

function onProductBulkModifyDone(d) {
  const st = TABS.get(d.tab_id); if (!st) return;
  st.busy = false; st.taskKind = ""; st.taskStarted = false; st._prog = null;
  if (d.products) st.query.products = d.products;
  if (d.health) st.query.health = d.health;
  reconcileProductSelection(st);
  (d.succeeded || []).forEach((item) => {
    delete st.query.edits[String(item.id)];
    delete st.query.selected[String(item.id)];
  });
  (d.failed || []).forEach((item) => {
    const id = String(item && item.id || "");
    if (!id || !isWriteReview(item)) return;
    const edit = st.query.edits[id] || (st.query.edits[id] = {});
    edit.requiresReview = true;
  });
  if (typeof persistProductEdits === "function") persistProductEdits(st);
  const okCount = (d.succeeded || []).length, failCount = (d.failed || []).length;
  const pendingCount = (d.not_attempted || []).length;
  st.query.msg = `${d.ok ? "✅" : "⚠"} 批量修改：成功 ${okCount}、失败 ${failCount}、未执行 ${pendingCount}` +
    (d.cache_warning ? `\n${d.cache_warning}` : "") + (d.msg ? `\n${d.msg}` : "");
  st.query.msgClass = d.ok ? "ok" : (d.requires_review ? "review" : (d.cancelled ? "" : "bad"));
  renderTabStrip();
  if (d.tab_id === ACTIVE) renderActiveTab();
}

async function runBackendDiagnostic() {
  const st = activeState();
  if (!st || st.busy) return;
  const op = beginOperation(st, "diagnostic");
  if (!op) return;
  st.taskStarted = true;
  st._prog = { done: 0, total: 1, text: "准备扫描…", task: "diagnostic" };
  st.diagnostic.msg = "正在只读扫描后台结构…";
  setMsg("diagMsg", st.diagnostic.msg);
  if (st.id === ACTIVE) renderActiveTab();
  const r = await api().diagnose_backend_structure(st.id);
  if (!operationIsCurrent(st, op)) return;
  if (!r || !r.ok) {
    st.diagnostic.msg = (r && r.msg) || "结构诊断任务启动失败";
    finishOperation(st, op);
    if (st.id === ACTIVE) setMsg("diagMsg", st.diagnostic.msg, "bad");
  }
}
function onDiagnosticDone(d) {
  const st = TABS.get(d.tab_id);
  if (st) {
    st.busy = false; st.taskKind = ""; st.taskStarted = false; st._prog = null;
  }
  renderTabStrip();
  const message = d.ok
    ? `✅ 诊断完成：${d.model_count} 个模型、${d.category_count} 个栏目\n报告：${d.path}`
    : (d.msg || "结构诊断失败");
  if (st) st.diagnostic.msg = message;
  log(message, d.tab_id);
  if (d.tab_id === ACTIVE) {
    renderActiveTab();
    setMsg("diagMsg", message, d.ok ? "ok" : "bad");
  }
}

/* ══════════ 留言 ══════════ */
function messageStatusKind(value) {
  const text = String(value ?? "").trim().toLowerCase();
  return ({"前端显示": "visible", "显示": "visible", "visible": "visible",
    "前端隐藏": "hidden", "隐藏": "hidden", "hidden": "hidden",
    "已处理": "processed", "done": "processed", "未处理": "unprocessed", "待处理": "unprocessed",
    "已审核": "approved", "未审核": "unapproved", "开启": "enabled", "关闭": "disabled",
    "0": "value0", "1": "value1", "状态值 0": "value0", "状态值 1": "value1"})[text] || "unknown";
}

function filteredMessages(st) {
  const meta = st.msgMeta || {}, keyword = String(meta.filter || "").trim().toLowerCase();
  const status = meta.status || "all";
  return (st.msgs || []).filter((message) => {
    if (status !== "all" && (message.status_info?.kind || messageStatusKind(message.status)) !== status) return false;
    if (!keyword) return true;
    const extras = Object.entries(message.extras || {}).map(([key, value]) => `${key} ${value}`).join(" ");
    return [message.id, message.name, message.email, message.phone, message.contact,
      message.industry, message.city, message.product, message.content, message.visitor,
      message.status, extras, ...(message.fields || []).map((field) => `${field.label} ${field.value}`)]
      .some((value) => String(value ?? "").toLowerCase().includes(keyword));
  });
}

function reconcileMessageSelection(st) {
  const valid = new Set((st.msgs || []).map((message) => String(message.id || "")).filter(Boolean));
  st.msgMeta.selected = st.msgMeta.selected || {};
  Object.keys(st.msgMeta.selected).forEach((id) => {
    if (!valid.has(String(id))) delete st.msgMeta.selected[id];
  });
}

function messageDetailLines(message) {
  if (Array.isArray(message.fields) && message.fields.length)
    return message.fields.map((field) => `${field.label || "（无标签）"}：${field.value ?? ""}`)
      .concat(message.status_info?.label ? [`后台状态：${message.status_info.label}`] : []);
  return ["time", "name", "email", "phone", "industry", "city", "product", "status", "visitor", "content"]
    .map((key, index) => `${["时间", "姓名", "邮箱", "电话", "行业", "城市", "需求产品", "状态", "访客", "内容"][index]}：${message[key] ?? ""}`)
    .concat(Object.entries(message.extras || {}).map(([key, value]) => `${key}：${value}`));
}

function renderMessages(msgs, meta) {
  const st = activeState();
  if (!st) return;
  reconcileMessageSelection(st);
  const tbody = $("msgTable").querySelector("tbody");
  tbody.innerHTML = "";
  const rows = filteredMessages(st);
  rows.forEach((m) => {
    const contact = m.email || m.phone || m.contact || "";
    const messageId = String(m.id || "");
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input class="message-select" type="checkbox" data-id="${esc(messageId)}" ` +
      `${st.msgMeta.selected[messageId] ? "checked" : ""} ${messageId ? "" : "disabled"} aria-label="选择留言 ${esc(messageId)}"></td>` +
      `<td>${esc(m.time || "")}</td><td>${esc(m.name)}</td>` +
      `<td>${esc(contact)}</td><td>${esc(m.industry)}</td><td>${esc(m.city)}</td>` +
      `<td>${esc(m.product)}</td><td>${esc(m.status_info?.label || m.status || "—")}</td>` +
      `<td><span class="cell-preview" title="${esc(m.visitor || "")}">${esc(m.visitor || "—")}</span></td>` +
      `<td><span class="cell-preview" title="${esc(m.content)}">${esc(m.content)}</span></td>` +
      `<td><div class="product-link-actions"><button class="ghost mini message-detail" type="button">详情</button>` +
      `${m.reply_url ? '<button class="ghost mini message-native" type="button" title="在完整浏览器中打开该留言的后台表单">网页操作</button>' : ""}` +
      `${m.can_reply ? '<button class="ghost mini message-reply" type="button">回复</button>' : ""}` +
      `${m.can_status ? `<button class="ghost mini message-status" type="button">${esc(m.status_info?.target_label ? `设为${m.status_info.target_label}` : "修改状态")}</button>` : ""}` +
      `${m.can_delete ? '<button class="ghost mini danger-action message-delete" type="button">删除</button>' : ""}</div></td>`;
    const selector = tr.querySelector(".message-select");
    selector.addEventListener("change", () => {
      if (selector.checked) st.msgMeta.selected[messageId] = true;
      else delete st.msgMeta.selected[messageId];
      renderMessageSelection(st, rows);
    });
    tr.querySelector(".message-detail").addEventListener("click", () => {
      alertDialog("", {
        title: `留言详情${m.id ? ` #${m.id}` : ""}`,
        lines: messageDetailLines(m),
      });
    });
    const nativeButton = tr.querySelector(".message-native");
    if (nativeButton) nativeButton.addEventListener("click", async () => {
      const opened = await openNativeRecordUrl(st, m.reply_url, "留言后台表单");
      if (!opened) await alertDialog("无法打开原生留言网页，请确认系统浏览器和登录会话。", {
        title: "无法打开网页操作", kind: "warn"
      });
    });
    const statusButton = tr.querySelector(".message-status");
    const replyButton = tr.querySelector('.message-reply');
    if (replyButton) replyButton.addEventListener('click', () => openMessageReply(st, m));
    if (statusButton) statusButton.addEventListener("click", () => runMessageAction(m, "status"));
    const deleteButton = tr.querySelector(".message-delete");
    if (deleteButton) deleteButton.addEventListener("click", () => runMessageAction(m, "delete"));
    tbody.appendChild(tr);
  });
  const info = meta || {};
  const suffix = info.warning ? " · 部分加载" : (info.complete ? " · 已到底" : "");
  $("msgCount").textContent = info.loading ? "正在加载…" : `共 ${(msgs || []).length} 条留言${suffix}`;
  $("msgCount").title = info.warning || (info.pages ? `已读取 ${info.pages} 页` : "");
  const serverFilterButton = $("btnMsgServerFilter");
  if (serverFilterButton) {
    serverFilterButton.disabled = !!st.busy || !!info.loading;
    serverFilterButton.title = info.server_filter === true
      ? "当前结果由后台真实筛选并分页"
      : "使用后台真实关键词/状态筛选并重新分页；无法确认时保留本地筛选";
  }
  const nativeFilterButton = $("btnMsgNativeFilter");
  if (nativeFilterButton) {
    const nativeUrl = String(info.native_url || "").trim();
    nativeFilterButton.hidden = !nativeUrl;
    nativeFilterButton.disabled = !!st.busy || !!info.loading || !nativeUrl;
    nativeFilterButton.title = nativeUrl
      ? "后台筛选依赖 POST/JavaScript 时，在当前认证网页中执行"
      : "当前没有可验证的原生留言列表地址";
  }
  $("msgFilter").value = info.filter || "";
  $("msgStatus").value = info.status || "all";
  $("msgFilteredCount").textContent = `当前 ${rows.length} 条`;
  renderMessageSelection(st, rows);
}

function renderMessageSelection(st, rows) {
  const selected = st.msgMeta.selected || {};
  const currentIds = (rows || filteredMessages(st)).map((message) => String(message.id || "")).filter(Boolean);
  const currentSelected = currentIds.filter((id) => selected[id]).length;
  const totalSelected = Object.keys(selected).length;
  const checkbox = $("msgSelectAll");
  checkbox.checked = !!currentIds.length && currentSelected === currentIds.length;
  checkbox.indeterminate = currentSelected > 0 && currentSelected < currentIds.length;
  $("msgSelected").textContent = `已选 ${totalSelected} 项` +
    (totalSelected !== currentSelected ? `（当前结果 ${currentSelected} 项）` : "");
  $("btnMsgBulkStatus").disabled = !!st.busy || !totalSelected;
}

function keepMessageMeta(st, values) {
  st.msgMeta = Object.assign({ complete: false, pages: 0, warning: "", loading: false,
    filter: "", status: "all", selected: {}, limit: 50,
    server_filter: null, server_filter_query: {}, filter_warning: "", native_url: "" },
    st.msgMeta || {}, values || {});
}

let _messageReplyContext = null;
async function openMessageReply(st, message) {
  if (!isLiveState(st) || st.busy) return;
  const op = beginOperation(st, 'message_reply_load'); if (!op) return;
  let result;
  try { result = await api().prepare_message_reply(st.id, message.id, message.revision || ''); }
  catch (error) { result = {ok: false, msg: String(error)}; }
  if (!operationIsCurrent(st, op)) return;
  finishOperation(st, op);
  if (!isLiveState(st) || st.id !== ACTIVE) return;
  if (!result?.ok) return alertDialog(result?.msg || '回复表单读取失败', {title: '无法打开回复', kind: 'warn'});
  _messageReplyContext = {st, message, form: result, updates: {}, saving: false};
  const context = _messageReplyContext;
  $('messageReplyTitle').textContent = `留言回复 #${message.id}`;
  $('messageReplyFields').innerHTML = '';
  (result.fields || []).forEach((field) => $('messageReplyFields').appendChild(
    createCmsFieldControl(field, field.value, (value) => {
      if (_messageReplyContext === context && !context.saving) context.updates[field.name] = value;
    }, 'message_reply')));
  setMsg('messageReplyMsg', (result.warnings || []).join('\n'), '');
  $('messageReplySave').disabled = false;
  $('messageReplyMask').hidden = false;
}

async function closeMessageReply(force = false) {
  const context = _messageReplyContext;
  if (context?.saving) return;
  if (!force && context && Object.keys(context.updates).length && !await confirmDialog('', {
    title: '放弃当前未保存的回复编辑？', kind: 'warn', okText: '放弃编辑', cancelText: '继续编辑',
    lines: [context.review ? '上次请求可能已保存；关闭窗口不会撤销后台操作，请先核对。' : '关闭后本次输入不会保存。'],
  })) return;
  _messageReplyContext = null;
  $('messageReplyMask').hidden = true;
  $('messageReplyFields').innerHTML = '';
}

async function saveMessageReply() {
  const context = _messageReplyContext;
  if (!context || context.saving || !isLiveState(context.st) || context.st.busy) return;
  const {st, message, form} = context;
  for (const control of $('messageReplyFields').querySelectorAll('input,textarea,select'))
    if (!control.disabled && !control.reportValidity()) return;
  if ((context.review || st.msgMeta.review?.[message.id]) && !await confirmDialog('', {
    title: '上次回复结果待核对', kind: 'warn', okText: '已核对，继续', cancelText: '取消',
    lines: ['请先核对后台回复与显示状态。再次提交可能重复覆盖已保存的回复。'],
  })) return;
  if (!await confirmDialog('', {title: '保存留言回复？', kind: 'confirm', okText: '保存回复',
    lines: [`留言 ID：${message.id}`, context.updates.recontent === '' ? '回复内容：明确清空' : '仅提交本次修改，其他字段保持后台原值。']})) return;
  if (_messageReplyContext !== context) return;
  const op = beginOperation(st, 'message_reply'); if (!op) return;
  context.saving = true; $('messageReplySave').disabled = true;
  setMsg('messageReplyMsg', '正在读取最新表单、检查变化并保存…', '');
  let result;
  try { result = await api().save_message_reply(st.id, message.id, context.updates, form.revision, form.message_revision); }
  catch (error) { result = {ok: false, requires_review: true, msg: `回复调用中断，结果待核对：${error}`}; }
  if (!result) result = {ok: false, requires_review: true, msg: '未收到回复保存结果，请先核对后台'};
  context.saving = false;
  if (!operationIsCurrent(st, op)) return;
  finishOperation(st, op); $('messageReplySave').disabled = false;
  if (!result.ok) {
    if (result.requires_review) {
      context.review = true;
      if (typeof setMessageReview === "function") setMessageReview(st, message.id, true);
      else { st.msgMeta.review = st.msgMeta.review || {}; st.msgMeta.review[message.id] = true; }
    }
    setMsg('messageReplyMsg', result.msg || '回复未完成',
      result.cancelled ? '' : (result.requires_review ? 'review' : 'bad'));
    if (result.cancelled && st.id === ACTIVE)
      await alertDialog(result.msg || '回复操作已取消', {title: '已取消', kind: 'warn'});
    return;
  }
  if (typeof setMessageReview === "function") setMessageReview(st, message.id, false);
  else if (st.msgMeta.review) delete st.msgMeta.review[message.id];
  if (Array.isArray(result.messages)) {
    st.msgs = result.messages;
    keepMessageMeta(st, {complete: !!result.complete, pages: Number(result.pages || 0), warning: result.warning || ''});
    reconcileMessageSelection(st);
  }
  await closeMessageReply(true);
  if (st.id === ACTIVE) renderActiveTab();
  await alertDialog(result.msg || '回复已保存', {title: '回复完成'});
}
async function runMessageAction(message, action) {
  const st = activeState();
  if (!st || st.busy || !message || !message.id) return;
  if (st.msgMeta.review?.[message.id] && !await confirmDialog("", {
    title: "上次留言操作结果待核对", kind: "warn", okText: "已核对，继续", cancelText: "取消",
    lines: ["请先在后台确认上次操作的实际结果。再次切换状态可能把已成功的修改切回去。"],
  })) return;
  const deleting = action === "delete";
  const confirmed = await confirmDialog("", {
    title: deleting ? "删除留言？" : "修改留言状态？",
    kind: deleting ? "warn" : "confirm",
    okText: deleting ? "确认删除" : "确认切换",
    lines: [`留言 ID：${message.id}`, `姓名：${message.name || "—"}`,
      `时间：${message.time || "—"}`].concat(deleting ? ["删除后无法在软件中恢复。"] :
      [`当前：${message.status_info?.label || message.status || "未知"}`, `目标：${message.status_info?.target_label || "按后台提供的操作"}`]),
  });
  if (!confirmed || !isLiveState(st)) return;
  const op = beginOperation(st, "message");
  if (!op) return;
  st.msgMeta = Object.assign({}, st.msgMeta || {}, { loading: true, warning: "" });
  if (st.id === ACTIVE) renderActiveTab();
  const expected = { name: message.name || "", time: message.time || "",
    ...(message.revision ? {revision: message.revision} : {}) };
  let result;
  try {
    result = deleting
      ? await api().delete_message(st.id, message.id, expected)
      : await api().toggle_message_status(st.id, message.id, expected);
  } catch (error) {
    result = {ok: false, requires_review: true, msg: `操作调用中断，结果待核对：${error}`};
  }
  if (!result) result = {ok: false, requires_review: true, msg: "未收到留言操作结果，请核对后台"};
  if (!operationIsCurrent(st, op)) return;
  if (result && result.ok) {
    if (typeof setMessageReview === "function") setMessageReview(st, message.id, false);
    else if (st.msgMeta.review) delete st.msgMeta.review[message.id];
    st.msgs = result.messages || [];
    keepMessageMeta(st, { complete: !!result.complete, pages: Number(result.pages || 0),
      warning: result.warning || "", loading: false });
    reconcileMessageSelection(st);
    finishOperation(st, op);
    if (st.id === ACTIVE) renderActiveTab();
    await alertDialog(result.msg || "留言操作成功", { title: "操作完成" });
  } else {
    if (result.requires_review) {
      if (typeof setMessageReview === "function") setMessageReview(st, message.id, true);
      else { st.msgMeta.review = st.msgMeta.review || {}; st.msgMeta.review[message.id] = true; }
    }
    st.msgMeta.loading = false;
    st.msgMeta.warning = (result && result.msg) || "留言操作失败";
    finishOperation(st, op);
    if (st.id === ACTIVE) renderActiveTab();
    await alertDialog(st.msgMeta.warning, {
      title: result.cancelled ? "已取消" : (result.requires_review ? "结果待核对" : "操作未完成"),
      kind: "warn"
    });
  }
}
async function loadMessages(limit, serverFilter = false) {
  const st = activeState();
  if (!st || st.busy) return;
  const req = nextRequest(st, "messages");
  st.msgMeta = Object.assign({}, st.msgMeta || {}, { loading: true, warning: "" });
  if (st.id === ACTIVE) renderMessages(st.msgs, st.msgMeta);
  const requestedLimit = Number(limit ?? st.msgMeta.limit ?? 50);
  const keyword = serverFilter ? String(st.msgMeta.filter || "") : "";
  const status = serverFilter ? String(st.msgMeta.status || "all") : "";
  const r = await api().load_messages(st.id, requestedLimit, keyword, status);
  if (!requestIsCurrent(st, "messages", req)) return;
  st.msgMeta.loading = false;
  if (!r.ok) {
    st.msgMeta.warning = r.msg || "留言加载失败";
    if (st.id === ACTIVE) { renderMessages(st.msgs, st.msgMeta); $("msgCount").textContent = st.msgMeta.warning; }
    return;
  }
  st.msgs = r.messages;
  keepMessageMeta(st, { complete: !!r.complete, pages: Number(r.pages || 0),
    warning: r.warning || r.filter_warning || "", loading: false,
    limit: requestedLimit, server_filter: r.server_filter,
    server_filter_query: r.server_filter_query || {},
    filter_warning: r.filter_warning || "", native_url: r.native_url || "" });
  reconcileMessageSelection(st);
  if (st.id === ACTIVE) renderMessages(st.msgs, st.msgMeta);
}

async function bulkToggleMessageStatus() {
  const st = activeState();
  if (!st || st.busy) return;
  reconcileMessageSelection(st);
  const selected = new Set(Object.keys(st.msgMeta.selected || {}));
  if ([...selected].some((id) => (st.msgMeta.review || {})[id]))
    return alertDialog("所选留言含结果待核对的操作，请先到后台核对，再从该条留言的单项入口处理。", { title: "结果待核对", kind: "warn" });
  const items = (st.msgs || []).filter((message) => selected.has(String(message.id || "")))
    .map((message) => ({ id: String(message.id), name: message.name || "", time: message.time || "",
      ...(message.revision ? {revision: message.revision} : {}) }));
  if (!items.length) return;
  const confirmed = await confirmDialog("", {
    title: `批量切换 ${items.length} 条留言状态？`, kind: "confirm", okText: "逐条切换",
    lines: ["每条都会重新读取后台真实操作入口，并在提交后回读验证。"],
  });
  if (!confirmed || !isLiveState(st)) return;
  const op = beginOperation(st, "message_bulk"); if (!op) return;
  st.taskStarted = true;
  st._prog = { done: 0, total: items.length, text: "准备批量处理留言…", task: "message_bulk" };
  renderActiveTab();
  const result = await api().bulk_toggle_message_status(st.id, items);
  if (!operationIsCurrent(st, op)) return;
  if (!result || !result.ok || !result.started) {
    keepMessageMeta(st, { warning: (result && result.msg) || "批量操作启动失败" });
    finishOperation(st, op);
  }
}

function onMessageBulkDone(data) {
  const st = TABS.get(data.tab_id); if (!st) return;
  st.busy = false; st.taskKind = ""; st.taskStarted = false; st._prog = null;
  if (Array.isArray(data.messages)) st.msgs = data.messages;
  keepMessageMeta(st, { complete: !!data.complete, pages: Number(data.pages || 0),
    warning: data.ok ? (data.warning || "") : (data.msg || "批量操作未全部完成"),
    loading: false, selected: {} });
  (data.failed || []).forEach((item) => {
    if (!item.requires_review) return;
    if (typeof setMessageReview === "function") setMessageReview(st, item.id, true);
    else { st.msgMeta.review = st.msgMeta.review || {}; st.msgMeta.review[item.id] = true; }
  });
  renderTabStrip();
  if (st.id === ACTIVE) renderActiveTab();
  alertDialog(data.msg || "批量留言操作完成", {
    title: data.requires_review ? "结果待核对，队列已停止" : data.ok ? "操作完成" : "部分操作未完成", kind: data.ok ? "success" : "warn",
  });
}

async function exportMessages(all = false) {
  const st = activeState(); if (!st || st.busy) return;
  reconcileMessageSelection(st);
  const selected = Object.keys(st.msgMeta.selected || {});
  const ids = all ? [] : selected.length ? selected : filteredMessages(st)
    .map((message) => String(message.id || "")).filter(Boolean);
  if (!all && !ids.length) return alertDialog("当前没有可导出的留言。", { title: "无数据", kind: "warn" });
  const button = $(all ? "btnMsgExportAll" : "btnMsgExport"); button.disabled = true;
  try {
    const result = await api().export_messages(st.id, ids);
    if (!result || !result.ok) throw new Error((result && result.msg) || "导出失败");
    await alertDialog(`已导出 ${result.count} 条留言\n${result.path}${result.warning ? `\n${result.warning}` : ""}`, { title: "CSV 导出完成" });
    if (result.path) api().open_local_path(result.path);
  } catch (error) {
    await alertDialog(String(error), { title: "导出失败", kind: "error" });
  } finally { button.disabled = false; }
}

/* ══════════ 操作审计 ══════════ */
const AUDIT_ACTION_LABELS = { category_create: "新增栏目", category_update: "修改栏目",
  category_delete: "删除栏目", category_batch_create: "批量新增栏目", product_update: "修改产品", product_advanced_update: "高级修改产品",
  product_bulk_update: "批量修改产品", message_status: "切换留言状态", message_delete: "删除留言",
  message_bulk_status: "批量切换留言", message_reply: "保存留言回复", cache_clear: "清理所有缓存",
  content_publish: "发布内容", content_edit: "编辑内容", slide_create: "新增网站轮播",
  slide_update: "修改网站轮播", slide_delete: "删除网站轮播" };

function auditTime(value) {
  const date = new Date(Number(value || 0) * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function renderAuditRecords(st) {
  const tbody = $("auditTable").querySelector("tbody"); tbody.innerHTML = "";
  (st.audit.records || []).forEach((record) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(auditTime(record.created_at))}</td>` +
      `<td>${esc(AUDIT_ACTION_LABELS[record.action] || record.action || "—")}</td>` +
      `<td>${esc(record.target_type || "—")}</td><td>${esc(record.target_id || "—")}</td>` +
      `<td><span class="audit-status ${esc(record.status || "")}">${esc(record.status || "—")}</span></td>` +
      `<td><span class="cell-preview" title="${esc(record.message || "")}">${esc(record.message || "—")}</span></td>` +
      `<td><button class="ghost mini audit-detail" type="button">查看摘要</button></td>`;
    tr.querySelector(".audit-detail").addEventListener("click", () => alertDialog("", {
      title: `审计详情 #${record.id || ""}`, lines: [
        `时间：${auditTime(record.created_at)}`, `操作：${AUDIT_ACTION_LABELS[record.action] || record.action || "—"}`,
        `对象：${record.target_type || "—"} ${record.target_id || ""}`, `结果：${record.status || "—"}`,
        `修改前摘要：${JSON.stringify(record.before || {}, null, 2)}`,
        `修改后摘要：${JSON.stringify(record.after || {}, null, 2)}`, `后台信息：${record.message || "—"}`],
    }));
    tbody.appendChild(tr);
  });
  $("auditCount").textContent = st.audit.loading ? "正在加载…" :
    (st.audit.error || `最近 ${(st.audit.records || []).length} 条`);
}

async function loadAuditRecords(targetState) {
  const st = targetState || activeState(); if (!st || !st.loggedIn || st.audit.loading) return;
  st.audit.loading = true; st.audit.error = ""; if (st.id === ACTIVE) renderAuditRecords(st);
  const request = nextRequest(st, "audit");
  const result = await api().load_audit_records(st.id, 200);
  if (!requestIsCurrent(st, "audit", request)) return;
  st.audit.loading = false; st.audit.loaded = !!(result && result.ok);
  if (result && result.ok) st.audit.records = result.records || [];
  else st.audit.error = (result && result.msg) || "审计记录加载失败";
  if (st.id === ACTIVE) renderAuditRecords(st);
}

async function exportAuditRecords() {
  const st = activeState(); if (!st || !st.loggedIn) return;
  const result = await api().export_audit_records(st.id);
  if (!result || !result.ok) return alertDialog((result && result.msg) || "导出失败", {
    title: "审计记录导出失败", kind: "error" });
  await alertDialog(`已导出 ${result.count} 条审计记录\n${result.path}`, { title: "审计 CSV 导出完成" });
  if (result.path) api().open_local_path(result.path);
}

/* ══════════ 事件绑定 ══════════ */
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    const st = activeState();
    if (st) st.activeFuncTab = btn.dataset.tab;
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("on"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("on"));
    btn.classList.add("on");
    $("tab-" + btn.dataset.tab).classList.add("on");
    if (st && btn.dataset.tab === "log" && st.loggedIn && !st.audit.loaded)
      loadAuditRecords(st);
    if (st && btn.dataset.tab === "slide" && st.loggedIn && !st.slide.loaded)
      loadSlides(st);
    if (st && btn.dataset.tab === "single" && st.loggedIn && !st.single.loaded)
      loadSinglePages(st);
    if (st && btn.dataset.tab === "admin" && st.loggedIn && !st.adminModules.loaded)
      loadAdminModules(st);
  });
});

$("btnNewTab").addEventListener("click", newTab);
$("btnNewTabBig").addEventListener("click", newTab);
$("btnAbout").addEventListener("click", openAbout);
$("btnClearAllCache").addEventListener("click", clearAllCache);
$("btnPubPreview").addEventListener("click", () => openHtmlPreview("pub"));
$("btnEditPreview").addEventListener("click", () => openHtmlPreview("edit"));
$("htmlPreviewClose").addEventListener("click", closeHtmlPreview);
$("htmlPreviewMask").addEventListener("click", (e) => {
  if (e.target === $("htmlPreviewMask")) closeHtmlPreview();
});
$("btnOpenAdmin").addEventListener("click", async () => {
  const st = activeState();
  if (!st || !st.loggedIn || !st.url) return;
  const opened = await openNativeRecordUrl(st, st.url, "原生后台");
  if (!opened) setMsg("pubMsg", "无法打开原生后台", "bad");
});
$("btnAboutClose").addEventListener("click", () => { $("aboutMask").hidden = true; });
$("aboutMask").addEventListener("click", (e) => { if (e.target === $("aboutMask")) $("aboutMask").hidden = true; });
$("btnCheckUpdate").addEventListener("click", checkForUpdates);
$("btnUpdateBackup").addEventListener("click", createUpdateBackup);
$("btnOpenBackup").addEventListener("click", () => { if (_lastBackupPath) api().open_local_path(_lastBackupPath); });
$("btnFetchCap").addEventListener("click", refreshCaptcha);
$("btnLogin").addEventListener("click", doLogin);
$("btnNativeLogin").addEventListener("click", openNativeLogin);
$("btnSyncNativeLogin").addEventListener("click", syncNativeLogin);
$("btnLoginCancel").addEventListener("click", () => {
  $("loginMask").hidden = true;
  const st = activeState();
  if (st && !st.loggedIn) closeTab(st.id);
});
$("capImg").addEventListener("click", refreshCaptcha);
$("btnLogout").addEventListener("click", doLogout);
$("btnTogglePass").addEventListener("click", () => {
  const input = $("loginPass"), showing = input.type === "text";
  input.type = showing ? "password" : "text";
  $("btnTogglePass").textContent = showing ? "显示" : "隐藏";
  $("btnTogglePass").title = showing ? "显示密码" : "隐藏密码";
  $("btnTogglePass").setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
  input.focus();
});
$("loginCode").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
$("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
// 地址填完回车即取验证码（不用再去找按钮）
$("loginUrl").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); refreshCaptcha(); }
});
$("loginUrl").addEventListener("input", (e) => {
  // 地址一变，旧地址的密码立即失效；不能等 blur/change 才清理。
  _credentialSeq += 1;
  _loginSeq += 1;
  if ($("loginHistory").value !== e.target.value) $("loginHistory").value = "";
  $("loginUser").value = "admin";
  $("loginPass").value = "";
  $("loginCode").value = "";
  $("capRow").hidden = true;
});

$("btnLoadCats").addEventListener("click", loadCategories);
// 统一对话框按钮：确定/取消/遮罩点击/Esc
$("dlgOk").addEventListener("click", () => _closeDialog(true));
$("dlgCancel").addEventListener("click", () => _closeDialog(false));
$("dlgMask").addEventListener("click", (e) => { if (e.target === $("dlgMask")) _closeDialog(false); });
// 待处理链接：任何改稿动作都必须是明确的“替换”或“去链”。
$("linkIgnoreAll").addEventListener("click", () => {
  const actions = _linkDeadCache.map((d) => linkActionFor(d, ""));
  _closeLinkPanel(actions);
});
$("linkCancel").addEventListener("click", () => _closeLinkPanel(null));
$("linkApply").addEventListener("click", () => {
  const actions = _collectLinkActions(_linkDeadCache);
  if (actions !== null) _closeLinkPanel(actions);
});
$("linkApplyVerified").addEventListener("click", () => {
  $("linkList").querySelectorAll(".link-row").forEach((row) => {
    const input = row.querySelector(".link-new");
    const item = _linkDeadCache[Number(input.dataset.idx)] || {};
    if (item.suggest) {
      input.value = item.suggest;
      input.disabled = false;
      row.querySelector(".link-action").value = "replace";
    }
  });
  $("linkError").textContent = "";
});
$("fieldEditorSave").addEventListener("click", () => closeFieldEditor(true));
$("fieldEditorCancel").addEventListener("click", () => closeFieldEditor(false));
$("fieldEditorRestore").addEventListener("click", () => {
  if (_fieldEditorContext) $("fieldEditorValue").value = _fieldEditorContext.originalValue;
});
$("fieldEditorMask").addEventListener("click", (e) => {
  if (e.target === $("fieldEditorMask")) closeFieldEditor(false);
});
$("productEditorSave").addEventListener("click", saveProductAdvancedEditor);
$("productEditorCancel").addEventListener("click", closeProductAdvancedEditor);
$("productEditorMask").addEventListener("click", (e) => {
  if (e.target === $("productEditorMask")) closeProductAdvancedEditor();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("productEditorMask").hidden) {
    e.preventDefault(); closeProductAdvancedEditor(); return;
  }
  if (e.key === "Escape" && !$("fieldEditorMask").hidden) {
    e.preventDefault(); closeFieldEditor(false); return;
  }
  if (e.key === "Escape" && !$("aboutMask").hidden) {
    e.preventDefault(); $("aboutMask").hidden = true; return;
  }
  if (e.key === "Escape" && !$("linkMask").hidden) {
    e.preventDefault(); _closeLinkPanel(null); return;
  }
  if ($("dlgMask").hidden) return;
  if (e.key === "Escape") { e.preventDefault(); _closeDialog(false); }
  else if (e.key === "Enter") { e.preventDefault(); _closeDialog(true); }
});
$("pubCatToggle").addEventListener("click", (e) => {
  e.stopPropagation(); toggleCatPanel("pub");
});
$("editCatToggle").addEventListener("click", (e) => {
  e.stopPropagation(); toggleCatPanel("edit");
});
$("btnPickHtml").addEventListener("click", pickHtml);
$("btnPickBatchHtml").addEventListener("click", () => pickBatchHtml(false));
$("btnPickBatchFolder").addEventListener("click", () => pickBatchHtml(true));
installFileDropTarget($("btnPickBatchHtml"), { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  const paths = await materializeDroppedFiles(files, "batch_html_drop", true, [".html", ".htm"]);
  await appendBatchHtmlPaths(st, paths);
}});
const dropPublishHtml = { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  const paths = await materializeDroppedFiles(files, "publish_html_drop", false, [".html", ".htm"]);
  if (paths.length) await loadPublishHtmlPath(st, paths[0]);
}};
installFileDropTarget($("btnPickHtml"), dropPublishHtml);
installFileDropTarget($("pubHtml"), dropPublishHtml);
installFileDropTarget($("pubManualImageDrop"), { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  const paths = await materializeDroppedFiles(
    files, "publish_content_images_drop", true,
    imageDropExtensions(st, "content", "pub"));
  appendManualImagePaths(st, paths, false);
}});
$("pubCat").addEventListener("change", onSelectCategory);
$("btnPickImgs").addEventListener("click", pickManualImages);
$("pubWidth").addEventListener("change", (e) => { const st = activeState(); st.pub.width = e.target.value; notePublishChanged(st); });
$("pubInsertStrategy").addEventListener("change", (e) => { const st = activeState(); if (st) { st.pub.insertStrategy = e.target.value; notePublishChanged(st); } });
$("pubTop").addEventListener("change", (e) => { const st = activeState(); st.pub.top = e.target.checked; st.pub.flagChanges = Object.assign({}, st.pub.flagChanges, { istop: e.target.checked }); notePublishChanged(st); });
$("pubRec").addEventListener("change", (e) => { const st = activeState(); st.pub.rec = e.target.checked; st.pub.flagChanges = Object.assign({}, st.pub.flagChanges, { isrecommend: e.target.checked }); notePublishChanged(st); });
$("pubHead").addEventListener("change", (e) => { const st = activeState(); st.pub.head = e.target.checked; st.pub.flagChanges = Object.assign({}, st.pub.flagChanges, { isheadline: e.target.checked }); notePublishChanged(st); });
$("pubSubmitter").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  const index = Number(e.target.value);
  st.pub.submitter = Number.isInteger(index) && index >= 0
    ? (st.pub.submitterOptions || [])[index] || null : null;
  notePublishChanged(st); renderContentSubmitter(st, "pub");
});
$("pubCheckLinks").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  st.pub.checkLinks = e.target.checked; notePublishChanged(st);
});
$("pubIco").addEventListener("change", async (e) => {
  const st = activeState();
  const previous = st.pub.ico;
  st.pub.ico = e.target.value;
  st.pub.thumbServerInfo = "";
  st.pub.thumbServerUrl = "";
  if (e.target.value === "file") {
    if (!await pickThumbnail("pub")) {
      // 取消文件框不是“不要缩略图”，应恢复用户之前的选项。
      st.pub.ico = previous;
      e.target.value = previous;
    }
  } else {
    st.pub.thumbPath = "";
    $("btnPubThumb").hidden = true;
    $("pubThumbInfo").textContent = "";
  }
  if (st.pub.ico === "url" && !st.pub.thumbUrl)
    st.pub.thumbUrl = String((st.pub.fields || []).find(field => field.name === "ico")?.value || "");
  renderThumbnailControl(st, "pub");
  notePublishChanged(st);
});
$("pubThumbUrl").addEventListener("input", (e) => {
  const st = activeState(); st.pub.thumbUrl = e.target.value; st.pub.thumbServerInfo = ""; st.pub.thumbServerUrl = ""; notePublishChanged(st);
  if (typeof renderThumbnailPreview === "function") void renderThumbnailPreview(st, "pub");
});
$("pubThumbRemember").addEventListener("change", (e) => {
  const st = activeState();
  if (st) { st.pub.rememberThumb = e.target.checked; notePublishChanged(st); }
});
$("loginHistory").addEventListener("change", async (e) => {
  const url = e.target.value;
  if (!url) return;
  $("loginUrl").value = url;
  if (await fillSavedCredentials(url))
    setMsg("loginMsg", "已载入该后台对应的账号和密码，可获取验证码后登录", "ok");
});
$("btnForgetHistory").addEventListener("click", async () => {
  const url = $("loginHistory").value;
  if (!url) return setMsg("loginMsg", "请先选择要删除的历史地址", "bad");
  const ok = await confirmDialog("该地址保存的用户名和密码也会一并删除。", {
    title: "删除历史后台？", kind: "warn", okText: "删除", cancelText: "取消" });
  if (!ok) return;
  _credentialSeq += 1;
  const r = await api().forget_saved_site(url);
  if (!r || !r.ok) return setMsg("loginMsg", (r && r.msg) || "删除失败", "bad");
  renderSiteList(r.urls || []);
  LAST_URL = r.last_url || "";
  $("loginHistory").value = "";
  if ($("loginUrl").value.trim() === url) {
    $("loginUser").value = "admin";
    $("loginPass").value = "";
  }
  setMsg("loginMsg", r.msg, "ok");
});
$("loginUrl").addEventListener("change", () => fillSavedCredentials($("loginUrl").value.trim()));
$("loginNetworkMode").addEventListener("change", () => { reprepareForNetworkChange(); });
$("loginProxyUrl").addEventListener("change", () => {
  if ($("loginNetworkMode").value === "custom") reprepareForNetworkChange();
});
$("btnPubThumb").addEventListener("click", () => pickThumbnail("pub"));
$("btnPubCarousel").addEventListener("click", () => pickCarousel("pub"));
$("btnPubGalleryEdit").addEventListener("click", () => openGalleryEditor("pub"));
$("btnEditGalleryEdit").addEventListener("click", () => openGalleryEditor("edit"));
$("galleryEditorAdd").addEventListener("click", addGalleryFiles);
$("galleryEditorCancel").addEventListener("click", () => closeGalleryEditor(false));
$("galleryEditorApply").addEventListener("click", () => closeGalleryEditor(true));
$("btnPubCarouselClear").addEventListener("click", () => {
  const st = activeState();
  if (!st || st.busy) return;
  st.pub.carouselImages = [];
  if (st.pub.galleryPlan != null) st.pub.galleryPlan = st.pub.galleryPlan.filter(item => item.kind !== "file");
  notePublishChanged(st);
  renderActiveTab();
});
installFileDropTarget($("btnPubThumb"), { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  const paths = await materializeDroppedFiles(
    files, "publish_thumbnail_drop", false,
    imageDropExtensions(st, "ico", "pub"));
  applyThumbnailPath(st, "pub", paths[0]);
}});
installFileDropTarget($("btnEditThumb"), { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  const paths = await materializeDroppedFiles(
    files, "edit_thumbnail_drop", false,
    imageDropExtensions(st, "ico", "edit"));
  applyThumbnailPath(st, "edit", paths[0]);
}});
installFileDropTarget($("btnPubCarousel"), { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  const paths = await materializeDroppedFiles(
    files, "publish_carousel_drop", true,
    imageDropExtensions(st, "pics", "pub"));
  appendCarouselPaths(st, "pub", paths);
}});
installFileDropTarget($("btnEditCarousel"), { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  const paths = await materializeDroppedFiles(
    files, "edit_carousel_drop", true,
    imageDropExtensions(st, "pics", "edit"));
  appendCarouselPaths(st, "edit", paths);
}});
$("pubCarouselSize").addEventListener("change", (e) => {
  const st = activeState();
  if (e.target.value !== "original") e.target.value = "original";
  if (st) { st.pub.carouselSize = "original"; notePublishChanged(st); }
});
$("btnAutoMap").addEventListener("click", async () => {
  const st = activeState();
  if (!st || st.busy || !st.pub.html) return;
  const path = st.pub.html;
  const req = nextRequest(st, "pubHtml");
  st.pub.htmlLoading = true;
  if (st.id === ACTIVE) renderActiveTab();
  const p = await api().parse_html(st.id, path, "publish");
  if (!requestIsCurrent(st, "pubHtml", req) || st.pub.html !== path) return;
  st.pub.htmlLoading = false;
  if (p && p.ok) applyPubParse(st, p);
  else {
    st.pub.htmlReady = false;
    st.pub.mapping = {};
    st.pub.htmlInfo = `HTML 重新解析失败：${(p && p.msg) || "未知错误"}`;
    st.pub.htmlInfoClass = "bad";
    if (st.id === ACTIVE) renderActiveTab();
  }
});
$("btnPreflight").addEventListener("click", preflight);
$("btnPublish").addEventListener("click", publish);
$("btnPubNative").addEventListener("click", async () => {
  const st = activeState();
  if (!st || st.busy || !st.pub.nativeUrl) return;
  await openNativeRecordUrl(st, st.pub.nativeUrl, "原生内容发布页");
});
$("btnRetryImgs").addEventListener("click", retryImages);
$("btnCancelTask").addEventListener("click", () => {
  const st = activeState();
  if (st && st.batch && st.batch.running) {
    st.batch.stopRequested = true;
    setAreaMsg(st, "pub", "pubMsg", "正在停止批量队列；当前写入若已开始会先等待取消结果…", "");
    renderTaskControls(st);
  }
  try { api().cancel_task(ACTIVE); } catch (_) {}
});
$("batchApplyVerified").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  st.batch.applyVerified = e.target.checked; scheduleDraftSave(st, "batch");
});
$("batchSkipUnverified").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  st.batch.skipUnverified = e.target.checked; scheduleDraftSave(st, "batch");
});
$("btnBatchStart").addEventListener("click", () => startBatchQueue(false));
$("btnBatchPause").addEventListener("click", pauseBatchQueue);
$("btnBatchResume").addEventListener("click", resumeBatchQueue);
$("btnBatchRetry").addEventListener("click", () => startBatchQueue(true));
$("btnBatchClear").addEventListener("click", clearBatchQueue);

$("btnEditLoadArts").addEventListener("click", editLoadArticles);
$("btnEditNative").addEventListener("click", openEditArticleNative);
$("btnEditFrontPreview").addEventListener("click", openEditArticlePreview);
$("editCat").addEventListener("change", (e) => {
  const st = activeState();
  if (!st || st.busy) return;
  const previousCat = st.edit.cat;
  if (previousCat && previousCat !== e.target.value)
    clearEditArticleDraft(st, true);
  st.edit.cat = e.target.value;
  scheduleDraftSave(st, "edit");
  nextRequest(st, "editArticles");
  nextRequest(st, "editForm");
  nextRequest(st, "editLinks");
  st.edit.articles = [];
  st.edit.artId = "";
  st.edit.fields = [];
  st.edit.mapping = {};
  st.edit.formReady = false;
  st.edit.linkReport = null;
  st.edit.articlesLoading = false;
  st.edit.formLoading = false;
  setAreaMsg(st, "edit", "editMsg", st.edit.cat ? "请点击“载入文章”" : "请选择栏目", "");
  if (st.id === ACTIVE) renderActiveTab();
});
$("editFilter").addEventListener("input", (e) => {
  const st = activeState(); if (!st) return;
  st.edit.filter = e.target.value;
  renderArticleOptions(e.target.value);
});
$("btnContentAdminLoad").addEventListener("click", loadContentAdmin);
$("contentAdminFilter").addEventListener("input", (e) => {
  const st = activeState(); if (!st) return;
  st.contentAdmin.keyword = e.target.value; renderContentAdmin(st);
});
$("contentAdminSelectAll").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  const kw = String(st.contentAdmin.keyword || "").trim().toLowerCase();
  (st.contentAdmin.records || []).forEach((row) => {
    if (!kw || String(row.title || "").toLowerCase().includes(kw) || String(row.id).includes(kw))
      st.contentAdmin.selected[String(row.id)] = !!e.target.checked;
  });
  renderContentAdmin(st);
});
$("contentAdminTable").addEventListener("change", (e) => {
  if (!e.target.classList.contains("content-admin-select")) return;
  const st = activeState(); if (!st) return;
  st.contentAdmin.selected[String(e.target.dataset.id)] = !!e.target.checked;
  renderContentAdmin(st);
});
$("btnContentAdminCopy").addEventListener("click", () => contentAdminAction("copy"));
$("btnContentAdminMove").addEventListener("click", () => contentAdminAction("move"));
$("btnContentAdminDelete").addEventListener("click", () => contentAdminAction("delete"));
$("btnContentAdminSort").addEventListener("click", () => contentAdminAction("sorting"));
$("editArt").addEventListener("change", editOnArticleChange);
$("btnEditPickHtml").addEventListener("click", editPickHtml);
const dropEditHtml = { onDrop: async files => {
  const st = activeState(); if (!st || st.busy) return;
  if (Object.keys(st.edit.imageReplacements || {}).length) {
    const clear = await confirmDialog("选择新 HTML 会清除已选择的原正文图片替换项。", {
      title: "切换到 HTML 修改？", kind: "warn", okText: "清除并选择", cancelText: "取消" });
    if (!clear || !isLiveState(st)) return;
    st.edit.imageReplacements = {};
  }
  const paths = await materializeDroppedFiles(files, "edit_html_drop", false, [".html", ".htm"]);
  if (paths.length) await loadEditHtmlPath(st, paths[0]);
}};
installFileDropTarget($("btnEditPickHtml"), dropEditHtml);
installFileDropTarget($("editHtml"), dropEditHtml);
$("editRefreshDate").addEventListener("change", (e) => { const st = activeState(); st.edit.refreshDate = e.target.checked; scheduleDraftSave(st, "edit"); });
$("editCover").addEventListener("change", (e) => { const st = activeState(); st.edit.cover = e.target.value; scheduleDraftSave(st, "edit"); });
$("editInsertStrategy").addEventListener("change", (e) => { const st = activeState(); if (st) { st.edit.insertStrategy = e.target.value; scheduleDraftSave(st, "edit"); } });
$("editTop").addEventListener("change", (e) => { const st = activeState(); st.edit.top = e.target.checked; st.edit.flagChanges = Object.assign({}, st.edit.flagChanges, { istop: e.target.checked }); scheduleDraftSave(st, "edit"); });
$("editRec").addEventListener("change", (e) => { const st = activeState(); st.edit.rec = e.target.checked; st.edit.flagChanges = Object.assign({}, st.edit.flagChanges, { isrecommend: e.target.checked }); scheduleDraftSave(st, "edit"); });
$("editHead").addEventListener("change", (e) => { const st = activeState(); st.edit.head = e.target.checked; st.edit.flagChanges = Object.assign({}, st.edit.flagChanges, { isheadline: e.target.checked }); scheduleDraftSave(st, "edit"); });
$("editSubmitter").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  const index = Number(e.target.value);
  st.edit.submitter = Number.isInteger(index) && index >= 0
    ? (st.edit.submitterOptions || [])[index] || null : null;
  scheduleDraftSave(st, "edit"); renderContentSubmitter(st, "edit");
});
$("editCheckLinks").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  st.edit.checkLinks = e.target.checked;
  invalidateEditLinkReport(st); scheduleDraftSave(st, "edit");
});
$("btnEditCarousel").addEventListener("click", () => pickCarousel("edit"));
$("btnEditCarouselClear").addEventListener("click", () => {
  const st = activeState();
  if (!st || st.busy) return;
  st.edit.carouselImages = [];
  if (st.edit.galleryPlan != null) st.edit.galleryPlan = st.edit.galleryPlan.filter(item => item.kind !== "file");
  scheduleDraftSave(st, "edit");
  renderActiveTab();
});
$("editCarouselMode").addEventListener("change", (e) => { const st = activeState(); st.edit.carouselMode = e.target.value; scheduleDraftSave(st, "edit"); });
$("editCarouselSize").addEventListener("change", (e) => {
  const st = activeState();
  if (!st) return;
  e.target.value = "original";
  st.edit.carouselSize = "original";
  scheduleDraftSave(st, "edit");
});
$("editIco").addEventListener("change", async (e) => {
  const st = activeState();
  const previous = st.edit.ico;
  st.edit.ico = e.target.value;
  st.edit.thumbServerInfo = "";
  st.edit.thumbServerUrl = "";
  if (e.target.value === "file") {
    if (!await pickThumbnail("edit")) {
      st.edit.ico = previous;
      e.target.value = previous;
    }
  } else {
    st.edit.thumbPath = "";
    $("btnEditThumb").hidden = true;
    $("editThumbInfo").textContent = "";
  }
  if (st.edit.ico === "url" && !st.edit.thumbUrl)
    st.edit.thumbUrl = String((st.edit.fields || []).find(field => field.name === "ico")?.value || "");
  renderThumbnailControl(st, "edit");
  scheduleDraftSave(st, "edit");
});
$("editThumbUrl").addEventListener("input", (e) => {
  const st = activeState(); st.edit.thumbUrl = e.target.value; st.edit.thumbServerInfo = ""; st.edit.thumbServerUrl = ""; scheduleDraftSave(st, "edit");
  if (typeof renderThumbnailPreview === "function") void renderThumbnailPreview(st, "edit");
});
$("btnEditThumb").addEventListener("click", () => pickThumbnail("edit"));
$("btnSubmitEdit").addEventListener("click", submitEdit);
$("btnEditCancel").addEventListener("click", () => api().cancel_task(ACTIVE));
$("btnEditLinkCancel").addEventListener("click", () => api().cancel_task(ACTIVE));
$("btnEditLinkRefresh").addEventListener("click", () => {
  const st = activeState(); if (st) refreshEditLinks(st, !st.edit.html, st.edit.artId);
});
$("editIssuesOnly").addEventListener("change", () => { const st = activeState(); if (st) renderEditLinkReport(st); });

$("btnPull").addEventListener("click", pullProducts);
$("btnPullFull").addEventListener("click", () => startProductSync("full"));
$("btnRemoteQuery").addEventListener("click", () => {
  const st = activeState();
  if (!st) return;
  if (Array.isArray(st.query.remoteProducts)) {
    st.query.remoteProducts = null; st.query.remotePage = 1; st.query.remoteHasNext = false;
    setAreaMsg(st, "query", "qMsg", "已返回本地产品缓存。", "");
    renderActiveTab();
    return;
  }
  queryProductsRemote(st, 1);
});
$("btnProductNative").addEventListener("click", async () => {
  const st = activeState();
  if (!st || !st.query || !st.query.nativeUrl) return;
  await openNativeRecordUrl(st, st.query.nativeUrl, "产品后台列表");
});
$("btnRepairLinks").addEventListener("click", () => startProductSync("repair"));
$("btnProductModels").addEventListener("click", loadProductModels);
$("qModel").addEventListener("change", (e) => {
  const st = activeState(); if (st) { st.query.mcode = String(e.target.value || "");
    setAreaMsg(st, "query", "qMsg", st.query.mcode ? `已选择产品模型 mcode=${st.query.mcode}，下一次同步将使用该模型。` : "已恢复自动识别模型。", ""); }
});
$("btnPullCancel").addEventListener("click", () => api().cancel_task(ACTIVE));
$("qFilter").addEventListener("input", (e) => { const st = activeState(); st.query.filter = e.target.value; st.query.page = 1; st.query.remoteProducts = null; st.query.remotePage = 1; st.query.remoteHasNext = false; renderProducts(e.target.value); });
$("qHealth").addEventListener("change", (e) => { const st = activeState(); st.query.healthFilter = e.target.value; st.query.page = 1; renderActiveTab(); });
$("qSort").addEventListener("change", (e) => { const st = activeState(); st.query.sort = e.target.value; st.query.page = 1; renderProducts(st.query.filter); });
$("qContinueErrors").addEventListener("change", (e) => {
  const st = activeState();
  if (st) st.query.continueOnError = !!e.target.checked;
});
document.querySelectorAll(".health-card[data-health]").forEach((card) => card.addEventListener("click", () => {
  const st = activeState(); if (!st) return;
  st.query.healthFilter = card.dataset.health; st.query.page = 1; renderActiveTab();
}));
$("qSelectAll").addEventListener("change", (e) => {
  const st = activeState(); if (!st) return;
  $("qTable").querySelectorAll(".product-select").forEach((input) => {
    input.checked = e.target.checked;
    if (e.target.checked) st.query.selected[input.dataset.id] = true;
    else delete st.query.selected[input.dataset.id];
  });
  renderProductSelection(st);
});
$("btnBulkRepair").addEventListener("click", repairSelectedProducts);
$("btnBulkEdit").addEventListener("click", bulkModifySelectedProducts);
$("qPrev").addEventListener("click", () => {
  const st = activeState(); if (!st) return;
  if (Array.isArray(st.query.remoteProducts)) {
    if (st.query.remotePage > 1) queryProductsRemote(st, st.query.remotePage - 1);
  } else if (st.query.page > 1) { st.query.page -= 1; renderProducts(st.query.filter); }
});
$("qNext").addEventListener("click", () => {
  const st = activeState(); if (!st) return;
  if (Array.isArray(st.query.remoteProducts)) {
    if (st.query.remoteHasNext) queryProductsRemote(st, st.query.remotePage + 1);
  } else { st.query.page += 1; renderProducts(st.query.filter); }
});

$("categoryFilter").addEventListener("input", (e) => {
  const st = activeState(); if (!st || !st.category) return;
  st.category.filter = e.target.value; renderCategoryAdminTree(st);
});
$("btnCategoryRefresh").addEventListener("click", refreshCategoryManager);
$("btnCategoryAddRoot").addEventListener("click", () => openCategoryCreate(""));
$("btnCategoryBatch").addEventListener("click", openCategoryBatchCreate);
$("btnCategoryAddChild").addEventListener("click", () => {
  const st = activeState(); if (st && st.category && st.category.selectedId) openCategoryCreate(st.category.selectedId);
});
$("btnCategorySave").addEventListener("click", saveCategory);
$("btnCategoryNative").addEventListener("click", () => {
  const st = activeState();
  const item = st && findCatNode(st.cats, st.category && st.category.selectedId);
  if (item) openNativeRecordUrl(st, item.edit_url, "栏目后台编辑页");
});
$("btnCategoryCancel").addEventListener("click", async () => {
  const st = activeState(); if (!st || !st.category) return;
  if (!await confirmDiscardCategory(st)) return;
  resetCategoryEditor(st); setCategoryMsg(st, "", ""); renderCategoryManager(st);
});
$("btnCategoryDelete").addEventListener("click", removeCategory);
$("btnSlideRefresh").addEventListener("click", () => loadSlides(activeState(), true));
$("btnSlideAdd").addEventListener("click", () => openSlideCreate(activeState()));
$("btnSlideSave").addEventListener("click", saveSlide);
$("btnSlideCancel").addEventListener("click", () => {
  const st = activeState(); if (!st) return;
  st.slide.form = null; st.slide.dirty = false; setSlideMsg(st, "", ""); renderSlideManager(st);
});
$("btnSlideDelete").addEventListener("click", () => {
  const st = activeState();
  const item = st && (st.slide.records || []).find((row) => String(row.id) === String(st.slide.selectedId));
  if (item) deleteSlide(st, item);
});
$("btnSingleRefresh").addEventListener("click", () => loadSinglePages(activeState(), true));
$("singleFilter").addEventListener("input", (event) => {
  const st = activeState(); if (!st || !st.single) return;
  st.single.keyword = event.target.value || "";
  // Filtering already-loaded rows is immediate; an explicit refresh is used
  // for server-side keyword filtering so the page mirrors the backend list.
  if (st.single.loaded) renderSingleManager(st);
});
$("btnSingleSave").addEventListener("click", saveSingle);
$("btnSingleCancel").addEventListener("click", async () => {
  const st = activeState(); if (!st || !st.single || !st.single.form) return;
  if (st.single.dirty && !await confirmDialog("放弃当前单页修改吗？", {
    title: "取消单页编辑", kind: "warn", okText: "放弃修改", cancelText: "继续编辑" })) return;
  st.single.form = null; st.single.dirty = false; setSingleMsg(st, "", ""); renderSingleManager(st);
});
$("btnAdminModuleRefresh").addEventListener("click", () => loadAdminModules(activeState(), true));
$("btnAdminModuleSave").addEventListener("click", saveAdminModule);
$("btnAdminModuleCancel").addEventListener("click", async () => {
  const st = activeState(); if (!st || !st.adminModules || !st.adminModules.form) return;
  if (st.adminModules.dirty && !await confirmDialog("放弃当前后台模块修改吗？", {
    title: "取消后台模块编辑", kind: "warn", okText: "放弃修改", cancelText: "继续编辑" })) return;
  st.adminModules.form = null; st.adminModules.dirty = false;
  setAdminModuleMsg(st, "", ""); renderAdminModules(st);
});

$("btnMsg50").addEventListener("click", () => loadMessages(50));
$("btnMsgAll").addEventListener("click", () => loadMessages(0));
$("msgFilter").addEventListener("input", (event) => {
  const st = activeState(); if (!st) return;
  st.msgMeta.filter = event.target.value; renderMessages(st.msgs, st.msgMeta);
});
$("msgStatus").addEventListener("change", (event) => {
  const st = activeState(); if (!st) return;
  st.msgMeta.status = event.target.value; renderMessages(st.msgs, st.msgMeta);
});
$("btnMsgServerFilter").addEventListener("click", () => {
  const st = activeState();
  if (!st || st.busy) return;
  loadMessages(Number(st.msgMeta.limit || 50), true);
});
$("btnMsgNativeFilter").addEventListener("click", async () => {
  const st = activeState();
  const target = String(st && st.msgMeta && st.msgMeta.native_url || "").trim();
  if (!st || st.busy || !target) return;
  const opened = await openNativeRecordUrl(st, target, "留言原生筛选页", {allowBusy: true});
  if (!opened) await alertDialog("无法打开原生留言筛选网页，请确认系统浏览器和登录会话。", {
    title: "无法打开网页筛选", kind: "warn"
  });
});
$("msgSelectAll").addEventListener("change", (event) => {
  const st = activeState(); if (!st) return;
  filteredMessages(st).forEach((message) => {
    const id = String(message.id || ""); if (!id) return;
    if (event.target.checked) st.msgMeta.selected[id] = true;
    else delete st.msgMeta.selected[id];
  });
  renderMessages(st.msgs, st.msgMeta);
});
$("btnMsgBulkStatus").addEventListener("click", bulkToggleMessageStatus);
$("btnMsgExport").addEventListener("click", () => exportMessages(false));
$("btnMsgExportAll").addEventListener("click", () => exportMessages(true));
$('messageReplyCancel').addEventListener('click', () => closeMessageReply());
$('messageReplySave').addEventListener('click', saveMessageReply);
$("btnAuditRefresh").addEventListener("click", () => loadAuditRecords());
$("btnAuditExport").addEventListener("click", exportAuditRecords);

$("btnDiag").addEventListener("click", async () => {
  const r = await api().export_diagnostics();
  if (r.ok && r.path) {
    const st = activeState(); if (st) st.diagnostic.lastPath = r.path;
    log("诊断包已导出: " + r.path, ACTIVE);
    if (st) renderActiveTab();
  }
});
$("btnDiagCopy").addEventListener("click", async () => {
  const st = activeState(); if (!st || !st.diagnostic.lastPath) return;
  const result = await api().clipboard_set(st.diagnostic.lastPath);
  setMsg("diagMsg", result && result.ok && result.done ? "诊断路径已复制" : "复制失败", result && result.done ? "ok" : "bad");
});
$("btnDiagOpen").addEventListener("click", () => {
  const st = activeState(); if (st && st.diagnostic.lastPath) api().open_local_path(st.diagnostic.lastPath);
});
$("btnBackendDiag").addEventListener("click", runBackendDiagnostic);
$("btnBackendDiagCancel").addEventListener("click", () => api().cancel_task(ACTIVE));
$("btnLogClear").addEventListener("click", () => {
  const st = activeState(); if (st) st.log = [];
  $("logBox").textContent = "";
});
$("pendingOpsList").addEventListener("click", async (event) => {
  const button = event.target.closest(".pending-op-resolve");
  const st = activeState();
  if (!button || !st) return;
  button.disabled = true;
  const result = await api().resolve_pending_operation(button.dataset.op, "用户已在后台核对");
  if (!result || !result.ok) {
    button.disabled = false;
    st.diagnostic.msg = (result && result.msg) || "处理待核对操作失败";
    renderActiveTab();
    return;
  }
  st.diagnostic.pendingOperations = (st.diagnostic.pendingOperations || [])
    .filter((item) => item.operation_id !== button.dataset.op);
  st.diagnostic.msg = "已确认后台结果并关闭该待核对记录；软件不会自动重发。";
  renderActiveTab();
});

/* pywebview 就绪：事件 + 轮询双保险 */
let _booted = false;
function boot() {
  if (_booted) return;
  if (!(window.pywebview && window.pywebview.api)) return;
  _booted = true;
  init();
}
window.addEventListener("pywebviewready", boot);
const _bootTimer = setInterval(() => { boot(); if (_booted) clearInterval(_bootTimer); }, 120);
setTimeout(() => { if (!_booted) log("⚠ 未检测到 pywebview 桥，请重启程序", ACTIVE); }, 6000);


function renderBulkLinks(st) {
  if (!st || st.id !== ACTIVE) return;
  const v = st.bulkLinks || {};
  const filters = st.bulkLinkFilters || {};
  $("bulkLinkAfter").value = filters.after || "";
  $("bulkLinkAdd").checked = !!filters.add;
  $("bulkLinkMsg").textContent = v.msg || "请先选择栏目和起始发布时间，再预览变更。";
  const pending = !!v.running;
  $("bulkLinkPreview").disabled = pending || !!st.busy;
  $("bulkLinkRestorePreview").disabled = pending || !!st.busy;
  $("bulkLinkApply").disabled = pending || !!st.busy || !v.token || v.restore || !(v.rows || []).length;
  $("bulkLinkRestore").disabled = pending || !!st.busy || !v.token || !v.restore || !(v.rows || []).length;
  $("bulkLinkCancel").disabled = !pending;
  $("bulkLinkRows").innerHTML = (v.rows || []).map(row => `<div style="padding:8px;border-bottom:1px solid #ddd"><strong>${esc(row.title || row.id)}</strong> · ID ${esc(row.id)} · ${esc(row.date || "")}<br>` +
    (row.changes || []).map(c => `${esc(c.model)}：${esc(c.old || "未链接")} → ${esc(c.new)}`).join("<br>") + "</div>").join("");
  $("bulkLinkDetails").textContent = (v.results || v.skipped || []).map(r => `${r.id || ""} ${r.model || ""} ${r.status || ""} ${r.reason || r.msg || ""}`).join("\n") +
    (v.backup_path ? `\n备份目录：${v.backup_path}` : "");
}
async function bulkLinkAction(action) {
  const st = activeState();
  if (!st || st.busy || st.bulkLinks?.running) return;
  const v = st.bulkLinks || {};
  const options = { scode: st.edit.cat, after: $("bulkLinkAfter").value, add: $("bulkLinkAdd").checked, token: v.token };
  st.bulkLinks = {running:true, msg:"正在启动…"}; renderBulkLinks(st);
  try {
    const r = await api().bulk_link_start(st.id, action, options);
    if (!r?.ok) throw new Error(r?.msg || "启动失败");
    async function poll() {
      try {
        const result = await api().bulk_link_status(st.id);
        if (!result?.ok) throw new Error(result?.msg || "状态读取失败");
        st.bulkLinks = result; renderBulkLinks(st);
        if (result.running) setTimeout(poll, 1000);
      } catch (e) { st.bulkLinks = {msg:String(e), error:true}; renderBulkLinks(st); }
    }
    await poll();
  } catch (e) { st.bulkLinks = {msg:String(e), error:true}; renderBulkLinks(st); }
}
$("bulkLinkPreview").addEventListener("click", () => bulkLinkAction("preview"));
$("bulkLinkApply").addEventListener("click", () => bulkLinkAction("apply"));
$("bulkLinkRestorePreview").addEventListener("click", () => bulkLinkAction("restore_preview"));
$("bulkLinkRestore").addEventListener("click", () => bulkLinkAction("restore"));
$("bulkLinkCancel").addEventListener("click", () => { const st=activeState(); if(st) api().cancel_task(st.id); });

function invalidateBulkLinkPreview() {
  const st=activeState(); if(!st) return;
  st.bulkLinkFilters={after:$("bulkLinkAfter").value, add:$("bulkLinkAdd").checked};
  if (!st.bulkLinks?.running) st.bulkLinks={msg:"筛选已变化，请重新预览。"};
  renderBulkLinks(st);
}
$("bulkLinkAfter").addEventListener("change", invalidateBulkLinkPreview);
$("bulkLinkAdd").addEventListener("change", invalidateBulkLinkPreview);
$("editCat").addEventListener("change", invalidateBulkLinkPreview);
