# One image for api, worker and mock-platforms. python:3.12-slim + uv, runs as a non-root user.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN pip install --no-cache-dir uv==0.12.21 \
    && groupadd --system --gid 10001 ibx \
    && useradd --system --uid 10001 --gid ibx --home-dir /srv --shell /usr/sbin/nologin ibx

WORKDIR /srv
# Dependencies first (cached layer), runtime deps only.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY mocks ./mocks
COPY scripts ./scripts
# runtime data read by the catalogue tools and the seed loader; deploy/ stays out (bootstrap only)
COPY catalogue ./catalogue
COPY seed ./seed
RUN chown -R ibx:ibx /srv

USER ibx
EXPOSE 8000 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
