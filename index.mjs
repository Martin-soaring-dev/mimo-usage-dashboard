// 包入口：供 MiMo/OpenCode 之类的插件加载器使用。
// 加载器会遍历本模块的所有导出并当作插件函数调用，因此这里只暴露一个 default。
export { default } from "./plugin/usage-dashboard.mjs";
