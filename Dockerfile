# Image shared by the PGP worker (default command) and the admin console
# (started with uvicorn, see k8s/sftp-console).
#
# Built on Wolfi, a minimal distribution for containers with fast security
# fixes. The runtime filesystem is assembled from packages into an empty root:
# no shell, no package manager, only Python, GnuPG and the application.
# Runs as non-root with a read-only root filesystem; /tmp must be an emptyDir.

ARG WOLFI_BASE=cgr.dev/chainguard/wolfi-base@sha256:833c48f1faf32c90e6f9501a2410a9aa28ceb2c594102edd4470053c30efa089

# --- Python dependencies -----------------------------------------------------
FROM ${WOLFI_BASE} AS build
RUN apk add --no-cache python-3.14
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/bin/python3.14
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY sftp_platform ./sftp_platform

# --- Runtime root filesystem -------------------------------------------------
FROM ${WOLFI_BASE} AS rootfs
RUN mkdir -p /rootfs/etc/apk \
    && cp -r /etc/apk/keys /etc/apk/repositories /rootfs/etc/apk/ \
    && apk add --no-cache --initdb --root /rootfs \
        wolfi-baselayout ca-certificates-bundle tzdata \
        python-3.14 gpg gpg-agent gnupg-gpgconf \
    && rm -rf /rootfs/etc/apk /rootfs/lib/apk /rootfs/var/cache/apk \
    && echo "app:x:10001:10001::/nonexistent:/sbin/nologin" >> /rootfs/etc/passwd \
    && echo "app:x:10001:" >> /rootfs/etc/group

# --- Runtime -----------------------------------------------------------------
FROM scratch
COPY --from=rootfs /rootfs /
COPY --from=build /app /app
WORKDIR /app
ENV PATH="/app/.venv/bin:/usr/bin" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TMPDIR=/tmp
USER 10001:10001
CMD ["python", "-m", "sftp_platform.consumer"]
