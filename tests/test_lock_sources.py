"""锁文件必须指向官方 PyPI。

GitHub Actions 的 runner 在境外，境内镜像（清华 / 阿里 / 中科大等）连不上，会以
"Failed to download ... operation timed out" 让 docker-release 与 Workers 部署红掉。
本地用 `UV_DEFAULT_INDEX` 指向镜像没问题，但锁文件是提交物，必须保持官方源。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILES = ("uv.lock", "pylock.toml")
MIRROR_HOSTS = (
    "tuna.tsinghua",
    "pypi.douban",
    "mirrors.aliyun",
    "mirrors.ustc",
    "mirrors.cloud.tencent",
    "repo.huaweicloud",
)


class LockSourceTest(unittest.TestCase):
    def test_locks_do_not_point_at_china_mirrors(self) -> None:
        for name in LOCK_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            for host in MIRROR_HOSTS:
                with self.subTest(file=name, host=host):
                    self.assertNotIn(
                        host,
                        text,
                        f"{name} 仍引用镜像源 {host}：境外 runner 会超时，请改成官方 PyPI 后提交",
                    )

    def test_uv_lock_uses_canonical_pypi_hosts(self) -> None:
        text = (ROOT / "uv.lock").read_text(encoding="utf-8")
        hosts = set(re.findall(r'https://([^/"\s]+)', text))
        for host in hosts:
            with self.subTest(host=host):
                self.assertIn(
                    host,
                    {"pypi.org", "files.pythonhosted.org"},
                    f"uv.lock 引用了非官方下载源：{host}",
                )


if __name__ == "__main__":
    unittest.main()
