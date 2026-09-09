# Base on the official Python 3.14 image so the interpreter is guaranteed,
# then install uv via pip. (Railway's Nixpacks pins an old uv that can't
# provide a 3.14 interpreter.)
FROM python:3.14-slim-bookworm

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.11.17

# Install dependencies first for better layer caching (no project code yet).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# Now copy the project and install it into the venv.
COPY . .
RUN uv sync --no-dev --frozen

# Railway injects $PORT at runtime; default to 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn outside_line.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
