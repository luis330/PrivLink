// 语法自检：编译 index.html 的内联 JS（不执行）。用法：node scripts/check-inline-js.cjs
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) {
  console.error("未找到内联 <script> 块");
  process.exit(1);
}
new vm.Script(match[1]);
console.log("JS OK");
