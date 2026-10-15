// 用 CDP 给本地 HTML 截图（沙箱里 chrome --screenshot 不可用时的替代方案）
// 用法: node test/shot.mjs <file.html> <out.png> [width] [height]
// Chrome 路径可用 CHROME_PATH 覆盖；找不到浏览器时退出码 3。
import { fileUrl, openPage } from "./cdp.mjs";

const [htmlPath, outPng, w = "1000", h = "2600"] = process.argv.slice(2);
if (!htmlPath || !outPng) {
  console.error("usage: node test/shot.mjs <file.html> <out.png> [w] [h]");
  process.exit(2);
}

const page = await openPage({ width: Number(w), height: Number(h) });
if (!page) {
  console.error("未找到可用的 Chrome（可用 CHROME_PATH 指定）");
  process.exit(3);
}

try {
  await page.goto(fileUrl(htmlPath), 1500);
  await page.screenshot(outPng);
  console.log(`shot ok: ${outPng}`);
} catch (err) {
  console.error(`shot failed: ${err.message}`);
  process.exitCode = 4;
} finally {
  await page.close();
}
