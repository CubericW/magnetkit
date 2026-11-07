/* MagnetKit 图形界面。后端是 magnet.py 里的本地 API（127.0.0.1，需要令牌）。
 * 原则：任何会开始下载的操作都要先让用户看清楚要下载什么、确认之后才发请求。 */
"use strict";
(function () {
  // ───────────────────────── 基础 ─────────────────────────
  const NATIVE = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.native);
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);

  let token = "";
  const hashMatch = /token=([\w-]+)/.exec(location.hash);
  if (hashMatch) {
    token = hashMatch[1];
    try { localStorage.setItem("mk-token", token); } catch (e) { /* 忽略 */ }
    history.replaceState(null, "", location.pathname);
  } else {
    try { token = localStorage.getItem("mk-token") || ""; } catch (e) { token = ""; }
  }
  const SESSION = Math.random().toString(36).slice(2, 10);

  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
  }

  async function api(method, path, body) {
    const opt = {
      method,
      // 请求头只能是 ASCII
      headers: { "X-MK-Token": token, "X-MK-Session": SESSION, "X-MK-Client": NATIVE ? "app" : "browser" },
    };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let res;
    try { res = await fetch(path, opt); } catch (e) { throw new ApiError("连不上 MagnetKit 后台服务", 0); }
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (res.status === 401) { authLost(); throw new ApiError("连接已失效", 401, data); }
    if (!res.ok) throw new ApiError(data.error || "请求失败（" + res.status + "）", res.status, data);
    return data;
  }

  // ───────────────────────── 格式化 ─────────────────────────
  const UNITS = ["B", "KB", "MB", "GB", "TB"];
  function fmtSize(n) {
    n = Number(n) || 0;
    let i = 0;
    while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
    return i === 0 ? n + " B" : (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + " " + UNITS[i];
  }
  const fmtSpeed = (n) => fmtSize(n) + "/s";
  function fmtDur(s) {
    if (s == null) return "";
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + " 秒";
    if (s < 3600) return Math.floor(s / 60) + " 分 " + (s % 60) + " 秒";
    if (s < 86400) return Math.floor(s / 3600) + " 小时 " + Math.floor((s % 3600) / 60) + " 分";
    return Math.floor(s / 86400) + " 天 " + Math.floor((s % 86400) / 3600) + " 小时";
  }
  function fmtAgo(ts) {
    const d = Date.now() / 1000 - ts;
    if (d < 60) return "刚刚";
    if (d < 3600) return Math.floor(d / 60) + " 分钟前";
    if (d < 86400) return Math.floor(d / 3600) + " 小时前";
    if (d < 7 * 86400) return Math.floor(d / 86400) + " 天前";
    const t = new Date(ts * 1000);
    return (t.getMonth() + 1) + " 月 " + t.getDate() + " 日";
  }
  const pct = (x) => (x >= 1 ? "100%" : (Math.floor(x * 1000) / 10).toFixed(1) + "%");
  const baseName = (p) => String(p).split("/").pop();
  const dirName = (p) => (String(p).includes("/") ? String(p).slice(0, String(p).lastIndexOf("/") + 1) : "");
  const looksLikeSource = (s) =>
    /^\s*(magnet:|https?:\/\/|file:\/\/)/i.test(s) || /^\s*([0-9a-f]{40}|[a-z2-7]{32})\s*$/i.test(s);

  // ───────────────────────── 图标（线条风格） ─────────────────────────
  const svg = (d, cls) => '<svg viewBox="0 0 24 24"' + (cls ? ' class="' + cls + '"' : "") + ' aria-hidden="true">' + d + "</svg>";
  const PLAY_SHAPE = '<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><path d="M10.2 9.4v5.2a.4.4 0 0 0 .6.3l4.1-2.6a.4.4 0 0 0 0-.6l-4.1-2.6a.4.4 0 0 0-.6.3z"/>';
  const DOC_SHAPE = '<path d="M13.5 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z"/><path d="M13.5 3.5V9H19"/>';
  const I = {
    download: svg('<path d="M12 4.5v10.5M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>'),
    clock: svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
    done: svg('<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.3 2.4 2.4 4.7-4.9"/>'),
    sliders: svg('<path d="M4 7.5h9M17 7.5h3M4 16.5h3M11 16.5h9"/><circle cx="15" cy="7.5" r="2"/><circle cx="9" cy="16.5" r="2"/>'),
    pulse: svg('<path d="M3 12h4l2.5-6.5 5 13L17 12h4"/>'),
    magnet: svg('<path d="M6 4.5h4v7a2 2 0 0 0 4 0v-7h4v7a6 6 0 0 1-12 0z"/><path d="M6 8.5h4M14 8.5h4"/>'),
    plus: svg('<path d="M12 5v14M5 12h14"/>'),
    pause: svg('<path d="M9 6.5v11M15 6.5v11"/>'),
    resume: svg('<path d="M8 5.8v12.4a.6.6 0 0 0 .9.5l9.6-6.2a.6.6 0 0 0 0-1L8.9 5.3a.6.6 0 0 0-.9.5z"/>'),
    play: svg(PLAY_SHAPE),
    folder: svg('<path d="M3.5 7.5a2 2 0 0 1 2-2h3.8l2 2h7.2a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>'),
    more: svg('<circle cx="6" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="18" cy="12" r="1.5"/>', "fill"),
    warn: svg('<path d="M12 4.5 20.5 19h-17z"/><path d="M12 10v4M12 16.8v.2"/>'),
    x: svg('<path d="m6.5 6.5 11 11M17.5 6.5l-11 11"/>'),
    check: svg('<path d="m5 12.5 4.5 4.5L19 7.5"/>'),
    video: svg(PLAY_SHAPE),
    audio: svg('<path d="M9 17V6.5l10-2V15"/><circle cx="6.5" cy="17" r="2.5"/><circle cx="16.5" cy="15" r="2.5"/>'),
    sub: svg('<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><path d="M7 11.5h10M7 14.5h4M13 14.5h4"/>'),
    image: svg('<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="9" cy="10" r="1.6"/><path d="m20.5 15.5-4.8-4.8L7 19.5"/>'),
    archive: svg('<rect x="3.5" y="4.5" width="17" height="4.5" rx="1.2"/><path d="M5 9v8.5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 12.5h4"/>'),
    doc: svg(DOC_SHAPE + '<path d="M8.5 13h7M8.5 16.5h5"/>'),
    other: svg(DOC_SHAPE),
  };
  const typeIcon = (f) => (f.suspicious ? I.warn : I[f.type] || I.other);
  const isMedia = (f) => f.type === "video" || f.type === "audio";

  // tracker 上的做种人数（后台定时询问，取各 tracker 的最大值）
  function swarmLevel(sw) {
    if (!sw || !sw.responded) return "";
    if (sw.seeders === 0) return "dead";
    return sw.seeders <= 2 ? "low" : "ok";
  }
  function swarmShort(sw) {
    if (!sw || !sw.queried) return "";
    return sw.responded ? "做种 " + sw.seeders + " · 下载 " + sw.leechers : "tracker 没有回应";
  }
  function swarmLong(sw) {
    if (!sw) return "正在询问 tracker 有多少人在做种…";
    if (!sw.queried) return "没有可以询问做种人数的 tracker";
    if (!sw.responded) return "问了 " + sw.queried + " 个 tracker 都没有回应，无法判断有多少人在做种";
    return "tracker 上 " + sw.seeders + " 个做种 · " + sw.leechers + " 个在下载（" + sw.responded + " 个 tracker 回应）";
  }
  const SWARM_HINT = {
    dead: "tracker 上没有人在做种，这个资源可能已经没人分享了；DHT 里也许还有，后台会继续尝试。",
    low: "做种的人很少，解析和下载都可能很慢。",
  };

  // ───────────────────────── 通用组件 ─────────────────────────
  function toast(msg, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind === "error" ? " error" : "");
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 5500 : 3200);
  }

  const modals = [];
  function openModal(inner, opts) {
    opts = opts || {};
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = '<div class="sheet' + (opts.small ? " small" : "") + '" role="dialog" aria-modal="true">' + inner + "</div>";
    document.body.appendChild(back);
    const entry = {
      el: back,
      sheet: back.firstElementChild,
      closed: false,
      onEsc: null,
      close() {
        if (entry.closed) return;
        entry.closed = true;
        back.remove();
        const i = modals.indexOf(entry);
        if (i >= 0) modals.splice(i, 1);
        if (opts.onClose) opts.onClose();
      },
    };
    modals.push(entry);
    return entry;
  }

  function confirmDialog(o) {
    return new Promise((resolve) => {
      const m = openModal(
        '<div class="sheet-head"><h2>' + esc(o.title) + "</h2></div>" +
        '<div class="sheet-body"><p class="selectable">' + esc(o.text) + "</p></div>" +
        '<div class="sheet-foot"><div class="grow"></div>' +
        '<button class="btn" data-a="no">' + esc(o.cancel || "取消") + "</button>" +
        '<button class="btn primary' + (o.danger ? " danger-fill" : "") + '" data-a="yes">' + esc(o.ok || "确认") + "</button></div>",
        { small: true, onClose: () => resolve(false) }
      );
      m.sheet.addEventListener("click", (e) => {
        const a = e.target.closest("[data-a]");
        if (!a) return;
        resolve(a.dataset.a === "yes");
        m.close();
      });
      // 危险操作默认聚焦「取消」
      setTimeout(() => { const b = $(o.danger ? '[data-a="no"]' : '[data-a="yes"]', m.sheet); if (b) b.focus(); }, 30);
    });
  }

  let menuEl = null;
  function menuOutside(e) { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }
  function closeMenu() {
    if (!menuEl) return false;
    menuEl.remove();
    menuEl = null;
    document.removeEventListener("mousedown", menuOutside, true);
    return true;
  }
  function popMenu(anchor, items) {
    closeMenu();
    const m = document.createElement("div");
    m.className = "menu";
    items.forEach((it) => {
      if (it === "-") { m.appendChild(document.createElement("hr")); return; }
      const b = document.createElement("button");
      b.textContent = it.label;
      if (it.danger) b.className = "danger";
      b.addEventListener("click", () => { closeMenu(); it.run(); });
      m.appendChild(b);
    });
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    let top = r.bottom + 4;
    if (top + m.offsetHeight > innerHeight - 8) top = r.top - m.offsetHeight - 4;
    m.style.top = Math.max(8, top) + "px";
    m.style.left = Math.max(8, Math.min(innerWidth - m.offsetWidth - 8, r.right - m.offsetWidth)) + "px";
    menuEl = m;
    setTimeout(() => document.addEventListener("mousedown", menuOutside, true), 0);
  }

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (closeMenu()) return;
    const top = modals[modals.length - 1];
    if (!top) return;
    e.preventDefault();
    if (top.onEsc) top.onEsc(); else top.close();
  });

  // 与原生外壳（MagnetKit.app）通信
  const waiters = {};
  function native(cmd, args) {
    if (!NATIVE) return Promise.resolve(null);
    const id = Math.random().toString(36).slice(2);
    return new Promise((resolve) => {
      waiters[id] = resolve;
      window.webkit.messageHandlers.native.postMessage(Object.assign({ cmd: cmd, id: id }, args || {}));
    });
  }
  function nativeResult(id, value) {
    const f = waiters[id];
    delete waiters[id];
    if (f) f(value);
  }

  let overlayEl = null;
  function authLost() {
    if (overlayEl) return;
    overlayEl = document.createElement("div");
    overlayEl.className = "overlay";
    overlayEl.innerHTML =
      "<div><h2>需要重新连接</h2><p>" +
      (NATIVE ? "后台服务刚刚更新过，重新连接一下就好。" : "请从 MagnetKit 应用打开，或在终端运行 magnet ui --browser。") +
      "</p>" + (NATIVE ? '<button class="btn primary" id="reconnect">重新连接</button>' : "") + "</div>";
    document.body.appendChild(overlayEl);
    const b = $("#reconnect", overlayEl);
    if (b) b.addEventListener("click", () => native("reconnect"));
  }

  // ───────────────────────── 状态与刷新 ─────────────────────────
  const S = { view: "tasks", st: null, offline: false };
  let polling = false;

  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      S.st = await api("GET", "/api/state");
      S.offline = false;
    } catch (e) {
      if (e.status !== 401) S.offline = true;
    } finally {
      polling = false;
    }
    renderChrome();
    const v = VIEWS[S.view];
    if (v && v.update) v.update();
  }

  function renderChrome() {
    const st = S.st;
    const tasks = st ? st.tasks : [];
    $("#count-tasks").textContent = tasks.length ? String(tasks.length) : "";
    const jobs = st ? st.jobs : [];
    const badge = $("#count-jobs");
    badge.hidden = !jobs.length;
    badge.textContent = String(jobs.length);
    badge.classList.toggle("hot", jobs.some((j) => j.state === "ready"));
    const sp = $("#speed");
    if (S.offline) sp.innerHTML = '<span class="err-text">后台服务未连接</span>';
    else if (st && !st.rqbit) sp.innerHTML = '<span class="err-text">下载服务未运行</span>';
    else if (st) sp.innerHTML = "↓ " + esc(fmtSpeed(st.speed.down)) + "<br>↑ " + esc(fmtSpeed(st.speed.up));
  }

  function go(view) {
    if (!VIEWS[view]) view = "tasks";
    closeMenu();
    S.view = view;
    $$(".nav").forEach((n) => n.classList.toggle("active", n.dataset.view === view));
    const main = $("#main");
    main.scrollTop = 0;
    main.innerHTML = "";
    VIEWS[view].mount(main);
  }

  // ───────────────────────── 视图：下载 ─────────────────────────
  const Tasks = {
    rows: new Map(),
    mount(el) {
      el.innerHTML =
        '<section class="view">' +
        '<header class="view-head"><h1>下载</h1><div class="grow"></div>' +
        '<button class="btn" id="pick">' + I.plus + "<span>打开种子文件</span></button></header>" +
        '<form class="addbar" id="addbar" autocomplete="off"><span class="addbar-ic">' + I.magnet + "</span>" +
        '<input id="src" type="text" placeholder="粘贴磁力链接、info hash 或种子网址" spellcheck="false" autocomplete="off">' +
        '<button class="btn primary" type="submit">查看文件</button></form>' +
        '<div id="notices"></div>' +
        '<div class="list" id="task-list"></div>' +
        '<div class="empty" id="task-empty" hidden><div class="empty-ic">' + I.magnet + "</div>" +
        "<h3>还没有下载任务</h3><p>把磁力链接粘贴到上面，或者把种子文件拖进窗口。</p>" +
        "<p>每次下载前都会先列出文件，由你确认。</p></div></section>";
      this.rows = new Map();
      const input = $("#src", el);
      $("#addbar", el).addEventListener("submit", (e) => {
        e.preventDefault();
        const v = input.value.trim();
        if (!v) { input.focus(); return; }
        input.value = "";
        openSource(v);
      });
      input.addEventListener("paste", () => setTimeout(() => {
        const v = input.value.trim();
        if (looksLikeSource(v)) { input.value = ""; openSource(v); }
      }, 0));
      $("#pick", el).addEventListener("click", () => $("#file-input").click());
      $("#task-list", el).addEventListener("click", onTaskClick);
      this.update();
      if (!modals.length) input.focus();
    },
    update() {
      const list = $("#task-list");
      if (!list) return;
      renderNotices();
      const st = S.st;
      if (!st) return;
      const tasks = st.tasks;
      $("#task-empty").hidden = tasks.length > 0 || S.offline;
      const seen = new Set();
      tasks.forEach((t, i) => {
        let row = this.rows.get(t.hash);
        if (!row) { row = makeTaskRow(t); this.rows.set(t.hash, row); }
        if (list.children[i] !== row) list.insertBefore(row, list.children[i] || null);
        fillTaskRow(row, t);
        seen.add(t.hash);
      });
      for (const [h, row] of this.rows) {
        if (!seen.has(h)) { row.remove(); this.rows.delete(h); }
      }
    },
  };

  function renderNotices() {
    const box = $("#notices");
    if (!box) return;
    const st = S.st;
    const items = [];
    if (S.offline) items.push(["warn", "连不上 MagnetKit 后台服务。", NATIVE ? ["重新连接", () => native("reconnect")] : null]);
    else if (st && !st.rqbit) items.push(["warn", "下载服务没有运行。", ["重启下载服务", restartService]]);
    const ready = st ? st.jobs.filter((j) => j.state === "ready" && !openSheets[j.id]).length : 0;
    if (ready) items.push(["", ready + " 个磁力链接已经拿到文件列表，等你确认。", ["去确认", () => go("pending")]]);
    const key = JSON.stringify(items.map((i) => [i[0], i[1]]));
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = items.map((it, i) =>
      '<div class="notice ' + it[0] + '"><span>' + esc(it[1]) + '</span><div class="grow"></div>' +
      (it[2] ? '<button class="btn" data-n="' + i + '">' + esc(it[2][0]) + "</button>" : "") + "</div>").join("");
    $$("[data-n]", box).forEach((b) => b.addEventListener("click", () => items[+b.dataset.n][2][1]()));
  }

  function makeTaskRow(t) {
    const row = document.createElement("div");
    row.className = "row clickable";
    row.dataset.hash = t.hash;
    row.innerHTML =
      '<div class="row-ic"></div>' +
      '<div class="row-body"><div class="row-name"></div><div class="bar"><i></i></div><div class="row-meta"></div></div>' +
      '<div class="row-actions">' +
      '<button class="icon-btn" data-act="toggle"></button>' +
      '<button class="icon-btn" data-act="play" title="播放">' + I.play + "</button>" +
      '<button class="icon-btn" data-act="folder" title="打开文件夹">' + I.folder + "</button>" +
      '<button class="icon-btn" data-act="more" title="更多">' + I.more + "</button></div>";
    return row;
  }

  function fillTaskRow(row, t) {
    row.className = "row clickable st-" + t.status;
    row._t = t;
    const ic = t.media ? "video" : "folder";
    if (row.dataset.ic !== ic) { row.dataset.ic = ic; $(".row-ic", row).innerHTML = I[ic]; }
    const name = $(".row-name", row);
    if (name.textContent !== t.name) { name.textContent = t.name; name.title = t.name; }
    $(".bar i", row).style.width = (Math.min(1, t.progress) * 100).toFixed(1) + "%";
    const parts = ['<span class="st">' + esc(t.status_text) + "</span>"];
    if (t.status === "downloading" || t.status === "paused" || t.status === "checking") {
      parts.push(esc(pct(t.progress)), esc(fmtSize(t.done) + " / " + fmtSize(t.size)));
    } else {
      parts.push(esc(fmtSize(t.size)));
    }
    if (t.down) parts.push("↓ " + esc(fmtSpeed(t.down)));
    if (t.up) parts.push("↑ " + esc(fmtSpeed(t.up)));
    if (t.eta != null && t.status === "downloading") parts.push("剩余 " + esc(fmtDur(t.eta)));
    if (t.selected < t.files) parts.push(esc("已选 " + t.selected + " / " + t.files + " 个文件"));
    if (t.error) parts.push('<span class="err-text">' + esc(t.error) + "</span>");
    const html = parts.map((p) => "<span>" + p + "</span>").join("");
    const meta = $(".row-meta", row);
    if (meta.dataset.k !== html) { meta.dataset.k = html; meta.innerHTML = html; }
    const tog = $('[data-act="toggle"]', row);
    const stopped = t.status === "paused" || t.status === "done" || t.status === "error";
    const mode = stopped ? "resume" : "pause";
    if (tog.dataset.mode !== mode) {
      tog.dataset.mode = mode;
      tog.innerHTML = stopped ? I.resume : I.pause;
    }
    tog.title = stopped ? (t.status === "done" ? "继续做种" : "继续") : "暂停";
    $('[data-act="play"]', row).hidden = !t.media;
  }

  function onTaskClick(e) {
    const row = e.target.closest(".row");
    if (!row || !row._t) return;
    const t = row._t;
    const btn = e.target.closest("[data-act]");
    if (!btn) { openTask(t.hash); return; }
    const act = btn.dataset.act;
    if (act === "toggle") taskAction(t, btn.dataset.mode === "pause" ? "pause" : "resume");
    else if (act === "play") playTask(t.hash);
    else if (act === "folder") openPath({ hash: t.hash });
    else if (act === "more") {
      popMenu(btn, [
        { label: "查看文件 / 修改选择", run: () => openTask(t.hash) },
        { label: "在访达中显示", run: () => openPath({ hash: t.hash, reveal: true }) },
        "-",
        { label: "移出列表（保留文件）", run: () => removeTask(t) },
        { label: "删除任务和文件…", danger: true, run: () => deleteTask(t) },
      ]);
    }
  }

  async function taskAction(t, action) {
    try { await api("POST", "/api/task/" + t.hash + "/" + action); refresh(); } catch (e) { toast(e.message, "error"); }
  }

  async function removeTask(t) {
    const ok = await confirmDialog({
      title: "移出下载列表？",
      text: "「" + t.name + "」会从列表里移除，已下载的文件保留在 " + t.folder_display + "。没有选择的文件留下的残缺部分会被清理。",
      ok: "移出列表",
    });
    if (!ok) return false;
    try {
      const r = await api("POST", "/api/task/" + t.hash + "/remove");
      toast("已移出列表" + (r.cleaned ? "，清理了 " + r.cleaned + " 个残缺文件" : ""));
      refresh();
      return true;
    } catch (e) { toast(e.message, "error"); return false; }
  }

  async function deleteTask(t) {
    const ok = await confirmDialog({
      title: "删除任务和文件？",
      text: "会删除「" + t.name + "」已经下载的全部文件，不能撤销。",
      ok: "删除",
      danger: true,
    });
    if (!ok) return false;
    try { await api("POST", "/api/task/" + t.hash + "/delete"); toast("已删除任务和文件"); refresh(); return true; }
    catch (e) { toast(e.message, "error"); return false; }
  }

  async function openPath(body) {
    try { await api("POST", "/api/open", body); } catch (e) { toast(e.message, "error"); }
  }

  async function restartService() {
    toast("正在重启下载服务…");
    try {
      const r = await api("POST", "/api/service/restart");
      toast(r.ok ? "下载服务已重启" : "重启后没有响应，去「诊断」页看看", r.ok ? "" : "error");
      refresh();
    } catch (e) { toast(e.message, "error"); }
  }

  // 播放：如果要播放的文件还没在下载列表里，先确认
  async function playTask(hash, fileIndex) {
    const body = fileIndex == null ? {} : { file: fileIndex };
    try {
      const r = await api("POST", "/api/task/" + hash + "/play", body);
      toast("已用 " + r.player + " 打开（" + r.how + "）");
    } catch (e) {
      if (e.status !== 409 || !e.data.confirm) { toast(e.message, "error"); return; }
      const c = e.data.confirm;
      const ok = await confirmDialog({
        title: "播放前需要下载这个文件",
        text: "「" + baseName(c.file) + "」还没有下载，播放需要下载 " + fmtSize(c.size) + "。确认下载并播放？",
        ok: "下载并播放",
      });
      if (!ok) return;
      try {
        const r = await api("POST", "/api/task/" + hash + "/play", Object.assign({ confirm: true }, body));
        toast("已用 " + r.player + " 打开（" + r.how + "）");
        refresh();
      } catch (e2) { toast(e2.message, "error"); }
    }
  }

  // ───────────────────────── 视图：待确认 ─────────────────────────
  const Pending = {
    key: "",
    mount(el) {
      el.innerHTML =
        '<section class="view"><header class="view-head"><h1>待确认</h1></header>' +
        '<p class="lead">解析较慢的磁力链接会在后台继续，拿到文件列表后放在这里。确认要下载的文件之后才会开始下载。</p>' +
        '<div class="list" id="job-list"></div>' +
        '<div class="empty" id="job-empty" hidden><div class="empty-ic">' + I.clock + "</div>" +
        "<h3>没有待确认的项目</h3><p>在「下载」页粘贴磁力链接就可以开始。</p></div></section>";
      $("#job-list", el).addEventListener("click", onJobClick);
      this.key = "";
      this.update();
    },
    update() {
      const list = $("#job-list");
      if (!list || !S.st) return;
      const jobs = S.st.jobs;
      $("#job-empty").hidden = jobs.length > 0;
      const key = JSON.stringify(jobs.map((j) => [j.id, j.state, j.name, j.error, j.files, j.size, j.tries, swarmShort(j.swarm)]));
      if (key !== this.key) { this.key = key; list.innerHTML = jobs.map(jobRowHtml).join(""); }
      jobs.forEach((j) => {
        const el = list.querySelector('[data-job="' + j.id + '"] .j-elapsed');
        if (el) el.textContent = fmtDur(j.elapsed);
      });
    },
  };

  function jobRowHtml(j) {
    let ic, meta, actions;
    const sw = swarmShort(j.swarm);
    const swarmTag = sw ? '<span class="swarm-tag ' + swarmLevel(j.swarm) + '">' + esc(sw) + "</span>" : "";
    if (j.state === "resolving") {
      ic = '<div class="spinner sm"></div>';
      meta = '<span>正在获取文件列表 · 已等待 <span class="j-elapsed">' + esc(fmtDur(j.elapsed)) + "</span></span>" +
        swarmTag + (j.tries ? "<span>已重试 " + j.tries + " 次</span>" : "") +
        (j.error ? '<span class="err-text">' + esc(j.error) + "</span>" : "");
      actions = '<button class="btn" data-j="open">查看</button><button class="btn" data-j="drop">放弃</button>';
    } else if (j.state === "ready") {
      ic = I.check;
      meta = "<span>" + esc(j.files + " 个文件 · " + fmtSize(j.size)) + "</span>" + swarmTag +
        "<span>" + esc(fmtAgo(j.created)) + "加入</span>";
      actions = '<button class="btn primary" data-j="open">查看文件并确认</button><button class="btn" data-j="drop">放弃</button>';
    } else {
      ic = I.warn;
      meta = '<span class="err-text">' + esc(j.error || "解析失败") + "</span>";
      actions = '<button class="btn" data-j="retry">重试</button><button class="btn" data-j="drop">删除</button>';
    }
    return '<div class="row" data-job="' + esc(j.id) + '"><div class="row-ic">' + ic + "</div>" +
      '<div class="row-body"><div class="row-name">' + esc(j.name || "磁力链接") + '</div><div class="row-meta">' + meta + "</div></div>" +
      '<div class="row-actions">' + actions + "</div></div>";
  }

  async function onJobClick(e) {
    const b = e.target.closest("[data-j]");
    if (!b) return;
    const id = b.closest("[data-job]").dataset.job;
    const act = b.dataset.j;
    try {
      if (act === "open") jobSheet(id, { keep: true });
      else if (act === "drop") { await api("DELETE", "/api/job/" + id); refresh(); }
      else if (act === "retry") { await api("POST", "/api/job/" + id + "/retry"); refresh(); }
    } catch (err) { toast(err.message, "error"); }
  }

  // ───────────────────────── 视图：已完成 ─────────────────────────
  const Done = {
    key: "",
    mount(el) {
      el.innerHTML =
        '<section class="view"><header class="view-head"><h1>已完成</h1></header>' +
        '<p class="lead">做种结束、已经移出下载列表的记录。文件都还在原来的位置。</p>' +
        '<div class="list" id="hist-list"></div>' +
        '<div class="empty" id="hist-empty" hidden><div class="empty-ic">' + I.done + "</div>" +
        "<h3>还没有完成的下载</h3><p>下载完成并做种结束后，会记录在这里。</p></div></section>";
      $("#hist-list", el).addEventListener("click", onHistClick);
      this.key = "";
      this.update();
    },
    update() {
      const list = $("#hist-list");
      if (!list || !S.st) return;
      const hist = S.st.history;
      $("#hist-empty").hidden = hist.length > 0;
      const key = JSON.stringify(hist.map((h) => [h.hash, h.exists, Math.floor((Date.now() / 1000 - h.at) / 60)]));
      if (key === this.key) return;
      this.key = key;
      list.innerHTML = hist.map((h) =>
        '<div class="row" data-h="' + esc(h.hash) + '"><div class="row-ic">' + I.folder + "</div>" +
        '<div class="row-body"><div class="row-name">' + esc(h.name) + '</div><div class="row-meta">' +
        "<span>" + esc(fmtSize(h.size)) + "</span><span>" + esc(fmtAgo(h.at)) + "完成</span>" +
        "<span>" + (h.exists ? esc(h.folder_display) : '<span class="err-text">文件夹已经不在了</span>') + "</span></div></div>" +
        '<div class="row-actions">' + (h.exists ? '<button class="btn" data-hact="open">打开</button>' : "") +
        '<button class="icon-btn" data-hact="forget" title="从记录里移除">' + I.x + "</button></div></div>").join("");
    },
  };

  async function onHistClick(e) {
    const b = e.target.closest("[data-hact]");
    if (!b) return;
    const h = b.closest("[data-h]").dataset.h;
    const item = ((S.st && S.st.history) || []).find((x) => x.hash === h);
    if (!item) return;
    if (b.dataset.hact === "open") { openPath({ path: item.folder }); return; }
    try { await api("POST", "/api/history/remove", { hash: h }); refresh(); } catch (err) { toast(err.message, "error"); }
  }

  // ───────────────────────── 视图：设置 ─────────────────────────
  const FIELDS = [
    { group: "下载" },
    { key: "download_dir", label: "保存位置", type: "dir", help: "新任务默认下载到这里。修改后需要重启下载服务。" },
    { group: "做种（上传）" },
    { key: "seed_ratio", label: "分享率目标", type: "number", step: "0.1", min: "0", help: "上传量达到下载量的几倍后停止做种，0 表示不看分享率。" },
    { key: "seed_minutes", label: "最长做种时间", type: "number", step: "1", min: "-1", suffix: "分钟", help: "下载完成后最多做种多久，-1 表示不限。两个条件满足任意一个就停止。" },
    { key: "after_seeding", label: "做种结束后", type: "select", options: [["remove", "移出列表并清理残缺文件"], ["pause", "暂停任务"], ["keep", "一直做种"]] },
    { group: "速度" },
    { key: "download_limit", label: "下载限速", type: "text", placeholder: "不限速，例如 5M", help: "每秒上限，可以写 500K、5M。修改后需要重启下载服务。" },
    { key: "upload_limit", label: "上传限速", type: "text", placeholder: "不限速，例如 1M" },
    { group: "播放" },
    { key: "player", label: "播放器", type: "select", options: [["auto", "自动（IINA → mpv → VLC）"], ["iina", "IINA"], ["mpv", "mpv"], ["vlc", "VLC"], ["quicktime", "QuickTime Player"]] },
    { group: "网络" },
    { key: "public_trackers", label: "公共 tracker", type: "switch", help: "解析磁力链接时附加公共 tracker，找节点更快。私有种子不会附加。" },
    { key: "upnp", label: "UPnP 端口映射", type: "switch", help: "在路由器上自动开放端口，能连到更多节点。修改后需要重启下载服务。" },
    { key: "utp", label: "uTP 协议", type: "switch", help: "rqbit 的实验功能。修改后需要重启下载服务。" },
    { key: "queue_retry_hours", label: "后台解析时长", type: "number", step: "1", min: "1", suffix: "小时", help: "解析不出来的磁力链接，最多在后台尝试多久。" },
    { group: "通知" },
    { key: "notify", label: "系统通知", type: "switch", help: "下载完成、出错，或者文件列表准备好时提醒你。" },
  ];
  const FIELD_BY_KEY = {};
  FIELDS.forEach((f) => { if (f.key) FIELD_BY_KEY[f.key] = f; });

  function fieldHtml(f) {
    if (f.group) return '<div class="group-title">' + esc(f.group) + "</div>";
    const k = ' data-k="' + f.key + '"';
    let control;
    if (f.type === "switch") control = '<label class="switch"><input type="checkbox"' + k + "><span></span></label>";
    else if (f.type === "select") control = "<select" + k + ">" + f.options.map((o) => '<option value="' + esc(o[0]) + '">' + esc(o[1]) + "</option>").join("") + "</select>";
    else if (f.type === "dir") control = '<input type="text"' + k + ' spellcheck="false">' + (NATIVE ? '<button class="btn" data-choose="' + f.key + '">选择…</button>' : "");
    else {
      control = '<input type="' + (f.type === "number" ? "number" : "text") + '"' + k +
        (f.step ? ' step="' + f.step + '"' : "") + (f.min ? ' min="' + f.min + '"' : "") +
        (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : "") + ' spellcheck="false">' +
        (f.suffix ? '<span class="muted">' + esc(f.suffix) + "</span>" : "");
    }
    return '<div class="field"><div class="field-label">' + esc(f.label) + '</div><div class="field-control">' + control + "</div>" +
      (f.help ? '<div class="field-help" data-help="' + f.key + '">' + esc(f.help) + "</div>" : "") + "</div>";
  }

  function readField(inp) {
    const f = FIELD_BY_KEY[inp.dataset.k];
    if (f.type === "switch") return inp.checked;
    if (f.type === "number") return inp.value === "" ? "" : Number(inp.value);
    return inp.value.trim();
  }

  const Settings = {
    orig: null,
    async mount(el) {
      el.innerHTML = '<section class="view narrow"><header class="view-head"><h1>设置</h1></header>' +
        '<div id="form"><div class="resolving"><div class="spinner"></div></div></div></section>';
      let cfg;
      try { cfg = await api("GET", "/api/config"); } catch (e) {
        const box = $("#form"); if (box) box.innerHTML = '<p class="err-text">' + esc(e.message) + "</p>";
        return;
      }
      const form = $("#form");
      if (!form || S.view !== "settings") return;
      this.orig = cfg.values;
      form.innerHTML = FIELDS.map(fieldHtml).join("") +
        '<div class="form-foot"><button class="btn" id="revert" disabled>还原</button><button class="btn primary" id="save" disabled>保存</button></div>';
      this.fill(this.orig);
      form.addEventListener("input", () => this.dirty());
      form.addEventListener("change", () => this.dirty());
      form.addEventListener("click", async (e) => {
        const c = e.target.closest("[data-choose]");
        if (!c) return;
        const inp = $('[data-k="' + c.dataset.choose + '"]', form);
        const p = await native("chooseFolder", { start: inp.value });
        if (p) { inp.value = p; this.dirty(); }
      });
      $("#revert", form).addEventListener("click", () => { this.fill(this.orig); this.dirty(); });
      $("#save", form).addEventListener("click", () => this.save());
    },
    fill(values) {
      $$("[data-k]").forEach((inp) => {
        const v = values[inp.dataset.k];
        if (inp.type === "checkbox") inp.checked = !!v; else inp.value = v == null ? "" : String(v);
      });
    },
    changes() {
      const out = {};
      $$("[data-k]").forEach((inp) => {
        const k = inp.dataset.k;
        const v = readField(inp);
        const o = this.orig[k];
        const same = typeof o === "number" ? Number(v) === o && v !== "" : v === o;
        if (!same) out[k] = v;
      });
      return out;
    },
    dirty() {
      const n = Object.keys(this.changes()).length;
      const save = $("#save"), revert = $("#revert");
      if (save) save.disabled = !n;
      if (revert) revert.disabled = !n;
      const dirInp = $('[data-k="download_dir"]');
      const help = $('[data-help="download_dir"]');
      if (dirInp && help) {
        const bad = /^~\/(Downloads|Documents|Desktop)(\/|$)/.test(dirInp.value.trim());
        help.classList.toggle("warn", bad);
        help.textContent = bad
          ? "macOS 会不声不响地阻止后台服务写入「下载 / 文稿 / 桌面」，建议换一个位置。"
          : FIELD_BY_KEY.download_dir.help;
      }
    },
    async save() {
      const body = this.changes();
      if (!Object.keys(body).length) return;
      try {
        const r = await api("POST", "/api/config", body);
        const fresh = await api("GET", "/api/config");
        this.orig = fresh.values;
        this.fill(this.orig);
        this.dirty();
        toast("设置已保存");
        if (r.restart_needed) {
          const ok = await confirmDialog({
            title: "需要重启下载服务",
            text: "部分设置要重启下载服务才能生效。正在进行的下载会在重启后自动继续。现在重启吗？",
            ok: "重启", cancel: "稍后",
          });
          if (ok) restartService();
        }
      } catch (e) { toast(e.message, "error"); }
    },
  };

  // ───────────────────────── 视图：诊断 ─────────────────────────
  const Diag = {
    mount(el) {
      el.innerHTML =
        '<section class="view narrow"><header class="view-head"><h1>诊断</h1><div class="grow"></div>' +
        '<button class="btn" id="net">联网测试</button><button class="btn" id="recheck">重新检查</button></header>' +
        '<div class="checks" id="checks"></div>' +
        '<div class="section-title">维护</div><div class="btn-row">' +
        '<button class="btn" id="restart">重启下载服务</button><button class="btn" id="trackers">更新 tracker 列表</button>' +
        '<button class="btn" id="rqbit">打开 rqbit 网页界面</button></div>' +
        '<div class="section-title">日志</div><div class="seg" id="seg">' +
        '<button data-log="rqbit" class="on">下载引擎</button><button data-log="watcher">后台服务</button>' +
        '<button data-log="app">App</button></div>' +
        '<pre class="log selectable" id="log">…</pre><p class="foot-note" id="about"></p></section>';
      $("#recheck", el).addEventListener("click", () => this.run(false));
      $("#net", el).addEventListener("click", () => this.run(true));
      $("#restart", el).addEventListener("click", restartService);
      $("#trackers", el).addEventListener("click", async () => {
        toast("正在更新 tracker 列表…");
        try { const r = await api("POST", "/api/trackers/update"); toast("已更新 " + r.count + " 个 tracker"); } catch (e) { toast(e.message, "error"); }
      });
      $("#rqbit", el).addEventListener("click", async () => {
        try { const r = await api("POST", "/api/rqbit-ui"); toast("已在浏览器打开，用户名 " + r.username + "，密码已复制到剪贴板"); } catch (e) { toast(e.message, "error"); }
      });
      $("#seg", el).addEventListener("click", (e) => {
        const b = e.target.closest("[data-log]");
        if (!b) return;
        $$("#seg button").forEach((x) => x.classList.toggle("on", x === b));
        this.loadLog(b.dataset.log);
      });
      this.run(false);
      this.loadLog("rqbit");
      this.update();
    },
    update() {
      const about = $("#about");
      if (about && S.st) about.textContent = "MagnetKit " + S.st.version + " · 下载引擎 rqbit · 下载目录 " + S.st.download_dir;
    },
    async run(net) {
      const box = $("#checks");
      if (!box) return;
      box.innerHTML = '<div class="check"><span class="mark"><span class="spinner sm"></span></span><span>' +
        (net ? "正在检查，联网测试最多需要一分半…" : "正在检查…") + "</span></div>";
      try {
        const r = await api("GET", "/api/doctor" + (net ? "?net=1" : ""));
        if (!$("#checks")) return;
        box.innerHTML = r.checks.map((c) =>
          '<div class="check ' + esc(c.level) + '"><span class="mark">' + (c.level === "ok" ? I.check : c.level === "warn" ? I.warn : I.x) +
          '</span><span class="selectable">' + esc(c.text) + "</span></div>").join("");
      } catch (e) {
        box.innerHTML = '<div class="check bad"><span class="mark">' + I.x + "</span><span>" + esc(e.message) + "</span></div>";
      }
    },
    async loadLog(name) {
      const pre = $("#log");
      if (!pre) return;
      try {
        const r = await api("GET", "/api/logs?name=" + encodeURIComponent(name));
        pre.textContent = r.text || "（暂无日志）";
        pre.scrollTop = pre.scrollHeight;
      } catch (e) { pre.textContent = e.message; }
    },
  };

  const VIEWS = { tasks: Tasks, pending: Pending, done: Done, settings: Settings, diag: Diag };

  // ───────────────────────── 添加：解析 → 看文件 → 确认下载 ─────────────────────────
  async function openSource(src) {
    src = String(src || "").trim();
    if (!src) return;
    try { handleResolve(await api("POST", "/api/resolve", { source: src }), false); }
    catch (e) { toast(e.message, "error"); }
  }

  async function openTorrentData(name, b64) {
    try { handleResolve(await api("POST", "/api/resolve", { torrent_b64: b64, name: name }), false); }
    catch (e) { toast(e.message, "error"); }
  }

  function handleResolve(r) {
    if (r.existing) {
      toast("「" + r.name + "」已经在下载列表里了");
      if (S.view !== "tasks") go("tasks");
      openTask(r.existing);
      return;
    }
    jobSheet(r.job, { keep: false });
    refresh();
  }

  const openSheets = {};
  // keep=true：从「待确认」打开，取消时保留在列表里；keep=false：刚粘贴的链接，取消就丢弃
  function jobSheet(jid, opts) {
    if (openSheets[jid]) return;
    const keep = !!(opts && opts.keep);
    let timer = null;
    const m = openModal(
      '<div class="sheet-head"><h2 class="js-title">正在获取文件列表…</h2><div class="sheet-sub js-sub"></div></div>' +
      '<div class="sheet-body js-body"></div><div class="sheet-foot js-foot"></div>',
      { onClose: () => { clearTimeout(timer); delete openSheets[jid]; } }
    );
    openSheets[jid] = m;
    let shown = "";
    async function poll() {
      if (m.closed) return;
      let j;
      try { j = await api("GET", "/api/job/" + jid); } catch (e) {
        if (e.status === 404) { m.close(); toast("这个解析任务已经不存在了"); refresh(); return; }
        timer = setTimeout(poll, 1500);
        return;
      }
      if (m.closed) return;
      if (j.state === "resolving") {
        renderResolving(m, j, jid, shown !== "resolving");
        shown = "resolving";
        timer = setTimeout(poll, 700);
      } else if (j.state === "ready") {
        if (shown !== "ready") { shown = "ready"; renderReady(m, j, jid, keep); }
      } else if (shown !== "failed") {
        shown = "failed";
        renderFailed(m, j, jid);
      }
    }
    poll();
  }

  function toBackground(m) {
    m.close();
    toast("已转到后台，拿到文件列表后会通知你");
    refresh();
  }

  function renderResolving(m, j, jid, first) {
    const s = m.sheet;
    if (first) {
      $(".js-title", s).textContent = j.name || "磁力链接";
      $(".js-body", s).innerHTML =
        '<div class="resolving"><div class="spinner"></div><div>正在从 P2P 网络获取文件列表…</div>' +
        '<div class="swarm js-swarm"></div>' +
        '<div class="small">资源越冷门越慢。可以放到后台继续，拿到文件列表后会通知你。</div>' +
        '<div class="hint js-hint" hidden></div></div>';
      $(".js-foot", s).innerHTML = '<button class="btn" data-a="drop">取消解析</button><div class="grow"></div>' +
        '<button class="btn primary" data-a="bg">在后台继续</button>';
      s.onclick = async (e) => {
        const a = e.target.closest("[data-a]");
        if (!a) return;
        if (a.dataset.a === "bg") toBackground(m);
        if (a.dataset.a === "drop") {
          m.close();
          try { await api("DELETE", "/api/job/" + jid); } catch (err) { /* 已经不存在也没关系 */ }
          refresh();
        }
      };
      m.onEsc = () => toBackground(m);
    }
    $(".js-sub", s).textContent = "已等待 " + fmtDur(j.elapsed) + (j.tries ? " · 已重试 " + j.tries + " 次" : "");
    const level = swarmLevel(j.swarm);
    const swarm = $(".js-swarm", s);
    const html = esc(swarmLong(j.swarm)) + (SWARM_HINT[level] ? '<div class="small">' + esc(SWARM_HINT[level]) + "</div>" : "");
    if (swarm.dataset.k !== html) {
      swarm.dataset.k = html;
      swarm.className = "swarm js-swarm " + level;
      swarm.innerHTML = html;
    }
    const hint = $(".js-hint", s);
    const text = j.hint || (j.error ? "上次尝试：" + j.error : "");
    hint.hidden = !text;
    hint.textContent = text;
  }

  function renderFailed(m, j, jid) {
    const s = m.sheet;
    $(".js-title", s).textContent = j.name || "磁力链接";
    $(".js-sub", s).textContent = "没能拿到文件列表";
    $(".js-body", s).innerHTML = '<p class="err-text selectable">' + esc(j.error || "解析失败") + "</p>";
    $(".js-foot", s).innerHTML = '<button class="btn" data-a="drop">删除</button><div class="grow"></div>' +
      '<button class="btn" data-a="close">关闭</button><button class="btn primary" data-a="retry">重试</button>';
    m.onEsc = null;
    s.onclick = async (e) => {
      const a = e.target.closest("[data-a]");
      if (!a) return;
      if (a.dataset.a === "close") { m.close(); return; }
      try {
        if (a.dataset.a === "drop") { await api("DELETE", "/api/job/" + jid); m.close(); }
        if (a.dataset.a === "retry") { await api("POST", "/api/job/" + jid + "/retry"); m.close(); jobSheet(jid, { keep: true }); }
      } catch (err) { toast(err.message, "error"); }
      refresh();
    };
  }

  // 文件选择 + 确认下载（这个面板本身就是下载前的确认）
  function renderReady(m, j, jid, keep) {
    const s = m.sheet;
    const meta = j.meta;
    const files = meta.files;
    const byI = new Map(files.map((f) => [f.i, f]));
    const sel = new Set(meta.default);
    const hasMedia = files.some(isMedia);
    const sus = files.filter((f) => f.suspicious).length;
    let customDir = "";

    $(".js-title", s).innerHTML = esc(meta.name) +
      (j.existing ? '<span class="tag blue">已在下载列表</span>' : "") + (meta.private ? '<span class="tag">私有种子</span>' : "");
    const level = swarmLevel(j.swarm);
    $(".js-sub", s).textContent = files.length + " 个文件 · " + fmtSize(meta.total) + (meta.summary ? " · " + meta.summary : "") +
      (level ? " · tracker 上 " + j.swarm.seeders + " 个做种" : "");
    const swarmWarn = level === "dead" ? "tracker 上现在没有人做种，确认后也可能一直下不动。"
      : level === "low" ? "只有 " + j.swarm.seeders + " 个人在做种，下载可能会很慢。" : "";
    $(".js-body", s).innerHTML =
      quickBar(files) +
      '<div class="files">' + files.map((f) => fileRow(f, sel.has(f.i), null)).join("") + "</div>" +
      (swarmWarn ? '<div class="warnline">' + I.warn + "<span>" + esc(swarmWarn) + "</span></div>" : "") +
      (sus ? '<div class="warnline">' + I.warn + "<span>" + sus + " 个可执行程序 / 快捷方式默认没有勾选，下载后请谨慎打开。</span></div>" : "") +
      (j.existing ? '<div class="warnline">' + I.warn + "<span>这个种子已经在下载列表里，确认后会把勾选的文件追加进去。</span></div>" : "") +
      '<div class="dest"><span>保存到</span><span class="path js-dest"></span>' + (NATIVE && !j.existing ? '<button class="link js-change">更改…</button>' : "") + "</div>";
    $(".js-foot", s).innerHTML = '<div class="info js-count"></div><div class="grow"></div>' +
      '<button class="btn" data-a="cancel">取消</button>' +
      (hasMedia ? '<button class="btn js-play" data-a="play">下载并播放</button>' : "") +
      '<button class="btn primary js-ok" data-a="ok">确认下载</button>';

    const destText = () => (customDir ? customDir + (files.length > 1 ? "/" + meta.name : "") : meta.dir);
    function sync() {
      let n = 0, bytes = 0, media = false;
      sel.forEach((i) => {
        const f = byI.get(i);
        if (f) { n++; bytes += f.size; media = media || isMedia(f); }
      });
      $(".js-count", s).textContent = "已选 " + n + " / " + files.length + " 个文件 · " + fmtSize(bytes);
      $(".js-ok", s).disabled = n === 0;
      const p = $(".js-play", s);
      if (p) p.disabled = !media;
      $$("input[type=checkbox][data-i]", s).forEach((c) => { c.checked = sel.has(+c.dataset.i); });
      $(".js-dest", s).textContent = destText();
      $(".js-dest", s).title = destText();
    }
    async function cancel() {
      m.close();
      if (!keep) { try { await api("DELETE", "/api/job/" + jid); } catch (e) { /* 忽略 */ } }
      refresh();
    }
    async function confirmAdd(play) {
      const buttons = $$(".js-ok, .js-play", s);
      buttons.forEach((b) => { b.disabled = true; });
      try {
        const r = await api("POST", "/api/add", {
          job: jid, files: Array.from(sel), dir: customDir || undefined, play: !!play, confirm: true,
        });
        m.close();
        if (r.existed) toast("已追加 " + r.added_files + " 个文件");
        else toast("已开始下载 · " + fmtSize(r.size) + (r.played ? " · 正在用 " + r.played.player + " 播放" : ""));
        if (S.view !== "tasks") go("tasks");
        refresh();
      } catch (e) {
        sync();
        toast(e.message, "error");
      }
    }
    bindFileList(s, files, sel, sync);
    s.onclick = async (e) => {
      if (quickClick(e, s, files, sel, sync)) return;
      if (e.target.closest(".js-change")) {
        const p = await native("chooseFolder", { start: customDir });
        if (p) { customDir = p; sync(); }
        return;
      }
      const a = e.target.closest("[data-a]");
      if (!a) return;
      if (a.dataset.a === "cancel") cancel();
      else if (a.dataset.a === "ok") confirmAdd(false);
      else if (a.dataset.a === "play") confirmAdd(true);
    };
    m.onEsc = cancel;
    sync();
  }

  // 文件列表的公共部分：快捷选择、筛选、勾选
  function quickBar(files) {
    return '<div class="quick"><button class="chip" data-q="rec">推荐</button><button class="chip" data-q="all">全选</button>' +
      '<button class="chip" data-q="none">全不选</button>' +
      (files.some((f) => f.type === "video") ? '<button class="chip" data-q="video">只要视频</button>' : "") +
      '<div class="grow"></div>' +
      (files.length > 8 ? '<input type="text" class="js-filter" placeholder="筛选文件名" spellcheck="false">' : "") + "</div>";
  }

  function fileRow(f, checked, progress) {
    const media = isMedia(f) && progress !== null;
    return '<label class="file' + (f.suspicious ? " sus" : "") + '" data-i="' + f.i + '" title="' + esc(f.path) + '">' +
      '<input type="checkbox" data-i="' + f.i + '"' + (checked ? " checked" : "") + ">" +
      '<span class="f-ic">' + typeIcon(f) + "</span>" +
      '<span class="f-name"><span class="f-dir">' + esc(dirName(f.path)) + "</span>" + esc(baseName(f.path)) + "</span>" +
      (media ? '<button class="icon-btn" data-play="' + f.i + '" title="播放">' + I.play + "</button>" : "") +
      (progress !== null ? '<span class="f-prog"><div class="bar"><i style="width:' + (progress * 100).toFixed(1) + '%"></i></div></span>' : "") +
      '<span class="f-size">' + esc(fmtSize(f.size)) + "</span></label>";
  }

  function bindFileList(s, files, sel, sync) {
    const byI = new Map(files.map((f) => [f.i, f]));
    s.onchange = (e) => {
      const c = e.target;
      if (!c.matches("input[type=checkbox][data-i]")) return;
      if (c.checked) sel.add(+c.dataset.i); else sel.delete(+c.dataset.i);
      sync();
    };
    s.oninput = (e) => {
      if (!e.target.classList.contains("js-filter")) return;
      const q = e.target.value.trim().toLowerCase();
      $$(".file", s).forEach((row) => {
        const f = byI.get(+row.dataset.i);
        row.hidden = !!q && !f.path.toLowerCase().includes(q);
      });
    };
  }

  // 快捷选择只作用于当前筛选出来的文件
  function quickClick(e, s, files, sel, sync) {
    const q = e.target.closest("[data-q]");
    if (!q) return false;
    const vis = files.filter((f) => { const row = s.querySelector('.file[data-i="' + f.i + '"]'); return row && !row.hidden; });
    const kind = q.dataset.q;
    vis.forEach((f) => {
      if (kind === "all") sel.add(f.i);
      else if (kind === "none") sel.delete(f.i);
      else if (kind === "rec") { if (f.suspicious) sel.delete(f.i); else sel.add(f.i); }
      else if (kind === "video") { if (f.type === "video" || f.type === "sub") sel.add(f.i); else sel.delete(f.i); }
    });
    sync();
    return true;
  }

  // ───────────────────────── 任务详情：查看文件 / 修改选择 ─────────────────────────
  async function openTask(hash) {
    let t;
    try { t = await api("GET", "/api/task/" + hash); } catch (e) { toast(e.message, "error"); return; }
    const files = t.files;
    const byI = new Map(files.map((f) => [f.i, f]));
    const orig = new Set(files.filter((f) => f.included).map((f) => f.i));
    const sel = new Set(orig);
    const m = openModal(
      '<div class="sheet-head"><h2>' + esc(t.name) + '</h2><div class="sheet-sub">' +
      esc(t.status_text + " · " + pct(t.progress)) + '</div><div class="sheet-sub one-line" title="' + esc(t.folder) + '">' +
      esc(t.folder_display) + "</div></div>" +
      '<div class="sheet-body">' + quickBar(files) +
      '<div class="files">' + files.map((f) => fileRow(f, f.included, f.size ? f.done / f.size : 1)).join("") + "</div>" +
      '<p class="muted small" style="margin:10px 0 2px">勾选的文件会被下载。新勾选的文件会先让你确认再开始下载。</p></div>' +
      '<div class="sheet-foot"><button class="btn danger" data-a="delete">删除任务和文件</button>' +
      '<button class="btn" data-a="remove">移出列表</button><div class="grow"></div>' +
      '<button class="btn" data-a="close">关闭</button><button class="btn primary js-save" data-a="save" disabled>保存更改</button></div>'
    );
    const s = m.sheet;
    const changed = () => sel.size !== orig.size || Array.from(sel).some((i) => !orig.has(i));
    const sync = () => {
      $$("input[type=checkbox][data-i]", s).forEach((c) => { c.checked = sel.has(+c.dataset.i); });
      $(".js-save", s).disabled = !changed();
    };
    async function save() {
      if (!sel.size) { toast("至少要保留一个文件；不需要这个任务的话可以「移出列表」", "error"); return; }
      const added = Array.from(sel).filter((i) => !orig.has(i));
      const removed = Array.from(orig).filter((i) => !sel.has(i));
      const addedBytes = added.reduce((a, i) => a + Math.max(0, byI.get(i).size - byI.get(i).done), 0);
      const parts = [];
      if (added.length) parts.push("开始下载 " + added.length + " 个新文件（" + fmtSize(addedBytes) + "）");
      if (removed.length) parts.push("停止下载 " + removed.length + " 个文件（已下载的部分会在任务移出列表时清理）");
      const ok = await confirmDialog({
        title: added.length ? "确认下载新增的文件？" : "停止下载这些文件？",
        text: "将" + parts.join("，并") + "。",
        ok: added.length ? "确认下载" : "确认",
      });
      if (!ok) return;
      try {
        await api("POST", "/api/task/" + hash + "/files", { files: Array.from(sel), confirm: true });
        toast("已更新文件选择");
        m.close();
        refresh();
      } catch (e) { toast(e.message, "error"); }
    }
    bindFileList(s, files, sel, sync);
    s.onclick = async (e) => {
      const p = e.target.closest("[data-play]");
      if (p) { e.preventDefault(); playTask(hash, +p.dataset.play); return; }
      if (quickClick(e, s, files, sel, sync)) return;
      const a = e.target.closest("[data-a]");
      if (!a) return;
      const act = a.dataset.a;
      if (act === "close") m.close();
      else if (act === "save") save();
      else if (act === "remove") { if (await removeTask(t)) m.close(); }
      else if (act === "delete") { if (await deleteTask(t)) m.close(); }
    };
  }

  // ───────────────────────── 拖放 / 选择种子文件 ─────────────────────────
  function readB64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] || "");
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
  }

  async function openFiles(list) {
    for (const f of Array.from(list)) {
      if (!/\.torrent$/i.test(f.name)) { toast("「" + f.name + "」不是种子文件", "error"); continue; }
      if (f.size > 20 * 1024 * 1024) { toast("「" + f.name + "」太大了，不像种子文件", "error"); continue; }
      try { await openTorrentData(f.name, await readB64(f)); } catch (e) { toast("读不了「" + f.name + "」", "error"); }
    }
  }

  $("#file-input").addEventListener("change", (e) => {
    openFiles(e.target.files);
    e.target.value = "";
  });

  let dragDepth = 0;
  const droppable = (e) => {
    const types = e.dataTransfer ? Array.from(e.dataTransfer.types || []) : [];
    return types.includes("Files") || types.includes("text/uri-list") || types.includes("text/plain");
  };
  document.addEventListener("dragenter", (e) => {
    if (!droppable(e)) return;
    e.preventDefault();
    dragDepth++;
    $("#drop").hidden = false;
  });
  document.addEventListener("dragover", (e) => { if (droppable(e)) e.preventDefault(); });
  document.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) $("#drop").hidden = true;
  });
  document.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("#drop").hidden = true;
    const dt = e.dataTransfer;
    if (!dt) return;
    if (dt.files && dt.files.length) { openFiles(dt.files); return; }
    const text = (dt.getData("text/uri-list") || dt.getData("text/plain") || "").split("\n")[0].trim();
    if (looksLikeSource(text)) openSource(text);
    else if (text) toast("拖进来的不是磁力链接或种子", "error");
  });

  // ───────────────────────── 启动 ─────────────────────────
  function focusAdd() {
    if (S.view !== "tasks") go("tasks");
    const i = $("#src");
    if (i) { i.focus(); i.select(); }
  }

  window.MK = { openSource, openTorrentData, go, focusAdd, nativeResult, toast };
  // 告诉原生外壳页面已就绪，它会把排队的操作（点击的磁力链接、打开的种子）交过来
  if (NATIVE) window.webkit.messageHandlers.native.postMessage({ cmd: "ready" });

  $$("[data-icon]").forEach((el) => { el.innerHTML = I[el.dataset.icon] || ""; });
  $$(".nav").forEach((n) => n.addEventListener("click", () => go(n.dataset.view)));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  go("tasks");
  if (!token) authLost(); else refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 1500);
})();
