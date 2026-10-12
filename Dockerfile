# 使用项目已冻结的 Python 3.12 运行时。
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
# uv 只根据锁文件安装正式依赖，不复制本地虚拟环境和密钥。
RUN pip install --no-cache-dir uv==0.8.14
COPY pyproject.toml uv.lock PROJECT.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY configs ./configs
COPY src ./src
EXPOSE 8000
CMD ["uvicorn", "kg_agent.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
