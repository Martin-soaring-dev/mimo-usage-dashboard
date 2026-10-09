/*
 * Experimental Xiaomi MiMo Desktop renderer patch.
 * Adds “用量统计” below “剩余用量” in the account menu and opens the full
 * local dashboard in a modal iframe. This is not a supported MiMo plugin API.
 */
(() => {
  "use strict";

  if (window.__mimoUsageMenuPatchInstalled) return;
  window.__mimoUsageMenuPatchInstalled = true;

  const LABELS = new Set(["剩余用量", "Remaining usage", "Usage remaining"]);
  const MENU_ATTR = "data-mimo-usage-dashboard-entry";
  const MODAL_ID = "mimo-usage-dashboard-modal";
  const DASHBOARD_URL = "http://127.0.0.1:8790/";

  const style = document.createElement("style");
  style.textContent = `
    [${MENU_ATTR}] { cursor: pointer !important; }
    [${MENU_ATTR}]:hover { background: var(--background-element, rgba(127,127,127,.10)) !important; }
    #${MODAL_ID} { position:fixed; inset:0; z-index:2147483000; display:flex; align-items:center; justify-content:center;
      padding:24px; background:rgba(12,16,22,.42); backdrop-filter:blur(2px); }
    #${MODAL_ID}[hidden] { display:none !important; }
    #${MODAL_ID} .mimo-usage-dialog { width:min(1180px, calc(100vw - 48px)); height:min(820px, calc(100vh - 48px));
      display:flex; flex-direction:column; overflow:hidden; border:1px solid rgba(120,130,145,.22); border-radius:16px;
      background:var(--background, #fff); color:var(--text, #20242a); box-shadow:0 24px 80px rgba(0,0,0,.26); }
    #${MODAL_ID} .mimo-usage-head { height:48px; flex:none; display:flex; align-items:center; gap:12px;
      padding:0 16px; border-bottom:1px solid rgba(120,130,145,.18); font:600 14px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    #${MODAL_ID} .mimo-usage-subtitle { flex:1; font-size:11px; font-weight:400; opacity:.62; }
    #${MODAL_ID} .mimo-usage-close { border:0; background:transparent; color:inherit; width:30px; height:30px;
      border-radius:7px; font-size:20px; line-height:1; cursor:pointer; }
    #${MODAL_ID} .mimo-usage-close:hover { background:rgba(127,127,127,.12); }
    #${MODAL_ID} iframe { border:0; flex:1; width:100%; min-height:0; background:#f7f8fa; }
    #${MODAL_ID} .mimo-usage-fallback { display:none; padding:24px; font:13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    #${MODAL_ID} .mimo-usage-fallback a { color:#356cf6; }
    @media (max-width:700px) { #${MODAL_ID} { padding:8px; } #${MODAL_ID} .mimo-usage-dialog { width:100%; height:100%; border-radius:10px; } }
  `;
  (document.head || document.documentElement).appendChild(style);

  const modal = document.createElement("div");
  modal.id = MODAL_ID;
  modal.hidden = true;
  modal.innerHTML = `
    <section class="mimo-usage-dialog" role="dialog" aria-modal="true" aria-label="用量统计">
      <header class="mimo-usage-head">
        <span>◷</span><span>用量统计</span>
        <span class="mimo-usage-subtitle">MiMo Desktop 本机统计</span>
        <button class="mimo-usage-close" type="button" aria-label="关闭">×</button>
      </header>
      <iframe title="MiMo 用量仪表盘" src="${DASHBOARD_URL}" referrerpolicy="no-referrer"></iframe>
      <div class="mimo-usage-fallback">本机仪表盘暂时没有响应。请确认用量统计服务正在运行，或
        <a href="${DASHBOARD_URL}" target="_blank" rel="noreferrer">在浏览器中打开</a>。</div>
    </section>`;
  document.body.appendChild(modal);

  const close = () => { modal.hidden = true; };
  modal.querySelector(".mimo-usage-close").addEventListener("click", close);
  modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && !modal.hidden) close(); });

  const show = () => { modal.hidden = false; };
  const visible = (el) => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden");

  function findUsageLabel() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!LABELS.has((node.nodeValue || "").trim())) continue;
      let el = node.parentElement;
      if (!visible(el)) continue;
      // Find the menu row, not the tiny text span. The screenshot's account menu
      // rows are roughly 200px wide and 30–50px tall.
      for (let depth = 0; el && depth < 7; depth++, el = el.parentElement) {
        const r = el.getBoundingClientRect();
        if (r.width >= 150 && r.height >= 26 && r.height <= 64 && visible(el)) return { row: el, labelNode: node };
      }
    }
    return null;
  }

  function cloneRow(row) {
    const clone = row.cloneNode(true);
    clone.removeAttribute("id");
    clone.querySelectorAll("[id]").forEach((el) => el.removeAttribute("id"));
    clone.setAttribute(MENU_ATTR, "true");
    clone.setAttribute("role", "menuitem");
    clone.setAttribute("tabindex", "0");
    clone.setAttribute("aria-label", "用量统计");
    clone.querySelectorAll("*").forEach((el) => {
      const t = (el.textContent || "").trim();
      if (t === "订阅" || t === "Subscribe") el.remove();
    });
    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) if (LABELS.has((n.nodeValue || "").trim())) n.nodeValue = "用量统计";
    if (!(clone.textContent || "").includes("用量统计")) clone.append(document.createTextNode("用量统计"));
    clone.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); show(); });
    clone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); show(); }
    });
    return clone;
  }

  function injectMenuItem() {
    if (document.querySelector(`[${MENU_ATTR}]`)) return;
    const found = findUsageLabel();
    if (!found) return;
    const item = cloneRow(found.row);
    found.row.insertAdjacentElement("afterend", item);
  }

  const observer = new MutationObserver(() => injectMenuItem());
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  injectMenuItem();
  console.info("[mimo-usage-dashboard] Desktop menu patch active");
})();
