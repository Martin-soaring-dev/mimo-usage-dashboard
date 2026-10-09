/*
 * Experimental Xiaomi MiMo Desktop renderer patch.
 * Adds “用量统计” to the account menu and opens the local dashboard in an in-app
 * modal (iframe). Success is detected via the iframe load event, with retries,
 * instead of inspecting contentDocument (which is cross-origin). Not an official API.
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
          <button class="mimo-usage-head__refresh" type="button" title="重新加载">↻</button>
          <button class="mimo-usage-head__close" type="button" aria-label="关闭">×</button>
        </header>
        <div class="mimo-usage-body">
          <iframe title="MiMo 用量仪表盘" src="${DASHBOARD_URL}" referrerpolicy="no-referrer"></iframe>
          <div class="mimo-usage-fallback">
            <div class="mimo-usage-fallback__text">正在连接本机用量服务…</div>
            <button class="mimo-usage-fallback__retry" type="button">重试</button>
            <a class="mimo-usage-fallback__link" href="${DASHBOARD_URL}" target="_blank" rel="noreferrer">在浏览器中打开</a>
          </div>
        </div>
      </section>`;
    (document.documentElement || document.body).appendChild(overlay);

    const body = overlay.querySelector(".mimo-usage-body");
    const iframe = overlay.querySelector("iframe");
    const fallbackText = overlay.querySelector(".mimo-usage-fallback__text");
    const iframeLoadUrl = () => DASHBOARD_URL + (DASHBOARD_URL.includes("?") ? "&" : "?") + "t=" + Date.now();
    let loaded = false;
    let timer = null;
    let attempts = 0;

    iframe.addEventListener("load", () => {
      loaded = true;
      attempts = 0;
      body.classList.remove("is-error");
      if (timer) clearTimeout(timer);
      log("dashboard iframe loaded");
    });

    function schedule() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        if (loaded) return;
        attempts += 1;
        if (attempts <= 2) {
          log("dashboard iframe not loaded yet; retrying", attempts);
          iframe.setAttribute("src", iframeLoadUrl());
          schedule();
        } else {
          fallbackText.textContent = "本机仪表盘暂时没有响应（请确认用量服务在运行）。";
          body.classList.add("is-error");
        }
      }, 6000);
    }

    function show() {
      body.classList.remove("is-error");
      fallbackText.textContent = "正在连接本机用量服务…";
      if (!loaded) iframe.setAttribute("src", iframeLoadUrl());
      overlay.hidden = false;
      attempts = 0;
      schedule();
      log("open dashboard modal");
    }
    function close() {
      overlay.hidden = true;
      if (timer) clearTimeout(timer);
    }
    function retry() {
      loaded = false;
      attempts = 0;
      body.classList.remove("is-error");
      fallbackText.textContent = "正在连接本机用量服务…";
      iframe.setAttribute("src", iframeLoadUrl());
      schedule();
    }

    overlay.querySelector(".mimo-usage-head__close").addEventListener("click", close);
    overlay.querySelector(".mimo-usage-head__refresh").addEventListener("click", retry);
    overlay.querySelector(".mimo-usage-fallback__retry").addEventListener("click", retry);
    overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
    window.addEventListener("keydown", (event) => { if (event.key === "Escape" && !overlay.hidden) close(); });
    return { show };
  }

  const modal = buildModal();
  let injected = false;

  function buildItem() {
    const item = document.createElement("button");
    item.type = "button";
    item.className = ITEM_CLASS;
    item.setAttribute("data-mimo-usage-dashboard-entry", "true");
    item.setAttribute("role", "menuitem");
    item.innerHTML = `${ICON}<span class="mimo-usage-menu-item__label">用量统计</span>`;
    const open = (event) => { event.preventDefault(); event.stopPropagation(); modal.show(); };
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
