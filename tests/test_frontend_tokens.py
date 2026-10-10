"""前端设计 token 执法：index.html 内联 CSS 不得出现白名单外的裸值。

实际扫描逻辑在 scripts/check-tokens.py，这里以子进程跑一遍 CLI，
保证「pytest 通过 == 执法通过」，并让既有 CI 的 pytest 步骤自动携带该检查。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FrontendTokenEnforcementTest(unittest.TestCase):
    def test_index_html_has_no_raw_values_outside_whitelist(self) -> None:
        script = ROOT / "scripts" / "check-tokens.py"
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        self.assertEqual(
            0,
            proc.returncode,
            f"token 执法未通过：\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("token 执法通过", proc.stdout)


if __name__ == "__main__":
    unittest.main()
