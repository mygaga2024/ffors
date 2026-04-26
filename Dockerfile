# ============================================================
# FFORS Dockerfile
# 基于 python:3.12-slim，多阶段构建，最小化镜像体积
# 遵循 DEVELOPMENT_PROTOCOL.md §5 环境控制
# ============================================================

FROM python:3.12-slim AS base

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 尊重代理配置 (DEVELOPMENT_PROTOCOL.md §5)
ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY}

WORKDIR /app

# --- 依赖安装阶段 ---
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 运行阶段 ---
FROM base AS runtime

# 从依赖阶段复制已安装的包
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# 预创建挂载点目录
RUN mkdir -p /app/data /app/logs

# 复制应用代码与迁移配置
COPY app/ /app/app/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
COPY data/seed_ports.json /app/data/seed_ports.json

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
