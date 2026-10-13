# Dockbare 3 — Design

Dockbare backs up Docker volumes to local storage and mirrors them to one or more
remote servers (FTP / FTPS / SFTP), optionally encrypted with a system-wide key.
It can recover a wiped host by reading the remote structure and restoring volumes
by name.

## Goals of the rewrite

| # | Requirement | Design decision |
|---|-------------|-----------------|
| 1 | Own authentication, "bullet proof" | Admin login (user + password from `.env`, optional TOTP second factor), server-side sessions in SQLite with idle + absolute expiry, scrypt password hashing, per-IP login throttling with lockout, `HttpOnly`/`SameSite=Strict` cookies, CSRF defence through a required custom header, strict CSP with no third-party assets, security headers. |
| 2 | Multiple upload targets | "Remotes": any number of FTP/FTPS/SFTP targets. Each schedule and each manual backup picks the remotes it uploads to. Each backup tracks upload status per remote. |
| 3 | Recovery mode | Recovery page scans a remote, lists every volume/backup found there, compares with local Docker volumes and containers, and restores selected backups (download → verify → stop containers → clear → extract → restart). Missing volumes can be created. Only the volume name has to match. |
| 4 | Security bugs | Volume names are validated against Docker (previously an unvalidated name reached `docker run -v name:/data` and `rm -rf`). Paths are canonicalised and confined to the backup dir. No `docker` CLI, no `BACKUP_HOST_DIR` host-path hack; the Docker Engine archive API streams the data through the app. No CDN assets. Secrets never leave the server; remote credentials are encrypted at rest. Production WSGI server instead of the Flask dev server. |
| 5 | UI | New single-page UI (vanilla JS, no build step): sidebar navigation, light/dark theme, dashboard, volumes & backups, schedules, remotes, recovery, reports, activity, settings. |
| 6 | Encryption | Streaming authenticated encryption (AES-256-GCM, 1 MiB chunks, scrypt-derived master key from `DOCKBARE_ENCRYPTION_KEY`). Files are encrypted locally *before* upload, so remotes only ever see ciphertext. A standalone CLI (`python -m dockbare decrypt`) can decrypt without the app. |
| 7 | Reports | Daily / weekly / monthly reports built from the structured event log and storage samples: backups created/failed, uploads, pruned data, restores, storage usage and delta, per-volume table. Delivered via ntfy, email (SMTP) and/or webhook. History is kept in the UI. |
| 8 | Wrong texts/icons | App is called **Dockbare** everywhere; prune uses a prune icon; version 3. |
| 9 | `.env` | All secrets and deployment settings come from `.env` (`env_file` in compose). See `.env.example`. |

## Architecture

```
dockbare/
  config.py      .env + environment → Settings (paths, auth, encryption, server)
  db.py          SQLite (WAL) store: settings, remotes, schedules, backups index,
                 uploads, events, sessions, login attempts, reports, storage samples
  crypto.py      DKBE streaming file format (AES-256-GCM chunks, scrypt KDF)
  dockerctl.py   Docker Engine client: volumes, containers, archive streams
  archive.py     backup / restore / verify pipelines (tar → gzip → encrypt)
  remotes/       Remote base class + FTP/FTPS + SFTP implementations
  tasks.py       background task registry with progress + per-task log
  notify.py      ntfy, SMTP email, webhook channels
  reports.py     periodic report generation + delivery
  recovery.py    remote scan, local comparison, restore plan execution
  manager.py     orchestration: backups, uploads, sync, schedules, prune, restore
  auth.py        login, sessions, throttling, TOTP, CSRF, security headers
  api.py         JSON API (Flask blueprint)
  app.py         Flask app factory
  __main__.py    `python -m dockbare serve|hash-password|totp-setup|decrypt|gen-secret`
  static/        index.html, login.html, app.js, styles.css, icons, logo
```

### Data flow of a backup

1. Resolve containers that mount the volume (one pass over all containers).
2. Stop running ones (optional per schedule).
3. Create a helper container (the Dockbare image itself, never started) with the
   volume mounted read-only, call `GET /containers/{id}/archive?path=/data/.`.
   The daemon streams a tar whose entries are `./…`, identical to the legacy
   `tar -C /volume .` format, so old and new archives are interchangeable.
4. Stream through gzip and, if enabled, the DKBE encryptor, writing
   `/backups/<volume>/<UTC timestamp>.tar.gz[.enc]` and computing SHA-256.
5. Write `<file>.meta.json` (size, hash, encrypted flag, containers, compose
   project/service, key fingerprint).
