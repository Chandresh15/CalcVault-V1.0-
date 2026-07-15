/* =====================================================================
   CalcVault — Ramboll Edition   |   static/script.js
   Single vanilla-JS bundle, no dependencies.
   Attaches everything on DOMContentLoaded and exposes CV.* for pages
   that need to reuse helpers (module calc, builder, total pump head).
   ===================================================================== */
(function () {
  "use strict";

  /* -------------------------------------------------------------------
     0. Namespace + storage helpers
     ------------------------------------------------------------------- */
  const CV = window.CV = {};
  const LS = {
    get(k, def) { try { const v = localStorage.getItem(k); return v === null ? def : v; } catch (_) { return def; } },
    set(k, v)   { try { localStorage.setItem(k, v); } catch (_) {} },
  };

  /* -------------------------------------------------------------------
     1. Theme + accent + language
     (initial values are applied inline in base.html to avoid FOUC —
      here we only wire the CONTROLS that change them)
     ------------------------------------------------------------------- */
  function setTheme(mode) {
    document.documentElement.setAttribute("data-theme", mode);
    LS.set("cv.theme", mode);
    const btn = document.getElementById("themeToggle");
    if (btn) btn.textContent = (mode === "dark") ? "☀️" : "🌙";
  }

  function setAccent(name) {
    document.documentElement.setAttribute("data-accent", name);
    LS.set("cv.accent", name);
    document.querySelectorAll(".accent-dot")
      .forEach(d => d.classList.toggle("active", d.dataset.a === name));
  }

  function setLang(code) {
    document.documentElement.setAttribute("lang", code);
    LS.set("cv.lang", code);
    applyI18n();
  }

  function wireTopBar() {
    const themeBtn = document.getElementById("themeToggle");
    if (themeBtn) themeBtn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "light";
      setTheme(cur === "dark" ? "light" : "dark");
    });

    document.querySelectorAll(".accent-dot").forEach(dot => {
      dot.addEventListener("click", () => setAccent(dot.dataset.a));
    });

    const langSel = document.getElementById("langSelect");
    if (langSel) {
      langSel.value = LS.get("cv.lang", "en");
      langSel.addEventListener("change", e => setLang(e.target.value));
    }

    // Reflect current state
    setAccent(LS.get("cv.accent", "blue"));
    if (themeBtn) {
      const cur = document.documentElement.getAttribute("data-theme") || "light";
      themeBtn.textContent = (cur === "dark") ? "☀️" : "🌙";
    }
  }

  /* -------------------------------------------------------------------
     2. Tiny i18n  (English / Dansk / Suomi)
     Only strings marked with data-i18n are translated; the vast
     majority of the UI is server-rendered so keeping this small.
     ------------------------------------------------------------------- */
  const I18N = {
    en: {
      "nav.dashboard":"Dashboard","nav.history":"History","nav.compare":"Compare",
      "nav.calculations":"Calculations","nav.pumps":"Pump Databank",
      "nav.users":"Users","nav.approvals":"Approvals","nav.hub":"Module Hub",
      "nav.shutdown":"Shutdown","nav.batch":"Batch Import",
      "btn.submit":"Calculate","btn.pdf":"Download PDF",
      "btn.approve":"Submit for Approval","status.online":"Online",
      "status.offline":"Offline","status.connecting":"Connecting…",
      "notif.title":"Notifications","notif.empty":"No notifications yet.",
      "notif.markread":"Mark all read","confirm.delete":"Delete this calculation?",
      "confirm.deleteall":"Delete ALL your calculations? This cannot be undone.",
      "confirm.shutdown":"Shut down the CalcVault server for everyone?",
      "confirm.cancel":"Cancel this pending approval?",
    },
    da: {
      "nav.dashboard":"Oversigt","nav.history":"Historik","nav.compare":"Sammenlign",
      "nav.calculations":"Beregninger","nav.pumps":"Pumpedatabank",
      "nav.users":"Brugere","nav.approvals":"Godkendelser","nav.hub":"Modul Hub",
      "nav.shutdown":"Luk ned","nav.batch":"Batch-import",
      "btn.submit":"Beregn","btn.pdf":"Hent PDF",
      "btn.approve":"Send til godkendelse","status.online":"Online",
      "status.offline":"Offline","status.connecting":"Forbinder…",
      "notif.title":"Notifikationer","notif.empty":"Ingen notifikationer.",
      "notif.markread":"Marker alle som læst",
      "confirm.delete":"Slet denne beregning?",
      "confirm.deleteall":"Slet ALLE dine beregninger? Kan ikke fortrydes.",
      "confirm.shutdown":"Luk CalcVault-serveren for alle?",
      "confirm.cancel":"Annullér denne afventende godkendelse?",
    },
    fi: {
      "nav.dashboard":"Yleisnäkymä","nav.history":"Historia","nav.compare":"Vertaile",
      "nav.calculations":"Laskennat","nav.pumps":"Pumppupankki",
      "nav.users":"Käyttäjät","nav.approvals":"Hyväksynnät","nav.hub":"Moduulikeskus",
      "nav.shutdown":"Sammuta","nav.batch":"Erätuonti",
      "btn.submit":"Laske","btn.pdf":"Lataa PDF",
      "btn.approve":"Lähetä hyväksyttäväksi","status.online":"Yhteydessä",
      "status.offline":"Ei yhteyttä","status.connecting":"Yhdistetään…",
      "notif.title":"Ilmoitukset","notif.empty":"Ei ilmoituksia.",
      "notif.markread":"Merkitse kaikki luetuiksi",
      "confirm.delete":"Poistetaanko tämä laskenta?",
      "confirm.deleteall":"Poistetaanko KAIKKI laskentasi? Ei voi perua.",
      "confirm.shutdown":"Sammutetaanko CalcVault-palvelin kaikilta?",
      "confirm.cancel":"Peruuta odottava hyväksyntä?",
    }
  };

  function t(key) {
    const lang = LS.get("cv.lang", "en");
    return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
  }
  CV.t = t;

  function applyI18n() {
    document.querySelectorAll("[data-i18n]").forEach(el => {
      el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll("[data-i18n-title]").forEach(el => {
      el.title = t(el.dataset.i18nTitle);
    });
  }

  /* -------------------------------------------------------------------
     3. Toasts + Desktop notifications
     ------------------------------------------------------------------- */
  function ensureToastRoot() {
    let root = document.querySelector(".toasts");
    if (!root) {
      root = document.createElement("div");
      root.className = "toasts";
      document.body.appendChild(root);
    }
    return root;
  }

  function toast(title, message, kind) {
    const root = ensureToastRoot();
    const el = document.createElement("div");
    el.className = "toast";
    el.dataset.kind = kind || "info";
    el.innerHTML =
      `<div style="flex:1"><b></b><p></p></div>` +
      `<button class="btn btn-ghost btn-sm" aria-label="Close">✕</button>`;
    el.querySelector("b").textContent  = title || "";
    el.querySelector("p").textContent  = message || "";
    el.querySelector("button").addEventListener("click", () => el.remove());
    root.appendChild(el);
    setTimeout(() => el.remove(), 6000);
    return el;
  }
  CV.toast = toast;

  let _desktopAsked = false;
  function askDesktopPermission() {
    if (_desktopAsked || !("Notification" in window)) return;
    _desktopAsked = true;
    if (Notification.permission === "default") {
      try { Notification.requestPermission(); } catch (_) {}
    }
  }

  function desktopNotify(title, body, kind) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      const n = new Notification(title, {
        body: body || "",
        icon: "/static/favicon.png",
        tag:  "calcvault-" + (kind || "info"),
        silent: false,
      });
      setTimeout(() => n.close(), 6000);
    } catch (_) { /* Safari can throw */ }
  }
  CV.desktopNotify = desktopNotify;

  /* -------------------------------------------------------------------
     4. Server status orb  +  heartbeat  +  notifications polling
     One interval to rule them all → minimal network overhead.
     ------------------------------------------------------------------- */
  const HEARTBEAT_MS   = 8000;
  const OFFLINE_RETRY  = 3000;
  let lastServerState  = null;       // 'connecting' | 'online' | 'offline'
  let seenNotifIds     = new Set();
  let heartbeatTimer   = null;

  function setStatus(state, pingMs) {
    const el = document.querySelector(".server-status");
    if (!el) return;
    el.setAttribute("data-state", state);
    const txt  = el.querySelector(".status-txt b");
    const sub  = el.querySelector(".status-txt small");
    if (txt) txt.textContent = t("status." + state);
    if (sub) sub.textContent = (state === "online" && pingMs != null)
      ? `${pingMs} ms` : (state === "offline" ? "no route" : "…");

    // Notify on state transition to OFFLINE only (avoid spam on 'online')
    if (lastServerState && lastServerState !== state) {
      if (state === "offline") {
        toast("CalcVault server unreachable", "Retrying…", "warn");
        desktopNotify("CalcVault offline", "Server unreachable — retrying.", "warn");
      } else if (state === "online" && lastServerState === "offline") {
        toast("Back online", "Server reconnected.", "ok");
      }
    }
    lastServerState = state;
  }

  async function tick() {
    const t0 = performance.now();
    try {
      const res = await fetch("/api/heartbeat", {
        method: "GET", credentials: "same-origin", cache: "no-store"
      });
      const ping = Math.round(performance.now() - t0);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      setStatus("online", ping);

      // "👥 N online" chip
      const chip = document.getElementById("onlineChip");
      if (chip && typeof data.online === "number") {
        chip.textContent = `👥 ${data.online} online`;
      }

      // Notifications — piggy-back on the same tick, but only if bell exists
      await pollNotifications();

    } catch (_) {
      setStatus("offline", null);
      // Retry sooner while offline
      clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(tick, OFFLINE_RETRY);
      return;
    }
    // Reset to normal cadence if we were in fast-retry mode
    if (!heartbeatTimer || heartbeatTimer._retry) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(tick, HEARTBEAT_MS);
      heartbeatTimer._retry = false;
    }
  }

  async function pollNotifications() {
    const bell = document.getElementById("notifBtn");
    if (!bell) return;
    try {
      const r = await fetch("/api/notifications", { credentials: "same-origin" });
      if (!r.ok) return;
      const data = await r.json();
      const badge = bell.querySelector(".badge");
      if (data.unread > 0) {
        if (!badge) {
          const b = document.createElement("span");
          b.className = "badge";
          b.textContent = String(data.unread);
          bell.appendChild(b);
        } else {
          badge.textContent = String(data.unread);
        }
      } else if (badge) {
        badge.remove();
      }
      renderNotifList(data.items || []);

      // Desktop-notify only NEW items we haven't seen this session
      (data.items || []).forEach(n => {
        if (!seenNotifIds.has(n.id) && !n.is_read) {
          if (seenNotifIds.size > 0) desktopNotify(n.title, n.message, n.kind);
        }
        seenNotifIds.add(n.id);
      });
    } catch (_) { /* silently ignore */ }
  }

  function renderNotifList(items) {
    const list = document.querySelector(".notif-list");
    if (!list) return;
    if (!items.length) {
      list.innerHTML = `<div class="notif-item"><div class="notif-body">
        <p>${t("notif.empty")}</p></div></div>`;
      return;
    }
    list.innerHTML = items.map(n => `
      <a class="notif-item ${n.is_read ? "" : "unread"}"
         data-kind="${n.kind || "info"}"
         href="${n.link || "#"}">
        <span class="notif-dot"></span>
        <div class="notif-body">
          <b>${escapeHtml(n.title)}</b>
          <p>${escapeHtml(n.message || "")}</p>
          <time>${formatRelative(n.created_at)}</time>
        </div>
      </a>`).join("");
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g,
      c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
  }

  function formatRelative(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    const now  = Date.now();
    const s    = Math.round((now - then) / 1000);
    if (s < 60)      return s + "s ago";
    if (s < 3600)    return Math.round(s / 60)   + "m ago";
    if (s < 86400)   return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function wireHeartbeat() {
    if (!document.querySelector(".server-status")) return;
    setStatus("connecting", null);
    tick();
    heartbeatTimer = setInterval(tick, HEARTBEAT_MS);
    // Pause when tab hidden, resume immediately when it returns
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearInterval(heartbeatTimer);
      } else {
        tick();
        heartbeatTimer = setInterval(tick, HEARTBEAT_MS);
      }
    });
  }

  /* -------------------------------------------------------------------
     5. Notifications popover
     ------------------------------------------------------------------- */
  function wireNotifPop() {
    const btn = document.getElementById("notifBtn");
    const pop = document.querySelector(".notif-pop");
    if (!btn || !pop) return;

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      pop.classList.toggle("open");
      askDesktopPermission();
      if (pop.classList.contains("open")) pollNotifications();
    });
    document.addEventListener("click", (e) => {
      if (!pop.contains(e.target) && e.target !== btn) pop.classList.remove("open");
    });

    const mark = document.getElementById("notifMarkRead");
    if (mark) mark.addEventListener("click", async () => {
      await fetch("/api/notifications/mark-read",
                  { method: "POST", credentials: "same-origin" });
      pollNotifications();
    });
  }

  /* -------------------------------------------------------------------
     6. Confirmation prompts + shutdown
     Use data-confirm="msg-key or literal" on any form/button.
     ------------------------------------------------------------------- */
  function wireConfirms() {
    document.addEventListener("submit", (e) => {
      const form = e.target.closest("form[data-confirm]");
      if (!form) return;
      const msg = form.dataset.confirm.startsWith("i18n:")
        ? t(form.dataset.confirm.slice(5)) : form.dataset.confirm;
      if (!confirm(msg)) e.preventDefault();
    }, true);
  }

  /* -------------------------------------------------------------------
     7. ENTER key advances focus (never accidentally submits)
     Applied automatically to any form flagged with data-enter-advance,
     which is the default for our module calc pages.
     ------------------------------------------------------------------- */
  function wireEnterAdvance(rootSelector) {
    document.querySelectorAll(rootSelector || "form[data-enter-advance]")
      .forEach(form => {
        const fields = () => Array.from(form.querySelectorAll(
          "input:not([type=hidden]):not([disabled]), select, textarea"
        )).filter(el => el.offsetParent !== null);
        form.addEventListener("keydown", (e) => {
          if (e.key !== "Enter") return;
          if (e.target.tagName === "TEXTAREA") return;  // allow newlines
          if (e.target.type === "submit") return;
          e.preventDefault();
          const list = fields();
          const i    = list.indexOf(e.target);
          const next = list[i + 1];
          if (next) next.focus();
          else e.target.blur();  // last field → blur (user can Tab or click Calc)
        });
      });
  }
  CV.wireEnterAdvance = wireEnterAdvance;

  /* -------------------------------------------------------------------
     8. Debounce + live-preview helper for calc pages
     Usage in a module template:
        CV.liveCalc({
          form:       document.getElementById("calcForm"),
          endpoint:   null,       // client-only pre-check
          compute:    (values) => { … return { primary:{label,value,unit},
                                               details:[[l,v,u]…] } },
          target:     document.getElementById("livePreview"),
        });
     ------------------------------------------------------------------- */
  function debounce(fn, ms) {
    let h; return function () {
      clearTimeout(h);
      const args = arguments, ctx = this;
      h = setTimeout(() => fn.apply(ctx, args), ms || 200);
    };
  }
  CV.debounce = debounce;

  function readForm(form) {
    const out = {};
    form.querySelectorAll("input, select, textarea").forEach(el => {
      if (!el.name) return;
      if (el.type === "checkbox") out[el.name] = el.checked;
      else out[el.name] = el.value;
    });
    return out;
  }
  CV.readForm = readForm;

  function liveCalc(opts) {
    if (!opts || !opts.form || !opts.target || !opts.compute) return;
    const run = debounce(() => {
      let payload;
      try {
        const values = readForm(opts.form);
        payload = opts.compute(values);
      } catch (err) {
        opts.target.innerHTML =
          `<div class="text-muted small">Enter valid inputs to preview.</div>`;
        return;
      }
      if (!payload) return;
      const html = [];
      if (payload.primary) {
        html.push(`<div class="result-primary">
          ${escapeHtml(payload.primary.value)}
          <small>${escapeHtml(payload.primary.unit || "")}</small></div>`);
        if (payload.primary.label) {
          html.push(`<div class="text-muted small mb-1">
            ${escapeHtml(payload.primary.label)} (live preview)</div>`);
        }
      }
      if (payload.details && payload.details.length) {
        html.push(`<dl class="result-grid">`);
        payload.details.forEach(([l, v, u]) => {
          html.push(`<dt>${escapeHtml(l)}</dt>
                     <dd>${escapeHtml(v)} <span class="text-muted small">${escapeHtml(u || "")}</span></dd>`);
        });
        html.push(`</dl>`);
      }
      opts.target.innerHTML = html.join("");
    }, 150);
    opts.form.addEventListener("input",  run);
    opts.form.addEventListener("change", run);
    run();  // paint once on load
  }
  CV.liveCalc = liveCalc;

  /* -------------------------------------------------------------------
     9. Reference-table "Use" buttons  (auto-fill inputs)
     Any button/anchor with data-use='{"field":value,...}' fills those
     form fields on click.  Used by C-factor & Kutter tables + pump
     suggestion side panel.
     ------------------------------------------------------------------- */
  function wireUseButtons() {
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-use]");
      if (!btn) return;
      let payload;
      try { payload = JSON.parse(btn.dataset.use); }
      catch (_) { return; }
      Object.entries(payload).forEach(([name, val]) => {
        const el = document.querySelector(`[name="${name}"]`);
        if (!el) return;
        el.value = val;
        el.dispatchEvent(new Event("input",  { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      });
      toast("Applied", btn.dataset.useTitle || "Values inserted.", "info");
    });
  }

  /* -------------------------------------------------------------------
     10. Modal helper  (history "view details", builder share dialog…)
     ------------------------------------------------------------------- */
  function openModal(title, bodyHtml, footerHtml) {
    let bd = document.querySelector(".modal-backdrop");
    if (!bd) {
      bd = document.createElement("div");
      bd.className = "modal-backdrop";
      bd.innerHTML = `<div class="modal">
        <div class="modal-head"><h3></h3>
          <button class="icon-btn" data-close>✕</button></div>
        <div class="modal-body"></div>
        <div class="modal-foot"></div>
      </div>`;
      document.body.appendChild(bd);
      bd.addEventListener("click", (e) => {
        if (e.target === bd || e.target.closest("[data-close]")) closeModal();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeModal();
      });
    }
    bd.querySelector(".modal-head h3").textContent = title || "";
    bd.querySelector(".modal-body").innerHTML      = bodyHtml || "";
    bd.querySelector(".modal-foot").innerHTML      = footerHtml ||
      `<button class="btn btn-ghost" data-close>Close</button>`;
    bd.classList.add("open");
  }
  function closeModal() {
    const bd = document.querySelector(".modal-backdrop");
    if (bd) bd.classList.remove("open");
  }
  CV.openModal  = openModal;
  CV.closeModal = closeModal;

  /* -------------------------------------------------------------------
     11. History table  →  view-details modal, delete confirms, compare
     ------------------------------------------------------------------- */
  function wireHistoryTable() {
    const tbl = document.getElementById("historyTable");
    if (!tbl) return;

    // View details
    tbl.addEventListener("click", async (e) => {
      const view = e.target.closest("[data-view-id]");
      if (view) {
        e.preventDefault();
        const id = view.dataset.viewId;
        try {
          const r = await fetch(`/history/${id}/view`, { credentials: "same-origin" });
          if (!r.ok) throw new Error();
          const d = await r.json();
          openModal(`${d.icon || ""} ${d.module}`,
            renderCalcDetail(d),
            `<a class="btn btn-soft" href="/history/${id}/pdf">📄 PDF</a>
             <button class="btn btn-ghost" data-close>Close</button>`);
        } catch (_) {
          toast("Could not load details", "", "error");
        }
      }
    });

    // Compare: Ctrl-click rows to accumulate an id list, "Compare" button reads it
    const compareBtn = document.getElementById("compareBtn");
    const selected   = new Set();
    tbl.querySelectorAll("tr[data-id]").forEach(tr => {
      tr.addEventListener("click", (e) => {
        if (!(e.ctrlKey || e.metaKey)) return;
        if (e.target.closest("a, button, form")) return;
        const id = tr.dataset.id;
        if (selected.has(id)) { selected.delete(id); tr.classList.remove("selected"); }
        else if (selected.size < 4) { selected.add(id); tr.classList.add("selected"); }
        if (compareBtn) {
          compareBtn.disabled = selected.size < 2;
          compareBtn.textContent = `⚖ Compare (${selected.size})`;
        }
      });
    });
    if (compareBtn) compareBtn.addEventListener("click", () => {
      if (selected.size < 2) return;
      window.location.href = "/compare?ids=" + Array.from(selected).join(",");
    });
  }

  function renderCalcDetail(d) {
    const rows = (obj) => Object.entries(obj || {})
      .map(([k, v]) => `<tr><td class="text-muted">${escapeHtml(k)}</td>
        <td><b>${escapeHtml(typeof v === "object" ? JSON.stringify(v) : v)}</b></td></tr>`)
      .join("") || `<tr><td colspan="2" class="text-muted">—</td></tr>`;
    return `
      <div class="small text-muted mb-1">${escapeHtml(d.created_at || "")} · ${escapeHtml(d.status)}</div>
      ${d.formula ? `<div class="formula-box mb-2">${escapeHtml(d.formula)}</div>` : ""}
      <h4>Inputs</h4>
      <table class="data"><tbody>${rows(d.inputs)}</tbody></table>
      <h4 class="mt-2">Results</h4>
      <table class="data"><tbody>${rows(d.results)}</tbody></table>
      ${d.review_comment ? `<h4 class="mt-2">Reviewer comment</h4>
        <p class="text-muted">${escapeHtml(d.review_comment)}</p>` : ""}`;
  }

  /* -------------------------------------------------------------------
     12. Module Builder — tab switching + keyboard shortcuts + live test
     Exposed via CV.builder so builder_edit.html can wire it up.
     ------------------------------------------------------------------- */
  const builder = {
    _tab: "installed",
    switchTab(name) {
      this._tab = name;
      document.querySelectorAll(".builder-tabs button").forEach(b =>
        b.classList.toggle("active", b.dataset.tab === name));
      document.querySelectorAll("[data-tab-panel]").forEach(p =>
        p.classList.toggle("hidden", p.dataset.tabPanel !== name));
    },
    addRow(gridId) {
      const tbody = document.querySelector(`#${gridId} tbody`);
      if (!tbody) return;
      const tpl = tbody.querySelector("tr[data-template]");
      if (!tpl) return;
      const row = tpl.cloneNode(true);
      row.removeAttribute("data-template");
      row.classList.remove("hidden");
      tbody.appendChild(row);
      const firstInput = row.querySelector("input, select");
      if (firstInput) firstInput.focus();
    },
    removeRow(btn) {
      const tr = btn.closest("tr");
      if (tr) tr.remove();
      this.autosave();
      this.livePreview();
    },
    readSchema() {
      const readGrid = (gridId, keys) => {
        const rows = document.querySelectorAll(
          `#${gridId} tbody tr:not([data-template])`);
        return Array.from(rows).map(tr => {
          const obj = {};
          keys.forEach(k => {
            const el = tr.querySelector(`[data-k="${k}"]`);
            obj[k] = el ? el.value : "";
          });
          return obj;
        }).filter(o => o.var);
      };
      return {
        name:        document.getElementById("mName")?.value.trim() || "",
        icon:        document.getElementById("mIcon")?.value.trim() || "📐",
        category:    document.getElementById("mCategory")?.value.trim() || "Custom",
        description: document.getElementById("mDescription")?.value.trim() || "",
        status:      document.getElementById("mStatus")?.value || "active",
        assigned_users: Array.from(
          document.querySelectorAll("input[name=assignedUser]:checked")
        ).map(el => parseInt(el.value, 10)),
        inputs:  readGrid("inputsGrid",
                          ["var","label","unit","default","note"]),
        outputs: readGrid("outputsGrid",
                          ["var","label","unit","formula","decimals","primary"])
                 .map(o => ({ ...o, primary: o.primary === "true" || o.primary === true })),
      };
    },
    async save() {
      const payload = this.readSchema();
      const idField = document.getElementById("mId");
      const fd = new FormData();
      fd.append("payload", JSON.stringify(payload));
      if (idField && idField.value) fd.append("id", idField.value);
      const r = await fetch("/hub/save", {
        method: "POST", body: fd, credentials: "same-origin"
      });
      const j = await r.json();
      if (j.ok) {
        toast("Saved", `Module '${payload.name}' saved.`, "ok");
        if (!idField.value) window.location.href = `/hub/${j.id}/edit`;
      } else {
        toast("Save failed", j.error || "Unknown error", "error");
      }
    },
    livePreview: debounce(async function () {
      const panel = document.getElementById("testPanel");
      if (!panel) return;
      const payload = builder.readSchema();
      // Collect current test values from the panel inputs
      const values = {};
      panel.querySelectorAll("input[data-test-var]").forEach(el => {
        values[el.dataset.testVar] = el.value;
      });
      try {
        const r = await fetch("/api/module-preview", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...payload, values }),
        });
        const j = await r.json();
        renderTestResult(j);
      } catch (_) { /* silent */ }
    }, 250),
    autosave: debounce(function () {
      try {
        LS.set("cv.builder.draft", JSON.stringify(builder.readSchema()));
      } catch (_) {}
    }, 400),
    loadDraft() {
      const raw = LS.get("cv.builder.draft");
      if (!raw) return null;
      try { return JSON.parse(raw); } catch (_) { return null; }
    },
    clearDraft() { try { localStorage.removeItem("cv.builder.draft"); } catch (_) {} },
  };
  CV.builder = builder;

  function renderTestResult(j) {
    const box = document.getElementById("testResult");
    if (!box) return;
    if (!j) { box.innerHTML = ""; return; }
    if (j.error) {
      box.innerHTML = `<div class="err">${escapeHtml(j.error)}</div>`;
      return;
    }
    const rows = Object.entries(j.results || {}).map(([k, v]) => {
      const err = (j.errors || {})[k];
      return `<div class="row">
        <span>${escapeHtml(k)}</span>
        <b>${err ? `<span class="err">${escapeHtml(err)}</span>` : escapeHtml(v)}</b>
      </div>`;
    }).join("");
    box.innerHTML = rows || `<div class="text-muted">Add outputs to see results.</div>`;
  }

  function wireBuilderKeys() {
    if (!document.getElementById("builderRoot")) return;
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey) {
        if (e.key === "s") { e.preventDefault(); builder.save(); }
        else if (e.key === "i") { e.preventDefault(); builder.addRow("inputsGrid"); }
        else if (e.key === "o") { e.preventDefault(); builder.addRow("outputsGrid"); }
      }
    });
    const root = document.getElementById("builderRoot");
    root.addEventListener("input",  () => { builder.autosave(); builder.livePreview(); });
    root.addEventListener("change", () => { builder.autosave(); builder.livePreview(); });
  }

  /* -------------------------------------------------------------------
     13. Pump suggestion side panel  (used by total_pump_head.html)
     ------------------------------------------------------------------- */
  CV.suggestPumps = async function (flow, head, targetEl) {
    if (!targetEl) return;
    if (!flow || !head) { targetEl.innerHTML = ""; return; }
    try {
      const r = await fetch(
        `/api/pump-suggest?flow=${encodeURIComponent(flow)}&head=${encodeURIComponent(head)}`,
        { credentials: "same-origin" }
      );
      const list = await r.json();
      if (!list.length) {
        targetEl.innerHTML =
          `<div class="text-muted small">No reference pump within tolerance.</div>`;
        return;
      }
      targetEl.innerHTML = list.map(p => `
        <div class="card mb-1">
          <div class="between">
            <div>
              <b>${escapeHtml(p.vendor)} — ${escapeHtml(p.model)}</b>
              <div class="small text-muted">
                Q ${p.flow_m3h} m³/hr · H ${p.head_m} m ·
                η<sub>p</sub> ${p.pump_eff_pct}%
              </div>
            </div>
            <button class="btn btn-sm" data-use='${
              JSON.stringify({
                flow_m3h:     p.flow_m3h,
                pump_eff_pct: p.pump_eff_pct,
                motor_eff_pct:p.motor_eff_pct,
              })
            }' data-use-title="Pump values applied">📌 Use</button>
          </div>
        </div>`).join("");
    } catch (_) {
      targetEl.innerHTML =
        `<div class="text-muted small">Could not load suggestions.</div>`;
    }
  };

  /* -------------------------------------------------------------------
     14. Public share URL copy helper (builder)
     ------------------------------------------------------------------- */
  CV.copyToClipboard = async function (text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied", text, "ok");
    } catch (_) {
      // Fallback
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta);
      ta.select(); try { document.execCommand("copy"); } catch (_) {}
      ta.remove();
      toast("Copied", text, "ok");
    }
  };

  /* -------------------------------------------------------------------
     15. Boot
     ------------------------------------------------------------------- */
  document.addEventListener("DOMContentLoaded", () => {
    wireTopBar();
    applyI18n();
    wireNotifPop();
    wireConfirms();
    wireEnterAdvance();      // covers all forms flagged data-enter-advance
    wireUseButtons();
    wireHistoryTable();
    wireBuilderKeys();
    wireHeartbeat();

    // Draft restore prompt in builder
    const builderRoot = document.getElementById("builderRoot");
    if (builderRoot && !document.getElementById("mId")?.value) {
      const draft = builder.loadDraft();
      if (draft && draft.name) {
        if (confirm(`Restore unsaved draft "${draft.name}"?`)) {
          document.getElementById("mName").value        = draft.name || "";
          document.getElementById("mIcon").value        = draft.icon || "📐";
          document.getElementById("mCategory").value    = draft.category || "Custom";
          document.getElementById("mDescription").value = draft.description || "";
          // Rows are re-populated by dispatching a custom event the
          // template listens for, keeping this JS file schema-agnostic.
          builderRoot.dispatchEvent(new CustomEvent("cv:restore-draft",
                                                    { detail: draft }));
        } else {
          builder.clearDraft();
        }
      }
    }
  });

})();