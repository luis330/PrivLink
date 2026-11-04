"""前端 assets 同步执法：仓库根目录原件与 assets/ 副本必须逐字节一致。

实际比对逻辑在 scripts/sync-frontend.py --check（只打印差异、退出码 1 = 有漂移，
不复制也不删除任何文件）。这里以子进程跑一遍 CLI，保证「pytest 通过 == 同步无
漂移」，并让既有 CI 的 pytest 步骤自动携带该检查（与 test_frontend_tokens.py
同款范式）。

Workers 部署服务的是 assets/ 那份副本，本地部署读根目录原件——两份不一致时
线上与本地表现会分叉，因此这里必须红。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FrontendSyncEnforcementTest(unittest.TestCase):
    def test_assets_mirror_matches_repo_root(self) -> None:
        script = ROOT / "scripts" / "sync-frontend.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        self.assertEqual(
            0,
            proc.returncode,
            "assets 副本与仓库根目录不一致（先运行 python scripts/sync-frontend.py）："
            f"\n{proc.stdout}\n{proc.stderr}",
        )
        # 断言逐文件回显存在，证明脚本确实跑完而非静默跳过；
        # 只比对纯 ASCII 片段，避免依赖中文在管道上的编码往返
        self.assertIn("OK   index.html", proc.stdout)
        self.assertIn("OK   plugins/anniversary/index.html", proc.stdout)


if __name__ == "__main__":
    unittest.main()
