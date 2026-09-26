#!/usr/bin/env python3
"""构建页面: 校验源资源并输出到 web/dist (无外部依赖, 可离线构建)。"""

from __future__ import annotations

import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "web", "src")
DIST = os.path.join(ROOT, "web", "dist")

REQUIRED = ["index.html", "styles.css", "app.js"]


def fail(msg: str) -> None:
    print(f"[build] 失败: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    for name in REQUIRED:
        if not os.path.isfile(os.path.join(SRC, name)):
            fail(f"缺少源文件 web/src/{name}")

    html = open(os.path.join(SRC, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(SRC, "app.js"), encoding="utf-8").read()
    css = open(os.path.join(SRC, "styles.css"), encoding="utf-8").read()

    # 引用完整性: html 中引用的本地资源必须存在, JS 中调用的 API 路径做基本检查
    for ref in re.findall(r'(?:href|src)="([^"]+)"', html):
        if ref.startswith(("http://", "https://", "#")):
            continue
        if not os.path.isfile(os.path.join(SRC, ref)):
            fail(f"index.html 引用了不存在的资源 {ref}")
    for api_path in ["/api/health", "/api/state", "/api/facts",
                     "/api/rules", "/api/retract"]:
        if api_path not in js:
            fail(f"app.js 缺少接口调用 {api_path}")
    if not css.strip():
        fail("styles.css 为空")

    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    shutil.copytree(SRC, DIST)

    copied = sorted(os.listdir(DIST))
    total = sum(os.path.getsize(os.path.join(DIST, f)) for f in copied)
    print(f"[build] 页面已构建到 web/dist: {', '.join(copied)} ({total} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
