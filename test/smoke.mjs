// 冒烟测试：加载插件入口，确认导出与工具形状正确。
// 不启动服务、不读取数据库。
import assert from "node:assert/strict";
import plugin from "../index.mjs";

assert.equal(typeof plugin, "function", "index.mjs 应默认导出一个插件函数");

const hooks = await plugin({}, {});
assert.ok(hooks && typeof hooks === "object", "插件应返回 hooks 对象");
assert.ok(hooks.tool && typeof hooks.tool === "object", "hooks.tool 应为对象");

const names = Object.keys(hooks.tool).sort();
assert.deepEqual(names, ["usage_dashboard", "usage_stats"], `工具名不符：${names.join(",")}`);

for (const [name, def] of Object.entries(hooks.tool)) {
  assert.equal(typeof def.description, "string", `${name}.description 应为字符串`);
  assert.ok(def.description.length > 0, `${name}.description 不应为空`);
  assert.ok(def.args && typeof def.args === "object", `${name}.args 应为对象`);
  assert.equal(typeof def.execute, "function", `${name}.execute 应为函数`);
}

console.log(`smoke ok: ${names.join(", ")}`);
