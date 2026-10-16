FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY pyproject.toml constraints.txt ./
COPY app ./app
# alembic.ini and migrations/ are runtime assets, not just dev tooling: the
# deploy runs `alembic upgrade head` as a pre-deploy step, which executes
# inside this image and can't find either if they're left out. That step is
# configured per-service on the host rather than in a repo-level config
# file, so it runs once (on the web service) instead of racing a second
# copy of itself on the worker.
COPY alembic.ini ./
# docs/index.html is served at /privacy (app.main) - the privacy-policy URL
# Meta requires before an app can be published.
COPY docs ./docs
# Operational one-offs (bootstrap_tenant, set_channel_credentials,
# create_operator) run against the deployed database over the host's private
# network, which nothing outside the cluster can reach -- so they have to
# execute from inside this image rather than from an operator's machine.
COPY scripts ./scripts
COPY data ./data
COPY migrations ./migrations

# Pinned to the versions production is known to run (constraints.txt): an
# unpinned rebuild pulled whatever was newest that day.
RUN pip install --no-cache-dir -c constraints.txt .

USER app

EXPOSE 8000

# One image, two roles. The web API and the arq worker run the same code
# and differ only in entrypoint, but a host that builds every service in a
# project from this one repo has nowhere per-service to put that
# difference: a committed start command (railway.json) applies to the whole
# repo and would silently make the worker a second web server. Environment
# variables *are* per-service, so the role comes from APP_ROLE. Unset means
# web, which keeps `docker run` and any host that knows nothing about
# APP_ROLE doing the obvious thing.
#
# docker-compose.yml overrides `command:` for its worker and never reaches
# this dispatch.
#
# Shell form (not exec-array) so ${PORT:-8000} actually expands: hosts
# inject PORT at runtime and expect the container to bind to it, while
# local `docker run` (no PORT set) falls back to 8000. `exec` replaces the
# shell so the process gets SIGTERM directly on shutdown instead of the
# shell swallowing it and the platform resorting to SIGKILL.
CMD if [ "$APP_ROLE" = worker ]; then exec arq app.workers.tasks.WorkerSettings; elif [ "$APP_ROLE" = all ]; then exec sh scripts/run_all.sh; else exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}; fi