6. Restart containers, record events, upload file + sidecar to the selected
   remotes, apply retention locally (and on remotes when enabled).

### Restore

Decrypt → gunzip → stream into `PUT /containers/{id}/archive?path=/data` of a
helper container with the target volume mounted read-write, after stopping the
containers and clearing the volume. Ownership and modes are preserved.

### Encryption format (DKBE v1)

```
magic "DKBE" | version=1 | kdf=1 (scrypt n=2^16 r=8 p=1) | kdf_salt[16] |
file_salt[16] | chunk_size u32 | key_check[16]           ← header (58 bytes), AAD of every chunk
chunk_i: AES-256-GCM(key=HKDF(master, file_salt), nonce=counter_i‖last_flag) → ct‖tag
```

The master key is derived once per `kdf_salt` and cached; the per-file key comes
from HKDF. `key_check = HMAC(master, "dockbare-key-check")[:16]` lets the app
detect a wrong key up front and gives the UI a key fingerprint. The last-chunk
flag prevents truncation; the header AAD prevents header tampering.

### Instances, shared remotes and peers

Every installation has an instance name (`DOCKBARE_INSTANCE_NAME` or generated once). Remotes with
`instance_dir` store under `<base>/<instance>/<volume>/`; the migrated v2 remote keeps the flat
`<base>/<volume>/` layout. `Remote.list_tree()` understands both, so recovery can show every
instance found on a server and restore any of them into any local volume name.

Remotes and notification channels can come from `.env` (`DOCKBARE_REMOTE_<n>_*`, `DOCKBARE_SMTP_*`,
…); those rows are synced at start-up, locked in the UI, and removed again when they disappear
from the environment.

Peers (`peers.py`): `GET /api/peer/status` (bearer `DOCKBARE_PEER_TOKEN`) returns a self-assessment
including a `problems` list. A one-minute scheduler job checks every peer that is due; after
`failure_threshold` consecutive failures it raises `peer.down` (urgent notification), `peer.up` when
it recovers, and `peer.problems` when the reported problem list changes.

### Uptime monitoring (`uptime.py`)

A poll loop (default 15 s, woken immediately by Docker container events) reads
`GET /containers/json?all=1` once per cycle and classifies each container
(`running`/`unhealthy`/`down`/`dead`/`oom`/`restart-loop`/`missing`); failing
containers are inspected for exit code, OOM flag and health log. After the
per-container grace period an incident row is opened (notification), optional
auto-restart runs against a budget, and the incident is closed on recovery.
Uptime percentages are computed from incident overlap with 24 h / 7 d / 30 d
windows. `/api/uptime/public` and `/uptime` (opt-in, unauthenticated) expose
only containers flagged public, plus peers and their public items.

Announcements (manual incidents with a status workflow and severity, and
maintenance windows) live in the `announcements` table. Active maintenance
windows suppress incident creation and downtime accounting for the containers
they cover. The public payload carries per-service 90-day daily availability
(`daily_history`, from incident overlap per UTC day, `None` before the container
was first seen) and the announcements; peers forward their public items and
announcements in `/api/peer/status`.

### Configuration snapshot (`configsync.py`)

Everything outside `.env` is exported as JSON (secrets included), encrypted
with the backup key (DKBE) and uploaded as `<base>/[<instance>/]_dockbare/config.json.enc`
to every enabled remote, debounced after configuration events and daily.
Recovery scans look for snapshots per instance and can import selected
sections; rows that come from `.env` are never overwritten by an import.

### Security model

* Mounting the Docker socket is root on the host. The app is still meant to run
  behind TLS (reverse proxy) — set `DOCKBARE_BEHIND_PROXY=true` so the real
  client IP and scheme are used.
* One admin identity. Password from `DOCKBARE_ADMIN_PASSWORD` (or a hash from
  `DOCKBARE_ADMIN_PASSWORD_HASH`). Optional TOTP via `DOCKBARE_TOTP_SECRET`.
* Sessions: random 256-bit id, signed cookie, stored server-side; idle timeout
  and absolute lifetime; revocation of all sessions from the UI.
* Login throttling: per-IP sliding window and escalating lockout; all attempts
  logged as events.
* Mutating API calls require `X-Requested-With: Dockbare` (browsers never add
  custom headers cross-origin without CORS consent) and a `SameSite=Strict`
  cookie.
* CSP `default-src 'self'`; no inline scripts; no external fonts, icons or CDNs.
* All user-supplied paths are confined to the backup directory; volume names are
  validated against a strict pattern *and* Docker's volume list.
