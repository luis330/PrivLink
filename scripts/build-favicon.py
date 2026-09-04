#!/usr/bin/env python3
"""把一组 PNG 打包成多尺寸 favicon.ico。

ICO 容器允许直接内嵌 PNG（Vista 起支持），因此不需要光栅化依赖，纯标准库即可。
之所以需要这一步：favicon 生成器导出的 "favicon.ico" 有时其实是裸 PNG 改了扩展名，
以 Content-Type: image/x-icon 发出去时名不副实，老浏览器与部分爬虫不认。
tests/test_favicon.py 会校验产物确实是 ICO 容器。

用法：
  python scripts/build-favicon.py <png> [<png> ...] [-o favicon.ico]
  python scripts/build-favicon.py --inspect <file> [<file> ...]   # 只看格式与尺寸

示例：
  python scripts/build-favicon.py icons/favicon-16x16.png icons/favicon-32x32.png
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def read_png_size(data: bytes) -> tuple[int, int]:
    """从 IHDR 读出宽高；非 PNG 抛 ValueError。"""
    if data[:8] != PNG_MAGIC or data[12:16] != b"IHDR":
        raise ValueError("不是 PNG 文件")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def describe(path: Path) -> str:
    data = path.read_bytes()
    if data[:4] == b"\x00\x00\x01\x00":
        count = int.from_bytes(data[4:6], "little")
        sizes = []
        for i in range(count):
            entry = data[6 + i * 16 : 22 + i * 16]
            w = entry[0] or 256
            h = entry[1] or 256
            sizes.append(f"{w}x{h}")
        return f"{path.name}: ICO 容器，{count} 张（{', '.join(sizes)}），{len(data)} 字节"
    try:
        width, height = read_png_size(data)
    except ValueError:
        return f"{path.name}: 未知格式，头 {data[:4]!r}，{len(data)} 字节"
    return f"{path.name}: 裸 PNG {width}x{height}，{len(data)} 字节"


def build_ico(images: list[tuple[int, int, bytes]]) -> bytes:
    """images: [(宽, 高, PNG 字节), ...]，按宽度升序写入。"""
    images = sorted(images, key=lambda item: item[0])
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=1(icon), count

    offset = 6 + len(images) * 16
    entries = []
    for width, height, data in images:
        entries.append(
            struct.pack(
                "<BBBBHHII",
                0 if width >= 256 else width,   # 256 在目录项里记作 0
                0 if height >= 256 else height,
                0,                              # 调色板色数：PNG 不使用
                0,                              # reserved
                1,                              # color planes
                32,                             # bits per pixel
                len(data),
                offset,
            )
        )
        offset += len(data)
    return b"".join([header, *entries, *(data for _, _, data in images)])


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    if argv[0] == "--inspect":
        targets = [Path(a) for a in argv[1:]]
        if not targets:
            print("--inspect 需要至少一个文件路径")
            return 2
        for path in targets:
            print(describe(path) if path.is_file() else f"{path}: 文件不存在")
        return 0

    out = Path("favicon.ico")
    sources: list[Path] = []
    i = 0
    while i < len(argv):
        if argv[i] in ("-o", "--output"):
            if i + 1 >= len(argv):
                print("-o 后面要跟输出路径")
                return 2
            out = Path(argv[i + 1])
            i += 2
            continue
        sources.append(Path(argv[i]))
        i += 1

    images: dict[int, tuple[int, int, bytes]] = {}
    for path in sources:
        if not path.is_file():
            print(f"跳过：{path} 不存在")
            continue
        data = path.read_bytes()
        try:
            width, height = read_png_size(data)
        except ValueError:
            print(f"跳过：{path.name} 不是 PNG（{describe(path)}）")
            continue
        if width > 256 or height > 256:
            print(f"跳过：{path.name} {width}x{height} 超出 ICO 上限 256")
            continue
        # 同尺寸出现多次时后者覆盖前者
        images[width] = (width, height, data)

    if not images:
        print("没有可用的 PNG 输入，未生成任何文件")
        return 1

    out.write_bytes(build_ico(list(images.values())))
    print(f"已生成 {out}：{describe(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
