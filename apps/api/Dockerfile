# MRIP API image — serves the api, worker and scheduler from one build.
#
# One image, three commands (docker-compose sets them), so the three processes
# can never drift in dependency versions or code revision. A worker running a
# different revision than the API is a class of bug this removes entirely.
#
# Multi-stage: the build stage carries the compiler toolchain some wheels need,
# the runtime stage does not.

# ---------------------------------------------------------------- build stage
FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build-only: psycopg and pymupdf compile against these. They are absent from
# the runtime stage, which keeps the shipped image smaller and its attack
# surface narrower.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Dependencies resolve from pyproject alone, so this layer caches across every
# source change — a code edit does not re-download the dependency tree.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install .

COPY mrip ./mrip
COPY migrations ./migrations
COPY alembic.ini ./
RUN /opt/venv/bin/pip install --no-deps .

# -------------------------------------------------------------- runtime stage
FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MRIP_DATA_DIR=/var/lib/mrip/data \
    MRIP_BLOB_DIR=/var/lib/mrip/blobs

# Runtime library only, not the -dev package.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY --from=build /build/mrip ./mrip
COPY --from=build /build/migrations ./migrations
COPY --from=build /build/alembic.ini ./

# Non-root. The blob store holds unpublished production figures and draft
# parliamentary answers; a container process that can write outside its own data
# directory has no reason to.
RUN useradd --create-home --uid 10001 mrip \
 && mkdir -p /var/lib/mrip/data /var/lib/mrip/blobs \
 && chown -R mrip:mrip /var/lib/mrip /app
USER mrip

EXPOSE 8000

# No default CMD: the compose file names the process (uvicorn / mrip-worker /
# mrip-scheduler). An image that silently defaults to one of them invites
# starting the wrong thing.
CMD ["uvicorn", "mrip.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
