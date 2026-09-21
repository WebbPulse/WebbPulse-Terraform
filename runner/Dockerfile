# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim-trixie

FROM ${PYTHON_IMAGE} AS engines

ARG TARGETARCH=arm64

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    rm -rf /var/lib/apt/lists/*

COPY versions.env /tmp/versions.env

RUN set -eu; \
    . /tmp/versions.env; \
    case "${TARGETARCH}" in \
        arm64) TF_SHA="${TERRAFORM_SHA256_ARM64}"; TOFU_SHA="${TOFU_SHA256_ARM64}" ;; \
        amd64) TF_SHA="${TERRAFORM_SHA256_AMD64}"; TOFU_SHA="${TOFU_SHA256_AMD64}" ;; \
        *) echo "TARGETARCH=${TARGETARCH} has no pinned checksum in versions.env." >&2; exit 1 ;; \
    esac; \
    mkdir -p /out/bin; \
    curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip"; \
    echo "${TF_SHA}  /tmp/terraform.zip" | sha256sum -c -; \
    unzip -q /tmp/terraform.zip -d /out/bin; \
    curl -fsSL -o /tmp/tofu.zip \
        "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${TARGETARCH}.zip"; \
    echo "${TOFU_SHA}  /tmp/tofu.zip" | sha256sum -c -; \
    unzip -q -o /tmp/tofu.zip -d /tmp/tofu; \
    install -m 0755 /tmp/tofu/tofu /out/bin/tofu; \
    chmod 0755 /out/bin/terraform; \
    rm -rf /tmp/terraform.zip /tmp/tofu.zip /tmp/tofu; \
    /out/bin/terraform version; \
    /out/bin/tofu version

FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv

WORKDIR /build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON=/usr/local/bin/python3.13

COPY pyproject.toml uv.lock ./

RUN uv sync --locked --no-dev --no-install-project

ARG DEPENDENCY_RESOLUTION=local

RUN set -eu; \
    if [ "${DEPENDENCY_RESOLUTION}" = "local" ]; then \
        uv sync --locked --no-dev --no-install-project; \
    else \
        uv lock --upgrade; \
        uv sync --frozen --no-dev --no-install-project; \
    fi; \
    /opt/venv/bin/python -c "import boto3, httpx, pydantic; print('runtime dependencies resolved')"

RUN find /opt/venv -type d -name "__pycache__" -prune -exec rm -rf {} + \
    && find /opt/venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

FROM ${PYTHON_IMAGE} AS runtime

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates git openssh-client; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --create-home --shell /usr/sbin/nologin --uid 10001 runner; \
    mkdir -p /opt/terraform-plugin-cache /work; \
    chown runner:runner /opt/terraform-plugin-cache /work

COPY --from=engines /out/bin/terraform /usr/local/bin/terraform
COPY --from=engines /out/bin/tofu /usr/local/bin/tofu
COPY --from=builder /opt/venv /opt/venv
COPY app/ /srv/runner/app/

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:$PATH \
    PYTHONPATH=/srv/runner \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TF_IN_AUTOMATION=1 \
    TF_INPUT=0 \
    CHECKPOINT_DISABLE=1 \
    TF_PLUGIN_CACHE_DIR=/opt/terraform-plugin-cache \
    GIT_TERMINAL_PROMPT=0

RUN set -eu; \
    terraform version; \
    tofu version; \
    git --version; \
    PYTHONPATH=/srv/runner /opt/venv/bin/python -c "import app.main; print('entrypoint importable')"

USER runner
WORKDIR /work

CMD ["python", "-m", "app.main"]
