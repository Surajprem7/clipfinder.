"use strict";
/* clipfinder - the page. No libraries, no outside requests. Every piece of text from the server is put into
   the page with textContent / createTextNode, never as HTML. */
(() => {
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ small helpers
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") e.className = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "vars") for (const [n, val] of Object.entries(v)) e.style.setProperty(n, val);
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return e;
  }
  function svg(markup) {
    const doc = new DOMParser().parseFromString(markup, "image/svg+xml");
    return document.importNode(doc.documentElement, true);
  }
  const ICON = {
    check: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#14161b" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg>',
    play: '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="#eceef2"/></svg>',
    done: '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" stroke-width="1.8"/><path d="M7.5 12.5l3 3 6-6.5"/></svg>',
    active: '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="40 24" stroke-linecap="round"/></svg>',
    wait: '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#6b7385" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="10"/></svg>',
  };
  const fmt = (t) => {
    t = Math.max(0, Math.round(t));
    const hh = Math.floor(t / 3600), mm = Math.floor((t % 3600) / 60), ss = t % 60;
    const p = (n) => String(n).padStart(2, "0");
    return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
  };
  const fmtSize = (b) => (b >= 1073741824 ? (b / 1073741824).toFixed(1) + " GB" : (b / 1048576).toFixed(1) + " MB");
  const base = (p) => String(p).split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ------------------------------------------------------------------ texts
  const LEVEL = { high: "ഉയർന്നത്", mid: "ഇടത്തരം", low: "കുറഞ്ഞത്" };
  const PRESET = { all: "എല്ലാം", action: "ആക്ഷൻ", slowmo: "സ്ലോ-മോഷൻ", clutch: "ക്ലച്ച്", voice: "ആർപ്പ്/ശബ്ദം" };
  const ERR = {
    no_ffmpeg: "ffmpeg ഇൻസ്റ്റാൾ ചെയ്തിട്ടില്ല. README-യിലെ ഇൻസ്റ്റാൾ നിർദ്ദേശം നോക്കുക.",
    no_ytdlp: "yt-dlp ഇല്ല (ലിങ്ക് ഉപയോഗിക്കാൻ വേണം). സെറ്റപ്പ് വീണ്ടും ഓടിക്കുക, അല്ലെങ്കിൽ ടെർമിനലിൽ: pip install yt-dlp",
    bad_link: "ഇത് സാധുവായ YouTube ലിങ്ക് അല്ല (https://www.youtube.com/… അല്ലെങ്കിൽ https://youtu.be/…).",
    too_short: "വീഡിയോ 10 സെക്കൻഡിൽ കുറവാണ്.",
    unreadable: "ഈ ഫയൽ വായിക്കാൻ കഴിഞ്ഞില്ല (കേടായതോ വീഡിയോ അല്ലാത്തതോ ആകാം).",
    download_failed: "ഡൗൺലോഡ് പരാജയപ്പെട്ടു. ലിങ്ക് ശരിയാണോ എന്നും വീഡിയോ പൊതുവായതോ അൺലിസ്റ്റഡോ ആണോ എന്നും നോക്കുക.",
    source_missing: "വീഡിയോ ഫയൽ കാണുന്നില്ല (മാറ്റുകയോ ഡിലീറ്റ് ചെയ്യുകയോ ചെയ്തിരിക്കാം).",
    stale: "ലിസ്റ്റ് മാറിയിരിക്കുന്നു. ക്ലിപ്പുകൾ വീണ്ടും തുറക്കുക.",
    busy: "മറ്റൊരു ജോലി നടക്കുന്നുണ്ട്. അത് കഴിയുന്നത് വരെ കാത്തിരിക്കുക.",
    local_only: "ഇത് PC-യിൽ നേരിട്ട് മാത്രമേ ചെയ്യാൻ കഴിയൂ.",
    bad_host_name: "ഇത് Tailscale പേര് അല്ല. ഉദാഹരണം: pc.tail1234.ts.net",
    bad_path: "ഫയലിന്റെ പാത്ത് ശരിയല്ല.",
    file_missing: "ഈ ഫയൽ കണ്ടെത്താൻ കഴിഞ്ഞില്ല. പാത്ത് ശരിയാണോ എന്ന് നോക്കുക.",
    bad_type: "ഇത് വീഡിയോ ഫയൽ അല്ല (mp4, mkv, mov, webm… മാത്രം).",
    no_space: "കമ്പ്യൂട്ടറിൽ ഇതിന് മതിയായ സ്ഥലം ഇല്ല.",
    too_large: "ഫയൽ വളരെ വലുതാണ്.",
    incomplete: "ഫയൽ പൂർണ്ണമായി എത്തിയില്ല. വീണ്ടും ശ്രമിക്കുക.",
    damaged: "സേവ് ചെയ്ത വിശകലനം കേടായി. വീഡിയോ വീണ്ടും വിശകലനം ചെയ്യുക.",
    not_found: "ഇത് കണ്ടെത്താൻ കഴിഞ്ഞില്ല.",
    network: "ആപ്പുമായി ബന്ധപ്പെടാൻ കഴിഞ്ഞില്ല. clipfinder ഓടുന്നുണ്ടോ എന്ന് നോക്കുക.",
    no_session: "അനുമതി ഇല്ല. ടെർമിനലിൽ കാണിച്ച ലിങ്ക് വീണ്ടും തുറക്കുക.",
    internal: "എന്തോ പിശക് സംഭവിച്ചു. ടെർമിനലിൽ വിശദാംശം കാണാം.",
  };
  const SHOW_DETAIL = new Set(["other", "internal", "download_failed", "unreadable"]);

  // ------------------------------------------------------------------ talking to the server
  class ApiErr extends Error {
    constructor(code, message) { super(message || code); this.code = code; this.detail = message || ""; }
  }
  async function api(method, path, body) {
    const opt = { method, credentials: "same-origin", headers: { "X-CF": "1" } };
    if (method !== "GET") { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body || {}); }
    let r;
    try { r = await fetch(path, opt); } catch (e) { throw new ApiErr("network"); }
    let data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new ApiErr((data && data.error) || "other", (data && data.message) || "");
    return data;
  }

  const state = {
    app: null,          // /api/state
    a: null,            // the analysis on screen
    sel: new Set(), selRun: null,
    shown: 6,
    edits: {}, editsRun: null, cur: null,
    watching: false,
    result: null,       // last render result
    preview: null,      // card currently playing
  };

  // ------------------------------------------------------------------ errors
  function clearError() { $("alert").hidden = true; $("alert").replaceChildren(); }
  function showError(e) {
    const code = e && e.code ? e.code : "other";
    const text = ERR[code] || "എന്തോ പിശക് സംഭവിച്ചു.";
    const box = $("alert");
    const msg = h("div", null, text);
    if (e && e.detail && SHOW_DETAIL.has(code)) msg.append(h("small", null, e.detail));
    box.replaceChildren(msg, h("button", { type: "button", "aria-label": "അടയ്ക്കുക", onclick: clearError }, "✕"));
    box.hidden = false;
    box.scrollIntoView({ block: "nearest" });
  }

  // ------------------------------------------------------------------ screens
  const SCREENS = ["home", "progress", "review", "make", "done"];
  function show(name, o = {}) {
    stopPreview();
    for (const s of SCREENS) $("s-" + s).hidden = s !== name;
    clearError();
    $("back").hidden = !o.back;
    if (o.back) { $("back").replaceChildren(h("span", { "aria-hidden": "true" }, "←"), " " + o.back); $("back").onclick = o.onBack; }
    $("brand").hidden = !!o.title;
    $("top-title").hidden = !o.title;
    $("top-title").textContent = o.title || "";
    $("top-info").hidden = !o.info;
    $("top-info").textContent = o.info || "";
    document.title = (o.title ? o.title + " · " : "") + "clipfinder";
    window.scrollTo(0, 0);
    const h1 = $("s-" + name).querySelector("h1");
    if (h1) h1.focus({ preventScroll: true });
  }
  function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }

  async function route() {
    if (state.watching) return;
    const m = location.hash.match(/^#\/(review|make|done)\/([0-9a-f]{12})$/);
    try {
      if (!m) return await showHome();
      if (m[1] === "review") return await showReview(m[2]);
      if (m[1] === "make") return await showMake(m[2]);
      return await showDone(m[2]);
    } catch (e) {
      await showHome().catch(() => {});
      showError(e);
    }
  }
  window.addEventListener("hashchange", route);

  async function ensureState() {
    state.app = await api("GET", "/api/state");
    document.body.classList.toggle("remote", !!state.app.remote);
    return state.app;
  }

  // ------------------------------------------------------------------ 1. home
  async function showHome() {
    const s = await ensureState();
    if (s.job && s.job.state === "running") return resumeJob(s.job);
    show("home");
    const fresh = s.videos.find((v) => !v.analysis);
    $("new-card").hidden = !fresh;
    if (fresh) {
      $("new-name").textContent = fresh.name;
      $("new-meta").textContent = `${fmtSize(fresh.size)} · വീഡിയോ ഫോൾഡറിൽ നിന്ന്`;
      $("new-go").onclick = () => startAnalyze({ kind: "file", path: fresh.path }, base(fresh.name));
    }
    $("empty-card").hidden = !!(fresh || s.library.length);
    $("empty-folder").textContent = s.video_dir;
    const lib = $("lib");
    lib.replaceChildren(...s.library.map((it) => h("li", { class: "item" },
      h("div", { class: "t" }, h("b", null, it.name),
        h("span", null, `${it.clips} ക്ലിപ്പ് കണ്ടെത്തി · ${it.made ? it.made + " ഉണ്ടാക്കി" : "ഒന്നും ഉണ്ടാക്കിയില്ല"}`)),
      h("button", { type: "button", class: "ghost", "aria-label": `${it.name} തുറക്കുക`, onclick: () => go("#/review/" + it.id) }, "തുറക്കുക"))));
    $("lib-empty").hidden = s.library.length > 0;
  }

  async function startAnalyze(body, label) {
    try {
      const r = await api("POST", "/api/analyze", body);
      if (r.existing) return go("#/review/" + r.existing);
      watchJob({ kind: "analyze", link: body.kind === "link", title: `${label} വിശകലനം ചെയ്യുന്നു`, sub: "" });
    } catch (e) { showError(e); }
  }

  $("pick").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const r = await api("POST", "/api/pick", {});
      if (r.unavailable) $("file-input").click();            // no system dialog here: use the browser's dialog
      else if (r.path) startAnalyze({ kind: "file", path: r.path }, base(r.path));
    } catch (e) { showError(e); } finally { btn.disabled = false; }
  });
  $("file-input").addEventListener("change", (ev) => {
    const f = ev.target.files[0];
    ev.target.value = "";
    if (f) uploadAndAnalyze(f);
  });
  const drop = $("drop");
  for (const t of ["dragover", "dragenter"]) drop.addEventListener(t, (ev) => { ev.preventDefault(); drop.classList.add("over"); });
  for (const t of ["dragleave", "drop"]) drop.addEventListener(t, () => drop.classList.remove("over"));
  drop.addEventListener("drop", (ev) => { ev.preventDefault(); const f = ev.dataTransfer.files[0]; if (f) uploadAndAnalyze(f); });
  for (const t of ["dragover", "drop"]) window.addEventListener(t, (ev) => ev.preventDefault());   // never navigate away to a dropped file

  function uploadAndAnalyze(file) {
    clearError();
    $("upload").hidden = false;
    $("upload-text").textContent = `${file.name} · കോപ്പി ചെയ്യുന്നു…`;
    $("upload-bar").style.setProperty("--w", "0%");
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/upload?name=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("X-CF", "1");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      $("upload-bar").style.setProperty("--w", Math.round((e.loaded / e.total) * 100) + "%");
      $("upload-text").textContent = `${file.name} · ${fmtSize(e.loaded)} / ${fmtSize(e.total)}`;
    };
    xhr.onerror = () => { $("upload").hidden = true; showError(new ApiErr("network")); };
    xhr.onload = () => {
      $("upload").hidden = true;
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
      if (xhr.status !== 200) return showError(new ApiErr(data.error || "other", data.message || ""));
      startAnalyze({ kind: "file", path: data.path }, base(file.name));
    };
    xhr.send(file);
  }

  function submitLink() {
    const v = $("link").value.trim();
    if (!v) return $("link").focus();
    if (/^https?:\/\//i.test(v)) startAnalyze({ kind: "link", url: v }, "YouTube ലിങ്ക്");
    else startAnalyze({ kind: "file", path: v }, base(v));
  }
  $("link-go").addEventListener("click", submitLink);
  $("link").addEventListener("keydown", (e) => { if (e.key === "Enter") submitLink(); });

  // ------------------------------------------------------------------ 2. progress (analysing and making)
  const STEPS_ANALYZE = [
    ["download", "ചെറിയ വീഡിയോ ഡൗൺലോഡ് ചെയ്യുന്നു (കണ്ടെത്താൻ മാത്രം)"],
    ["read", "ശബ്ദവും സ്ക്രീനിലെ ചലനവും വായിക്കുന്നു"],
    ["detect", "ക്ലിപ്പ് ആകാൻ സാധ്യതയുള്ള ഭാഗങ്ങൾ കണ്ടെത്തുന്നു"],
    ["thumbs", "ചെറു ചിത്രങ്ങൾ ഉണ്ടാക്കുന്നു"],
  ];

  function etaText(s) {
    if (s == null) return "";
    if (s < 60) return "ഒരു മിനിറ്റിൽ താഴെ ബാക്കി";
    return `ഏകദേശം ${Math.ceil(s / 60)} മിനിറ്റ് ബാക്കി`;
  }

  function drawProgress(cfg, job) {
    let steps, active, pct = null;
    if (cfg.kind === "render") {
      steps = cfg.labels.map((l) => [null, l]);
      const f = job.frac || 0;
      active = Math.min(steps.length - 1, Math.floor(f * steps.length));
      pct = Math.round(f * 100);
      if (job.state === "done") { active = steps.length; pct = 100; }
    } else {
      steps = STEPS_ANALYZE.filter(([k]) => cfg.link || k !== "download");
      active = Math.max(0, steps.findIndex(([k]) => k === job.stage));
      const f = job.frac || 0;
      if (job.stage === "read") pct = Math.round(2 + f * 85);
      else if (job.stage === "detect") pct = 90;
      else if (job.stage === "thumbs") pct = Math.round(92 + f * 8);
      else if (job.stage === "download") pct = null;
      else pct = 1;
    }
    $("pg-title").textContent = cfg.title;
    $("pg-sub").textContent = job.stage === "fetch" ? "തിരഞ്ഞെടുത്ത ഭാഗം 1080p-യിൽ ഡൗൺലോഡ് ചെയ്യുന്നു…"
      : job.stage === "fetch_full" ? "ഭാഗം മാത്രം കിട്ടിയില്ല, മുഴുവൻ വീഡിയോ ഡൗൺലോഡ് ചെയ്യുന്നു (കൂടുതൽ സമയം എടുക്കും)…"
      : (cfg.sub || "");
    const bar = $("pg-bar");
    bar.classList.toggle("indet", pct == null);
    if (pct == null) { bar.removeAttribute("aria-valuenow"); $("pg-pct").textContent = ""; }
    else { bar.setAttribute("aria-valuenow", pct); $("pg-fill").style.setProperty("--w", pct + "%"); $("pg-pct").textContent = pct + "%"; }
    $("pg-eta").textContent = cfg.kind === "analyze" ? etaText(job.eta) : "";
    $("pg-note").textContent = cfg.kind === "render"
      ? "ഫയലിന്റെ നീളം അനുസരിച്ച് കുറച്ച് സമയം എടുക്കും."
      : "ഈ ജാലകം തുറന്ന് വയ്ക്കുക. ഒരിക്കൽ കഴിഞ്ഞാൽ അടുത്ത തവണ ഇത് ഉടൻ തുറക്കും.";
    $("pg-steps").replaceChildren(...steps.map(([, label], i) => {
      const kind = i < active ? "done" : i === active ? "active" : "wait";
      return h("li", { class: kind }, svg(ICON[kind]), h("span", null, label));
    }));
  }

  // Ask the server about the running job until it ends. onTick(job) is called each round.
  async function waitJob(onTick) {
    for (;;) {
      let job;
      try { job = (await api("GET", "/api/job")).job; } catch (e) { await sleep(1500); continue; }
      if (!job) return { state: "gone" };
      if (onTick) onTick(job);
      if (job.state !== "running") return job;
      await sleep(job.stage === "read" || job.kind === "render" ? 700 : 350);
    }
  }

  async function watchJob(cfg) {
    state.watching = true;
    $("pg-cancel").disabled = false;
    $("pg-cancel").onclick = async () => { $("pg-cancel").disabled = true; try { await api("POST", "/api/cancel", {}); } catch (e) { showError(e); } };
    show("progress");
    drawProgress(cfg, { state: "running", stage: "start", frac: null });
    const job = await waitJob((j) => drawProgress(cfg, j));
    state.watching = false;
    if (job.state === "done") {
      if (cfg.kind === "render") {
        state.result = { aid: job.result.analysis, files: job.result.files, edits: cfg.edits };
        return go("#/done/" + job.result.analysis);
      }
      state.selRun = null;                                   // new analysis: preselect its best clips
      return go("#/review/" + job.result.analysis);
    }
    if (cfg.kind === "render" && state.a) await showMake(state.a.id).catch(() => showHome());
    else await showHome();
    if (job.state === "error") showError(new ApiErr(job.code || "other", job.message));
  }

  function resumeJob(job) {
    if (job.kind === "render") {
      return watchJob({ kind: "render", title: "Shorts ഉണ്ടാക്കുന്നു", sub: "", labels: ["ക്ലിപ്പുകൾ ഉണ്ടാക്കുന്നു"], edits: {} });
    }
    return watchJob({ kind: "analyze", link: job.stage === "download", title: "വിശകലനം നടക്കുന്നു", sub: "" });
  }

  // ------------------------------------------------------------------ 3. review
  const BROWSER_OK = new Set([".mp4", ".m4v", ".webm", ".mov"]);

  async function showReview(id, opts = {}) {
    if (!state.app) await ensureState();
    const a = await api("GET", "/api/analysis/" + id);
    state.a = a;
    if (state.selRun !== a.run) { state.sel = new Set(a.clips.slice(0, 3).map((c) => c.id)); state.selRun = a.run; state.shown = 6; }
    show("review", { title: a.name, info: `${a.clips.length} ക്ലിപ്പ് കണ്ടെത്തി`, back: "ഹോം", onBack: () => go("#/") });
    $("sens").value = a.sensitivity;
    $("presets").replaceChildren(...state.app.presets.map((p) =>
      h("button", { type: "button", class: "chip", "aria-pressed": a.preset === p ? "true" : "false", onclick: () => redetect(p, Number($("sens").value)) }, PRESET[p] || p)));
    const warn = $("rv-warn");
    if (!a.source_ok) { warn.textContent = ERR.source_missing + " ക്ലിപ്പുകൾ ഉണ്ടാക്കാൻ ഫയൽ വേണം."; warn.hidden = false; }
    else if (!BROWSER_OK.has(a.ext)) { warn.textContent = "ഈ ഫോർമാറ്റ് ബ്രൗസറിൽ പ്ലേ ആകണമെന്നില്ല. ക്ലിപ്പ് ഉണ്ടാക്കുന്നതിന് പ്രശ്നമില്ല, ഉണ്ടാക്കിയ ശേഷം കാണാം."; warn.hidden = false; }
    else warn.hidden = true;
    $("rv-empty").hidden = a.clips.length > 0;
    drawGrid();
    if (opts.keepScroll) window.scrollTo(0, opts.keepScroll);
  }

  function drawGrid() {
    const a = state.a;
    const grid = $("grid");
    grid.replaceChildren(...a.clips.slice(0, state.shown).map(clipCard));
    const rest = a.clips.length - state.shown;
    $("more").hidden = rest <= 0;
    if (rest > 0) $("more").textContent = `കൂടുതൽ ${Math.min(6, rest)} ക്ലിപ്പുകൾ കാണിക്കുക`;
    updateCount();
  }
  $("more").addEventListener("click", () => { state.shown += 6; drawGrid(); });

  function clipCard(c) {
    const a = state.a;
    const on = state.sel.has(c.id);
    const label = `ക്ലിപ്പ് ${c.id}`;
    const img = h("img", { src: `/thumb/${a.id}/${c.id}.jpg?r=${a.run}`, alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.remove());
    const pick = h("button", { type: "button", class: "pick", "aria-pressed": on ? "true" : "false", "aria-label": `${label} തിരഞ്ഞെടുക്കുക`, onclick: () => toggle(c.id) },
      h("span", { class: "dot" }, svg(ICON.check)),
      h("span", { class: "time" }, `${fmt(c.start)} – ${fmt(c.end)}`));
    const play = h("button", { type: "button", class: "play", "aria-label": `${label} കണ്ടുനോക്കുക`, onclick: (ev) => startPreview(card, c, thumb, ev.currentTarget) }, svg(ICON.play));
    const thumb = h("div", { class: "thumb" }, img, pick, a.source_ok ? play : null);
    const tags = c.tags.map((t) => h("span", { class: "tag" }, state.app.detectors[t] || t));
    if (c.flags.includes("menu_like")) tags.push(h("span", { class: "tag warn" }, "മെനു പോലെ തോന്നുന്നു"));
    const card = h("div", { class: "clip" + (on ? " sel" : ""), "data-id": c.id },
      thumb,
      h("div", { class: "head" }, h("b", null, label), h("span", null, `സാധ്യത: ${LEVEL[c.level]}`)),
      h("div", { class: "bar", "aria-hidden": "true" }, h("i", { vars: { "--w": Math.max(4, c.pct) + "%" } })),
      h("div", { class: "tags" }, tags));
    return card;
  }

  function toggle(id) {
    if (state.sel.has(id)) state.sel.delete(id); else state.sel.add(id);
    const card = document.querySelector(`.clip[data-id="${id}"]`);
    if (card) {
      card.classList.toggle("sel", state.sel.has(id));
      const b = card.querySelector(".pick");
      if (b) b.setAttribute("aria-pressed", state.sel.has(id) ? "true" : "false");
    }
    updateCount();
  }
  function updateCount() {
    const n = state.a ? state.a.clips.filter((c) => state.sel.has(c.id)).length : 0;
    const el = $("rv-count");
    el.setAttribute("aria-live", "polite");
    el.textContent = `${n} തിരഞ്ഞെടുത്തു`;
    $("rv-go").disabled = n === 0 || !state.a || !state.a.source_ok;
  }
  $("rv-go").addEventListener("click", () => { if (state.a) go("#/make/" + state.a.id); });

  function stopPreview() {
    const p = state.preview;
    if (!p) return;
    state.preview = null;
    p.video.pause();
    p.video.removeAttribute("src");
    p.video.load();
    p.video.remove();
    p.close.remove();
    for (const el of p.hidden) el.hidden = false;
  }
  function startPreview(card, c, thumb, playBtn) {
    stopPreview();
    const a = state.a;
    const v = h("video", { controls: true, playsinline: true, src: `/media/${a.id}#t=${c.start},${c.end}` });
    v.addEventListener("timeupdate", () => { if (v.currentTime >= c.end - 0.1) v.pause(); });
    v.addEventListener("error", () => {
      stopPreview();
      showError({ code: "other", detail: "" });
      $("alert").firstChild.textContent = "ഈ വീഡിയോ ബ്രൗസറിൽ പ്ലേ ചെയ്യാൻ കഴിയുന്നില്ല. ക്ലിപ്പ് ഉണ്ടാക്കിയാൽ ഫലം കാണാം.";
    });
    const close = h("button", { type: "button", class: "x", "aria-label": "പ്രിവ്യൂ അടയ്ക്കുക", onclick: stopPreview }, "✕");
    const hidden = [...thumb.children].filter((el) => el.tagName === "BUTTON");
    for (const el of hidden) el.hidden = true;
    thumb.append(v, close);
    state.preview = { video: v, close, hidden };
    v.play().catch(() => {});
    close.focus();
  }

  async function redetect(preset, sens) {
    const a = state.a;
    if (!a) return;
    const y = window.scrollY;
    $("rv-busy").hidden = false;
    for (const b of document.querySelectorAll("#presets button, #sens")) b.disabled = true;
    try {
      await api("POST", "/api/redetect", { id: a.id, preset, sensitivity: sens });
      const job = await waitJob();
      if (job.state === "error") throw new ApiErr(job.code || "other", job.message);
      state.selRun = null;
      await showReview(a.id, { keepScroll: y });
    } catch (e) {
      showError(e);
      for (const b of document.querySelectorAll("#presets button, #sens")) b.disabled = false;
    } finally { $("rv-busy").hidden = true; }
  }
  let sensTimer = null;
  $("sens").addEventListener("input", () => {
    clearTimeout(sensTimer);
    sensTimer = setTimeout(() => { if (state.a) redetect(state.a.preset, Number($("sens").value)); }, 600);
  });

  // ------------------------------------------------------------------ 4. make
  const TITLE_BY_TAG = [["slow_motion", "സ്ലോ-മോഷൻ നിമിഷം"], ["quiet_then_burst", "ക്ലച്ച് നിമിഷം"], ["voice_excitement", "ആർപ്പ് നിമിഷം"]];
  function defaultTitle(c) {
    const hit = TITLE_BY_TAG.find(([t]) => c.tags.includes(t));
    return `${hit ? hit[1] : "ആക്ഷൻ നിമിഷം"} #${c.id}`;
  }
  const chosen = () => state.a.clips.filter((c) => state.sel.has(c.id));
  const GAP = 3, MAXLEN = 180;

  async function showMake(id) {
    if (!state.a || state.a.id !== id) await showReview(id);
    const a = state.a;
    const list = chosen();
    if (!list.length) return go("#/review/" + id);
    if (state.editsRun !== a.run) { state.edits = {}; state.editsRun = a.run; }
    for (const c of list) if (!state.edits[c.id]) state.edits[c.id] = { start: c.start, end: c.end, layout: "blur", crop_x: 0.5, title: defaultTitle(c) };
    if (!list.some((c) => c.id === state.cur)) state.cur = list[0].id;
    show("make", { back: "ക്ലിപ്പുകളിലേക്ക്", onBack: () => go("#/review/" + id) });
    $("mk-h1").textContent = `${list.length} Shorts ഉണ്ടാക്കാം`;
    $("mk-lowres").hidden = !a.low_res;
    $("mk-go").textContent = "";
    $("mk-go").append(`${list.length} Shorts ഉണ്ടാക്കുക `, h("span", { "aria-hidden": "true" }, "→"));
    const v = $("ph-video");
    if (!v.dataset.aid || v.dataset.aid !== a.id) { v.dataset.aid = a.id; v.src = `/media/${a.id}`; }
    drawMake();
  }

  function window_(c) { const a = state.a; return [Math.max(0, c.start - 30), Math.min(a.duration, c.end + 30)]; }

  function drawMake() {
    const a = state.a, list = chosen();
    const c = list.find((x) => x.id === state.cur), e = state.edits[c.id];
    $("mk-tabs").replaceChildren(...list.map((x) => h("button", { type: "button", class: "chip", "aria-pressed": x.id === state.cur ? "true" : "false",
      onclick: () => { state.cur = x.id; pausePreview(); drawMake(); } }, `ക്ലിപ്പ് ${x.id}`)));
    const defs = [["blur", "ബ്ലർ പശ്ചാത്തലം", "ശുപാർശ"], ["crop", "ക്രോപ്പ്", "സ്ക്രീൻ നിറയും"], ["cam", "ഫേസ്‌ക്യാം + ഗെയിം", "ഉടൻ വരും"]];
    $("layouts").replaceChildren(...defs.map(([k, name, note]) => h("button", { type: "button", class: "layout", "aria-pressed": e.layout === k ? "true" : "false", disabled: k === "cam",
      onclick: () => { e.layout = k; drawMake(); } },
      h("span", { class: "mini " + k }), h("span", { class: "nm" }, h("b", null, name), h("span", null, note)))));
    $("crop-row").hidden = e.layout !== "crop";
    $("cropx").value = Math.round(e.crop_x * 100);
    const [lo, hi] = window_(c);
    for (const id of ["trim-a", "trim-b"]) { $(id).min = lo; $(id).max = hi; }
    $("trim-a").value = e.start; $("trim-b").value = e.end;
    $("title").value = e.title;
    const peak = $("trim-peak");
    peak.hidden = !(c.peak >= lo && c.peak <= hi);
    peak.style.setProperty("--p", ((c.peak - lo) / (hi - lo)) * 100 + "%");
    const bg = $("ph-bg");
    bg.hidden = false;
    bg.onerror = () => { bg.hidden = true; };
    bg.src = `/thumb/${a.id}/${c.id}.jpg?r=${a.run}`;
    paintMake();
    const v = $("ph-video");
    const seek = () => { try { v.currentTime = e.start; } catch (err) { /* not ready yet */ } };
    if (v.readyState >= 1) seek(); else v.addEventListener("loadedmetadata", seek, { once: true });
  }

  function paintMake() {
    const c = chosen().find((x) => x.id === state.cur), e = state.edits[c.id];
    const [lo, hi] = window_(c);
    const span = hi - lo;
    $("trim-fill").style.setProperty("--l", ((e.start - lo) / span) * 100 + "%");
    $("trim-fill").style.setProperty("--w", ((e.end - e.start) / span) * 100 + "%");
    $("trim-text").textContent = `${fmt(e.start)} – ${fmt(e.end)} · ${Math.round(e.end - e.start)} സെക്കൻഡ്`;
    $("trim-a").setAttribute("aria-valuetext", fmt(e.start));
    $("trim-b").setAttribute("aria-valuetext", fmt(e.end));
    $("phone").classList.toggle("crop", e.layout === "crop");
    $("phone").style.setProperty("--cx", Math.round(e.crop_x * 100) + "%");
    $("ph-title").textContent = e.title;
  }

  function onTrim(which) {
    const c = chosen().find((x) => x.id === state.cur), e = state.edits[c.id];
    const [lo, hi] = window_(c);
    let a = Number($("trim-a").value), b = Number($("trim-b").value);
    if (which === "a") a = Math.min(a, b - GAP); else b = Math.max(b, a + GAP);
    a = Math.max(lo, a); b = Math.min(hi, b);
    if (b - a > MAXLEN) { if (which === "a") a = b - MAXLEN; else b = a + MAXLEN; }
    e.start = a; e.end = b;
    $("trim-a").value = a; $("trim-b").value = b;
    paintMake();
    const v = $("ph-video");
    if (v.paused) { try { v.currentTime = which === "a" ? a : Math.max(a, b - 1); } catch (err) { /* ignore */ } }
  }
  $("trim-a").addEventListener("input", () => onTrim("a"));
  $("trim-b").addEventListener("input", () => onTrim("b"));
  $("cropx").addEventListener("input", () => { state.edits[state.cur].crop_x = Number($("cropx").value) / 100; paintMake(); });
  $("title").addEventListener("input", () => { state.edits[state.cur].title = $("title").value; paintMake(); });

  function pausePreview() {
    const v = $("ph-video");
    v.pause();
    $("ph-play").textContent = "▶ ഈ ഭാഗം പ്ലേ ചെയ്യുക";
  }
  $("ph-play").addEventListener("click", () => {
    const v = $("ph-video"), e = state.edits[state.cur];
    if (!v.paused) return pausePreview();
    v.muted = false;
    if (v.currentTime < e.start || v.currentTime >= e.end - 0.2) v.currentTime = e.start;
    v.play().then(() => { $("ph-play").textContent = "⏸ നിർത്തുക"; }).catch(() => { $("ph-fail").hidden = false; });
  });
  $("ph-video").addEventListener("timeupdate", () => {
    const e = state.edits[state.cur], v = $("ph-video");
    if (e && v.currentTime >= e.end) { pausePreview(); v.currentTime = e.start; }
  });
  $("ph-video").addEventListener("error", () => { $("ph-fail").hidden = false; });
  $("ph-video").addEventListener("loadeddata", () => { $("ph-fail").hidden = true; });

  $("mk-go").addEventListener("click", async () => {
    const a = state.a, list = chosen();
    pausePreview();
    const items = list.map((c) => { const e = state.edits[c.id]; return { id: c.id, start: e.start, end: e.end, layout: e.layout, crop_x: e.crop_x, title: e.title }; });
    try {
      await api("POST", "/api/render", { id: a.id, run: a.run, items });
      watchJob({ kind: "render", title: `${list.length} Shorts ഉണ്ടാക്കുന്നു`, sub: "", labels: list.map((c) => state.edits[c.id].title), edits: JSON.parse(JSON.stringify(state.edits)) });
    } catch (e) { showError(e); }
  });

  // ------------------------------------------------------------------ 5. done
  async function showDone(id) {
    const r = state.result;
    if (!r || r.aid !== id) return go("#/review/" + id);
    show("done");
    $("dn-h1").textContent = `${r.files.length} Shorts തയ്യാർ`;
    $("dn-path").hidden = true;
    $("dn-list").replaceChildren(...r.files.map((f) => {
      const e = r.edits[f.id] || {};
      const title = e.title || f.file;
      const meta = `ക്ലിപ്പ് ${f.id}` + (e.end ? ` · ${Math.round(e.end - e.start)} സെക്കൻഡ്` : "") + ` · ${fmtSize(f.size)}`;
      return h("li", { class: "item" }, h("div", { class: "t" }, h("b", null, title), h("span", null, meta)),
        h("button", { type: "button", class: "ghost", "aria-label": `${title} കാണുക`, onclick: () => openPlayer(id, f.file, title) }, "കാണുക"));
    }));
    $("dn-folder").onclick = async () => {
      try {
        const res = await api("POST", "/api/open-folder", { id });
        if (!res.opened) { $("dn-path").textContent = "ഫോൾഡർ തുറക്കാൻ കഴിഞ്ഞില്ല. ഇവിടെ നോക്കുക: " + res.path; $("dn-path").hidden = false; }
      } catch (e) { showError(e); }
    };
  }
  $("dn-home").addEventListener("click", () => go("#/"));

  function openPlayer(aid, file, title) {
    $("player-title").textContent = title;
    $("player-video").src = `/clip/${aid}/${encodeURIComponent(file)}`;
    $("player").showModal();
  }
  function closePlayer() { $("player-video").pause(); $("player-video").removeAttribute("src"); $("player-video").load(); if ($("player").open) $("player").close(); }
  $("player-close").addEventListener("click", closePlayer);
  $("player").addEventListener("close", () => { $("player-video").pause(); });

  // ------------------------------------------------------------------ settings
  $("open-settings").addEventListener("click", async () => {
    try { await ensureState(); } catch (e) { return showError(e); }
    const s = state.app, t = s.tools;
    const yn = (ok, yes, no) => h("span", { class: ok ? "ok" : "no" }, ok ? "✓ " + yes : "✗ " + no);
    $("kv").replaceChildren(
      h("dt", null, "ffmpeg"), h("dd", null, yn(t.ffmpeg && t.ffprobe, "ഉണ്ട്", "ഇല്ല - ഇൻസ്റ്റാൾ ചെയ്യണം")),
      h("dt", null, "yt-dlp (ലിങ്ക്)"), h("dd", null, yn(t.ytdlp, "ഉണ്ട്", "ഇല്ല - ലിങ്ക് ഉപയോഗിക്കണമെങ്കിൽ മാത്രം വേണം")),
      h("dt", null, "ഫയൽ ഡയലോഗ്"), h("dd", null, yn(t.picker, "ഉണ്ട് (കോപ്പി ഇല്ലാതെ)", "ഇല്ല - ബ്രൗസർ വഴി കോപ്പി ചെയ്യും")),
      h("dt", null, "വീഡിയോ ഫോൾഡർ"), h("dd", null, s.video_dir),
      h("dt", null, "എല്ലാ ഫയലുകളും"), h("dd", null, s.data_dir),
      h("dt", null, "പതിപ്പ്"), h("dd", null, s.version));
    $("st-quit").textContent = "ആപ്പ് അടയ്ക്കുക";
    $("settings").showModal();
  });
  $("settings-close").addEventListener("click", () => $("settings").close());
  $("st-folder").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/open-folder", {});
      if (!r.opened) showError({ code: "other", detail: "" }), ($("alert").firstChild.textContent = "ഫോൾഡർ തുറക്കാൻ കഴിഞ്ഞില്ല: " + r.path);
    } catch (e) { showError(e); }
  });
  $("st-update").addEventListener("click", async () => {
    const btn = $("st-update"), msg = $("st-update-msg");
    btn.disabled = true;
    msg.hidden = false;
    msg.textContent = "yt-dlp അപ്ഡേറ്റ് ചെയ്യുന്നു… (ഏതാനും മിനിറ്റ് എടുത്തേക്കാം)";
    try {
      const r = await api("POST", "/api/update-ytdlp", {});
      if (!r.ok) msg.textContent = "അപ്ഡേറ്റ് ചെയ്യാൻ കഴിഞ്ഞില്ല (ഇന്റർനെറ്റ് ഇല്ലാത്തതോ, ഫോൾഡറിൽ എഴുതാൻ അനുമതി ഇല്ലാത്തതോ ആകാം). പഴയ പതിപ്പ് അങ്ങനെ തന്നെ ഉപയോഗിക്കാം.";
      else if (r.changed) msg.textContent = "yt-dlp പുതിയ പതിപ്പ് " + r.after + " ആയി (മുമ്പ് " + (r.before || "?") + "). ലിങ്ക് ഡൗൺലോഡ് വീണ്ടും ശ്രമിക്കാം.";
      else msg.textContent = "yt-dlp പതിപ്പ് " + r.after + " തന്നെയാണ്; ഇതിലും പുതിയത് കിട്ടിയില്ല.";
    } catch (e) {
      msg.textContent = e && e.code === "busy" ? ERR.busy : (ERR[e && e.code] || "അപ്ഡേറ്റ് ചെയ്യാൻ കഴിഞ്ഞില്ല.");
    } finally { btn.disabled = false; }
  });
  let quitArmed = false;
  $("st-quit").addEventListener("click", async () => {
    if (!quitArmed) { quitArmed = true; $("st-quit").textContent = "ശരിക്കും അടയ്ക്കണോ? ഒരിക്കൽ കൂടി അമർത്തുക"; return; }
    try { await api("POST", "/api/quit", {}); } catch (e) { /* the server is going away */ }
    document.body.replaceChildren(h("p", { class: "screen" }, "clipfinder അടച്ചു. ഈ ടാബ് അടയ്ക്കാം."));
  });
  $("settings").addEventListener("close", () => { quitArmed = false; $("st-update-msg").hidden = true; $("cn-msg").hidden = true; $("rm-msg").hidden = true; });

  // ------------------------------------------------------------------ remote access (Tailscale)
  async function loadRemote() {
    const msg = $("rm-msg");
    try {
      const r = await api("GET", "/api/remote");
      $("rm-host").value = r.host || "";
      $("rm-cmd").value = r.command;
      $("rm-linkbox").hidden = !r.link;
      $("rm-link").value = r.link || "";
      msg.hidden = false;
      msg.textContent = r.active ? "റിമോട്ട് ആക്സസ് ഓണാണ്: " + r.active
                      : r.host ? "സേവ് ചെയ്തു. ClipFinder അടച്ച് വീണ്ടും തുറന്നാൽ പ്രവർത്തനക്ഷമമാകും." : "ഇപ്പോൾ ഓഫാണ് (ഈ PC-യിൽ മാത്രം).";
    } catch (e) { msg.hidden = false; msg.textContent = ERR[e && e.code] || "വിവരങ്ങൾ എടുക്കാൻ കഴിഞ്ഞില്ല."; }
  }
  $("remote").addEventListener("toggle", () => { if ($("remote").open) loadRemote(); });
  $("rm-save").addEventListener("click", async () => {
    const msg = $("rm-msg");
    try {
      await api("POST", "/api/remote", { host: $("rm-host").value.trim() });
      await loadRemote();
    } catch (e) { msg.hidden = false; msg.textContent = ERR[e && e.code] || "സേവ് ചെയ്യാൻ കഴിഞ്ഞില്ല."; }
  });
  $("rm-copy").addEventListener("click", async () => {
    const box = $("rm-link"), msg = $("rm-msg");
    box.focus(); box.select(); msg.hidden = false;
    try { await navigator.clipboard.writeText(box.value); msg.textContent = "പകർത്തി."; }
    catch (e) { msg.textContent = "ടെക്സ്റ്റ് തിരഞ്ഞെടുത്തിട്ടുണ്ട്, Ctrl+C അമർത്തുക."; }
  });

  // ------------------------------------------------------------------ Claude connector
  let connectorInfo = null;
  const quoteArg = (a) => (/[\s"]/.test(a) ? '"' + a.replace(/"/g, '\\"') + '"' : a);
  function showConnector() {
    if (!connectorInfo) return;
    const args = connectorInfo.args.concat($("cn-images").checked ? ["--images"] : []);
    $("cn-json").value = JSON.stringify({ mcpServers: { clipfinder: { command: connectorInfo.command, args } } }, null, 2);
    $("cn-cmd").value = "claude mcp add clipfinder -- " + [connectorInfo.command].concat(args).map(quoteArg).join(" ");
  }
  async function loadConnector() {
    try { connectorInfo = await api("GET", "/api/connector"); showConnector(); } catch (e) { connectorInfo = null; }
  }
  $("connect").addEventListener("toggle", () => { if ($("connect").open && !connectorInfo) loadConnector(); });
  $("cn-images").addEventListener("change", showConnector);
  async function copyFrom(id) {
    const box = $(id), msg = $("cn-msg");
    msg.hidden = false;
    box.focus(); box.select();
    try { await navigator.clipboard.writeText(box.value); msg.textContent = "പകർത്തി."; }
    catch (e) { msg.textContent = "സ്വയം പകർത്താൻ കഴിഞ്ഞില്ല; ടെക്സ്റ്റ് തിരഞ്ഞെടുത്തിട്ടുണ്ട്, Ctrl+C അമർത്തുക."; }
  }
  $("cn-copy").addEventListener("click", () => copyFrom("cn-json"));
  $("cn-copy2").addEventListener("click", () => copyFrom("cn-cmd"));
  $("cn-test").addEventListener("click", async () => {
    const btn = $("cn-test"), msg = $("cn-msg");
    btn.disabled = true; msg.hidden = false; msg.textContent = "പരീക്ഷിക്കുന്നു…";
    try {
      const r = await api("POST", "/api/connector-test", {});
      msg.textContent = r.ok ? "ശരിയായി പ്രവർത്തിക്കുന്നു: Claude-ന് " + r.tools + " ടൂളുകൾ കിട്ടും. (Claude-ൽ ചേർത്ത ശേഷം അവിടെ ടൂളുകൾ കാണുന്നുണ്ടോ എന്ന് നോക്കുക.)"
                       : "പരീക്ഷണം പരാജയപ്പെട്ടു. ആപ്പ് വീണ്ടും തുടങ്ങിയ ശേഷം ശ്രമിക്കുക.";
    } catch (e) { msg.textContent = ERR[e && e.code] || "പരീക്ഷണം പരാജയപ്പെട്ടു."; }
    finally { btn.disabled = false; }
  });

  // ------------------------------------------------------------------ start
  route();
})();
