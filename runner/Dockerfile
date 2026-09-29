# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim-trixie

FROM ${PYTHON_IMAGE} AS engines

ARG TARGETARCH=arm64

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg gpgv unzip; \
    rm -rf /var/lib/apt/lists/*

COPY versions.env /tmp/versions.env

RUN set -eu; \
    . /tmp/versions.env; \
    export GNUPGHOME=/tmp/gnupg; \
    mkdir -p -m 0700 "${GNUPGHOME}"; \
    mkdir -p -m 0755 /out/keys; \
    bake_key() { \
        name="$1"; url="$2"; fingerprint="$3"; \
        curl -fsSL -o "/tmp/${name}.asc" "${url}"; \
        primaries="$(gpg --batch --with-colons --import-options show-only --import "/tmp/${name}.asc" \
            | awk -F: '$1 == "pub" { want = 1; next } want && $1 == "fpr" { print $10; want = 0 }')"; \
        if [ "${primaries}" != "${fingerprint}" ]; then \
            echo "${url} does not carry exactly the pinned key ${fingerprint}: ${primaries}" >&2; exit 1; \
        fi; \
        gpg --batch --quiet --import "/tmp/${name}.asc"; \
        gpg --batch --export "${fingerprint}" > "/out/keys/${name}.gpg"; \
        chmod 0644 "/out/keys/${name}.gpg"; \
    }; \
    bake_key terraform "https://www.hashicorp.com/.well-known/pgp-key.txt" "${TERRAFORM_KEY_FINGERPRINT}"; \
    bake_key tofu "https://get.opentofu.org/opentofu.asc" "${TOFU_KEY_FINGERPRINT}"; \
    rm -rf "${GNUPGHOME}" /tmp/terraform.asc /tmp/tofu.asc; \
    base="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS"; \
    curl -fsSL -o /tmp/tf.sums "${base}"; \
    curl -fsSL -o /tmp/tf.sig "${base}.sig"; \
    gpgv --homedir /tmp --keyring /out/keys/terraform.gpg /tmp/tf.sig /tmp/tf.sums; \
    base="https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_SHA256SUMS"; \
    curl -fsSL -o /tmp/tofu.sums "${base}"; \
    curl -fsSL -o /tmp/tofu.sig "${base}.gpgsig"; \
    gpgv --homedir /tmp --keyring /out/keys/tofu.gpg /tmp/tofu.sig /tmp/tofu.sums; \
    rm -f /tmp/tf.sums /tmp/tf.sig /tmp/tofu.sums /tmp/tofu.sig

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
    apt-get install -y --no-install-recommends ca-certificates git gpgv openssh-client; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --create-home --shell /usr/sbin/nologin --uid 10001 runner; \
    mkdir -p /opt/terraform-plugin-cache /work; \
    chown runner:runner /opt/terraform-plugin-cache /work

COPY --from=engines /out/bin/terraform /usr/local/bin/terraform
COPY --from=engines /out/bin/tofu /usr/local/bin/tofu
COPY --from=engines /out/keys/ /usr/local/share/webbpulse-runner/keys/
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
    ENGINE_KEYRING_DIRECTORY=/usr/local/share/webbpulse-runner/keys \
    GIT_TERMINAL_PROMPT=0

RUN set -eu; \
    terraform version; \
    tofu version; \
    git --version; \
    gpgv --version | head -n 1; \
    test -s /usr/local/share/webbpulse-runner/keys/terraform.gpg; \
    test -s /usr/local/share/webbpulse-runner/keys/tofu.gpg; \
    PYTHONPATH=/srv/runner /opt/venv/bin/python -c "import app.main; print('entrypoint importable')"

USER runner
WORKDIR /work

CMD ["python", "-m", "app.main"]
