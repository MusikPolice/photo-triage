# One image for the app and the worker (plan §4). Build it with `just image`.
# The app is the default command; the worker runs from the same image with
#   command: ["python", "-m", "photo_triage.worker"]
#
# Every version is a build arg with no default (dev-environment §2): Python,
# uv, Node and pnpm from mise.toml, and exiftool and ffmpeg from versions.env.
# scripts/image_pins.sh turns those into build args, for `just image` and CI. A
# missing one fails the build with "<NAME>: is not allowed to be unset" (in a
# FROM) or "<NAME>: parameter null or not set" (in a RUN).

ARG PYTHON_VERSION
ARG UV_VERSION
ARG NODE_VERSION

FROM ghcr.io/astral-sh/uv:${UV_VERSION:?} AS uv

# --- Frontend: vite build ----------------------------------------------------

FROM node:${NODE_VERSION:?}-trixie-slim AS frontend
ARG PNPM_VERSION
RUN npm install --global "pnpm@${PNPM_VERSION:?}"
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
ARG EXIFTOOL_VERSION
ARG EXIFTOOL_SHA256
RUN curl --fail --silent --show-error --location --retry 3 --output /tmp/exiftool.tar.gz \
        "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION:?}.tar.gz/download" \
    && echo "${EXIFTOOL_SHA256:?}  /tmp/exiftool.tar.gz" | sha256sum --check \
    && mkdir /opt/exiftool \
    && tar -xzf /tmp/exiftool.tar.gz -C /opt/exiftool --strip-components=1 \
    && rm /tmp/exiftool.tar.gz
# The same static build as local dev. The release URL is rolling, so the
# old-releases path comes first and the checksum guards against drift.
ARG FFMPEG_VERSION
ARG FFMPEG_SHA256
RUN for url in \
        "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION:?}-amd64-static.tar.xz" \
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"; do \
        curl --fail --silent --show-error --location --retry 3 --output /tmp/ffmpeg.tar.xz "$url" \
            && echo "${FFMPEG_SHA256:?}  /tmp/ffmpeg.tar.xz" | sha256sum --check --status \
            && break; \
        rm -f /tmp/ffmpeg.tar.xz; \
    done \
    && test -f /tmp/ffmpeg.tar.xz \
    && mkdir /opt/ffmpeg \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1 \
    && rm /tmp/ffmpeg.tar.xz

# --- Backend: runtime dependencies only --------------------------------------

FROM python:${PYTHON_VERSION:?}-slim-trixie AS backend
COPY --from=uv /uv /usr/local/bin/uv
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

FROM python:${PYTHON_VERSION:?}-slim-trixie
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
