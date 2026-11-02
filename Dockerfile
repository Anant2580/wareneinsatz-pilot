# One image for the server: the REST API, the web app and the one-off setup job.
# Built on the server itself (ARM64 on the Oracle VM, x86 elsewhere); see deploy/README.md.
#
# No OCR or language model inside: reading photos needs a GPU-class machine, so on the server
# the demo shows results, and photos are read on a laptop (step 5).

FROM python:3.12-slim

# uv, pinned (the same version that wrote uv.lock)
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall them (Docker layer cache).
# Exactly the versions in uv.lock; the web app group, no test tools.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-default-groups --group ui --no-install-project

COPY src ./src
COPY app ./app
COPY .streamlit ./.streamlit
COPY migrations ./migrations
COPY alembic.ini ./
COPY data/ingredients.json data/recipes.json ./data/
COPY data/demo ./data/demo
COPY deploy/setup.sh ./deploy/setup.sh
RUN uv sync --locked --no-default-groups --group ui

ENV PATH="/app/.venv/bin:$PATH" \
    DOCUMENT_DIR=/app/data/documents

# Run as a normal user; uploaded documents live in a volume mounted at data/documents.
RUN useradd --create-home --uid 10001 app && mkdir -p data/documents && chown -R app data/documents
USER app

EXPOSE 8000 8501
CMD ["wareneinsatz", "serve", "--host", "0.0.0.0", "--port", "8000"]
