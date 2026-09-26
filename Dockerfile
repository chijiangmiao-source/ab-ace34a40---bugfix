FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/procedure.db

WORKDIR /app

# 纯标准库实现, 无需安装第三方依赖
COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY web/ ./web/

# 预构建页面到 web/dist (运行时直接托管; verify 会再次构建以校验)
RUN python scripts/build_page.py \
    && mkdir -p /data

EXPOSE 8080

CMD ["python", "-m", "app.server"]
