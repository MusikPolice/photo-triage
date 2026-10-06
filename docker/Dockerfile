# One image for the app and the worker (plan §4). Build from the repo root:
#   docker build -f docker/Dockerfile -t photo-triage .
# The app is the default command; the worker runs from the same image with
#   command: ["python", "-m", "photo_triage.worker"]
#
# Versions are pinned (dev-environment §2). Python, Node, pnpm and uv match
# mise.toml; exiftool and ffmpeg match scripts/bootstrap.sh, which the smoke test
# (scripts/docker_smoke.sh) checks.

ARG PYTHON_VERSION=3.13.15
ARG NODE_VERSION=24.21.0

# --- Frontend: vite build ----------------------------------------------------

FROM node:${NODE_VERSION}-trixie-slim AS frontend
ARG PNPM_VERSION=10.34.6
RUN npm install --global "pnpm@${PNPM_VERSION}"
WORKDIR /app/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

# --- exiftool and ffmpeg, downloaded and checked ------------------------------

FROM debian:trixie-slim AS tools
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*
# exiftool.org only serves the newest release; SourceForge keeps every version.
ARG EXIFTOOL_VERSION=13.59
ARG EXIFTOOL_SHA256=668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a
RUN curl --fail --silent --show-error --location --retry 3 --output /tmp/exiftool.tar.gz \
        "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz/download" \
    && echo "${EXIFTOOL_SHA256}  /tmp/exiftool.tar.gz" | sha256sum --check \
    && mkdir /opt/exiftool \
    && tar -xzf /tmp/exiftool.tar.gz -C /opt/exiftool --strip-components=1 \
    && rm /tmp/exiftool.tar.gz
# The same static build as local dev. The release URL is rolling, so the
# old-releases path comes first and the checksum guards against drift.
ARG FFMPEG_VERSION=7.0.2
ARG FFMPEG_SHA256=abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67
RUN for url in \
        "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION}-amd64-static.tar.xz" \
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"; do \
        curl --fail --silent --show-error --location --retry 3 --output /tmp/ffmpeg.tar.xz "$url" \
            && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum --check --status \
            && break; \
        rm -f /tmp/ffmpeg.tar.xz; \
    done \
    && test -f /tmp/ffmpeg.tar.xz \
    && mkdir /opt/ffmpeg \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1 \
    && rm /tmp/ffmpeg.tar.xz

# --- Backend: runtime dependencies only --------------------------------------

FROM python:${PYTHON_VERSION}-slim-trixie AS backend
COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEFAULT_GROUPS=1
WORKDIR /app/backend
# Dependencies first, so a code change doesn't reinstall them. No dependency
# groups: dev and export (torch, open_clip) stay out of the image.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-install-project
COPY README.md /app/
COPY backend/alembic.ini ./
COPY backend/src/ src/
RUN uv sync --locked

# --- Runtime -----------------------------------------------------------------

FROM python:${PYTHON_VERSION}-slim-trixie
# exiftool needs the full perl (perl-base lacks modules it loads).
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --yes --no-install-recommends perl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=tools /opt/exiftool /opt/exiftool
COPY --from=tools /opt/ffmpeg/ffmpeg /opt/ffmpeg/ffprobe /usr/local/bin/
RUN ln -s /opt/exiftool/exiftool /usr/local/bin/exiftool

# A fixed UID, so bind-mounted folders can be given to it on the host.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home app \
    && mkdir /photos /trash /data \
    && chown app:app /photos /trash /data

# The repo's layout, so the app finds the frontend at /app/frontend/dist.
WORKDIR /app
COPY --from=backend /app /app
COPY --from=frontend /app/frontend/dist /app/frontend/dist

ENV PATH=/app/backend/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PHOTO_DIR=/photos \
    TRASH_DIR=/trash \
    DATA_DIR=/data

USER 1000:1000
EXPOSE 8000
CMD ["python", "-m", "photo_triage.api", "--host", "0.0.0.0"]
