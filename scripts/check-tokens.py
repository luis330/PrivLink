"""设计 token 执法检查：扫描 index.html <style> 块内布局属性的数字裸值。

执法属性（7 项）：font-size / border-radius / padding* / margin* / *gap /
box-shadow / backdrop-filter。约定与 AGENTS.md「前端样式约束」一致：

- 值一律引用 :root 的设计 token（var(...)），颜色字面量另有人管，这里不查；
- 白名单：:root 块内部、行内含 ``token-allow`` 注释（例外须在文件内注册理由）、
  无单位 0（归零的自然写法）；
- var(...) 引用整体剔除后再找数字，因此 var(--sp-4) 这类带数字的 token 名不误报。

违例输出「文件行号 + 原行」并退出码 1；通过则输出扫描统计并退出码 0。

用法：python scripts/check-tokens.py [index.html 路径]
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

# margin-top / padding-left 等派生属性按前缀收编，row-gap / column-gap 按后缀收编
PROPS_EXACT = {"font-size", "border-radius", "box-shadow", "backdrop-filter"}
PROPS_PREFIX = ("margin", "padding")
PROPS_SUFFIX = ("gap",)

ALLOW_MARK = "token-allow"
DECL_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;{}]+)")
VAR_RE = re.compile(r"var\([^)]*\)")
NUM_RE = re.compile(r"\d*\.?\d+")


def _is_enforced(prop: str) -> bool:
    return (
        prop in PROPS_EXACT
        or prop.startswith(PROPS_PREFIX)
        or prop.endswith(PROPS_SUFFIX)
    )


def scan_index(path: Path) -> list[str]:
    """返回违例清单（含文件行号的描述），无违例返回空列表。"""
    text = io.open(path, encoding="utf-8").read()
    violations: list[str] = []
    declared = 0
    for style in re.finditer(r"<style>(.*?)</style>", text, re.S):
        base_line = text[: style.start(1)].count("\n") + 1
        in_root = False
        for offset, raw in enumerate(style.group(1).splitlines()):
            line_no = base_line + offset
            stripped = raw.strip()
            if stripped.startswith(":root"):
                in_root = True
            if in_root:
                if "}" in stripped:
                    in_root = False
                continue
            if ALLOW_MARK in raw:
                continue
            for match in DECL_RE.finditer(stripped):
                prop, value = match.group(1), match.group(2)
                if not _is_enforced(prop):
                    continue
                declared += 1
                residue = VAR_RE.sub("", value)
                for num in NUM_RE.findall(residue):
                    if num == "0":
                        continue
                    violations.append(
                        f"{path.name}:{line_no}: {prop} 裸值 {num!r} —— {raw.strip()}"
                    )
                    break
    if not declared:
        violations.insert(0, "未扫描到任何受管属性声明，检查 <style> 块是否完整")
    return violations


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "index.html"
    if not target.exists():
        print(f"目标文件不存在：{target}")
        return 2
    violations = scan_index(target)
    for item in violations:
        print(item)
    if violations:
        print(f"\n共 {len(violations)} 处违例：改用 :root token，或在行内加 /* {ALLOW_MARK}: 理由 */ 注释")
        return 1
    print(f"token 执法通过：{target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
