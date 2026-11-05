// homelab-agent panel: small progressive enhancements, no dependency.
// Everything here is optional: pages render and work without it.
(() => {
  "use strict";
  const root = document.documentElement;
  const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- theme
  const themeBtn = document.getElementById("theme");
  function cycleTheme() {
    const next = { undefined: "light", light: "dark", dark: undefined }[root.dataset.theme];
    const apply = () => {
      if (next) root.dataset.theme = next; else delete root.dataset.theme;
      try { next ? localStorage.setItem("hla-theme", next) : localStorage.removeItem("hla-theme"); } catch (e) {}
    };
    document.startViewTransition && !calm ? document.startViewTransition(apply) : apply();
  }
  if (themeBtn) themeBtn.addEventListener("click", cycleTheme);

  // cross-document view transitions can be skipped by the browser (resize, rotation…):
  // that is fine, just don't report it as an error.
  addEventListener("pagereveal", (e) => {
    if (e.viewTransition) { e.viewTransition.ready.catch(() => {}); e.viewTransition.finished.catch(() => {}); }
  });
  addEventListener("pageswap", (e) => {
    if (e.viewTransition) e.viewTransition.finished.catch(() => {});
  });

  // ------------------------------------------- light that follows the pointer
  // Glass panes catch the light where the pointer is (CSS reads --mx / --my).
  if (matchMedia("(hover: hover)").matches) {
    let raf = 0, target = null, x = 0, y = 0;
    addEventListener("pointermove", (e) => {
      target = e.target.closest && e.target.closest(".tile, .card, .login, .engine");
      x = e.clientX; y = e.clientY;
      if (!raf) raf = requestAnimationFrame(() => {
        raf = 0;
        if (!target) return;
        const r = target.getBoundingClientRect();
        target.style.setProperty("--mx", (x - r.left).toFixed(0) + "px");
        target.style.setProperty("--my", (y - r.top).toFixed(0) + "px");
      });
    }, { passive: true });
  }

  // ------------------------------------------------------- numbers count up
  if (!calm) {
    for (const el of document.querySelectorAll("[data-count]")) {
      const text = el.textContent.trim();
      const end = Number(text);
      if (!Number.isFinite(end) || end === 0) continue;
      const decimals = (text.split(".")[1] || "").length;
      const t0 = performance.now(), dur = 900 + Math.min(600, Math.log10(Math.abs(end) + 1) * 220);
      const tick = (t) => {
        const k = Math.min(1, (t - t0) / dur);
        const eased = 1 - Math.pow(2, -10 * k);   // ease-out expo
        el.textContent = (end * (k === 1 ? 1 : eased)).toFixed(decimals);
        if (k < 1) requestAnimationFrame(tick);
      };
      el.textContent = (0).toFixed(decimals);
      requestAnimationFrame(tick);
    }
  }

  // ------------------------------------------- keep the active tab in view
  const active = document.querySelector(".nav a.link.on");
  if (active && active.parentElement.scrollWidth > active.parentElement.clientWidth) {
    active.parentElement.scrollLeft = active.offsetLeft - active.parentElement.clientWidth / 2 + active.offsetWidth / 2;
  }

  // -------------------------------------------------- command palette (Ctrl/⌘ K)
  const dlg = document.getElementById("palette");
  if (!dlg) return;
  const q = dlg.querySelector("input");
  const list = dlg.querySelector("ul");
  const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const pages = [...document.querySelectorAll(".nav a.link")].map((a) => ({
    label: a.title || a.textContent.trim(), hint: a.getAttribute("href"),
    icon: a.querySelector("use") ? a.querySelector("use").getAttribute("href") : null,
    run: () => { location.href = a.href; },
  }));
  const actions = [
    { label: "Changer de thème", hint: "auto · clair · sombre", icon: "#i-theme", run: () => { close(); cycleTheme(); } },
    { label: "Se déconnecter", hint: "fin de session", icon: "#i-logout",
      run: () => { const f = document.querySelector('form[action="/logout"]'); if (f) f.requestSubmit(); } },
  ];
  let items = [], sel = 0;

  function render() {
    const term = fold(q.value.trim());
    items = [...pages, ...actions].filter((i) => !term || fold(i.label + " " + i.hint).includes(term));
    if (q.value.trim()) {
      items.push({ label: "Demander à l'agent : « " + q.value.trim() + " »", hint: "pré-remplit le chat", icon: "#i-chat",
                   run: () => { location.href = "/chat?q=" + encodeURIComponent(q.value.trim()); } });
    }
    sel = Math.min(sel, Math.max(0, items.length - 1));
    list.replaceChildren(...items.map((it, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === sel));
      if (it.icon) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
        use.setAttribute("href", it.icon); svg.appendChild(use); li.appendChild(svg);
      }
      const b = document.createElement("b"); b.textContent = it.label; li.appendChild(b);   // never innerHTML
      const s = document.createElement("span"); s.textContent = it.hint; li.appendChild(s);
      li.addEventListener("click", () => it.run());
      li.addEventListener("pointermove", () => { if (sel !== i) { sel = i; mark(); } });
      return li;
    }));
  }
  function mark() {
    [...list.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === sel)));
    const cur = list.children[sel]; if (cur) cur.scrollIntoView({ block: "nearest" });
  }
  function open() { q.value = ""; sel = 0; render(); dlg.showModal(); q.focus(); }
  function close() { if (dlg.open) dlg.close(); }

  addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); dlg.open ? close() : open(); }
  });
  const openBtn = document.getElementById("palette-open");
  if (openBtn) openBtn.addEventListener("click", open);
  q.addEventListener("input", () => { sel = 0; render(); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); sel = (sel + 1) % Math.max(1, items.length); mark(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = (sel - 1 + items.length) % Math.max(1, items.length); mark(); }
    else if (e.key === "Enter") { e.preventDefault(); if (items[sel]) items[sel].run(); }
  });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });   // click outside the pane
})();
