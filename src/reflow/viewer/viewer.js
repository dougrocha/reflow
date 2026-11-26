// reflow viewer: loads chunks into their slots as the reader nears them, and on
// contents/deep-link jumps. Loaded chunks stay in the DOM (no virtualization), so
// screen-reader browse mode, heading navigation and Ctrl+F keep working.
(() => {
  "use strict";
  const slots = [...document.querySelectorAll(".chunk[data-src]")];
  const status = document.getElementById("status");
  const pending = new Map();

  const say = (msg) => { if (status) status.textContent = msg; };
  const isLoaded = (slot) => slot.dataset.loaded === "1";
  const slotForHref = (href) => {
    const path = new URL(href, document.baseURI).pathname;
    return slots.find((s) => new URL(s.dataset.src, document.baseURI).pathname === path);
  };

  const tableResize = new ResizeObserver((entries) => {
    for (const { target } of entries) {
      const wrap = target.closest(".table-wrap");
      if (wrap.scrollWidth > wrap.clientWidth) wrap.setAttribute("tabindex", "0");
      else wrap.removeAttribute("tabindex");
    }
  });
  function observeTables(root) {
    for (const wrap of root.querySelectorAll(".table-wrap")) {
      tableResize.observe(wrap);
      tableResize.observe(wrap.querySelector("table"));
    }
  }
  observeTables(document);

  // A failed slot keeps its stub link and gets a retry button; retrying clears the failure.
  function markFailed(slot) {
    slot.dataset.failed = "1";
    if (slot.querySelector(".chunk-retry")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chunk-retry";
    button.textContent = `Try again: pages ${slot.dataset.first} to ${slot.dataset.last}`;
    button.addEventListener("click", () => {
      delete slot.dataset.failed;
      button.remove();
      say("Loading…");
      load(slot).then(() => say(isLoaded(slot) ? "" : "This section could not be loaded."));
    });
    slot.append(button);
  }

  function load(slot) {
    if (isLoaded(slot)) return Promise.resolve();
    if (pending.has(slot)) return pending.get(slot);
    slot.setAttribute("aria-busy", "true");
    const p = fetch(slot.dataset.src)
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); })
      .then((html) => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const content = doc.querySelector(".chunk-content");
        if (!content) throw new Error("no content");
        slot.replaceChildren(...[...content.childNodes].map((n) => document.adoptNode(n)));
        slot.dataset.loaded = "1";
        observeTables(slot);
      })
      .catch(() => markFailed(slot)) // the stub link still works
      .finally(() => {
        slot.removeAttribute("aria-busy");
        pending.delete(slot);
      });
    pending.set(slot, p);
    return p;
  }

  // Load ahead of the reader.
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { io.unobserve(e.target); load(e.target); }
      }
    }, { rootMargin: "2000px 0px" });
    slots.filter((s) => !isLoaded(s)).forEach((s) => io.observe(s));
  }

  function focusTarget(el) {
    if (!el) return;
    if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
    el.scrollIntoView({ block: "start" });
    el.focus({ preventScroll: true });
  }

  async function go(slot, hash) {
    if (!isLoaded(slot)) {
      say("Loading…");
      await load(slot);
      say(isLoaded(slot) ? "" : "This section could not be loaded.");
    }
    if (!isLoaded(slot)) return false;
    const target = hash ? document.getElementById(hash) : slot.querySelector("h2, h3, h4, h5, h6, .pg");
    focusTarget(target);
    if (hash) history.replaceState(null, "", `#${hash}`);
    return Boolean(target);
  }

  // Contents and "continue reading" links point at standalone chunk pages; with JS,
  // load the chunk in place instead.
  document.addEventListener("click", (event) => {
    const a = event.target.closest('a[href^="chunks/"]');
    if (!a || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    const [path, hash] = a.getAttribute("href").split("#");
    const slot = slotForHref(path);
    if (!slot) return;
    event.preventDefault();
    go(slot, hash).then((ok) => { if (!ok) location.href = a.href; });
  });

  // Deep links: #p37 (page) or a heading anchor.
  async function openHash() {
    const hash = decodeURIComponent(location.hash.slice(1));
    if (!hash || document.getElementById(hash)) return;
    const page = /^p(\d+)$/.exec(hash) ?? /^n(\d+)-/.exec(hash);
    let slot;
    if (page) {
      const n = Number(page[1]);
      slot = slots.find((s) => Number(s.dataset.first) <= n && n <= Number(s.dataset.last));
    }
    if (!slot) {
      try {
        const toc = await (await fetch("toc.json")).json();
        const entry = toc.find((e) => e.id === hash);
        if (entry) slot = slotForHref(entry.href);
      } catch { /* no toc: nothing to do */ }
    }
    if (slot) go(slot, hash);
  }
  window.addEventListener("hashchange", openHash);
  openHash();

  // Everything at once, for find-in-page and screen-reader heading lists.
  const all = document.getElementById("load-all");
  if (all && slots.some((s) => !isLoaded(s))) {
    all.hidden = false;
    all.addEventListener("click", async () => {
      all.disabled = true;
      say("Loading the entire document…");
      for (const s of slots) await load(s);
      const failed = slots.filter((s) => !isLoaded(s)).length;
      say(failed ? `Loaded, but ${failed} sections could not be loaded.` : "The entire document is loaded.");
      all.hidden = !failed;
      all.disabled = false;
    });
  }

  // Native print (Ctrl+P) prints only what is in the DOM. Say what is missing, and mark each
  // unloaded section with a placeholder line, so a partial printout is never silent.
  function preparePrint() {
    const missing = slots.filter((s) => !isLoaded(s));
    if (!missing.length) return;
    const notice = document.createElement("p");
    notice.className = "print-notice";
    notice.textContent = `${missing.length} sections were not loaded; use Load all before printing`;
    (document.getElementById("content") ?? document.body).prepend(notice);
    for (const s of missing) {
      const line = document.createElement("p");
      line.className = "print-placeholder";
      line.textContent = `Pages ${s.dataset.first} to ${s.dataset.last} were not loaded.`;
      s.append(line);
    }
  }
  function cleanupPrint() {
    document.querySelectorAll(".print-notice, .print-placeholder").forEach((el) => el.remove());
  }
  window.addEventListener("beforeprint", preparePrint);
  window.addEventListener("afterprint", cleanupPrint);
})();
