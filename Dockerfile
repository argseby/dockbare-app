FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKBARE_DATA_DIR=/data \
    DOCKBARE_BACKUP_DIR=/backups

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dockbare ./dockbare

# /data holds the database, logs and generated secrets; /backups holds the archives.
VOLUME ["/data", "/backups"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('DOCKBARE_PORT','5000'), timeout=4).status == 200 else 1)"

# The image doubles as the helper image that mounts volumes for backup/restore,
# so no extra image has to be pulled on the host.
CMD ["python", "-m", "dockbare", "serve"]
