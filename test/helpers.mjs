// test/helpers.mjs — 测试共用工具
// 与 register_command.py 保持一致的 JSONC 剥离与模板生成逻辑（便于交叉校验）。

/** 剥离 JSONC 的行注释与块注释（跳过字符串内部），并容忍尾随逗号。 */
export function stripJsonc(text) {
  let out = "";
  let i = 0;
  let inStr = false;
  let quote = "";
  while (i < text.length) {
    const ch = text[i];
    if (inStr) {
      out += ch;
      if (ch === "\\" && i + 1 < text.length) { out += text[i + 1]; i += 2; continue; }
      if (ch === quote) inStr = false;
      i++;
      continue;
    }
    if (ch === '"' || ch === "'") { inStr = true; quote = ch; out += ch; i++; continue; }
    if (ch === "/" && text[i + 1] === "/") { while (i < text.length && !"\r\n".includes(text[i])) i++; continue; }
    if (ch === "/" && text[i + 1] === "*") {
      i += 2;
      while (i + 1 < text.length && !(text[i] === "*" && text[i + 1] === "/")) i++;
      i += 2;
      continue;
    }
    out += ch;
    i++;
  }
  return out.replace(/,(\s*[}\]])/g, "$1");
}

const q = (s) => (" " in s && !s.startsWith('"') ? `"${s}"` : s);

/** 与 register_command.py 的 build_usage_template 同构（仅用于形状断言）。 */
export function buildUsageTemplate(py, script) {
  const cmd = `${q(py)} ${q(script)} --arg-string "$ARGUMENTS"`;
  return (
    "用户请求查看 MiMo Desktop 本机用量。下面一行是本地脚本的真实输出" +
    "（已生成好静态 HTML 页面，脚本零 AI 参与、纯离线）：\n\n" +
    "!`" + cmd + "`\n\n" +
    "请把上面这一行输出**原样**告诉用户，并提醒他打开该 HTML 文件即可看到" +
    "完整的用量看板（Token 用量 / Session 用量 / 上下文占用）。\n" +
    "不要臆造或改写任何数字，不要自己渲染图表，也不要额外调用其它工具。"
  );
}
