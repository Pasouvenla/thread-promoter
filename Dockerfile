FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Fixed uid so the data volume keeps working across rebuilds. Letting adduser
# pick one means a rebuild on a shifted base image can produce a container that
# cannot write to an already populated volume.
ARG PROMOTER_UID=1000
ARG PROMOTER_GID=1000

WORKDIR /srv

RUN groupadd --gid ${PROMOTER_GID} promoter \
 && useradd --uid ${PROMOTER_UID} --gid ${PROMOTER_GID} --no-create-home --shell /usr/sbin/nologin promoter

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data/state /data/manifests /data/attachments /data/exports \
 && chown -R promoter:promoter /data \
 && chmod 700 /data/state /data/manifests /data/attachments /data/exports

USER promoter

# A gateway bot exposes nothing to probe, so liveness is the heartbeat file the
# bot refreshes while the gateway is up. Stale means connected but stuck, which
# is the failure nobody notices.
HEALTHCHECK --interval=60s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.bot"]
