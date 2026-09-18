# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=432410731887.dkr.ecr.us-west-2.amazonaws.com/webbpulse/python-lambda-base@sha256:b5298b4b773ad6c9e311057cf5d43f37ceb98f0367347d714c6817f250a5cef7

FROM ${BASE_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv

USER root

WORKDIR /build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON=/usr/local/bin/python3.13

COPY pyproject.toml uv.lock ./

RUN uv sync --locked --no-dev --no-install-project --no-install-package webbpulse

ARG DEPENDENCY_RESOLUTION=local

RUN --mount=type=secret,id=codeartifact_token,required=true \
    set -eu; \
    UV_INDEX_CODEARTIFACT_USERNAME=aws \
    UV_INDEX_CODEARTIFACT_PASSWORD="$(cat /run/secrets/codeartifact_token)" \
    uv lock --upgrade-package webbpulse; \
    UV_INDEX_CODEARTIFACT_USERNAME=aws \
    UV_INDEX_CODEARTIFACT_PASSWORD="$(cat /run/secrets/codeartifact_token)" \
    uv sync --frozen --no-dev --no-install-project; \
    /opt/venv/bin/python -c "import importlib.metadata as m; print('webbpulse', m.version('webbpulse'))"

RUN find /opt/venv -type d -name "__pycache__" -prune -exec rm -rf {} + \
    && find /opt/venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

FROM ${BASE_IMAGE} AS runtime

ARG DOMAIN
RUN test -n "${DOMAIN}" || { \
        echo "DOMAIN build arg is required: one of workspaces, runs." >&2; \
        echo "  docker buildx build --build-arg DOMAIN=workspaces ..." >&2; \
        exit 1; \
    }; \
    case "${DOMAIN}" in \
        workspaces|runs) ;; \
        *) echo "DOMAIN=${DOMAIN} is not a domain. Expected one of workspaces, runs." >&2; exit 1 ;; \
    esac

COPY --from=builder /opt/venv /opt/venv
COPY app/ /var/task/app/

RUN test -f "/var/task/app/domains/${DOMAIN}/entrypoint.py" || { \
        echo "app/domains/${DOMAIN}/entrypoint.py is missing, so the CMD below has nothing to run." >&2; \
        exit 1; \
    }

ENV DOMAIN=${DOMAIN} \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/var/task \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_ASYNC_INIT=true \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    AWS_LWA_READINESS_CHECK_PROTOCOL=http

WORKDIR /var/task

CMD ["sh", "-c", "exec python -m app.domains.${DOMAIN}.entrypoint"]
