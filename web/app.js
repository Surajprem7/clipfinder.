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

  // ------------------------------------------------------------------ texts (Malayalam and English)
  // Every word the page shows lives here. tr(key, ...values) gives it in the chosen language.
  const TEXT = {
    ml: {
      level: { high: "ഉയർന്നത്", mid: "ഇടത്തരം", low: "കുറഞ്ഞത്" },
      preset: { all: "എല്ലാം", action: "ആക്ഷൻ", slowmo: "സ്ലോ-മോഷൻ", clutch: "ക്ലച്ച്", voice: "ആർപ്പ്/ശബ്ദം" },
      det: {},   // Malayalam condition names come from the server
      err: {
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
        yt_not_set_up: "YouTube അപ്‌ലോഡ് സജ്ജമാക്കിയിട്ടില്ല. ക്രമീകരണങ്ങളിലെ ഘട്ടങ്ങൾ നോക്കുക.",
        yt_not_connected: "YouTube ബന്ധിപ്പിച്ചിട്ടില്ല. ക്രമീകരണങ്ങളിൽ \"ബന്ധിപ്പിക്കുക\" അമർത്തുക.",
        yt_reconnect: "YouTube ലോഗിൻ കാലഹരണപ്പെട്ടു. ക്രമീകരണങ്ങളിൽ വീണ്ടും \"ബന്ധിപ്പിക്കുക\" അമർത്തുക.",
        yt_auth_failed: "Google ലോഗിൻ പരാജയപ്പെട്ടു. വീണ്ടും ശ്രമിക്കുക.",
        yt_quota: "YouTube-ന്റെ ഇന്നത്തെ അപ്‌ലോഡ് പരിധി കഴിഞ്ഞു. നാളെ ശ്രമിക്കുക.",
        yt_upload_failed: "അപ്‌ലോഡ് പരാജയപ്പെട്ടു. ഇന്റർനെറ്റ് നോക്കി വീണ്ടും ശ്രമിക്കുക (വിശദാംശം ടെർമിനലിൽ).",
        yt_bad_request: "ശീർഷകമോ വിവരണമോ ശരിയല്ല.",
      },
      err_other: "എന്തോ പിശക് സംഭവിച്ചു.",
      close: "അടയ്ക്കുക",
      from_folder: (size) => `${size} · വീഡിയോ ഫോൾഡറിൽ നിന്ന്`,
      lib_line: (n, made) => `${n} ക്ലിപ്പ് കണ്ടെത്തി · ${made ? made + " ഉണ്ടാക്കി" : "ഒന്നും ഉണ്ടാക്കിയില്ല"}`,
      open: "തുറക്കുക",
      open_x: (name) => `${name} തുറക്കുക`,
      analysing_x: (label) => `${label} വിശകലനം ചെയ്യുന്നു`,
      copying: (name) => `${name} · കോപ്പി ചെയ്യുന്നു…`,
      youtube_link: "YouTube ലിങ്ക്",
      step_download: "ചെറിയ വീഡിയോ ഡൗൺലോഡ് ചെയ്യുന്നു (കണ്ടെത്താൻ മാത്രം)",
      step_read: "ശബ്ദവും സ്ക്രീനിലെ ചലനവും വായിക്കുന്നു",
      step_detect: "ക്ലിപ്പ് ആകാൻ സാധ്യതയുള്ള ഭാഗങ്ങൾ കണ്ടെത്തുന്നു",
      step_thumbs: "ചെറു ചിത്രങ്ങൾ ഉണ്ടാക്കുന്നു",
      eta_short: "ഒരു മിനിറ്റിൽ താഴെ ബാക്കി",
      eta_min: (m) => `ഏകദേശം ${m} മിനിറ്റ് ബാക്കി`,
      fetch: "തിരഞ്ഞെടുത്ത ഭാഗം 1080p-യിൽ ഡൗൺലോഡ് ചെയ്യുന്നു…",
      fetch_full: "ഭാഗം മാത്രം കിട്ടിയില്ല, മുഴുവൻ വീഡിയോ ഡൗൺലോഡ് ചെയ്യുന്നു (കൂടുതൽ സമയം എടുക്കും)…",
      note_render: "ഫയലിന്റെ നീളം അനുസരിച്ച് കുറച്ച് സമയം എടുക്കും.",
      note_analyze: "ഈ ജാലകം തുറന്ന് വയ്ക്കുക. ഒരിക്കൽ കഴിഞ്ഞാൽ അടുത്ത തവണ ഇത് ഉടൻ തുറക്കും.",
      making_shorts: "Shorts ഉണ്ടാക്കുന്നു",
      making_n: (n) => `${n} Shorts ഉണ്ടാക്കുന്നു`,
      making_clips: "ക്ലിപ്പുകൾ ഉണ്ടാക്കുന്നു",
      analysing: "വിശകലനം നടക്കുന്നു",
      n_found: (n) => `${n} ക്ലിപ്പ് കണ്ടെത്തി`,
      home: "ഹോം",
      need_file: " ക്ലിപ്പുകൾ ഉണ്ടാക്കാൻ ഫയൽ വേണം.",
      format_warn: "ഈ ഫോർമാറ്റ് ബ്രൗസറിൽ പ്ലേ ആകണമെന്നില്ല. ക്ലിപ്പ് ഉണ്ടാക്കുന്നതിന് പ്രശ്നമില്ല, ഉണ്ടാക്കിയ ശേഷം കാണാം.",
      show_more: (n) => `കൂടുതൽ ${n} ക്ലിപ്പുകൾ കാണിക്കുക`,
      clip_n: (n) => `ക്ലിപ്പ് ${n}`,
      select_x: (label) => `${label} തിരഞ്ഞെടുക്കുക`,
      preview_x: (label) => `${label} കണ്ടുനോക്കുക`,
      menu_like: "മെനു പോലെ തോന്നുന്നു",
      chance: (lv) => `സാധ്യത: ${lv}`,
      n_selected: (n) => `${n} തിരഞ്ഞെടുത്തു`,
      cant_play: "ഈ വീഡിയോ ബ്രൗസറിൽ പ്ലേ ചെയ്യാൻ കഴിയുന്നില്ല. ക്ലിപ്പ് ഉണ്ടാക്കിയാൽ ഫലം കാണാം.",
      close_preview: "പ്രിവ്യൂ അടയ്ക്കുക",
      t_slow: "സ്ലോ-മോഷൻ നിമിഷം", t_clutch: "ക്ലച്ച് നിമിഷം", t_voice: "ആർപ്പ് നിമിഷം", t_action: "ആക്ഷൻ നിമിഷം",
      to_clips: "ക്ലിപ്പുകളിലേക്ക്",
      can_make: (n) => `${n} Shorts ഉണ്ടാക്കാം`,
      make_n: (n) => `${n} Shorts ഉണ്ടാക്കുക `,
      lay_blur: "ബ്ലർ പശ്ചാത്തലം", lay_blur_n: "ശുപാർശ",
      lay_crop: "ക്രോപ്പ്", lay_crop_n: "സ്ക്രീൻ നിറയും",
      sel_all: "എല്ലാം തിരഞ്ഞെടുക്കുക", sel_none: "ഒന്നും വേണ്ട",
      all_same: (n) => `ഈ ലേഔട്ട് എല്ലാ ${n} ക്ലിപ്പിനും`,
      all_same_done: "എല്ലാ ക്ലിപ്പിനും ഈ ലേഔട്ട് ആക്കി.",
      long_clip: "60 സെക്കൻഡിൽ കൂടുതൽ. ചെറിയ Shorts ആണ് സാധാരണ കൂടുതൽ പേർ മുഴുവൻ കാണുന്നത്.",
      copy_title: "ശീർഷകം പകർത്തുക",
      copy_title_x: (t) => `${t}: ശീർഷകം പകർത്തുക`,
      title_copied: "പകർത്തി",
      link_fixed: "ബോക്സിൽ ഒന്നിലധികം ലിങ്ക് ഉണ്ടായിരുന്നു; അവസാനത്തേത് ഉപയോഗിച്ചു.",
      yt_ok: "ബന്ധിപ്പിച്ചിരിക്കുന്നു. അവസാന സ്ക്രീനിൽ ഓരോ ക്ലിപ്പിനും \"YouTube-ലേക്ക് അപ്‌ലോഡ്\" ബട്ടൺ കാണാം.",
      yt_need_file: "youtube_client.json ഫയൽ ഡാറ്റ ഫോൾഡറിൽ ഇതുവരെ ഇല്ല (മുകളിലെ ഘട്ടങ്ങൾ നോക്കുക).",
      yt_ready: "ഫയൽ കിട്ടി. ഇനി \"ബന്ധിപ്പിക്കുക\" അമർത്തുക.",
      yt_wait: "തുറന്ന ബ്രൗസർ ടാബിൽ Google-ൽ ലോഗിൻ ചെയ്ത് അനുവദിക്കുക…",
      yt_remote: "ബന്ധിപ്പിക്കുന്നത് PC-യിൽ നേരിട്ട് മാത്രം.",
      yt_btn: "YouTube-ലേക്ക് അപ്‌ലോഡ്",
      yt_btn_x: (t) => `${t}: YouTube-ലേക്ക് അപ്‌ലോഡ്`,
      yt_uploading: "YouTube-ലേക്ക് അപ്‌ലോഡ് ചെയ്യുന്നു",
      yt_step: "വീഡിയോ അയയ്ക്കുന്നു",
      yt_done: (p) => `അപ്‌ലോഡ് ആയി (${p})`,
      yt_studio: "Studio-ൽ തുറക്കുക",
      yt_need_title: "ശീർഷകം വേണം.",
      yt_gameplay: "ഗെയിംപ്ലേ ഹൈലൈറ്റ്",
      lay_follow: "ചലനം പിന്തുടരുക", lay_follow_n: "ക്രോപ്പ് ആക്ഷന്റെ കൂടെ നീങ്ങും",
      follow_note: "ക്ലിപ്പ് ഉണ്ടാക്കുമ്പോൾ ക്രോപ്പ് സ്ക്രീനിൽ ചലനം ഉള്ള ഭാഗത്തെ പിന്തുടരും. ഇവിടെ കാണുന്നത് നടുഭാഗം മാത്രം; ശരിയായ ഫലം ഉണ്ടാക്കിയ ക്ലിപ്പിൽ കാണാം.",
      lay_cam: "ഫേസ്‌ക്യാം + ഗെയിം", lay_cam_n: "ഉടൻ വരും",
      secs: (n) => `${n} സെക്കൻഡ്`,
      play_part: "▶ ഈ ഭാഗം പ്ലേ ചെയ്യുക",
      stop: "⏸ നിർത്തുക",
      n_ready: (n) => `${n} Shorts തയ്യാർ`,
      watch: "കാണുക",
      watch_x: (t) => `${t} കാണുക`,
      folder_fail_here: (p) => "ഫോൾഡർ തുറക്കാൻ കഴിഞ്ഞില്ല. ഇവിടെ നോക്കുക: " + p,
      folder_fail: (p) => "ഫോൾഡർ തുറക്കാൻ കഴിഞ്ഞില്ല: " + p,
      have: "ഉണ്ട്",
      ff_no: "ഇല്ല - ഇൻസ്റ്റാൾ ചെയ്യണം",
      yt_label: "yt-dlp (ലിങ്ക്)",
      yt_no: "ഇല്ല - ലിങ്ക് ഉപയോഗിക്കണമെങ്കിൽ മാത്രം വേണം",
      picker_label: "ഫയൽ ഡയലോഗ്",
      picker_yes: "ഉണ്ട് (കോപ്പി ഇല്ലാതെ)",
      picker_no: "ഇല്ല - ബ്രൗസർ വഴി കോപ്പി ചെയ്യും",
      video_folder: "വീഡിയോ ഫോൾഡർ",
      all_files: "എല്ലാ ഫയലുകളും",
      version: "പതിപ്പ്",
      quit: "ആപ്പ് അടയ്ക്കുക",
      quit_sure: "ശരിക്കും അടയ്ക്കണോ? ഒരിക്കൽ കൂടി അമർത്തുക",
      closed: "clipfinder അടച്ചു. ഈ ടാബ് അടയ്ക്കാം.",
      yt_updating: "yt-dlp അപ്ഡേറ്റ് ചെയ്യുന്നു… (ഏതാനും മിനിറ്റ് എടുത്തേക്കാം)",
      yt_fail: "അപ്ഡേറ്റ് ചെയ്യാൻ കഴിഞ്ഞില്ല (ഇന്റർനെറ്റ് ഇല്ലാത്തതോ, ഫോൾഡറിൽ എഴുതാൻ അനുമതി ഇല്ലാത്തതോ ആകാം). പഴയ പതിപ്പ് അങ്ങനെ തന്നെ ഉപയോഗിക്കാം.",
      yt_new: (a, b) => "yt-dlp പുതിയ പതിപ്പ് " + a + " ആയി (മുമ്പ് " + b + "). ലിങ്ക് ഡൗൺലോഡ് വീണ്ടും ശ്രമിക്കാം.",
      yt_same: (a) => "yt-dlp പതിപ്പ് " + a + " തന്നെയാണ്; ഇതിലും പുതിയത് കിട്ടിയില്ല.",
      yt_fail_short: "അപ്ഡേറ്റ് ചെയ്യാൻ കഴിഞ്ഞില്ല.",
      rm_on: (h) => "റിമോട്ട് ആക്സസ് ഓണാണ്: " + h,
      rm_saved: "സേവ് ചെയ്തു. ClipFinder അടച്ച് വീണ്ടും തുറന്നാൽ പ്രവർത്തനക്ഷമമാകും.",
      rm_off: "ഇപ്പോൾ ഓഫാണ് (ഈ PC-യിൽ മാത്രം).",
      load_fail: "വിവരങ്ങൾ എടുക്കാൻ കഴിഞ്ഞില്ല.",
      save_fail: "സേവ് ചെയ്യാൻ കഴിഞ്ഞില്ല.",
      copied: "പകർത്തി.",
      copy_manual: "ടെക്സ്റ്റ് തിരഞ്ഞെടുത്തിട്ടുണ്ട്, Ctrl+C അമർത്തുക.",
      copy_manual2: "സ്വയം പകർത്താൻ കഴിഞ്ഞില്ല; ടെക്സ്റ്റ് തിരഞ്ഞെടുത്തിട്ടുണ്ട്, Ctrl+C അമർത്തുക.",
      testing: "പരീക്ഷിക്കുന്നു…",
      cn_ok: (n) => "ശരിയായി പ്രവർത്തിക്കുന്നു: Claude-ന് " + n + " ടൂളുകൾ കിട്ടും. (Claude-ൽ ചേർത്ത ശേഷം അവിടെ ടൂളുകൾ കാണുന്നുണ്ടോ എന്ന് നോക്കുക.)",
      cn_fail: "പരീക്ഷണം പരാജയപ്പെട്ടു. ആപ്പ് വീണ്ടും തുടങ്ങിയ ശേഷം ശ്രമിക്കുക.",
      cn_fail_short: "പരീക്ഷണം പരാജയപ്പെട്ടു.",
      page: {},   // the Malayalam page text is in index.html itself
    },
    en: {
      level: { high: "High", mid: "Medium", low: "Low" },
      preset: { all: "All", action: "Action", slowmo: "Slow-mo", clutch: "Clutch", voice: "Shouts/voice" },
      det: {
        loud_peak: "Sound jumps", motion_burst: "Motion jumps", quiet_then_burst: "Calm → burst",
        voice_excitement: "Voice/shout (speech band)", slow_motion: "Possible slow-mo",
      },
      err: {
        no_ffmpeg: "ffmpeg is not installed. See the install steps in README.",
        no_ytdlp: "yt-dlp is missing (needed for links). Run the setup again, or in a terminal: pip install yt-dlp",
        bad_link: "This is not a valid YouTube link (https://www.youtube.com/… or https://youtu.be/…).",
        too_short: "The video is shorter than 10 seconds.",
        unreadable: "Could not read this file (it may be damaged or not a video).",
        download_failed: "Download failed. Check that the link is right and the video is public or unlisted.",
        source_missing: "The video file is missing (it may have been moved or deleted).",
        stale: "The list has changed. Open the clips again.",
        busy: "Another job is running. Please wait until it finishes.",
        local_only: "This can only be done on the PC itself.",
        bad_host_name: "This is not a Tailscale name. Example: pc.tail1234.ts.net",
        bad_path: "The file path is not valid.",
        file_missing: "Could not find this file. Check the path.",
        bad_type: "This is not a video file (only mp4, mkv, mov, webm…).",
        no_space: "There is not enough free space on the computer for this.",
        too_large: "The file is too large.",
        incomplete: "The file did not arrive completely. Please try again.",
        damaged: "The saved analysis is damaged. Analyse the video again.",
        not_found: "Could not find this.",
        network: "Could not reach the app. Check that clipfinder is running.",
        no_session: "Not allowed. Open the link shown in the terminal again.",
        internal: "Something went wrong. Details are in the terminal.",
        yt_not_set_up: "YouTube upload is not set up. See the steps in Settings.",
        yt_not_connected: "YouTube is not connected. Press \"Connect\" in Settings.",
        yt_reconnect: "The YouTube sign-in has expired. Press \"Connect\" in Settings again.",
        yt_auth_failed: "Google sign-in failed. Please try again.",
        yt_quota: "YouTube's upload limit for today is used up. Try tomorrow.",
        yt_upload_failed: "The upload failed. Check the internet and try again (details in the terminal).",
        yt_bad_request: "The title or description is not valid.",
      },
      err_other: "Something went wrong.",
      close: "Close",
      from_folder: (size) => `${size} · from the video folder`,
      lib_line: (n, made) => `${n} clips found · ${made ? made + " made" : "none made"}`,
      open: "Open",
      open_x: (name) => `Open ${name}`,
      analysing_x: (label) => `Analysing ${label}`,
      copying: (name) => `${name} · copying…`,
      youtube_link: "YouTube link",
      step_download: "Downloading a small copy (only for finding clips)",
      step_read: "Reading the sound and on-screen motion",
      step_detect: "Finding parts that could be clips",
      step_thumbs: "Making thumbnails",
      eta_short: "Less than a minute left",
      eta_min: (m) => `About ${m} min left`,
      fetch: "Downloading the chosen parts in 1080p…",
      fetch_full: "Could not get just the parts, downloading the whole video (takes longer)…",
      note_render: "Takes a little while, depending on the clip length.",
      note_analyze: "Keep this window open. Once done, it opens instantly next time.",
      making_shorts: "Making Shorts",
      making_n: (n) => `Making ${n} Shorts`,
      making_clips: "Making clips",
      analysing: "Analysing",
      n_found: (n) => `${n} clips found`,
      home: "Home",
      need_file: " The file is needed to make clips.",
      format_warn: "This format may not play in the browser. Making clips still works; you can watch them afterwards.",
      show_more: (n) => `Show ${n} more clips`,
      clip_n: (n) => `Clip ${n}`,
      select_x: (label) => `Select ${label}`,
      preview_x: (label) => `Preview ${label}`,
      menu_like: "Looks like a menu",
      chance: (lv) => `Chance: ${lv}`,
      n_selected: (n) => `${n} selected`,
      cant_play: "This video cannot play in the browser. Make the clip to see the result.",
      close_preview: "Close preview",
      t_slow: "Slow-mo moment", t_clutch: "Clutch moment", t_voice: "Hype moment", t_action: "Action moment",
      to_clips: "Back to clips",
      can_make: (n) => `Make ${n} Shorts`,
      make_n: (n) => `Make ${n} Shorts `,
      lay_blur: "Blurred background", lay_blur_n: "Recommended",
      lay_crop: "Crop", lay_crop_n: "Fills the screen",
      sel_all: "Select all", sel_none: "Select none",
      all_same: (n) => `Use this layout for all ${n} clips`,
      all_same_done: "All clips now use this layout.",
      long_clip: "Longer than 60 seconds. Shorter Shorts are usually watched to the end by more people.",
      copy_title: "Copy title",
      copy_title_x: (t) => `${t}: copy title`,
      title_copied: "Copied",
      link_fixed: "The box had more than one link; the last one was used.",
      yt_ok: "Connected. On the last screen every clip has an \"Upload to YouTube\" button.",
      yt_need_file: "The youtube_client.json file is not in the data folder yet (see the steps above).",
      yt_ready: "File found. Now press \"Connect\".",
      yt_wait: "Sign in to Google and allow access in the browser tab that opened…",
      yt_remote: "Connecting is only possible on the PC itself.",
      yt_btn: "Upload to YouTube",
      yt_btn_x: (t) => `${t}: upload to YouTube`,
      yt_uploading: "Uploading to YouTube",
      yt_step: "Sending the video",
      yt_done: (p) => `Uploaded (${p})`,
      yt_studio: "Open in Studio",
      yt_need_title: "A title is needed.",
      yt_gameplay: "gameplay highlight",
      lay_follow: "Follow the action", lay_follow_n: "The crop moves with the action",
      follow_note: "When the clip is made, the crop follows the part of the screen that is moving. The preview here shows only the middle; the real result is in the finished clip.",
      lay_cam: "Facecam + game", lay_cam_n: "Coming soon",
      secs: (n) => `${n} seconds`,
      play_part: "▶ Play this part",
      stop: "⏸ Stop",
      n_ready: (n) => `${n} Shorts ready`,
      watch: "Watch",
      watch_x: (t) => `Watch ${t}`,
      folder_fail_here: (p) => "Could not open the folder. Look here: " + p,
      folder_fail: (p) => "Could not open the folder: " + p,
      have: "Installed",
      ff_no: "Missing - must be installed",
      yt_label: "yt-dlp (links)",
      yt_no: "Missing - only needed for links",
      picker_label: "File dialog",
      picker_yes: "Available (no copying)",
      picker_no: "Not available - files are copied through the browser",
      video_folder: "Video folder",
      all_files: "All files",
      version: "Version",
      quit: "Close the app",
      quit_sure: "Really close? Press once more",
      closed: "clipfinder is closed. You can close this tab.",
      yt_updating: "Updating yt-dlp… (may take a few minutes)",
      yt_fail: "Could not update (no internet, or no permission to write to the folder). The old version still works.",
      yt_new: (a, b) => "yt-dlp updated to " + a + " (was " + b + "). Try the link download again.",
      yt_same: (a) => "yt-dlp is already " + a + "; no newer version found.",
      yt_fail_short: "Could not update.",
      rm_on: (h) => "Remote access is on: " + h,
      rm_saved: "Saved. Close and reopen ClipFinder to turn it on.",
      rm_off: "Off for now (this PC only).",
      load_fail: "Could not load the details.",
      save_fail: "Could not save.",
      copied: "Copied.",
      copy_manual: "The text is selected, press Ctrl+C.",
      copy_manual2: "Could not copy automatically; the text is selected, press Ctrl+C.",
      testing: "Testing…",
      cn_ok: (n) => "Works: Claude will get " + n + " tools. (After adding it in Claude, check that the tools show up there.)",
      cn_fail: "The test failed. Restart the app and try again.",
      cn_fail_short: "The test failed.",
      page: {
        settings: "Settings",
        home_h1: "Which stream do you want clips from?",
        new_badge: "New video found",
        new_go: "Analyse",
        empty_big: "Start here",
        empty_text: "Drop the stream video below, or put a file downloaded from YouTube Studio in this folder:",
        drop: "Drop a video file here",
        pick: "Choose a file",
        link_label: "Or paste a YouTube link",
        link_go: "Start",
        link_help: "Use only your own videos. You can also paste the path of a file on this computer.",
        lib_h2: "Previous streams",
        lib_empty: "Nothing analysed yet.",
        progress: "Progress",
        cancel: "Cancel",
        rv_h1: "Choose clips",
        rv_what: "What to find?",
        sens_more: "More clips",
        sens_precise: "More precise",
        updating: "Updating…",
        rv_empty_big: "No clips found with this setting.",
        rv_empty_text: 'Move the slider above towards "More clips", or choose "All".',
        sel_all: "Select all",
        sel_none: "Select none",
        yt_summary: "Upload straight to YouTube (optional)",
        yt_intro: "Once connected, a finished Short can be uploaded to your channel with one button. Only the clip you press \"Upload\" on goes to YouTube.",
        yt_step1: "In Google Cloud Console, turn on \"YouTube Data API v3\" in a project.",
        yt_step2: "Set up the OAuth consent screen and add your Google account under \"Test users\".",
        yt_step3: "Credentials → Create credentials → OAuth client ID → \"Desktop app\". Download the JSON file.",
        yt_step4a: "Rename that file to",
        yt_step4b: "and put it in the \"All files\" folder (shown above). Then press \"Connect\" here.",
        yt_connect: "Connect",
        yt_disconnect: "Disconnect",
        yt_token_note: "When connected, a Google sign-in token is kept in the data folder on this PC. It can only upload videos. Do not share that folder with anyone.",
        yt_dlg_title: "Upload to YouTube",
        yt_game: "Name of the game",
        yt_game_help: "With a name, the description and hashtags are filled in to match. You can change the description.",
        yt_desc: "Description",
        yt_privacy: "Who can watch",
        yt_private: "Private (only me; publish from Studio after checking)",
        yt_unlisted: "Unlisted (anyone with the link)",
        yt_public: "Public (everyone, right away)",
        yt_upload: "Upload",
        rv_help: "Click a picture to select it · press ▶ to preview",
        make_shorts: "Make Shorts",
        ph_fail: "This video cannot play in the browser. You can watch it after the clip is made.",
        ph_play: "▶ Play this part",
        ph_note: "How it will look in Shorts (9:16)",
        layout: "Layout",
        crop_left: "Game: left",
        crop_right: "right",
        length: "Length",
        start: "Start",
        end: "End",
        trim_help: "Drag the white ends to change the start and end (or use ← → on the keyboard) · black line = the most intense moment",
        title: "Title",
        title_help: "Used as the file name. Enter the title on YouTube when you upload.",
        lowres: "You are seeing a low-resolution copy. When making Shorts, only the chosen parts are downloaded from YouTube in 1080p.",
        saved_here: "Clips are saved on your computer",
        open_folder: "Open folder",
        open_studio: "Open YouTube's upload page",
        upload_note: "Opens YouTube's upload box and the clips folder. Drag the clip from the folder into the box.",
        to_home: "Home",
        close: "Close",
        privacy: "Nothing is sent to the internet on its own. A YouTube link makes yt-dlp download that video; and if you set up YouTube upload, only the clip you press Upload on is sent to YouTube.",
        open_video_folder: "Open video folder",
        update_ytdlp: "Update yt-dlp",
        quit: "Close the app",
        cn_summary: "Connect to Claude (optional)",
        cn_intro: "Once connected, just type in Claude, for example: “Find the best 5 moments in today’s stream and make Shorts”. Everything can still be done here without it.",
        cn_gets: "Claude gets: file names, clip times and scores, and where the clip folder is. Video files are not sent. Claude cannot read or delete anything outside this folder.",
        cn_images: "Let Claude see one picture (frame) of a clip — pictures are sent to Anthropic",
        cn_desktop1: "In Claude Desktop: Settings → Developer → Edit Config. Add this to the",
        cn_desktop2: "file that opens (if the file already has",
        cn_desktop3: ", add only the",
        cn_desktop4: "part inside it). Save, then fully close and reopen Claude Desktop.",
        cn_json: "Claude Desktop setting",
        copy: "Copy",
        cn_cmd: "Claude Code command",
        cn_test: "Test here",
        rm_summary: "Use from other devices — Tailscale (optional)",
        rm_intro: "While ClipFinder runs on the PC, you can open this same page from your phone or laptop. Nothing is opened to the public internet: only devices on your Tailscale account, and only with the secret link.",
        rm_step1: "Install Tailscale on the PC and the phone and sign in to the same account. In the Tailscale admin page (DNS), turn on MagicDNS and HTTPS certificates.",
        rm_step2a: "Enter the PC's Tailscale name below (",
        rm_step2code: "name.tailXXXX.ts.net",
        rm_step2b: ") and save. Close and reopen ClipFinder.",
        rm_step3a: "Run this command on the PC once (",
        rm_step3b: " only; do not use",
        rm_step3c: ", it opens to the public).",
        rm_step4: "Open the link shown below on your phone.",
        rm_host: "PC's Tailscale name",
        save: "Save",
        command: "Command:",
        rm_cmd: "Tailscale command",
        rm_link_note: "Your link (keep it secret, do not share it with anyone):",
        rm_link: "Remote link",
        copy_link: "Copy link",
      },
    },
  };
  const LANGS = ["ml", "en"];
  let lang = "ml";      // the saved choice arrives with /api/state (kept by the app in the data folder)
  function tr(key, ...args) {
    const v = key in TEXT[lang] ? TEXT[lang][key] : TEXT.ml[key];
    return typeof v === "function" ? v(...args) : v;
  }
  const table = (name) => new Proxy({}, { get: (_, k) => TEXT[lang][name][k] ?? TEXT.ml[name][k] });
  const LEVEL = table("level"), PRESET = table("preset"), ERR = table("err");

  // The page itself: Malayalam is written in index.html; the English is in TEXT.en.page.
  // Every marked element holds plain text only, so it is swapped with textContent.
  const ORIGINAL = new Map();
  function applyPageText() {
    document.documentElement.lang = lang;
    for (const el of document.querySelectorAll("[data-i]")) {
      if (!ORIGINAL.has(el)) ORIGINAL.set(el, el.textContent);
      const en = TEXT.en.page[el.dataset.i];
      el.textContent = lang === "en" && en != null ? en : ORIGINAL.get(el);
    }
    for (const el of document.querySelectorAll("[data-i-aria]")) {
      if (!ORIGINAL.has(el)) ORIGINAL.set(el, el.getAttribute("aria-label"));
      const en = TEXT.en.page[el.dataset.iAria];
      el.setAttribute("aria-label", lang === "en" && en != null ? en : ORIGINAL.get(el));
    }
    for (const l of LANGS) $("lang-" + l).setAttribute("aria-pressed", l === lang ? "true" : "false");
  }
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
    progress: null,     // [cfg, job] last drawn on the progress screen
    lastLayout: "blur", // the layout chosen last; new clips start with it
    uploaded: {},       // clip file name -> { video, privacy } after a YouTube upload
    game: "",           // game name typed last in the upload box
  };

  // ------------------------------------------------------------------ errors
  function clearError() { $("alert").hidden = true; $("alert").replaceChildren(); }
  function showError(e) {
    const code = e && e.code ? e.code : "other";
    const text = ERR[code] || tr("err_other");
    const box = $("alert");
    const msg = h("div", null, text);
    if (e && e.detail && SHOW_DETAIL.has(code)) msg.append(h("small", null, e.detail));
    box.replaceChildren(msg, h("button", { type: "button", "aria-label": tr("close"), onclick: clearError }, "✕"));
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
    if (LANGS.includes(state.app.lang) && state.app.lang !== lang) { lang = state.app.lang; applyPageText(); }
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
      $("new-meta").textContent = tr("from_folder", fmtSize(fresh.size));
      $("new-go").onclick = () => startAnalyze({ kind: "file", path: fresh.path }, base(fresh.name));
    }
    $("empty-card").hidden = !!(fresh || s.library.length);
    $("empty-folder").textContent = s.video_dir;
    const lib = $("lib");
    lib.replaceChildren(...s.library.map((it) => h("li", { class: "item" },
      h("div", { class: "t" }, h("b", null, it.name),
        h("span", null, tr("lib_line", it.clips, it.made))),
      h("button", { type: "button", class: "ghost", "aria-label": tr("open_x", it.name), onclick: () => go("#/review/" + it.id) }, tr("open")))));
    $("lib-empty").hidden = s.library.length > 0;
  }

  async function startAnalyze(body, label) {
    try {
      const r = await api("POST", "/api/analyze", body);
      if (r.existing) return go("#/review/" + r.existing);
      watchJob({ kind: "analyze", link: body.kind === "link", title: () => tr("analysing_x", typeof label === "function" ? label() : label), sub: "" });
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
    $("upload-text").textContent = tr("copying", file.name);
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
    let v = $("link").value.trim();
    if (!v) return $("link").focus();
    // A link pasted on top of an older, unfinished one (two addresses stuck together): use the last one.
    const at = v.toLowerCase().lastIndexOf("http");
    if (at > 0 && /^https?:\/\//i.test(v)) { v = v.slice(at); $("link").value = v; }
    if (/^https?:\/\//i.test(v)) startAnalyze({ kind: "link", url: v }, () => tr("youtube_link"));
    else startAnalyze({ kind: "file", path: v }, base(v));
  }
  $("link-go").addEventListener("click", submitLink);
  $("link").addEventListener("keydown", (e) => { if (e.key === "Enter") submitLink(); });
  $("link").addEventListener("focus", () => $("link").select());      // a paste then replaces what was there

  // ------------------------------------------------------------------ 2. progress (analysing and making)
  const STEPS_ANALYZE = [
    ["download", "step_download"],
    ["read", "step_read"],
    ["detect", "step_detect"],
    ["thumbs", "step_thumbs"],
  ];

  function etaText(s) {
    if (s == null) return "";
    if (s < 60) return tr("eta_short");
    return tr("eta_min", Math.ceil(s / 60));
  }

  function drawProgress(cfg, job) {
    let steps, active, pct = null;
    if (cfg.kind === "render" || cfg.kind === "upload") {
      steps = cfg.labels.map((l) => [null, typeof l === "function" ? l() : l]);
      const f = job.frac || 0;
      active = Math.min(steps.length - 1, Math.floor(f * steps.length));
      pct = Math.round(f * 100);
      if (job.state === "done") { active = steps.length; pct = 100; }
    } else {
      steps = STEPS_ANALYZE.filter(([k]) => cfg.link || k !== "download").map(([k, key]) => [k, tr(key)]);
      active = Math.max(0, steps.findIndex(([k]) => k === job.stage));
      const f = job.frac || 0;
      if (job.stage === "read") pct = Math.round(2 + f * 85);
      else if (job.stage === "detect") pct = 90;
      else if (job.stage === "thumbs") pct = Math.round(92 + f * 8);
      else if (job.stage === "download") pct = null;
      else pct = 1;
    }
    state.progress = [cfg, job];
    $("pg-title").textContent = typeof cfg.title === "function" ? cfg.title() : cfg.title;
    $("pg-sub").textContent = job.stage === "fetch" ? tr("fetch")
      : job.stage === "fetch_full" ? tr("fetch_full")
      : (cfg.sub || "");
    const bar = $("pg-bar");
    bar.classList.toggle("indet", pct == null);
    if (pct == null) { bar.removeAttribute("aria-valuenow"); $("pg-pct").textContent = ""; }
    else { bar.setAttribute("aria-valuenow", pct); $("pg-fill").style.setProperty("--w", pct + "%"); $("pg-pct").textContent = pct + "%"; }
    $("pg-eta").textContent = cfg.kind === "analyze" ? etaText(job.eta) : "";
    $("pg-note").textContent = cfg.kind === "render"
      ? tr("note_render")
      : tr("note_analyze");
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
    if (cfg.kind === "upload") {
      if (job.state === "done") state.uploaded[job.result.file] = { video: job.result.video, privacy: job.result.privacy };
      if (state.result) await showDone(state.result.aid); else await showHome();
      if (job.state === "error") showError(new ApiErr(job.code || "other", job.message));
      return;
    }
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
    if (job.kind === "upload") return watchJob({ kind: "upload", title: () => tr("yt_uploading"), sub: "", labels: [() => tr("yt_step")] });
    if (job.kind === "render") {
      return watchJob({ kind: "render", title: () => tr("making_shorts"), sub: "", labels: [() => tr("making_clips")], edits: {} });
    }
    return watchJob({ kind: "analyze", link: job.stage === "download", title: () => tr("analysing"), sub: "" });
  }

  // ------------------------------------------------------------------ 3. review
  const BROWSER_OK = new Set([".mp4", ".m4v", ".webm", ".mov"]);

  async function showReview(id, opts = {}) {
    if (!state.app) await ensureState();
    const a = await api("GET", "/api/analysis/" + id);
    state.a = a;
    if (state.selRun !== a.run) { state.sel = new Set(a.clips.slice(0, 3).map((c) => c.id)); state.selRun = a.run; state.shown = 6; }
    show("review", { title: a.name, info: tr("n_found", a.clips.length), back: tr("home"), onBack: () => go("#/") });
    $("sens").value = a.sensitivity;
    $("presets").replaceChildren(...state.app.presets.map((p) =>
      h("button", { type: "button", class: "chip", "aria-pressed": a.preset === p ? "true" : "false", onclick: () => redetect(p, Number($("sens").value)) }, PRESET[p] || p)));
    const warn = $("rv-warn");
    if (!a.source_ok) { warn.textContent = ERR.source_missing + tr("need_file"); warn.hidden = false; }
    else if (!BROWSER_OK.has(a.ext)) { warn.textContent = tr("format_warn"); warn.hidden = false; }
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
    if (rest > 0) $("more").textContent = tr("show_more", Math.min(6, rest));
    updateCount();
  }
  $("more").addEventListener("click", () => { state.shown += 6; drawGrid(); });

  function clipCard(c) {
    const a = state.a;
    const on = state.sel.has(c.id);
    const label = tr("clip_n", c.id);
    const img = h("img", { src: `/thumb/${a.id}/${c.id}.jpg?r=${a.run}`, alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.remove());
    const pick = h("button", { type: "button", class: "pick", "aria-pressed": on ? "true" : "false", "aria-label": tr("select_x", label), onclick: () => toggle(c.id) },
      h("span", { class: "dot" }, svg(ICON.check)),
      h("span", { class: "time" }, `${fmt(c.start)} – ${fmt(c.end)} · ${tr("secs", Math.round(c.end - c.start))}`));
    const play = h("button", { type: "button", class: "play", "aria-label": tr("preview_x", label), onclick: (ev) => startPreview(card, c, thumb, ev.currentTarget) }, svg(ICON.play));
    const thumb = h("div", { class: "thumb" }, img, pick, a.source_ok ? play : null);
    const tags = c.tags.map((t) => h("span", { class: "tag" }, TEXT[lang].det[t] || state.app.detectors[t] || t));
    if (c.flags.includes("menu_like")) tags.push(h("span", { class: "tag warn" }, tr("menu_like")));
    const card = h("div", { class: "clip" + (on ? " sel" : ""), "data-id": c.id },
      thumb,
      h("div", { class: "head" }, h("b", null, label), h("span", null, tr("chance", LEVEL[c.level]))),
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
    el.textContent = tr("n_selected", n);
    $("rv-go").disabled = n === 0 || !state.a || !state.a.source_ok;
  }
  $("rv-go").addEventListener("click", () => { if (state.a) go("#/make/" + state.a.id); });
  function selectMany(all) {
    if (!state.a) return;
    state.sel = new Set(all ? state.a.clips.map((c) => c.id) : []);
    for (const card of $("grid").querySelectorAll(".clip")) {
      const on = state.sel.has(Number(card.dataset.id));
      card.classList.toggle("sel", on);
      const b = card.querySelector(".pick");
      if (b) b.setAttribute("aria-pressed", on ? "true" : "false");
    }
    updateCount();
  }
  $("rv-all").addEventListener("click", () => selectMany(true));
  $("rv-none").addEventListener("click", () => selectMany(false));

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
      $("alert").firstChild.textContent = tr("cant_play");
    });
    const close = h("button", { type: "button", class: "x", "aria-label": tr("close_preview"), onclick: stopPreview }, "✕");
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
  const TITLE_BY_TAG = [["slow_motion", "t_slow"], ["quiet_then_burst", "t_clutch"], ["voice_excitement", "t_voice"]];
  function defaultTitle(c) {
    const hit = TITLE_BY_TAG.find(([t]) => c.tags.includes(t));
    return `${tr(hit ? hit[1] : "t_action")} #${c.id}`;
  }
  const chosen = () => state.a.clips.filter((c) => state.sel.has(c.id));
  const GAP = 3, MAXLEN = 180;

  async function showMake(id) {
    if (!state.a || state.a.id !== id) await showReview(id);
    const a = state.a;
    const list = chosen();
    if (!list.length) return go("#/review/" + id);
    if (state.editsRun !== a.run) { state.edits = {}; state.editsRun = a.run; }
    for (const c of list) if (!state.edits[c.id]) state.edits[c.id] = { start: c.start, end: c.end, layout: state.lastLayout, crop_x: 0.5, title: defaultTitle(c) };
    if (!list.some((c) => c.id === state.cur)) state.cur = list[0].id;
    show("make", { back: tr("to_clips"), onBack: () => go("#/review/" + id) });
    $("mk-h1").textContent = tr("can_make", list.length);
    $("mk-lowres").hidden = !a.low_res;
    $("mk-go").textContent = "";
    $("mk-go").append(tr("make_n", list.length), h("span", { "aria-hidden": "true" }, "→"));
    const v = $("ph-video");
    if (!v.dataset.aid || v.dataset.aid !== a.id) { v.dataset.aid = a.id; v.src = `/media/${a.id}`; }
    drawMake();
  }

  function window_(c) { const a = state.a; return [Math.max(0, c.start - 30), Math.min(a.duration, c.end + 30)]; }

  function drawMake() {
    const a = state.a, list = chosen();
    const c = list.find((x) => x.id === state.cur), e = state.edits[c.id];
    $("mk-tabs").replaceChildren(...list.map((x) => h("button", { type: "button", class: "chip", "aria-pressed": x.id === state.cur ? "true" : "false",
      onclick: () => { state.cur = x.id; pausePreview(); drawMake(); } }, tr("clip_n", x.id))));
    const defs = [["blur", tr("lay_blur"), tr("lay_blur_n")], ["crop", tr("lay_crop"), tr("lay_crop_n")], ["follow", tr("lay_follow"), tr("lay_follow_n")], ["cam", tr("lay_cam"), tr("lay_cam_n")]];
    $("layouts").replaceChildren(...defs.map(([k, name, note]) => h("button", { type: "button", class: "layout", "aria-pressed": e.layout === k ? "true" : "false", disabled: k === "cam",
      onclick: () => { e.layout = k; state.lastLayout = k; $("all-same-msg").textContent = ""; drawMake(); } },
      h("span", { class: "mini " + k }), h("span", { class: "nm" }, h("b", null, name), h("span", null, note)))));
    $("all-same").hidden = list.length < 2;
    $("all-same").textContent = tr("all_same", list.length);
    $("all-same").onclick = () => {
      for (const x of list) { state.edits[x.id].layout = e.layout; state.edits[x.id].crop_x = e.crop_x; }
      $("all-same-msg").textContent = tr("all_same_done");
    };
    $("crop-row").hidden = e.layout !== "crop";
    $("follow-note").hidden = e.layout !== "follow";
    $("follow-note").textContent = tr("follow_note");
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
    $("trim-text").textContent = `${fmt(e.start)} – ${fmt(e.end)} · ${tr("secs", Math.round(e.end - e.start))}`;
    $("trim-long").hidden = e.end - e.start <= 60;
    $("trim-long").textContent = tr("long_clip");
    $("trim-a").setAttribute("aria-valuetext", fmt(e.start));
    $("trim-b").setAttribute("aria-valuetext", fmt(e.end));
    $("phone").classList.toggle("crop", e.layout === "crop" || e.layout === "follow");
    $("phone").style.setProperty("--cx", (e.layout === "follow" ? 50 : Math.round(e.crop_x * 100)) + "%");
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
    $("ph-play").textContent = tr("play_part");
  }
  $("ph-play").addEventListener("click", () => {
    const v = $("ph-video"), e = state.edits[state.cur];
    if (!v.paused) return pausePreview();
    v.muted = false;
    if (v.currentTime < e.start || v.currentTime >= e.end - 0.2) v.currentTime = e.start;
    v.play().then(() => { $("ph-play").textContent = tr("stop"); }).catch(() => { $("ph-fail").hidden = false; });
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
      watchJob({ kind: "render", title: () => tr("making_n", list.length), sub: "", labels: list.map((c) => state.edits[c.id].title), edits: JSON.parse(JSON.stringify(state.edits)) });
    } catch (e) { showError(e); }
  });

  // ------------------------------------------------------------------ 5. done
  async function showDone(id) {
    const r = state.result;
    if (!r || r.aid !== id) return go("#/review/" + id);
    show("done");
    $("dn-h1").textContent = tr("n_ready", r.files.length);
    $("dn-path").hidden = true;
    $("dn-list").replaceChildren(...r.files.map((f) => {
      const e = r.edits[f.id] || {};
      const title = e.title || f.file;
      const meta = tr("clip_n", f.id) + (e.end ? ` · ${tr("secs", Math.round(e.end - e.start))}` : "") + ` · ${fmtSize(f.size)}`;
      const copy = h("button", { type: "button", class: "ghost", "aria-label": tr("copy_title_x", title), onclick: async () => {
        try { await navigator.clipboard.writeText(title); copy.textContent = tr("title_copied"); }
        catch (err) { showError({ code: "other" }); }
      } }, tr("copy_title"));
      const up = state.uploaded[f.file];
      const yt = up
        ? h("a", { class: "done-link", href: "https://studio.youtube.com/video/" + encodeURIComponent(up.video) + "/edit",
                   target: "_blank", rel: "noopener noreferrer" }, tr("yt_done", up.privacy) + " · " + tr("yt_studio"))
        : (state.app && state.app.youtube.connected
          ? h("button", { type: "button", class: "ghost", "aria-label": tr("yt_btn_x", title), onclick: () => openUpload(id, f.file, title) }, tr("yt_btn"))
          : null);
      return h("li", { class: "item" }, h("div", { class: "t" }, h("b", null, title), h("span", null, meta)),
        h("div", { class: "row wrap" }, yt, copy,
          h("button", { type: "button", class: "ghost", "aria-label": tr("watch_x", title), onclick: () => openPlayer(id, f.file, title) }, tr("watch"))));
    }));
    const openFolder = async () => {
      try {
        const res = await api("POST", "/api/open-folder", { id });
        if (!res.opened) { $("dn-path").textContent = tr("folder_fail_here", res.path); $("dn-path").hidden = false; }
      } catch (e) { showError(e); }
    };
    $("dn-folder").onclick = openFolder;
    // The upload link opens YouTube in the browser; on this PC the clips folder opens too, ready to drag from.
    $("dn-studio").onclick = () => { if (!document.body.classList.contains("remote")) openFolder(); };
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
    fillSettings();
    $("settings").showModal();
  });
  function fillSettings() {
    const s = state.app, t = s.tools;
    const yn = (ok, yes, no) => h("span", { class: ok ? "ok" : "no" }, ok ? "✓ " + yes : "✗ " + no);
    $("kv").replaceChildren(
      h("dt", null, "ffmpeg"), h("dd", null, yn(t.ffmpeg && t.ffprobe, tr("have"), tr("ff_no"))),
      h("dt", null, tr("yt_label")), h("dd", null, yn(t.ytdlp, tr("have"), tr("yt_no"))),
      h("dt", null, tr("picker_label")), h("dd", null, yn(t.picker, tr("picker_yes"), tr("picker_no"))),
      h("dt", null, tr("video_folder")), h("dd", null, s.video_dir),
      h("dt", null, tr("all_files")), h("dd", null, s.data_dir),
      h("dt", null, tr("version")), h("dd", null, s.version));
    $("st-quit").textContent = tr("quit");
    quitArmed = false;
    showYouTube();
  }
  $("settings-close").addEventListener("click", () => $("settings").close());
  $("st-folder").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/open-folder", {});
      if (!r.opened) showError({ code: "other", detail: "" }), ($("alert").firstChild.textContent = tr("folder_fail", r.path));
    } catch (e) { showError(e); }
  });
  $("st-update").addEventListener("click", async () => {
    const btn = $("st-update"), msg = $("st-update-msg");
    btn.disabled = true;
    msg.hidden = false;
    msg.textContent = tr("yt_updating");
    try {
      const r = await api("POST", "/api/update-ytdlp", {});
      if (!r.ok) msg.textContent = tr("yt_fail");
      else if (r.changed) msg.textContent = tr("yt_new", r.after, r.before || "?");
      else msg.textContent = tr("yt_same", r.after);
    } catch (e) {
      msg.textContent = e && e.code === "busy" ? ERR.busy : (ERR[e && e.code] || tr("yt_fail_short"));
    } finally { btn.disabled = false; }
  });
  let quitArmed = false;
  $("st-quit").addEventListener("click", async () => {
    if (!quitArmed) { quitArmed = true; $("st-quit").textContent = tr("quit_sure"); return; }
    try { await api("POST", "/api/quit", {}); } catch (e) { /* the server is going away */ }
    document.body.replaceChildren(h("p", { class: "screen" }, tr("closed")));
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
      msg.textContent = r.active ? tr("rm_on", r.active)
                      : r.host ? tr("rm_saved") : tr("rm_off");
    } catch (e) { msg.hidden = false; msg.textContent = ERR[e && e.code] || tr("load_fail"); }
  }
  $("remote").addEventListener("toggle", () => { if ($("remote").open) loadRemote(); });
  $("rm-save").addEventListener("click", async () => {
    const msg = $("rm-msg");
    try {
      await api("POST", "/api/remote", { host: $("rm-host").value.trim() });
      await loadRemote();
    } catch (e) { msg.hidden = false; msg.textContent = ERR[e && e.code] || tr("save_fail"); }
  });
  $("rm-copy").addEventListener("click", async () => {
    const box = $("rm-link"), msg = $("rm-msg");
    box.focus(); box.select(); msg.hidden = false;
    try { await navigator.clipboard.writeText(box.value); msg.textContent = tr("copied"); }
    catch (e) { msg.textContent = tr("copy_manual"); }
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
    try { await navigator.clipboard.writeText(box.value); msg.textContent = tr("copied"); }
    catch (e) { msg.textContent = tr("copy_manual2"); }
  }
  $("cn-copy").addEventListener("click", () => copyFrom("cn-json"));
  $("cn-copy2").addEventListener("click", () => copyFrom("cn-cmd"));
  $("cn-test").addEventListener("click", async () => {
    const btn = $("cn-test"), msg = $("cn-msg");
    btn.disabled = true; msg.hidden = false; msg.textContent = tr("testing");
    try {
      const r = await api("POST", "/api/connector-test", {});
      msg.textContent = r.ok ? tr("cn_ok", r.tools) : tr("cn_fail");
    } catch (e) { msg.textContent = ERR[e && e.code] || tr("cn_fail_short"); }
    finally { btn.disabled = false; }
  });

  // ------------------------------------------------------------------ YouTube upload
  function describe(title, game) {
    const tag = game.replace(/[^\p{L}\p{N}]+/gu, "");
    return title + "\n\n" + (game ? game + " · " : "") + "PS5 " + tr("yt_gameplay") + "\n\n#Shorts " + (tag ? "#" + tag + " " : "") + "#PS5 #Gaming";
  }
  let ytAuto = "";      // the description we filled in last; if the user changed it, we leave theirs alone
  function refill() {
    if ($("yt-desc").value !== ytAuto) return;
    ytAuto = describe($("yt-title").value.trim(), $("yt-game").value.trim());
    $("yt-desc").value = ytAuto;
  }
  function openUpload(id, file, title) {
    $("yt-title").value = title.slice(0, 100);
    $("yt-game").value = state.game;
    ytAuto = ""; $("yt-desc").value = ""; refill();
    $("yt-privacy").value = "private";
    $("yt-msg").textContent = "";
    $("yt-go").onclick = async () => {
      const t = $("yt-title").value.trim();
      if (!t) { $("yt-msg").textContent = tr("yt_need_title"); return $("yt-title").focus(); }
      state.game = $("yt-game").value.trim();
      try {
        await api("POST", "/api/youtube/upload", { id, file, title: t, description: $("yt-desc").value, privacy: $("yt-privacy").value });
        $("ytdlg").close();
        watchJob({ kind: "upload", title: () => tr("yt_uploading"), sub: t, labels: [() => tr("yt_step")] });
      } catch (e) { $("yt-msg").textContent = ERR[e && e.code] || tr("err_other"); }
    };
    $("ytdlg").showModal();
  }
  $("yt-title").addEventListener("input", refill);
  $("yt-game").addEventListener("input", refill);
  $("ytdlg-close").addEventListener("click", () => $("ytdlg").close());

  let ytPoll = 0;
  function showYouTube() {
    const y = (state.app && state.app.youtube) || { client: false, connected: false };
    const remote = document.body.classList.contains("remote");
    $("yt-status").textContent = y.connected ? tr("yt_ok") : !y.client ? tr("yt_need_file") : remote ? tr("yt_remote") : tr("yt_ready");
    $("yt-connect").disabled = !y.client || y.connected;
    $("yt-disconnect").hidden = !y.connected;
  }
  $("yt-connect").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/youtube/connect", {});
      if (typeof r.url !== "string" || !r.url.startsWith("https://accounts.google.com/")) throw new ApiErr("yt_auth_failed");
      window.open(r.url, "_blank", "noopener,noreferrer");
      $("yt-status").textContent = tr("yt_wait");
      clearInterval(ytPoll);
      let tries = 0;
      ytPoll = setInterval(async () => {
        tries += 1;
        try { state.app = await api("GET", "/api/state"); } catch (e) { /* try again */ }
        if (state.app.youtube.connected || tries > 150 || !$("settings").open) {
          clearInterval(ytPoll);
          showYouTube();
        }
      }, 2000);
    } catch (e) { $("yt-status").textContent = ERR[e && e.code] || tr("err_other"); }
  });
  $("yt-disconnect").addEventListener("click", async () => {
    try { state.app.youtube = await api("POST", "/api/youtube/disconnect", {}); showYouTube(); }
    catch (e) { $("yt-status").textContent = ERR[e && e.code] || tr("err_other"); }
  });

  // ------------------------------------------------------------------ language
  async function setLang(l) {
    if (!LANGS.includes(l) || l === lang) return;
    lang = l;
    applyPageText();
    try { await api("POST", "/api/lang", { lang: l }); } catch (e) { /* not saved: still switched for now */ }
    if (state.app) state.app.lang = l;
    if ($("settings").open && state.app) fillSettings();
    if (connectorInfo) showConnector();
    pausePreview();
    if (state.watching && state.progress) drawProgress(...state.progress);
    else route();
  }
  for (const l of LANGS) $("lang-" + l).addEventListener("click", () => setLang(l));

  // ------------------------------------------------------------------ start
  applyPageText();
  route();
})();
