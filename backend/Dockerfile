# Built in CI, not required for local development.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

WORKDIR /app

# Dependency layer first so a source change does not reinstall the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend ./backend

# Non-root: a container that does not need to write to its own filesystem
# should not be able to.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

# --------------------------------------------------------------------------- #
# Running behind a proxy: FORWARDED_ALLOW_IPS is a DEPLOYMENT REQUIREMENT.
#
# `--proxy-headers` is NOT passed because uvicorn 0.52 already defaults it to on;
# adding it would imply it was the missing piece, and it is not. The missing piece is
# `--forwarded-allow-ips`, which defaults to FORWARDED_ALLOW_IPS in the environment,
# or `127.0.0.1`.
#
# That default is correct only for a proxy sharing this container's network
# namespace. In the ordinary topology -- a reverse proxy as its own service with its
# own address -- X-Forwarded-For arrives, is NOT trusted, and is discarded. The peer
# stays the proxy, so every client in the world lands in one rate-limit bucket and
# the first abusive caller locks out everybody. Nothing errors; the limiter works
# perfectly against the wrong subject.
#
# So the operator MUST set FORWARDED_ALLOW_IPS to the proxy's address. Not to `*`:
# trusting a forwarded header from anyone lets a client claim any address and evade
# the limit entirely by varying one header, which is worse than sharing a bucket.
# `core/proxy_trust.py` detects both mistakes at runtime and says so in the log.
# --------------------------------------------------------------------------- #
CMD ["uvicorn", "backend.app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
