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

# scripts/install_media_tools.sh is where they come from, for dev and CI too.
# perl is here only so it can check exiftool's version; the runtime stage
# installs its own.
FROM debian:trixie-slim AS tools
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl perl xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/install_media_tools.sh /tmp/
ARG EXIFTOOL_VERSION
ARG EXIFTOOL_SHA256
ARG FFMPEG_VERSION
ARG FFMPEG_SHA256
RUN : "${EXIFTOOL_VERSION:?}" "${EXIFTOOL_SHA256:?}" "${FFMPEG_VERSION:?}" "${FFMPEG_SHA256:?}" \
    && /tmp/install_media_tools.sh /opt

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
# The migrate step runs Alembic from the package, so alembic.ini stays out.
COPY README.md /app/
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
    && mkdir /photos /data \
    && chown app:app /photos /data

# The repo's layout, so the app finds the frontend at /app/frontend/dist.
WORKDIR /app
COPY --from=backend /app /app
COPY --from=frontend /app/frontend/dist /app/frontend/dist

ENV PATH=/app/backend/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PHOTO_DIR=/photos \
    DATA_DIR=/data
# TRASH_DIR is left to its default, /photos/.photo-triage-trash, on the photo
# library's filesystem, so trashing a file is a rename (plan §6.8).

USER 1000:1000
EXPOSE 8000
CMD ["python", "-m", "photo_triage.api", "--host", "0.0.0.0"]
