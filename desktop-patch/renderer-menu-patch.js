/*
 * Experimental Xiaomi MiMo Desktop renderer patch.
 * Adds “用量统计” to the account menu and opens the local dashboard in a modal.
 * Expects ./renderer-menu-patch.css (loaded via <link>). Not an official plugin API.
 */
(() => {
  "use strict";

  if (window.__mimoUsageMenuPatchInstalled) return;
  window.__mimoUsageMenuPatchInstalled = true;

  const ITEM_CLASS = "mimo-usage-menu-item";
  const MODAL_CLASS = "mimo-usage-overlay";
  const DASHBOARD_URL = "http://127.0.0.1:8790/";
  const USAGE_LABELS = new Set(["剩余用量", "Remaining usage", "Usage remaining"]);
  const SIGN_OUT_LABELS = new Set(["退出登录", "Sign out", "Log out"]);
  const ITEM_ICON = `<svg class="mimo-usage-menu-item__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v4.8l3 1.9"/></svg>`;

  const log = (...args) => console.info("[mimo-usage-dashboard]", ...args);
  const visible = (el) => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden");

  function ensureStylesheet() {
    if (document.querySelector('link[data-mimo-usage-css]')) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "./usage-dashboard-menu-patch.css";
    link.setAttribute("data-mimo-usage-css", "true");
    (document.head || document.documentElement).appendChild(link);
  }

  // Locate the menu row (a row-sized ancestor) of a text node with the given label.
  function rowOf(labelSet) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!labelSet.has((node.nodeValue || "").trim())) continue;
      let el = node.parentElement;
      for (let depth = 0; el && depth < 7; depth++, el = el.parentElement) {
        const r = el.getBoundingClientRect();
        if (visible(el) && r.height >= 26 && r.height <= 58 && r.width >= 140 && r.width <= 340) return el;
      }
    }
    return null;
  }

  function buildModal() {
    const overlay = document.createElement("div");
    overlay.className = MODAL_CLASS;
    overlay.hidden = true;
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "用量统计");
    overlay.innerHTML = `
      <section class="mimo-usage-dialog">
        <header class="mimo-usage-head">
          <span>用量统计</span>
          <span class="mimo-usage-head__subtitle">MiMo Desktop 本机统计</span>
          <button class="mimo-usage-head__close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="mimo-usage-body">
          <iframe title="MiMo 用量仪表盘" referrerpolicy="no-referrer"></iframe>
          <div class="mimo-usage-fallback">
            <div>本机仪表盘暂时没有响应。</div>
            <a href="${DASHBOARD_URL}" target="_blank" rel="noreferrer">在浏览器中打开</a>
          </div>
        </div>
      </section>`;
    const body = overlay.querySelector(".mimo-usage-body");
    const iframe = overlay.querySelector("iframe");
    overlay.querySelector(".mimo-usage-head__close").addEventListener("click", () => closeModal());
    overlay.addEventListener("click", (event) => { if (event.target === overlay) closeModal(); });
    window.addEventListener("keydown", (event) => { if (event.key === "Escape" && !overlay.hidden) closeModal(); });
    (document.documentElement || document.body).appendChild(overlay);

    let loadTimer = null;
    function closeModal() {
      overlay.hidden = true;
      if (loadTimer) clearTimeout(loadTimer);
    }
    function showModal() {
      body.classList.remove("is-error");
      if (iframe.getAttribute("src") !== DASHBOARD_URL) iframe.setAttribute("src", DASHBOARD_URL);
      overlay.hidden = false;
      log("open dashboard modal");
      if (loadTimer) clearTimeout(loadTimer);
      loadTimer = setTimeout(() => {
        // Detect the common failure mode instead of showing an empty frame.
        try { if (!iframe.contentDocument || !iframe.contentDocument.body) body.classList.add("is-error"); } catch { /* cross-origin is fine */ }
      }, 4000);
    }
    return { overlay, showModal, closeModal };
  }

  const modal = buildModal();
  let injected = false;

  function buildItem() {
    const item = document.createElement("button");
    item.type = "button";
    item.className = ITEM_CLASS;
    item.setAttribute("data-mimo-usage-dashboard-entry", "true");
    item.setAttribute("role", "menuitem");
    item.innerHTML = `${ITEM_ICON}<span class="mimo-usage-menu-item__label">用量统计</span>`;
    const open = (event) => { event.preventDefault(); event.stopPropagation(); modal.showModal(); };
    item.addEventListener("pointerdown", open, true);
    item.addEventListener("click", open, true);
    item.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") open(event); });
    return item;
  }

  function injectItem() {
    if (injected && document.querySelector('[data-mimo-usage-dashboard-entry]')) return;
    const signOut = rowOf(SIGN_OUT_LABELS);
    const container = signOut && signOut.parentElement;
    if (!container || !visible(container)) return;
    const usage = rowOf(USAGE_LABELS);
    const item = buildItem();
    // Prefer to place it just above “剩余用量”; otherwise just above “退出登录”.
    const anchor = usage && usage.parentElement === container ? usage : signOut;
    container.insertBefore(item, anchor);
    injected = true;
    log("menu item injected");
  }

  ensureStylesheet();
  const observer = new MutationObserver(() => {
    ensureStylesheet();
    if (!document.querySelector('[data-mimo-usage-dashboard-entry]')) injectItem();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  injectItem();
  log("renderer menu patch active");
})();
