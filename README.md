# Docker Volume Backup v2.0

A web UI to back up, restore, and manage Docker volumes with scheduling, retention policies, FTP/FTPS mirroring, and
ntfy notifications.

**Free to use — no license key, no account, no telemetry.** Made by [kiunke.dev](https://kiunke.dev).

![alt text](image.png "Docker Volume Backup")



> **⚠️ CRITICAL SECURITY WARNING**
>
> * **Do NOT expose this service to the internet.** Mounting `/var/run/docker.sock` is equivalent to granting root
    access to the host machine. You **MUST** run this service behind a secure reverse proxy (e.g., Traefik, Caddy) with
    strong authentication enabled for any remote access.
> * **Restore is a DESTRUCTIVE operation.** It permanently deletes all existing data in a volume before restoring the
    backup. Always double-check your target volume and backup file.
> * **Use FTPS instead of FTP.** Plain FTP sends credentials and data in cleartext.

### Features

- **Backup & Restore:** Create compressed `tar.gz` backups of any Docker volume and restore them with a single click.
- **Automated Scheduling:** Set up recurring backup schedules with flexible retention policies (e.g., keep backups for X
  days).
- **FTP/FTPS Mirroring:** Automatically mirror backups to a remote FTP or secure FTPS server for off-site storage.
- **Ntfy Notifications:** Receive real-time notifications for backup success/failure and restore events.
- **Simple UI:** A responsive, mobile-friendly dark interface with an activity dashboard and detailed logs.

### Prerequisites

- Docker
- Docker Compose

### Quick Start

Create a `docker-compose.yml` (or use the one in this repo):

   ```yaml
   services:
     backup-service:
       image: fluttered/dockbare:latest
       container_name: docker-volume-backup
       ports:
         - "127.0.0.1:5000:5000"
       volumes:
         - ./backups:/backups
         - /var/run/docker.sock:/var/run/docker.sock
       environment:
         - BACKUP_HOST_DIR=${PWD}/backups
       restart: unless-stopped
   ```

Then start it:

   ```bash
   # create the folder backups are written to
   mkdir -p backups

   # pull the image and start the service in the background
   docker compose up -d
   ```

> Note on Accessibility: By default, the service is only accessible from the host machine at http://localhost:5000 or http://127.0.0.1:5000. This is a security measure designed for access via a reverse proxy. If you understand the security risks and wish to expose the service directly, you can edit the docker-compose.yml file and change "127.0.0.1:5000:5000" to "5000:5000".

Access the web UI at `http://<your-server-ip>:5000`.

### UI Overview

- **Dashboard:** View storage usage, see running/upcoming jobs, and create a one-off manual backup.
- **Schedules:** Add, edit, pause, or remove automated backup schedules and their retention policies.
- **Backups:** View, download, restore, or delete existing backup archives and inspect their logs.
- **Logs:** A global, filterable view of all actions performed by the application.
- **Settings:** Configure FTP/FTPS server details and ntfy connection settings.