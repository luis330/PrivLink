# 如需走私有代理/镜像源，构建时覆盖：docker build --build-arg BASE_IMAGE=<你的镜像地址> .
ARG BASE_IMAGE=ghcr.io/astral-sh/uv:python3.12-bookworm-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
# cache clean 必须与 sync 同层：Docker 层是增量的，单独 RUN 清不掉前一层已固化的缓存（实测 ~27MB/层）
RUN uv sync --frozen --no-dev --no-install-project && uv cache clean

COPY main.py ./
COPY src/privlink/ ./src/privlink/
COPY index.html simple-icons.json manifest.json LICENSE favicon.ico favicon-16x16.png favicon-32x32.png apple-touch-icon.png android-chrome-192x192.png android-chrome-512x512.png ./
# --no-editable：项目以普通 wheel 安装（uv 官方生产镜像建议）。editable 安装在 uv cache clean
# 后会失去轮子缓存副本，导致 uv run 每次容器启动都现场重建，且依赖构建后端可获取
RUN uv sync --frozen --no-dev --no-editable && uv cache clean && mkdir -p /app/data /app/ICON /app/background

EXPOSE 8000

# --no-dev：pyproject 的 default-groups 含 dev（pytest、workers-py），不加则 uv run
# 会在每次容器启动时联网补装开发依赖；--frozen：锁文件已烘焙进镜像，禁止运行时重新解析
CMD ["uv", "run", "--no-dev", "--frozen", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
