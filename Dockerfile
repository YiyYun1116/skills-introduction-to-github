FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SIGNAL_ATLAS_ENV=production \
    SIGNAL_ATLAS_DB=/var/data/signal-atlas.sqlite3 \
    SIGNAL_ATLAS_ACCESS_USER=research

WORKDIR /opt/signal-atlas
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 atlas \
    && useradd --uid 10001 --gid atlas --no-create-home --shell /usr/sbin/nologin atlas \
    && mkdir -p /var/data \
    && chown atlas:atlas /var/data

COPY app ./app
COPY scripts/serve.py ./scripts/serve.py

USER 10001:10001
EXPOSE 8000
CMD ["python", "scripts/serve.py"]
