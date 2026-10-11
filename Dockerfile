FROM python:3.12.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock .python-version ./
ARG QS_INSTALL_BENCH=0
RUN if [ "$QS_INSTALL_BENCH" = "1" ]; then uv sync --frozen --no-dev --group bench --no-install-project; else uv sync --frozen --no-dev --no-install-project; fi
COPY hellabandz ./hellabandz
COPY scripts ./scripts
COPY benchmarks/*.py ./benchmarks/
COPY data/fixtures ./data/fixtures
RUN if [ "$QS_INSTALL_BENCH" = "1" ]; then uv sync --frozen --no-dev --group bench; else uv sync --frozen --no-dev; fi
ENV PATH="/app/.venv/bin:$PATH"
