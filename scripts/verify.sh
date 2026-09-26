#!/usr/bin/env bash
# Compose verify 服务入口: 一次执行内完成
#   1) 规则逻辑代码测试
#   2) 构建页面
#   3) 以真实 API/HTTP 冒烟验证:
#      - 撤回一条原始事实后结论凭替代依据保留
#      - 撤回最后依据后该结论与唯一下游失效, 并返回传播链
# 完成后退出, 以退出码报告结果 (任一环节失败即非零)。
set -euo pipefail

# 容器内代码位于 /app; 本地执行时定位到仓库根 (scripts 的上一级)
if [ -d /app ]; then cd /app; else cd "$(dirname "$0")/.."; fi

# 容器镜像内命令为 python, 部分本地环境只有 python3
if command -v python >/dev/null 2>&1; then PY=python; else PY=python3; fi

echo "==================== [verify 1/3] 规则逻辑单元测试 ===================="
"$PY" -m unittest discover -s tests -v

echo "==================== [verify 2/3] 构建页面 ===================="
"$PY" scripts/build_page.py

echo "==================== [verify 3/3] API/HTTP 冒烟 ===================="
# 在本容器内自起真实 HTTP 服务 (127.0.0.1:${SMOKE_PORT}), 经网络接口验证
SMOKE_PORT="${SMOKE_PORT:-8099}" "$PY" scripts/smoke_http.py

echo "==================== verify 全部通过 ===================="
