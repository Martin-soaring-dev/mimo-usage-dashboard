/*
 * Experimental Xiaomi MiMo Desktop renderer patch.
 * Adds “用量统计” to the account menu. MiMo runs its renderer on a secure custom
 * scheme, so an http://127.0.0.1 iframe is blocked as mixed content; the item
 * therefore opens the local dashboard via window.open(), which MiMo routes to the
 * system browser. Not an official plugin API.
 */
(() => {
  "use strict";

  if (window.__mimoUsageMenuPatchInstalled) return;
  window.__mimoUsageMenuPatchInstalled = true;

  const ITEM_CLASS = "mimo-usage-menu-item";
  const DASHBOARD_URL = "http://127.0.0.1:8790/";
  const USAGE_LABELS = new Set(["剩余用量", "Remaining usage", "Usage remaining"]);
  const SIGN_OUT_LABELS = new Set(["退出登录", "Sign out", "Log out"]);
  const ICON = `<svg class="mimo-usage-menu-item__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v4.8l3 1.9"/></svg>`;

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

  function openDashboard() {
    log("open dashboard in browser:", DASHBOARD_URL);
    // MiMo's window-open handler calls shell.openExternal and denies the popup,
    // so this opens the dashboard in the default browser.
    const win = window.open(DASHBOARD_URL, "_blank", "noopener,noreferrer");
    if (win) { try { win.focus(); } catch { /* ignore */ } }
  }

  function buildItem() {
    const item = document.createElement("button");
    item.type = "button";
    item.className = ITEM_CLASS;
    item.setAttribute("data-mimo-usage-dashboard-entry", "true");
    item.setAttribute("role", "menuitem");
    item.innerHTML = `${ICON}<span class="mimo-usage-menu-item__label">用量统计</span>`;
    const open = (event) => { event.preventDefault(); event.stopPropagation(); openDashboard(); };
    item.addEventListener("pointerdown", open, true);
    item.addEventListener("click", open, true);
    item.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") open(event); });
    return item;
  }

  let injected = false;
  function injectItem() {
    if (injected && document.querySelector('[data-mimo-usage-dashboard-entry]')) return;
    const signOut = rowOf(SIGN_OUT_LABELS);
    const container = signOut && signOut.parentElement;
    if (!container || !visible(container)) return;
    const usage = rowOf(USAGE_LABELS);
    const anchor = usage && usage.parentElement === container ? usage : signOut;
    container.insertBefore(buildItem(), anchor);
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
