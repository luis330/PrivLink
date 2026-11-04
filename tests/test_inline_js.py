"""内联 JS 语法门禁：宿主 index.html 与每个插件的内联 <script> 必须能被 V8 编译。

实际编译逻辑在 scripts/check-inline-js.cjs（node，只编译不执行），这里以子进程跑
一遍 CLI，保证「pytest 通过 == 内联 JS 无语法错」，并让既有 CI 的 pytest 步骤自动
携带该检查（与 test_frontend_tokens.py 同款范式；CI 的 setup-node 排在 pytest 之前）。

本机没装 node 时跳过——仓库不把 node 声明为依赖；但 CI 里 node 必须有，否则这枚门禁
会退化成"永远绿的空检查"，所以 CI 环境下缺 node 直接判失败。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InlineJsSyntaxTest(unittest.TestCase):
    def test_host_and_plugin_inline_js_compile(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI"):
                self.fail(
                    "CI 环境缺少 node：内联 JS 语法门禁会静默跳过（检查 workflow 的 setup-node 步骤）"
                )
            self.skipTest("本机未安装 node，跳过内联 JS 语法门禁")

        script = ROOT / "scripts" / "check-inline-js.cjs"
        proc = subprocess.run(
            [node, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        self.assertEqual(
            0,
            proc.returncode,
            f"内联 JS 语法检查未通过：\n{proc.stdout}\n{proc.stderr}",
        )
        # 断言逐文件回显与汇总行存在：既证明覆盖到插件，也证明脚本确实跑完而非静默跳过
        self.assertIn("OK   index.html", proc.stdout)
        self.assertIn("OK   plugins/anniversary/index.html", proc.stdout)
        self.assertIn("内联 JS 语法检查通过", proc.stdout)
        self.assertIn("JS OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
