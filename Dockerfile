# 如需走私有代理/镜像源，构建时覆盖：docker build --build-arg BASE_IMAGE=<你的镜像地址> .
ARG BASE_IMAGE=ghcr.io/astral-sh/uv:python3.12-bookworm-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY main.py index.html ./
COPY icons/ ./icons/
RUN mkdir -p /app/data /app/ICON

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
