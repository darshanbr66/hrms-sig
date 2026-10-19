# One image for the API, the worker, the migration job and the operator commands
# (docs/database-design.md §11: one image, promoted unchanged). Build from backend/:
#
#   docker build -t hrms-backend .
#
# The service decides what runs (docs/deployment.md §3):
#   API        the default command below
#   worker     python -m app.worker
#   migration  alembic upgrade head        (as hrms_migrator, before the worker and the API)
#   operator   python -m app.cli ...       (from an interactive shell, never a logged job)
#
# Pin the base images by digest when the production image is first built and record the
# digests here; the tags below name the versions this file was written for.

FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

FROM python:3.13-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
# Runtime dependencies only, exactly as locked; the test and lint tools stay out of the image.
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.13-slim-trixie
# ca-certificates: the system trust store that PostgreSQL (sslmode=verify-full), Redis (rediss),
# S3 and SMTP certificates are verified against. Deliberately unpinned (hadolint DL3008): it
# must follow Debian's current certificate authorities, which the image digest then fixes.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin hrms
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
# Code and migrations stay owned by root: the running process can read them, not change them.
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
USER 10001
EXPOSE 10000
# --no-access-log: uvicorn's access log writes raw paths, and invite and password-reset links
# carry their token in the path; the application logs route templates instead.
# --no-proxy-headers: the client address is resolved by the application from
# TRUSTED_PROXY_CIDRS (app/platform/middleware.py), not by uvicorn.
# The selector loop is Linux's default, so no --loop option is needed here.
CMD ["sh", "-c", "exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port \"${PORT:-10000}\" --no-access-log --no-proxy-headers --no-server-header"]
