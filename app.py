#!/usr/bin/env python3
"""
Docker Volume Backup Manager (merged)
- Keeps UI input/dropdown state across backend reloads (localStorage).
- Robust FTP/FTPS (explicit and implicit) connection handling.
- FTP health check exposed in /api/state and used by Overview + Schedules.
- Schedules, pruning, predictions, upload/restore, logs.
"""

# --- Core Imports ---
import atexit
import json
import os
import ssl
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from ftplib import FTP, FTP_TLS

# --- Third-Party Imports ---
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, render_template_string, request, Response, send_file
from werkzeug.utils import secure_filename

# --- HTML, CSS, JS Template (merged with persistence + FTP health UI) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Docker Volume Backup</title>
<script src="https://unpkg.com/lucide@latest"></script>
<style>
:root {
  --bg:#0f1113; --bg-elev:#15181b; --bg-elev-2:#1b1f24;
  --text:#e6e7e9; --muted:#aab0b6; --muted-2:#8b9197;
  --border:#23262a; --hover:rgba(255,255,255,.04);
  --focus:rgba(255,255,255,.08); --accent:#e5e7eb;
  --accent-contrast:#0f1113; --danger:#ef4444; --success:#22c55e;
  --info:#3b82f6; --shadow:0 1px 2px rgba(0,0,0,.35);
  --radius:10px; --font:ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.6 var(--font)}
a{color:inherit;text-decoration:none}
.wrap{max-width:1100px;margin:0 auto;padding:16px 12px 40px}
header{display:flex;align-items:center;justify-content:space-between;padding:8px 0}
.brand{font-weight:700;letter-spacing:.3px}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{padding:3px 8px;border:1px solid var(--border);color:var(--muted);border-radius:999px;cursor:pointer;font-size:12px}
.tab:hover{background:var(--hover);color:var(--text)}
.tab.active{color:var(--text);background:var(--bg-elev)}
.grid{display:grid;gap:12px}
.row-2{grid-template-columns:repeat(2,minmax(0,1fr))}
.row-3{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:768px){.row-2{grid-template-columns:1fr}}
.card{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.card-h{padding:8px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-c{padding:12px}
label{font-weight:600;color:var(--muted);font-size:12px;display:block;margin-bottom:4px}
.input-group{position:relative}
.input-group .btn-icon{position:absolute;right:1px;top:1px;bottom:1px;background:transparent;border:none;padding:0 6px;color:var(--muted);cursor:pointer}
select,input[type=text],input[type=number],input[type=password],input[type=file]{width:100%;padding:6px 8px;border-radius:8px;border:1px solid var(--border);background:var(--bg-elev-2);color:var(--text);outline:none;font-size:12px}
select:hover,input:hover{background:var(--bg-elev)}
select:focus,input:focus{box-shadow:0 0 0 3px var(--focus) inset}
input[readonly]{color:var(--muted-2);cursor:default}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:4px;padding:4px 8px;font-size:11px;border-radius:8px;border:1px solid var(--border);background:var(--bg-elev-2);color:var(--text);cursor:pointer;transition:background .15s;line-height:1.2}
.btn i{width:.95em;height:.95em}
.btn:hover{background:var(--bg-elev)}
.btn-primary{background:var(--accent);color:var(--accent-contrast);border-color:var(--accent)}
.btn-primary:hover{background:#d9dde3}
.btn-ghost{background:transparent}
.btn-danger{color:var(--danger);border-color:var(--danger)}
.btn-sm{padding:3px 6px;font-size:10.5px;gap:3px}
.btn-icon-only{padding:3px;width:22px;height:22px}
.spinner{display:none;width:.9em;height:.9em;border:2px solid rgba(255,255,255,.35);border-top-color:currentColor;border-radius:50%;animation:spin 1s linear infinite}
.loading .spinner{display:inline-block}
.loading .btn-text,.loading i:not(.spinner){display:none}
@keyframes spin{to{transform:rotate(360deg)}}
table{width:100%;border-collapse:collapse}
th,td{padding:6px 8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle;font-size:12px}
th{color:var(--muted);text-transform:uppercase;font-size:10px;letter-spacing:.3px}
.pill{display:inline-flex;align-items:center;padding:1px 6px;border-radius:12px;font-size:10px;border:1px solid var(--border)}
.pill.on{color:var(--success);border-color:var(--success)}
.pill.off{color:var(--muted)}
.pill.synced{color:var(--success);border-color:var(--success)}
.pill.pending,.pill.unknown{color:var(--muted);border-color:var(--muted)}
.pill.failed{color:var(--danger);border-color:var(--danger)}
.bool-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.bool-yes{background-color:var(--success)}
.bool-no{background-color:var(--muted)}
.log{background:#0b0d0f;color:#cbd5e1;border-radius:8px;padding:8px;height:220px;overflow:auto;font-family:ui-monospace, Menlo, Consolas, monospace;font-size:12px;white-space:pre-wrap}
.accordion .item:first-child{border-top:none}
.accordion .item{border-top:1px solid var(--border)}
.accordion .head{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;cursor:pointer}
.accordion .head:hover{background:var(--hover)}
.accordion .cnt{display:none;padding:0 10px 10px}
.accordion .item.open .cnt{display:block}
.toast-wrap{position:fixed;bottom:12px;right:12px;display:flex;flex-direction:column;gap:6px;z-index:999}
.toast{background:var(--bg-elev);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:8px;box-shadow:var(--shadow);opacity:0;transform:translateY(8px);transition:opacity .2s, transform .2s;font-size:12px}
.toast.show{opacity:1;transform:translateY(0)}
footer{margin-top:16px;color:var(--muted);font-size:11px;text-align:center}
.time-filter{display:flex;gap:4px;background:var(--bg-elev-2);padding:3px;border-radius:8px;border:1px solid var(--border)}
.time-filter .btn-sm{flex-grow:1;border:none}
.time-filter .btn-sm.active{background:var(--accent);color:var(--accent-contrast)}
.upcoming-list{list-style:none;padding:0;margin:0}
.upcoming-list li{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
.upcoming-list li:last-child{border-bottom:none}
.backup-actions{display:flex;flex-wrap:wrap;gap:4px}
.inline{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
#modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.6);display:none;z-index:1000}
.modal{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:90%;max-width:520px;z-index:1001}
.modal .card-c{max-height:70vh;overflow-y:auto}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:500px){.form-grid{grid-template-columns:1fr}}
.help{color:var(--muted-2);font-size:11px;margin-top:4px}
.preview{background:#0b0d0f;border:1px solid var(--border);border-radius:8px;padding:8px;max-height:180px;overflow:auto;font-size:11px;font-family:ui-monospace, monospace}
.small{font-size:11px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">Docker Volume Backup</div>
    <nav class="tabs" id="tabs">
      <div class="tab active" data-target="overview">Overview</div>
      <div class="tab" data-target="schedules">Schedules</div>
      <div class="tab" data-target="backups">Backups</div>
      <div class="tab" data-target="logs">Logs</div>
      <div class="tab" data-target="settings">Settings</div>
    </nav>
  </header>

  <main>
    <!-- Overview -->
    <section id="section-overview">
      <div class="grid row-2">
        <div class="card">
          <div class="card-h"><div>Storage Prediction</div></div>
          <div class="card-c">
            <div class="grid row-2">
              <div>
                <div class="muted">Current Usage</div>
                <div id="currentStorage" style="font-size:22px;font-weight:700;margin-top:2px;">...</div>
              </div>
              <div>
                <div class="muted">Predicted (30 days)</div>
                <div id="predictedStorage" style="font-size:22px;font-weight:700;margin-top:2px;">...</div>
              </div>
            </div>
            <div style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px;">
              <div class="grid row-2">
                <div>
                  <div class="muted">Volumes Backed Up</div>
                  <div id="overviewBackedUp" style="font-size:14px;font-weight:600;margin-top:2px;">...</div>
                </div>
                <div>
                  <div class="muted">Next Scheduled Overall</div>
                  <div id="overviewNextScheduled" style="font-size:14px;font-weight:600;margin-top:2px;">...</div>
                </div>
              </div>
              <div style="margin-top:8px" class="grid row-2">
                <div>
                  <div class="muted">FTP Status</div>
                  <div id="overviewFtpStatus" style="font-size:13px;margin-top:2px;">Checking...</div>
                </div>
                <div>
                  <div class="muted">Sync Status</div>
                  <div id="overviewSyncStatus" style="font-size:13px;margin-top:2px;">...</div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><div>Manual Backup</div></div>
          <div class="card-c">
            <div class="grid row-2">
              <div>
                <label for="volumeSelect">Select Volume</label>
                <select id="volumeSelect"></select>
              </div>
              <div>
                <label>Container In Use</label>
                <input type="text" id="containerInfo" readonly>
              </div>
            </div>
            <div class="inline" style="margin-top:8px; justify-content: space-between;">
              <div>
                <label for="uploadToFtpSelect">Upload to FTP</label>
                <select id="uploadToFtpSelect" style="width: auto;">
                  <option value="auto">Auto (per schedule)</option>
                  <option value="yes">Force Yes</option>
                  <option value="no">Force No</option>
                </select>
              </div>
              <button class="btn btn-primary" id="createBackupBtn"><i data-lucide="plus-circle"></i><span class="btn-text">Create Backup</span><span class="spinner"></span></button>
            </div>
          </div>
        </div>
      </div>
      <div class="card" style="margin-top:12px;">
        <div class="card-h"><div>Activity</div></div>
        <div class="card-c">
          <div class="grid row-2">
            <div>
              <div class="muted" style="margin-bottom:6px; font-weight:600;">Running Tasks</div>
              <ul id="runningBackupsList" class="upcoming-list"><li class="muted">None</li></ul>
            </div>
            <div>
              <div class="muted" style="margin-bottom:6px; font-weight:600;">Upcoming Scheduled Backups</div>
              <div class="time-filter" id="timeFilter">
                <button class="btn btn-sm" data-minutes="5">5m</button>
                <button class="btn btn-sm" data-minutes="15">15m</button>
                <button class="btn btn-sm active" data-minutes="60">1h</button>
                <button class="btn btn-sm" data-minutes="360">6h</button>
                <button class="btn btn-sm" data-minutes="1440">1d</button>
              </div>
              <ul id="upcomingBackupsList" class="upcoming-list" style="margin-top:6px;">
                <li class="muted">None scheduled in this timeframe.</li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- Schedules -->
    <section id="section-schedules" style="display:none;">
      <div class="card">
        <div class="card-h">
          <div>Schedules</div>
          <button class="btn btn-primary btn-sm" id="showScheduleModalBtn"><i data-lucide="plus"></i><span class="btn-text">Add</span></button>
        </div>
        <div class="card-c" style="padding:0; overflow-x: auto;">
          <table id="scheduleTable">
            <thead><tr><th>Status</th><th>Volume</th><th>Interval</th><th>Retention</th><th>Notify</th><th>FTP</th><th>Next Run</th><th>Actions</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- Backups -->
    <section id="section-backups" style="display:none;">
      <div class="card">
        <div class="card-h">
          <div>Existing Backups</div>
          <div>
            <button class="btn btn-ghost btn-sm" id="showUploadModalBtn" title="Upload"><i data-lucide="upload"></i><span class="btn-text">Upload</span></button>
            <button class="btn btn-ghost btn-sm" id="showPruneModalBtn" title="Prune"><i data-lucide="archive-restore"></i><span class="btn-text">Prune</span></button>
          </div>
        </div>
        <div class="card-c" style="padding:0;">
          <div id="backupAccordion" class="accordion"></div>
        </div>
      </div>
    </section>

    <!-- Logs -->
    <section id="section-logs" style="display:none;">
      <div class="card">
        <div class="card-h">
          <div>Logs</div>
          <a class="btn btn-ghost btn-sm" href="/api/logs/download"><i data-lucide="download"></i>Download Logs</a>
        </div>
        <div class="card-c">
          <input type="text" id="log-search" placeholder="Filter logs..." style="margin-bottom: 8px;">
          <div id="logsContainer" class="log"></div>
        </div>
      </div>
    </section>

    <!-- Settings -->
    <section id="section-settings" style="display:none;">
      <div class="grid row-3">
        <div class="card">
          <div class="card-h"><div>FTP Settings</div></div>
          <div class="card-c">
            <div class="form-grid">
              <div><label for="ftpHost">Host</label><input type="text" id="ftpHost"></div>
              <div><label for="ftpPort">Port</label><input type="number" id="ftpPort" value="21"></div>
            </div>
            <div style="margin-top:8px;"><label for="ftpUser">Username</label><input type="text" id="ftpUser"></div>
            <div style="margin-top:8px;"><label for="ftpPass">Password</label>
              <div class="input-group">
                <input type="password" id="ftpPass" placeholder="Unchanged if blank"><button class="btn-icon" onclick="app.togglePasswordVisibility('ftpPass')"><i data-lucide="eye"></i></button>
              </div>
            </div>
            <div style="margin-top:8px;"><label for="ftpBase">Remote Base Path</label><input type="text" id="ftpBase" placeholder="/backups"></div>
            <div class="form-grid" style="margin-top:8px;">
              <div><label>Passive</label><select id="ftpPassive"><option value="true">Yes</option><option value="false">No</option></select></div>
              <div><label>Secure (FTPS)</label><select id="ftpSecure"><option value="false">No</option><option value="true">Yes</option></select></div>
            </div>
            <div class="form-grid" style="margin-top:8px;">
              <div><label>FTPS Mode</label><select id="ftpMode"><option value="explicit">Explicit</option><option value="implicit">Implicit</option></select></div>
              <div><label>NAT PASV Fix</label><select id="ftpNatFix"><option value="false">No</option><option value="true">Yes</option></select></div>
            </div>
            <hr style="border:none; border-top:1px solid var(--border); margin: 12px 0;">
            <div class="form-grid">
              <div><label>Auto Sync</label><select id="syncEnabled"><option value="false">Disabled</option><option value="true">Enabled</option></select></div>
              <div><label>Sync Interval (min)</label><input type="number" id="syncInterval" value="60"></div>
            </div>
            <div class="inline" style="margin-top:12px; justify-content:space-between; gap:6px;">
              <button class="btn" id="syncNowBtn"><i data-lucide="refresh-cw"></i><span class="btn-text">Sync All</span><span class="spinner"></span></button>
              <div class="inline">
                <div id="nextSyncText" class="muted small"></div>
                <button class="btn" id="testFtpBtn"><i data-lucide="plug-zap"></i><span class="btn-text">Test</span><span class="spinner"></span></button>
                <button class="btn btn-primary" id="saveFtpBtn"><i data-lucide="save"></i><span class="btn-text">Save</span><span class="spinner"></span></button>
              </div>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><div>General Settings</div></div>
          <div class="card-c">
            <div><label for="uiTimeZone">Display Time Zone</label><select id="uiTimeZone"></select></div>
            <div class="help">Affects UI display only. Server and filenames use UTC.</div>
            <hr style="border:none; border-top:1px solid var(--border); margin: 12px 0;">
            <div class="muted" style="font-weight:600;">Notification (ntfy.sh)</div>
            <div style="margin-top:8px;"><label for="ntfyBase">Base URL</label><input type="text" id="ntfyBase" value="https://ntfy.sh"></div>
            <div style="margin-top:8px;"><label for="ntfyTopic">Topic</label><input type="text" id="ntfyTopic" value="docker-volume-backup"></div>
            <hr style="border:none; border-top:1px solid var(--border); margin: 12px 0;">
            <div class="muted" style="font-weight:600;">Log Settings</div>
            <div style="margin-top:8px;"><label for="logMaxLines">Max In-Memory Log Lines</label><input type="number" id="logMaxLines" value="200" min="50" max="5000"></div>
            <div style="text-align:right; margin-top:12px;">
              <button class="btn btn-primary" id="saveGeneralSettingsBtn"><i data-lucide="save"></i><span class="btn-text">Save All</span><span class="spinner"></span></button>
            </div>
          </div>
        </div>
      </div>
    </section>
  </main>
</div>

<!-- Modals -->
<div id="modal-backdrop"></div>
<div id="restoreModal" class="modal">
  <div class="card">
    <div class="card-h"><div>Confirm Restore</div><button class="btn btn-ghost btn-icon-only" onclick="app.closeModal('restoreModal')"><i data-lucide="x"></i></button></div>
    <div class="card-c">
      <p>Are you sure you want to restore <b id="restoreBackupInfo"></b>? This will stop the associated container, wipe the current volume data, and replace it with this backup.</p>
      <label for="restoreVolumeSelect">Target Volume</label>
      <select id="restoreVolumeSelect"></select>
    </div>
    <div class="card-h" style="border-top:1px solid var(--border); justify-content: flex-end; gap: 6px;">
      <button class="btn" onclick="app.closeModal('restoreModal')">Cancel</button>
      <button class="btn btn-danger" id="confirmRestoreBtn"><i data-lucide="history"></i><span class="btn-text">Restore</span><span class="spinner"></span></button>
    </div>
  </div>
</div>
<div id="scheduleModal" class="modal">
  <div class="card">
    <div class="card-h"><div id="scheduleModalTitle">Add Schedule</div><button class="btn btn-ghost btn-icon-only" onclick="app.closeModal('scheduleModal')"><i data-lucide="x"></i></button></div>
    <div class="card-c">
      <div><label for="scheduleVolumeSelect">Volume</label><select id="scheduleVolumeSelect"></select></div>
      <div style="margin-top:8px;"><label>Backup Interval</label>
        <div class="grid row-2">
          <div><label for="intervalDays">Days</label><input type="number" id="intervalDays" min="0" value="0"></div>
          <div><label for="intervalHours">Hours</label><input type="number" id="intervalHours" min="0" max="23" value="1"></div>
          <div><label for="intervalMinutes">Minutes</label><input type="number" id="intervalMinutes" min="0" max="59" value="0"></div>
          <div><label for="intervalSeconds">Seconds</label><input type="number" id="intervalSeconds" min="0" max="59" value="0"></div>
        </div>
      </div>
      <div style="margin-top:8px;"><label>Retention</label>
        <div class="grid row-3">
          <div><label for="pruneKeepDays">Days</label><input type="number" id="pruneKeepDays" min="0" value="30"></div>
          <div><label for="pruneKeepHours">Hours</label><input type="number" id="pruneKeepHours" min="0" value="0"></div>
          <div><label for="pruneKeepMinutes">Minutes</label><input type="number" id="pruneKeepMinutes" min="0" value="0"></div>
        </div>
        <div class="help">Backups older than this will be pruned after a scheduled run.</div>
      </div>
      <div style="margin-top:8px; display:flex; gap: 16px;">
        <label class="muted" style="display:flex;gap:6px;align-items:center;"><input type="checkbox" id="notificationsEnabled" checked> Notifications</label>
        <label class="muted" style="display:flex;gap:6px;align-items:center;"><input type="checkbox" id="ftpUploadEnabled" checked> FTP Upload</label>
      </div>
    </div>
    <div class="card-h" style="border-top:1px solid var(--border); justify-content: flex-end; gap: 6px;">
      <button class="btn" onclick="app.closeModal('scheduleModal')">Cancel</button>
      <button class="btn btn-primary" id="confirmScheduleBtn"><i data-lucide="save"></i><span class="btn-text">Save</span><span class="spinner"></span></button>
    </div>
  </div>
</div>
<div id="pruneModal" class="modal">
  <div class="card">
    <div class="card-h"><div>Prune Backups</div><button class="btn btn-ghost btn-icon-only" onclick="app.closeModal('pruneModal')"><i data-lucide="x"></i></button></div>
    <div class="card-c">
      <p>Delete backups older than the specified retention period for a volume.</p>
      <div><label for="pruneVolumeSelect">Volume</label><select id="pruneVolumeSelect"></select></div>
      <div style="margin-top:8px;"><label>Keep Backups For</label>
        <div class="grid row-3">
          <div><label for="pruneDaysInputManual">Days</label><input type="number" id="pruneDaysInputManual" min="0" value="30"></div>
          <div><label for="pruneHoursInputManual">Hours</label><input type="number" id="pruneHoursInputManual" min="0" value="0"></div>
          <div><label for="pruneMinutesInputManual">Minutes</label><input type="number" id="pruneMinutesInputManual" min="0" value="0"></div>
        </div>
      </div>
      <div style="margin-top: 8px;">
        <div id="pruneSummary" class="small muted"></div>
        <div id="prunePreviewList" class="preview"></div>
      </div>
    </div>
    <div class="card-h" style="border-top:1px solid var(--border); justify-content: flex-end; gap: 6px;">
      <button class="btn" onclick="app.closeModal('pruneModal')">Cancel</button>
      <button class="btn btn-danger" id="confirmPruneBtn" disabled><i data-lucide="archive-restore"></i><span class="btn-text">Prune</span><span class="spinner"></span></button>
    </div>
  </div>
</div>
<div id="uploadModal" class="modal">
  <div class="card">
    <div class="card-h"><div>Upload Backup</div><button class="btn btn-ghost btn-icon-only" onclick="app.closeModal('uploadModal')"><i data-lucide="x"></i></button></div>
    <div class="card-c">
      <p>Upload a <code>.tar.gz</code> backup file for a specific volume.</p>
      <div><label for="uploadVolumeSelect">Target Volume</label><select id="uploadVolumeSelect"></select></div>
      <div style="margin-top:8px;"><label for="backupFileInput">Backup File</label><input type="file" id="backupFileInput" accept=".tar.gz"></div>
    </div>
    <div class="card-h" style="border-top:1px solid var(--border); justify-content: flex-end; gap: 6px;">
      <button class="btn" onclick="app.closeModal('uploadModal')">Cancel</button>
      <button class="btn btn-primary" id="confirmUploadBtn"><i data-lucide="upload"></i><span class="btn-text">Upload</span><span class="spinner"></span></button>
    </div>
  </div>
</div>

<div id="backupLogModal" class="modal">
  <div class="card" style="width: 90%; max-width: 800px;">
    <div class="card-h"><div id="backupLogModalTitle">Backup Logs</div><button class="btn btn-ghost btn-icon-only" onclick="app.closeModal('backupLogModal')"><i data-lucide="x"></i></button></div>
    <div class="card-c"><div id="backupLogContent" class="log" style="height: 50vh;"></div></div>
  </div>
</div>

<div class="toast-wrap" id="toast-container"></div>
<footer>Docker Volume Backup Manager v2.0 &middot; free to use &middot; made by <a href="https://kiunke.dev" target="_blank" rel="noopener" style="color:inherit">kiunke.dev</a></footer>

<script>
document.addEventListener('DOMContentLoaded', () => {
  const state = {
    volumes: [], backups_structured: {}, schedules: [], logs: [],
    sync_status: {}, ftp_settings: {}, notification_settings: {},
    overview: {}, log_settings: {}, ftp_health: {}
  };
  let upcomingFilterMinutes = 60;
  const TZ_KEY = 'dvb-timezone';
  const PERSIST_KEYS = [
    'ftpHost','ftpPort','ftpUser','ftpBase','ftpPassive','ftpSecure','ftpMode','ftpNatFix',
    'syncEnabled','syncInterval','ntfyBase','ntfyTopic','logMaxLines','uiTimeZone',
    'uploadToFtpSelect'
  ];

  const getTZ = () => localStorage.getItem(TZ_KEY) || undefined;
  const formatDate = (isoString) => {
    if (!isoString || isoString === 'N/A') return 'N/A';
    try {
      return new Date(isoString).toLocaleString(navigator.language, { timeZone: getTZ() });
    } catch (e) {
      return new Date(isoString).toLocaleString();
    }
  };

  // Persistence helpers
  const persistInit = () => {
    PERSIST_KEYS.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      // Restore
      const saved = localStorage.getItem('persist:' + id);
      if (saved !== null) {
        try {
          if (el.tagName === 'SELECT' || el.type === 'text' || el.type === 'number' || el.type === 'password') {
            el.value = JSON.parse(saved);
          }
        } catch {}
      }
      // Listen
      el.addEventListener('input', () => {
        localStorage.setItem('persist:' + id, JSON.stringify(el.value));
      });
      el.addEventListener('change', () => {
        localStorage.setItem('persist:' + id, JSON.stringify(el.value));
      });
    });
  };

  const api = {
    async request(method, url, body) {
      const options = { method, headers: {} };
      if (body) {
        if (body instanceof FormData) {
          options.body = body;
        } else {
          options.headers['Content-Type'] = 'application/json';
          options.body = JSON.stringify(body);
        }
      }
      const r = await fetch(url, options);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || 'Request failed');
      return d;
    },
    get(url) { return this.request('GET', url); },
    post(url, body) { return this.request('POST', url, body); },
    del(url) { return this.request('DELETE', url); },
    put(url, body) { return this.request('PUT', url, body); }
  };

  const showToast = (msg, isError = false) => {
    const wrap = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    if (isError) el.style.borderColor = 'var(--danger)';
    wrap.appendChild(el);
    setTimeout(() => el.classList.add('show'), 10);
    setTimeout(() => {
      el.classList.remove('show');
      el.addEventListener('transitionend', () => el.remove());
    }, 3000);
  };

  const fmtBytes = (bytes, dec = 2) => {
    if (!+bytes) return '0 B';
    const k = 1024;
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dec))} ${['B', 'KB', 'MB', 'GB', 'TB'][i]}`;
  };
  const boolDot = (on, titleYes='Enabled', titleNo='Disabled') => `<span class="bool-dot ${on ? 'bool-yes' : 'bool-no'}" title="${on ? titleYes : titleNo}"></span>`;

  const render = {
    all() {
      this.volumes();
      this.schedules();
      this.backups();
      this.logs();
      this.syncStatus();
      this.settings();
      this.overview();
      lucide.createIcons();
    },
    volumes() {
      const opts = state.volumes.length ? state.volumes.map(v => `<option value="${v.name}">${v.name}</option>`).join('') : '<option>No volumes found</option>';
      ['volumeSelect','scheduleVolumeSelect','pruneVolumeSelect','restoreVolumeSelect','uploadVolumeSelect'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
          const currentVal = el.value;
          el.innerHTML = opts;
          if (state.volumes.some(v => v.name === currentVal)) el.value = currentVal;
        }
      });
      const selectedVolume = state.volumes.find(v => v.name === document.getElementById('volumeSelect').value);
      document.getElementById('containerInfo').value = selectedVolume ? (selectedVolume.container[1] || 'N/A') : 'N/A';
    },
    schedules() {
      const ftpReachable = !!state.ftp_health?.reachable;
      document.querySelector('#scheduleTable tbody').innerHTML = (state.schedules || []).map(s => {
        const keepStr = (s.prune_keep_days || s.prune_keep_hours || s.prune_keep_minutes)
          ? [s.prune_keep_days ? `${s.prune_keep_days}d` : '', s.prune_keep_hours ? `${s.prune_keep_hours}h` : '', s.prune_keep_minutes ? `${s.prune_keep_minutes}m` : ''].filter(Boolean).join(' ')
          : '∞';
        // FTP pill only green when schedule upload enabled AND current FTP reachable
        const ftpOk = s.ftp_upload_enabled && ftpReachable;
        return `<tr>
          <td><span class="pill ${s.enabled ? 'on' : 'off'}">${s.enabled ? 'Active' : 'Paused'}</span></td>
          <td>${s.volume}</td>
          <td>${s.interval_str}</td>
          <td>${keepStr}</td>
          <td>${boolDot(s.notifications_enabled)}</td>
          <td>${boolDot(ftpOk, 'FTP OK', 'FTP Unavailable')}</td>
          <td>${formatDate(s.next_run)}</td>
          <td class="backup-actions">
            <button class="btn btn-ghost btn-sm" onclick="app.showScheduleModal('${s.volume}')" title="Edit"><i data-lucide="edit"></i></button>
            <button class="btn btn-ghost btn-sm" onclick="app.toggleSchedule('${s.volume}', ${!s.enabled})" title="${s.enabled ? 'Pause' : 'Resume'}">${s.enabled ? '<i data-lucide="pause-circle"></i>' : '<i data-lucide="play-circle"></i>'}</button>
            <button class="btn btn-danger btn-sm" onclick="app.removeSchedule('${s.volume}')" title="Delete"><i data-lucide="trash-2"></i></button>
          </td></tr>`;
      }).join('');
    },
    backups() {
      const acc = document.getElementById('backupAccordion');
      const openVolumes = JSON.parse(sessionStorage.getItem('openAccordions') || '[]');
      const content = Object.entries(state.backups_structured || {}).map(([volume, backups]) => `
        <div class="item ${openVolumes.includes(volume) ? 'open' : ''}" data-volume="${volume}">
          <div class="head" onclick="app.toggleAccordion(this)">
            <div>${volume}</div><div class="muted">${backups.length} file(s)</div>
          </div>
          <div class="cnt" style="overflow-x: auto;">
            <table>
              <thead><tr><th>Date</th><th>Size</th><th>FTP Status</th><th>Actions</th></tr></thead>
              <tbody>${backups.map(b => `<tr>
                <td>${formatDate(b.timestamp)}</td>
                <td>${b.size_formatted}</td>
                <td><span class="pill ${b.ftp_status}">${b.ftp_status}</span></td>
                <td class="backup-actions">
                  <a class="btn btn-ghost btn-sm" href="${b.download_url}" title="Download"><i data-lucide="download"></i></a>
                  <button class="btn btn-ghost btn-sm" onclick="app.showRestoreModal('${b.path}','${b.filename}','${volume}')" title="Restore"><i data-lucide="history"></i></button>
                  <button class="btn btn-ghost btn-sm" onclick="app.showBackupLogs('${b.path}')" title="View Logs"><i data-lucide="file-text"></i></button>
                  <button class="btn btn-danger btn-sm" onclick="app.deleteBackup('${b.path}')" title="Delete"><i data-lucide="trash-2"></i></button>
                </td></tr>`).join('')}
              </tbody>
            </table>
          </div></div>`).join('');
      acc.innerHTML = content || '<div class="card-c muted">No backups found.</div>';
    },
    logs() {
      const container = document.getElementById('logsContainer');
      const filter = document.getElementById('log-search').value.toLowerCase();
      const formattedLogs = (state.logs || []).filter(log => log.toLowerCase().includes(filter)).map(log => {
        const match = log.match(/^\\[([^\\]]+)\\](.*)/);
        if (match) {
          return '[' + formatDate(match[1]) + ']' + match[2];
        }
        return log;
      });
      container.textContent = formattedLogs.join('\\n');
      if (filter === '') container.scrollTop = container.scrollHeight;
    },
    syncStatus() {
      const nextRun = state.sync_status?.next_run || 'N/A';
      const text = nextRun === 'N/A' ? '' : 'Next sync: ' + formatDate(nextRun).split(', ')[1];
      document.getElementById('nextSyncText').textContent = text;
    },
    settings() {
      const ftp = state.ftp_settings || {};
      const ntfy = state.notification_settings || {};
      const log = state.log_settings || {};
      const setValue = (id, val) => {
        const el = document.getElementById(id);
        if (!el) return;
        // Respect local persisted value if present
        const saved = localStorage.getItem('persist:' + id);
        if (saved !== null) return; // user-persisted value wins
        if (val !== undefined) el.value = val;
      };
      setValue('ftpHost', ftp.host);
      setValue('ftpPort', ftp.port);
      setValue('ftpUser', ftp.username);
      setValue('ftpBase', ftp.remote_base_path);
      setValue('ftpPassive', ftp.passive);
      setValue('ftpSecure', ftp.secure);
      setValue('ftpMode', ftp.mode || 'explicit');
      setValue('ftpNatFix', ftp.nat_pasv_fix);
      setValue('syncEnabled', ftp.sync_enabled);
      setValue('syncInterval', ftp.sync_interval_minutes);
      setValue('ntfyBase', ntfy.base_url);
      setValue('ntfyTopic', ntfy.topic);
      setValue('logMaxLines', log.max_lines);
      setValue('uiTimeZone', getTZ() || 'system');
    },
    overview() {
      const o = state.overview || {};
      document.getElementById('overviewBackedUp').textContent = `${o.backed_up_volumes_count ?? 0} / ${o.total_volumes_count ?? 0}`;
      document.getElementById('overviewNextScheduled').textContent = formatDate(o.next_scheduled_backup);
      const runningList = document.getElementById('runningBackupsList');
      runningList.innerHTML = (o.currently_running_backups?.length > 0) ? o.currently_running_backups.map(item => `<li>${item}</li>`).join('') : '<li class="muted">None</li>';
      // FTP status
      const fh = state.ftp_health || {};
      const ftpLine = fh.reachable
        ? `Connected (${fh.mode || (state.ftp_settings?.secure ? 'FTPS' : 'FTP')}, passive ${state.ftp_settings?.passive ? 'on' : 'off'})`
        : 'Not reachable — not syncing';
      document.getElementById('overviewFtpStatus').textContent = ftpLine;
      const syncEnabled = !!state.sync_status?.enabled;
      document.getElementById('overviewSyncStatus').textContent = syncEnabled ? 'Sync enabled' : 'Sync disabled';
      // Upcoming
      const upcomingList = document.getElementById('upcomingBackupsList');
      const now = new Date();
      const cutoff = new Date(now.getTime() + upcomingFilterMinutes * 60000);
      const upcoming = (state.schedules || [])
        .filter(s => s.enabled && s.next_run !== 'N/A' && new Date(s.next_run) < cutoff)
        .sort((a,b) => new Date(a.next_run) - new Date(b.next_run));
      upcomingList.innerHTML = (upcoming.length > 0) ? upcoming.map(s => `<li><span>${s.volume}</span><span class="muted">${formatDate(s.next_run)}</span></li>`).join('') : '<li class="muted">None scheduled in this timeframe.</li>';
    },
    predictions(data) {
      if (!data) return;
      document.getElementById('currentStorage').textContent = fmtBytes(data.current_size_bytes);
      document.getElementById('predictedStorage').textContent = fmtBytes(data.predicted_size_bytes_30d);
    },
    prunePreview() {
      const vol = document.getElementById('pruneVolumeSelect').value;
      const days = +document.getElementById('pruneDaysInputManual').value || 0;
      const hours = +document.getElementById('pruneHoursInputManual').value || 0;
      const minutes = +document.getElementById('pruneMinutesInputManual').value || 0;
      const keepSeconds = days*86400 + hours*3600 + minutes*60;
      const summaryEl = document.getElementById('pruneSummary');
      const listEl = document.getElementById('prunePreviewList');
      const btnEl = document.getElementById('confirmPruneBtn');

      if (!vol || keepSeconds <= 0) {
        summaryEl.textContent = 'Preview: 0 backups would be deleted.';
        listEl.textContent = 'Select a volume and set a retention period greater than 0.';
        btnEl.disabled = true;
        return;
      }
      const cutoff = new Date(Date.now() - keepSeconds * 1000);
      const backupsForVolume = state.backups_structured[vol] || [];
      const itemsToDelete = backupsForVolume.filter(b => new Date(b.timestamp) < cutoff);
      summaryEl.textContent = `Preview: ${itemsToDelete.length} backup(s) for ${vol} will be deleted.`;
      btnEl.disabled = itemsToDelete.length === 0;
      if (itemsToDelete.length > 0) {
        listEl.innerHTML = itemsToDelete.map(b => `• ${b.filename} <span class="muted">- ${formatDate(b.timestamp)}</span>`).join('<br>');
      } else {
        listEl.textContent = 'No backups match the specified retention period.';
      }
    }
  };

  const app = window.app = {
    async refreshState() {
      try {
        const newState = await api.get('/api/state');
        Object.assign(state, newState);
        render.all();
      } catch (e) {
        showToast("Failed to fetch state: " + e.message, true);
      }
    },
    async refreshPredictions() {
      render.predictions(await api.get('/api/storage_prediction'));
    },
    pollTask(taskId) {
      const interval = setInterval(async () => {
        try {
          const status = await api.get(`/api/task_status/${taskId}`);
          if (['success', 'failure'].includes(status.status)) {
            clearInterval(interval);
            showToast(status.message, status.status === 'failure');
            this.refreshState();
            this.refreshPredictions();
          } else if (status.status === 'not_found') {
            clearInterval(interval);
          }
        } catch (e) {
          clearInterval(interval);
          showToast('Failed to get task status.', true);
        }
      }, 2000);
    },
    async withLoading(btnId, action) {
      const btn = document.getElementById(btnId);
      if (btn?.classList.contains('loading')) return;
      btn?.classList.add('loading');
      try {
        const result = await action();
        if (result?.task_id) this.pollTask(result.task_id);
      } catch (e) {
        showToast(e.message, true);
      } finally {
        btn?.classList.remove('loading');
      }
    },
    createBackup() {
      this.withLoading('createBackupBtn', () => {
        const volume = document.getElementById('volumeSelect').value;
        if (!volume || volume === 'No volumes found') return Promise.resolve();
        const upSel = document.getElementById('uploadToFtpSelect').value;
        let upload_to_ftp;
        if (upSel === 'yes') upload_to_ftp = true;
        if (upSel === 'no') upload_to_ftp = false;
        showToast(`Backup started for ${volume}...`);
        return api.post('/api/backup', { volume, upload_to_ftp });
      });
    },
    toggleAccordion(headElement) {
      const item = headElement.parentElement;
      item.classList.toggle('open');
      let open = JSON.parse(sessionStorage.getItem('openAccordions') || '[]');
      const volume = item.dataset.volume;
      if (item.classList.contains('open')) open.push(volume);
      else open = open.filter(v => v !== volume);
      sessionStorage.setItem('openAccordions', JSON.stringify([...new Set(open)]));
    },
    async deleteBackup(path) {
      if (!confirm('Delete this backup? This cannot be undone.')) return;
      try {
        const r = await api.del(`/api/backups/${encodeURIComponent(path.substring(1))}`);
        showToast(r.message);
        this.refreshState();
        this.refreshPredictions()
      } catch (e) {
        showToast(e.message, true);
      }
    },
    async removeSchedule(volume) {
      if (!confirm(`Remove schedule for ${volume}?`)) return;
      try {
        const r = await api.del(`/api/schedules/${encodeURIComponent(volume)}`);
        showToast(r.message);
        this.refreshState();
        this.refreshPredictions()
      } catch (e) {
        showToast(e.message, true);
      }
    },
    async toggleSchedule(volume, enabled) {
      try {
        const r = await api.post('/api/schedules/toggle', { volume, enabled });
        showToast(r.message);
        this.refreshState();
        this.refreshPredictions()
      } catch (e) {
        showToast(e.message, true);
      }
    },
    showModal(id) {
      document.getElementById('modal-backdrop').style.display = 'block';
      document.getElementById(id).style.display = 'block';
      if (id === 'pruneModal') {
        render.prunePreview();
        const inputs = ['pruneVolumeSelect','pruneDaysInputManual','pruneHoursInputManual','pruneMinutesInputManual'];
        inputs.forEach(elId => document.getElementById(elId).addEventListener('input', render.prunePreview));
      }
    },
    closeModal(id) {
      document.getElementById('modal-backdrop').style.display = 'none';
      document.getElementById(id).style.display = 'none';
    },
    showRestoreModal(path, filename, defaultVolume) {
      const modal = document.getElementById('restoreModal');
      modal.dataset.path = path;
      document.getElementById('restoreBackupInfo').textContent = filename;
      document.getElementById('restoreVolumeSelect').value = defaultVolume;
      this.showModal('restoreModal');
    },
    showScheduleModal(volumeToEdit = null) {
      const modal = document.getElementById('scheduleModal');
      const volSelect = document.getElementById('scheduleVolumeSelect');
      const s = (state.schedules || []).find(x => x.volume === volumeToEdit);
      modal.dataset.mode = s ? 'edit' : 'add';
      document.getElementById('scheduleModalTitle').textContent = s ? `Edit Schedule: ${volumeToEdit}` : 'Add Schedule';
      volSelect.value = s ? s.volume : state.volumes?.name;
      volSelect.disabled = !!s;
      document.getElementById('intervalDays').value = s?.interval_days || 0;
      document.getElementById('intervalHours').value = s?.interval_hours || 1;
      document.getElementById('intervalMinutes').value = s?.interval_minutes || 0;
      document.getElementById('intervalSeconds').value = s?.interval_seconds || 0;
      document.getElementById('pruneKeepDays').value = s?.prune_keep_days || 30;
      document.getElementById('pruneKeepHours').value = s?.prune_keep_hours || 0;
      document.getElementById('pruneKeepMinutes').value = s?.prune_keep_minutes || 0;
      document.getElementById('notificationsEnabled').checked = s ? s.notifications_enabled : true;
      document.getElementById('ftpUploadEnabled').checked = s ? s.ftp_upload_enabled : true;
      this.showModal('scheduleModal');
    },
    async showBackupLogs(path) {
      const filename = path.split('/').pop();
      document.getElementById('backupLogModalTitle').textContent = `Logs for ${filename}`;
      const logContent = document.getElementById('backupLogContent');
      logContent.textContent = 'Loading logs...';
      this.showModal('backupLogModal');
      try {
        const logs = await api.get(`/api/backups/logs?path=${encodeURIComponent(path)}`);
        logContent.textContent = logs.length > 0 ? logs.map(log => {
          const match = log.match(/^\\[([^\\]]+)\\](.*)/);
          if (match) return `[${formatDate(match[1])}]${match[2]}`;
          return log;
        }).join('\\n') : 'No specific logs found for this backup file.';
      } catch (e) {
        logContent.textContent = `Error loading logs: ${e.message}`;
      }
    },
    confirmRestore() {
      this.withLoading('confirmRestoreBtn', async () => {
        showToast('Restore started...');
        const r = await api.post('/api/restore', {
          backup_path: document.getElementById('restoreModal').dataset.path,
          volume: document.getElementById('restoreVolumeSelect').value
        });
        this.closeModal('restoreModal');
        return r;
      });
    },
    confirmSchedule() {
      this.withLoading('confirmScheduleBtn', async () => {
        const modal = document.getElementById('scheduleModal');
        const isEdit = modal.dataset.mode === 'edit';
        const volume = document.getElementById('scheduleVolumeSelect').value;
        const body = {
          interval_days: +document.getElementById('intervalDays').value,
          interval_hours: +document.getElementById('intervalHours').value,
          interval_minutes: +document.getElementById('intervalMinutes').value,
          interval_seconds: +document.getElementById('intervalSeconds').value,
          prune_keep_days: +document.getElementById('pruneKeepDays').value,
          prune_keep_hours: +document.getElementById('pruneKeepHours').value,
          prune_keep_minutes: +document.getElementById('pruneKeepMinutes').value,
          notifications_enabled: document.getElementById('notificationsEnabled').checked,
          ftp_upload_enabled: document.getElementById('ftpUploadEnabled').checked
        };
        const r = isEdit ? await api.put(`/api/schedules/${encodeURIComponent(volume)}`, body) : await api.post('/api/schedules', { ...body, volume });
        showToast(r.message);
        await this.refreshState();
        this.refreshPredictions()
        this.closeModal('scheduleModal');
      });
    },
    confirmPrune() {
      this.withLoading('confirmPruneBtn', async () => {
        const r = await api.post('/api/prune_extended', {
          volume: document.getElementById('pruneVolumeSelect').value,
          days: +document.getElementById('pruneDaysInputManual').value,
          hours: +document.getElementById('pruneHoursInputManual').value,
          minutes: +document.getElementById('pruneMinutesInputManual').value
        });
        showToast(`Pruned ${r.deleted_count} backups.`);
        await this.refreshState();
        this.refreshPredictions()
        this.closeModal('pruneModal');
      });
    },
    confirmUpload() {
      this.withLoading('confirmUploadBtn', async () => {
        const fileInput = document.getElementById('backupFileInput');
        if (!fileInput.files || fileInput.files.length === 0) throw new Error('Please select a file.');
        const fd = new FormData();
        fd.append('volume', document.getElementById('uploadVolumeSelect').value);
        fd.append('backup_file', fileInput.files[0]);
        const r = await api.post('/api/upload_backup', fd);
        showToast(r.message);
        await this.refreshState();
        this.closeModal('uploadModal');
        fileInput.value = '';
      });
    },
    togglePasswordVisibility(id) {
      const el = document.getElementById(id);
      const icon = el.nextElementSibling.querySelector('i');
      if (el.type === 'password') {
        el.type = 'text';
        icon.setAttribute('data-lucide', 'eye-off');
      } else {
        el.type = 'password';
        icon.setAttribute('data-lucide', 'eye');
      }
      lucide.createIcons();
    },
    testFtp() {
      this.withLoading('testFtpBtn', async () => {
        const body = {
          host: document.getElementById('ftpHost').value.trim(),
          port: +document.getElementById('ftpPort').value,
          username: document.getElementById('ftpUser').value,
          password: document.getElementById('ftpPass').value || undefined,
          remote_base_path: document.getElementById('ftpBase').value.trim(),
          passive: document.getElementById('ftpPassive').value === 'true',
          secure: document.getElementById('ftpSecure').value === 'true',
          mode: document.getElementById('ftpMode').value,
          nat_pasv_fix: document.getElementById('ftpNatFix').value === 'true'
        };
        const r = await api.post('/api/settings/ftp/test', body);
        showToast(r.message, !r.ok);
      });
    },
    saveFtpSettings() {
      this.withLoading('saveFtpBtn', async () => {
        const body = {
          host: document.getElementById('ftpHost').value.trim(),
          port: +document.getElementById('ftpPort').value,
          username: document.getElementById('ftpUser').value,
          password: document.getElementById('ftpPass').value,
          remote_base_path: document.getElementById('ftpBase').value.trim(),
          passive: document.getElementById('ftpPassive').value === 'true',
          secure: document.getElementById('ftpSecure').value === 'true',
          mode: document.getElementById('ftpMode').value,
          nat_pasv_fix: document.getElementById('ftpNatFix').value === 'true',
          sync_enabled: document.getElementById('syncEnabled').value === 'true',
          sync_interval_minutes: +document.getElementById('syncInterval').value
        };
        const r = await api.post('/api/settings/ftp', body);
        showToast(r.message);
        this.refreshState();
        this.refreshPredictions()
      });
    },
    saveGeneralSettings() {
      this.withLoading('saveGeneralSettingsBtn', async () => {
        const ntfyBody = {
          base_url: document.getElementById('ntfyBase').value.trim(),
          topic: document.getElementById('ntfyTopic').value.trim()
        };
        const logBody = {
          max_lines: +document.getElementById('logMaxLines').value,
          timezone: document.getElementById('uiTimeZone').value
        };
        localStorage.setItem(TZ_KEY, logBody.timezone);
        await api.post('/api/settings/notifications', ntfyBody);
        await api.post('/api/settings/logs', logBody);
        showToast('General settings saved');
        this.refreshState();
        this.refreshPredictions()
      });
    },
    syncNow(btnId) {
      this.withLoading(btnId, async () => {
        const r = await api.post('/api/sync_now');
        showToast(r.message);
        return r;
      });
    },
    populateTimezones() {
      const timezones = ["system","UTC","Europe/London","Europe/Berlin","Europe/Moscow","America/New_York","America/Chicago","America/Denver","America/Los_Angeles","Asia/Tokyo","Asia/Dubai","Asia/Kolkata","Australia/Sydney"];
      const select = document.getElementById('uiTimeZone');
      select.innerHTML = timezones.map(tz => `<option value="${tz === 'system' ? '' : tz}">${tz.replace(/_/g, ' ')}</option>`).join('');
    },
    init() {
      this.populateTimezones();
      persistInit();

      document.getElementById('tabs').addEventListener('click', e => {
        const tab = e.target.closest('.tab');
        if (!tab) return;
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('main > section').forEach(s => s.style.display = 'none');
        document.getElementById(`section-${tab.dataset.target}`).style.display = 'block';
        this.refreshState();
        this.refreshPredictions()
      });
      document.getElementById('timeFilter').addEventListener('click', e => {
        const btn = e.target.closest('.btn-sm');
        if (!btn) return;
        upcomingFilterMinutes = +btn.dataset.minutes;
        document.querySelectorAll('#timeFilter .btn-sm').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        render.overview();
      });
      document.getElementById('log-search').addEventListener('input', () => render.logs());
      document.getElementById('createBackupBtn').addEventListener('click', () => this.createBackup());
      document.getElementById('confirmRestoreBtn').addEventListener('click', () => this.confirmRestore());
      document.getElementById('confirmScheduleBtn').addEventListener('click', () => this.confirmSchedule());
      document.getElementById('confirmPruneBtn').addEventListener('click', () => this.confirmPrune());
      document.getElementById('confirmUploadBtn').addEventListener('click', () => this.confirmUpload());
      document.getElementById('testFtpBtn').addEventListener('click', () => this.testFtp());
      document.getElementById('saveFtpBtn').addEventListener('click', () => this.saveFtpSettings());
      document.getElementById('saveGeneralSettingsBtn').addEventListener('click', () => this.saveGeneralSettings());
      document.getElementById('syncNowBtn').addEventListener('click', (e) => this.syncNow(e.currentTarget.id));
      document.getElementById('showScheduleModalBtn').addEventListener('click', () => this.showScheduleModal());
      document.getElementById('showUploadModalBtn').addEventListener('click', () => this.showModal('uploadModal'));
      document.getElementById('showPruneModalBtn').addEventListener('click', () => this.showModal('pruneModal'));
      document.getElementById('volumeSelect').addEventListener('change', () => render.volumes());

      this.refreshState().then(() => this.refreshPredictions());
      setInterval(() => {
        if (document.getElementById('modal-backdrop').style.display === 'block') return;
        this.refreshState();
        this.refreshPredictions()
      }, 5000);
    }
  };

  app.init();
});
</script>
</body>
</html>
"""

app = Flask(__name__)


# --- FTP helpers: explicit/implicit FTPS + NAT PASV fix ---

class ImplicitFTP_TLS(FTP_TLS):
    """
    FTP over implicit TLS (typically port 990).
    Wrap control socket immediately after connect, then login, then PROT P. [2][3]
    """

    def __init__(self, *args, **kwargs):
        self._context = kwargs.pop('context', ssl.create_default_context())
        super().__init__(*args, **kwargs)

    def connect(self, host='', port=0, timeout=-1, source_address=None):
        if host:
            self.host = host
        if port:
            self.port = port
        self.af = 0
        if source_address:
            source_address = (source_address, 0)
        sock = socket.create_connection((self.host, self.port), timeout, source_address)
        if timeout != -1:
            sock.settimeout(timeout)
        # Wrap immediately for implicit
        self.sock = self._context.wrap_socket(sock)
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def apply_nat_pasv_fix(ftp, control_host):
    """
    Override makepasv so that host from PASV is replaced with control_host, preserving port. [11]
    """

    def _makepasv():
        host, port = FTP.makepasv(ftp)
        return (control_host, port)

    try:
        ftp.makepasv = _makepasv
    except Exception:
        pass


# --- Manager ---

class DockerVolumeBackupManager:
    """Encapsulates all logic for the backup manager."""

    def __init__(self):
        # --- Paths Configuration ---
        self.backup_root_dir = "/backups"
        self.config_file = os.path.join(self.backup_root_dir, "backup_config.json")
        self.ftp_status_file = os.path.join(self.backup_root_dir, "ftp_sync_status.json")
        self.log_dir = "/logs"
        self.log_file = None

        # --- Runtime State ---
        self.backup_schedules = {}
        self.logs = []
        self.tasks = {}
        self.scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
        self.active_backups = set()
        self.ftp_sync_status = {}
        self.lock = threading.Lock()

        # --- Default Settings ---
        self.ftp_settings = {
            "host": "", "port": 21, "username": "", "password": "", "passive": True,
            "secure": False, "mode": "explicit", "nat_pasv_fix": False,
            "remote_base_path": "", "sync_enabled": False, "sync_interval_minutes": 60,
        }
        self.notification_settings = {
            "base_url": os.environ.get("NTFY_BASE_URL", "https://ntfy.sh"),
            "topic": os.environ.get("NTFY_TOPIC", "docker-volume-backup"),
        }
        self.log_settings = {"max_lines": 200, "timezone": "system"}

        # --- Initialization ---
        os.makedirs(self.backup_root_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        self.load_config()
        self.load_ftp_status()
        startup_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = os.path.join(self.log_dir, f"backup_manager_{startup_time}.log")
        self.scheduler.start()
        atexit.register(lambda: self.scheduler.shutdown())
        self.setup_all_schedules()
        self.schedule_ftp_sync()
        self.log("Docker Volume Backup Manager initialized successfully.")

    # --- Logging and Notifications ---
    def log(self, message: str, volume_name: str = None, backup_filename: str = None):
        ts = datetime.now(timezone.utc).isoformat()
        context = ""
        if volume_name:
            context += f" volume={volume_name}"
        if backup_filename:
            context += f" file={backup_filename}"
        entry = f"[{ts}]{context} {message}"
        self.logs.append(entry)
        max_lines = self.log_settings.get("max_lines", 200)
        if len(self.logs) > max_lines:
            self.logs = self.logs[-max_lines:]
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as e:
            print(f"Failed to write to log file: {e}")
        print(entry)

    def get_backup_logs(self, backup_path: str):
        backup_filename = os.path.basename(backup_path)
        volume_name = os.path.basename(os.path.dirname(backup_path))
        relevant_logs = []
        log_files = sorted([os.path.join(self.log_dir, f) for f in os.listdir(self.log_dir) if f.endswith('.log')],
                           reverse=True)
        for log_file_path in log_files:
            try:
                with open(log_file_path, 'r', encoding="utf-8") as f:
                    for line in f:
                        if f"volume={volume_name}" in line or f"file={backup_filename}" in line:
                            relevant_logs.append(line.strip())
            except Exception as e:
                self.log(f"Error reading log file {log_file_path}: {e}")
        return relevant_logs

    def send_notification(self, message: str, title: str, priority: str = "default", volume_name: str = None):
        if volume_name:
            sched = self.backup_schedules.get(volume_name)
            if sched and not sched.get('notifications_enabled', True):
                self.log(f"Notifications disabled for {volume_name}; skipping.", volume_name=volume_name)
                return
        base = (self.notification_settings.get("base_url") or "").rstrip("/")
        topic = self.notification_settings.get("topic") or ""
        if not base or not topic:
            return
        try:
            url = f"{base}/{topic}"
            requests.post(url, data=message.encode("utf-8"), headers={"Title": title, "Priority": priority}, timeout=10)
            self.log(f"Notification sent to {url}: {title}")
        except requests.RequestException as e:
            self.log(f"Notification error: {e}")

    # --- Task Management ---
    def _run_task(self, func, *args):
        task_id = str(uuid.uuid4())
        self.tasks[task_id] = {"status": "running", "message": "Task has started."}

        def task_wrapper():
            try:
                success, message = func(*args)
                self.tasks[task_id] = {"status": "success" if success else "failure", "message": message}
            except Exception as e:
                self.log(f"Task {func.__name__} failed with exception: {e}")
                self.tasks[task_id] = {"status": "failure", "message": str(e)}

        threading.Thread(target=task_wrapper).start()
        return task_id

    def get_task_status(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task and task["status"] in ["success", "failure"]:
            return self.tasks.pop(task_id)
        return task or {"status": "not_found", "message": "Task not found."}

    # --- Docker Operations ---
    def run_command(self, command: list, timeout: int = 60):
        try:
            return subprocess.run(command, capture_output=True, text=True, check=True, timeout=timeout)
        except FileNotFoundError:
            self.log(f"Command not found: {command}")
        except subprocess.CalledProcessError as e:
            self.log(f"Command error ({' '.join(command)}): {e.stderr.strip() or e.stdout.strip()}")
        except subprocess.TimeoutExpired as e:
            self.log(f"Command timeout ({' '.join(command)}): {e}")
        except Exception as e:
            self.log(f"Command exception: {e}")
        return None

    def get_docker_volumes(self) -> list:
        res = self.run_command(['docker', 'volume', 'ls', '--format', '{{.Name}}'])
        return res.stdout.strip().split('\n') if res and res.returncode == 0 and res.stdout.strip() else []

    def get_container_using_volume(self, volume_name: str) -> tuple:
        cmd = ['docker', 'ps', '-a', '--filter', f'volume={volume_name}', '--format', '{{.ID}}:{{.Names}}']
        res = self.run_command(cmd)
        if res and res.returncode == 0 and res.stdout.strip():
            first_line = res.stdout.strip().split('\n')[0]
            if ':' in first_line:
                cid, cname = first_line.split(':', 1)
                return cid, cname
        return None, None

    def get_container_status(self, container_id: str) -> str:
        res = self.run_command(['docker', 'inspect', '--format', '{{.State.Status}}', container_id])
        return res.stdout.strip() if res and res.returncode == 0 else "unknown"

    def stop_container(self, container_id: str) -> bool:
        self.log(f"Stopping container {container_id[:12]}...")
        res = self.run_command(['docker', 'stop', container_id], timeout=30)
        ok = bool(res and res.returncode == 0)
        self.log("Container stopped." if ok else f"Failed to stop {container_id[:12]}.")
        return ok

    def start_container(self, container_id: str) -> bool:
        self.log(f"Starting container {container_id[:12]}...")
        res = self.run_command(['docker', 'start', container_id], timeout=60)
        if res and res.returncode == 0:
            time.sleep(3)
            st = self.get_container_status(container_id)
            if 'running' in st.lower():
                self.log(f"Container {container_id[:12]} running.")
                return True
            self.log(f"Container {container_id[:12]} status after start: {st}")
        else:
            self.log(f"CRITICAL: Could not restart {container_id[:12]}")
        return False

    # --- Backup and Restore ---
    def create_backup(self, volume_name: str, upload_to_ftp: bool = None) -> tuple[bool, str]:
        with self.lock:
            if volume_name in self.active_backups:
                return False, f"Backup for {volume_name} is already in progress."
            self.active_backups.add(volume_name)

        try:
            self.log(f"Backup start: {volume_name}", volume_name=volume_name)
            self.send_notification(f"Backup started: {volume_name}", "Backup", volume_name=volume_name)
            cid, cname = self.get_container_using_volume(volume_name)
            was_running = False
            if cid and self.get_container_status(cid) == 'running':
                self.log(f"Stopping '{cname}' ({cid[:12]}) for backup.", volume_name=volume_name)
                was_running = True
                if not self.stop_container(cid):
                    msg = f"Backup aborted; cannot stop container for {volume_name}."
                    self.log(msg, volume_name=volume_name)
                    self.send_notification(msg, "Backup", priority="urgent", volume_name=volume_name)
                    return False, msg
                time.sleep(2)

            vdir = os.path.join(self.backup_root_dir, volume_name)
            os.makedirs(vdir, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            fname = f"{ts}.tar.gz"
            target_path = os.path.join(vdir, fname)

            host_root = os.environ.get('BACKUP_HOST_DIR', self.backup_root_dir)
            host_vol_dir = os.path.join(host_root, volume_name)
            cmd = [
                'docker', 'run', '--rm',
                '-v', f'{volume_name}:/volume_data:ro',
                '-v', f'{host_vol_dir}:/backup_target',
                'alpine', 'tar', '-czf', f'/backup_target/{fname}', '-C', '/volume_data', '.'
            ]
            self.log(f"Running backup command for {volume_name}", volume_name=volume_name, backup_filename=fname)
            res = self.run_command(cmd, timeout=300)

            if was_running:
                self.start_container(cid)

            if not (res and res.returncode == 0 and os.path.isfile(target_path)):
                msg = f"Backup for {volume_name} failed. Check logs for details."
                self.log(msg, volume_name=volume_name, backup_filename=fname)
                self.send_notification(msg, "Backup", priority="urgent", volume_name=volume_name)
                return False, msg

            self.log(f"Backup for {volume_name} created successfully at {target_path}", volume_name=volume_name,
                     backup_filename=fname)
            self.send_notification(f"Backup completed: {volume_name}", "Backup", priority="high",
                                   volume_name=volume_name)

            sched = self.backup_schedules.get(volume_name, {})
            ftp_connected = bool(self.ftp_settings.get("host"))
            should_upload = ftp_connected and sched.get("ftp_upload_enabled",
                                                        True) if upload_to_ftp is None else upload_to_ftp

            if should_upload:
                self._upload_to_ftp(target_path, volume_name, fname)

            return True, f"Backup for {volume_name} completed successfully."

        finally:
            with self.lock:
                self.active_backups.discard(volume_name)

    def restore_backup(self, volume_name: str, backup_path: str) -> tuple[bool, str]:
        backup_filename = os.path.basename(backup_path)
        self.log(f"Restore start: {backup_path} -> {volume_name}", volume_name=volume_name,
                 backup_filename=backup_filename)
        self.send_notification(f"Restore started for {volume_name} from {backup_filename}", "Restore")
        if not os.path.isfile(backup_path):
            return self._finalize_restore(False, f"Restore failed: file not found at {backup_path}", volume_name,
                                          backup_filename)

        cid, _ = self.get_container_using_volume(volume_name)
        was_running = False
        if cid and self.get_container_status(cid) == 'running':
            was_running = True
            if not self.stop_container(cid):
                return self._finalize_restore(False, f"Restore aborted; cannot stop container for {volume_name}.",
                                              volume_name, backup_filename)
            time.sleep(2)

        self.log(f"Clearing volume: {volume_name}", volume_name=volume_name)
        clear_cmd = ['docker', 'run', '--rm', '-v', f'{volume_name}:/volume_data', 'alpine', 'sh', '-c',
                     'rm -rf /volume_data/* /volume_data/..?* /volume_data/.[!.]*']
        self.run_command(clear_cmd, timeout=120)

        bdir, bname = os.path.dirname(backup_path), os.path.basename(backup_path)
        host_root = os.environ.get('BACKUP_HOST_DIR', self.backup_root_dir)
        rel = os.path.relpath(bdir, self.backup_root_dir)
        host_bdir = os.path.join(host_root, rel)
        restore_cmd = ['docker', 'run', '--rm', '-v', f'{volume_name}:/volume_data', '-v',
                       f'{host_bdir}:/backup_source:ro', 'alpine', 'tar', '-xzf', f'/backup_source/{bname}', '-C',
                       '/volume_data']
        res = self.run_command(restore_cmd, timeout=300)
        if was_running:
            self.start_container(cid)

        success = res and res.returncode == 0
        msg = f"Restore to {volume_name} completed." if success else f"Restore to {volume_name} failed."
        return self._finalize_restore(success, msg, volume_name, backup_filename)

    def _finalize_restore(self, success, msg, volume, filename):
        priority = "high" if success else "urgent"
        self.log(msg, volume_name=volume, backup_filename=filename)
        self.send_notification(msg, "Restore", priority=priority)
        return success, msg

    def save_uploaded_backup(self, volume_name: str, file_storage) -> tuple:
        if not volume_name or not file_storage:
            return False, "Volume name and file are required."
        filename = secure_filename(file_storage.filename)
        if not filename.endswith('.tar.gz'):
            return False, "Invalid file type. Only .tar.gz is allowed."

        volume_dir = os.path.join(self.backup_root_dir, volume_name)
        os.makedirs(volume_dir, exist_ok=True)
        save_path = os.path.join(volume_dir, filename)
        if os.path.exists(save_path):
            return False, f"Backup file '{filename}' already exists."

        try:
            file_storage.save(save_path)
            self.log(f"Saved uploaded backup: {save_path}", volume_name=volume_name, backup_filename=filename)
            return True, "Upload successful."
        except Exception as e:
            self.log(f"Error saving uploaded file: {e}", volume_name=volume_name)
            return False, "Server error while saving file."

    # --- Backup Management ---
    def get_backups_structured(self) -> dict:
        data = {}
        if not os.path.exists(self.backup_root_dir):
            return data
        for vdir in sorted(os.listdir(self.backup_root_dir)):
            vpath = os.path.join(self.backup_root_dir, vdir)
            if not os.path.isdir(vpath):
                continue
            items = []
            for f in sorted(os.listdir(vpath), reverse=True):
                if not f.endswith('.tar.gz'):
                    continue
                fp = os.path.join(vpath, f)
                try:
                    st = os.stat(fp)
                    ts = datetime.strptime(f.replace('.tar.gz', ''), "%Y-%m-%d_%H-%M-%S")
                    items.append({
                        'filename': f,
                        'size': st.st_size,
                        'path': fp,
                        'timestamp': ts.replace(tzinfo=timezone.utc).isoformat(),
                        'ftp_status': self.ftp_sync_status.get(fp, 'unknown')
                    })
                except (OSError, ValueError):
                    continue
            if items:
                data[vdir] = items
        return data

    def delete_backup(self, backup_path: str) -> bool:
        try:
            if not os.path.abspath(backup_path).startswith(os.path.abspath(self.backup_root_dir)):
                self.log(f"SECURITY: Refused deletion outside backup dir: {backup_path}")
                return False
            volume_name = os.path.basename(os.path.dirname(backup_path))
            filename = os.path.basename(backup_path)
            os.remove(backup_path)
            if backup_path in self.ftp_sync_status:
                del self.ftp_sync_status[backup_path]
                self.save_ftp_status()
            self.log(f"Deleted backup: {filename}", volume_name=volume_name, backup_filename=filename)
            return True
        except OSError as e:
            self.log(f"Delete error: {e}")
            return False

    def prune_backups_by_seconds(self, volume_name: str, keep_seconds: int) -> dict:
        if keep_seconds <= 0:
            self.log("Prune skipped: keep_seconds must be > 0", volume_name=volume_name)
            return {'deleted_count': 0}
        self.log(f"Prune start for {volume_name}, keeping backups from last {keep_seconds}s", volume_name=volume_name)
        cnt = 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=keep_seconds)
        for b in self.get_backups_structured().get(volume_name, []):
            if datetime.fromisoformat(b['timestamp']) < cutoff and self.delete_backup(b['path']):
                cnt += 1
        self.log(f"Prune done; deleted {cnt} backups for '{volume_name}'", volume_name=volume_name)
        return {'deleted_count': cnt}

    # --- Scheduling ---
    def add_or_update_schedule(self, volume_name: str, **kwargs) -> bool:
        is_update = volume_name in self.backup_schedules
        current_sched = self.backup_schedules.get(volume_name, {})
        new_sched = {
            'interval_days': int(kwargs.get('interval_days', current_sched.get('interval_days', 0))),
            'interval_hours': int(kwargs.get('interval_hours', current_sched.get('interval_hours', 0))),
            'interval_minutes': int(kwargs.get('interval_minutes', current_sched.get('interval_minutes', 0))),
            'interval_seconds': int(kwargs.get('interval_seconds', current_sched.get('interval_seconds', 0))),
            'prune_keep_days': int(kwargs.get('prune_keep_days', current_sched.get('prune_keep_days', 0))),
            'prune_keep_hours': int(kwargs.get('prune_keep_hours', current_sched.get('prune_keep_hours', 0))),
            'prune_keep_minutes': int(kwargs.get('prune_keep_minutes', current_sched.get('prune_keep_minutes', 0))),
            'notifications_enabled': bool(
                kwargs.get('notifications_enabled', current_sched.get('notifications_enabled', True))),
            'ftp_upload_enabled': bool(kwargs.get('ftp_upload_enabled', current_sched.get('ftp_upload_enabled', True))),
            'enabled': bool(kwargs.get('enabled', current_sched.get('enabled', True)))
        }
        self.backup_schedules[volume_name] = new_sched
        trig = IntervalTrigger(
            days=new_sched['interval_days'], hours=new_sched['interval_hours'],
            minutes=new_sched['interval_minutes'], seconds=new_sched['interval_seconds']
        )
        job_id = f"backup_{volume_name}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        self.scheduler.add_job(func=self.scheduled_backup, args=[volume_name], trigger=trig, id=job_id,
                               replace_existing=True)
        if not new_sched['enabled']:
            self.scheduler.pause_job(job_id)
        self.save_config()
        action = "updated" if is_update else "saved"
        self.log(f"Schedule {action}: '{volume_name}' every {self._interval_str(new_sched)}")
        return True

    def remove_schedule(self, volume_name: str) -> bool:
        if volume_name in self.backup_schedules:
            job_id = f"backup_{volume_name}"
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            del self.backup_schedules[volume_name]
            self.save_config()
            self.log(f"Schedule removed: '{volume_name}'")
            return True
        return False

    def toggle_schedule(self, volume_name: str, enabled: bool) -> bool:
        if volume_name in self.backup_schedules:
            self.backup_schedules[volume_name]['enabled'] = enabled
            job = self.scheduler.get_job(f"backup_{volume_name}")
            if job:
                if enabled:
                    job.resume()
                else:
                    job.pause()
                self.log(f"Schedule {'resumed' if enabled else 'paused'}: '{volume_name}'")
            elif enabled:
                self.add_or_update_schedule(volume_name, **self.backup_schedules[volume_name])
            self.save_config()
            return True
        return False

    def setup_all_schedules(self):
        for volume_name, info in self.backup_schedules.items():
            self.add_or_update_schedule(volume_name, **info)

    def scheduled_backup(self, volume_name: str):
        self.log(f"Scheduled backup triggered for: {volume_name}", volume_name=volume_name)
        sched = self.backup_schedules.get(volume_name, {})
        upload_flag = sched.get('ftp_upload_enabled', True)
        success, _ = self.create_backup(volume_name, upload_to_ftp=upload_flag)
        if success:
            keep_seconds = self._compute_keep_seconds_from_info(sched)
            if keep_seconds > 0:
                self.log(f"Scheduled prune: '{volume_name}', keep={keep_seconds}s", volume_name=volume_name)
                self.prune_backups_by_seconds(volume_name, keep_seconds)

    def get_schedule_status(self) -> list:
        status = []
        for volume, cfg in self.backup_schedules.items():
            job = self.scheduler.get_job(f"backup_{volume}")
            status.append({
                'volume': volume,
                **cfg,
                'interval_str': self._interval_str(cfg),
                'next_run': job.next_run_time.isoformat() if job and job.next_run_time else "N/A",
            })
        return status

    # --- FTP Operations (explicit/implicit + NAT PASV fix + health) ---
    def _connect_ftp(self):
        host = (self.ftp_settings.get("host") or "").strip()
        if not host:
            raise RuntimeError("FTP host is not configured.")
        port = int(self.ftp_settings.get("port") or 21)
        user = self.ftp_settings.get("username", "")
        pwd = self.ftp_settings.get("password", "")
        secure = bool(self.ftp_settings.get("secure", False))
        mode = (self.ftp_settings.get("mode") or "explicit").lower()
        passive = bool(self.ftp_settings.get("passive", True))
        nat_fix = bool(self.ftp_settings.get("nat_pasv_fix", False))

        if secure and mode == "implicit":
            ftp = ImplicitFTP_TLS(timeout=30)
            ftp.connect(host, port)
            ftp.login(user=user, passwd=pwd)
            ftp.prot_p()
        elif secure:  # explicit
            ftp = FTP_TLS(timeout=30)
            ftp.connect(host, port)
            ftp.login(user=user, passwd=pwd)
            ftp.prot_p()
        else:
            ftp = FTP(timeout=30)
            ftp.connect(host, port)
            ftp.login(user=user, passwd=pwd)

        ftp.set_pasv(passive)
        if nat_fix and passive:
            apply_nat_pasv_fix(ftp, host)
        return ftp

    def _upload_to_ftp(self, local_path: str, volume_name: str, filename: str):
        try:
            with self._connect_ftp() as ftp:
                base = self._normalized_remote_base()
                rdir = f"{base}/{volume_name}"
                self._ensure_remote_dirs(ftp, rdir)
                with open(local_path, "rb") as f:
                    ftp.storbinary(f"STOR {rdir}/{filename}", f)
                self.ftp_sync_status[local_path] = 'synced'
                self.log(f"Uploaded to FTP: {rdir}/{filename}", volume_name=volume_name, backup_filename=filename)
        except Exception as e:
            self.ftp_sync_status[local_path] = 'failed'
            self.log(f"FTP upload failed for {local_path}: {e}", volume_name=volume_name, backup_filename=filename)
        finally:
            self.save_ftp_status()

    def test_ftp_connection(self) -> tuple:
        try:
            with self._connect_ftp() as ftp:
                cur = ftp.pwd()
            return True, f"FTP connection OK (current directory: {cur})"
        except Exception as e:
            return False, f"FTP test failed: {e}"

    def ftp_health(self) -> dict:
        ok, msg = self.test_ftp_connection()
        return {"reachable": ok, "message": msg, "mode": self.ftp_settings.get("mode", "explicit")}

    def sync_to_ftp(self) -> tuple[bool, str]:
        self.send_notification("FTP sync started", "FTP Sync")
        self.log("FTP sync started")
        try:
            with self._connect_ftp() as ftp:
                base = self._normalized_remote_base()
                self._ensure_remote_dirs(ftp, base)
                all_local_backups = self.get_backups_structured()
                synced_count = 0
                failed_count = 0
                for volume, backups in all_local_backups.items():
                    for backup in backups:
                        if self.ftp_sync_status.get(backup['path']) != 'synced':
                            try:
                                remote_dir = f"{base}/{volume}"
                                remote_path = f"{remote_dir}/{backup['filename']}"
                                self._ensure_remote_dirs(ftp, remote_dir)
                                with open(backup['path'], "rb") as f:
                                    ftp.storbinary(f"STOR {remote_path}", f)
                                self.ftp_sync_status[backup['path']] = 'synced'
                                self.log(f"Synced to FTP: {remote_path}", volume_name=volume,
                                         backup_filename=backup['filename'])
                                synced_count += 1
                            except Exception as e:
                                self.ftp_sync_status[backup['path']] = 'failed'
                                self.log(f"FTP sync failed for {backup['path']}: {e}", volume_name=volume,
                                         backup_filename=backup['filename'])
                                failed_count += 1
                self.save_ftp_status()
            msg = f"FTP sync completed. Synced: {synced_count}, Failed: {failed_count}."
            self.log(msg)
            self.send_notification(msg, "FTP Sync", priority="high")
            return True, msg
        except Exception as e:
            msg = f"FTP sync failed: {e}"
            self.log(msg)
            self.send_notification(msg, "FTP Sync", priority="urgent")
            return False, msg

    def schedule_ftp_sync(self):
        job_id = "ftp_sync"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        enabled = bool(self.ftp_settings.get("sync_enabled", False))
        minutes = int(self.ftp_settings.get("sync_interval_minutes") or 0)
        if enabled and minutes > 0:
            self.scheduler.add_job(func=self.sync_to_ftp, trigger=IntervalTrigger(minutes=minutes), id=job_id)
            self.log(f"Scheduled FTP sync every {minutes} minutes.")
        else:
            self.log("FTP sync schedule disabled.")

    def get_sync_status(self) -> dict:
        job = self.scheduler.get_job("ftp_sync")
        return {
            "enabled": self.ftp_settings.get("sync_enabled", False),
            "interval_minutes": self.ftp_settings.get("sync_interval_minutes", 0),
            "next_run": job.next_run_time.isoformat() if job and job.next_run_time else "N/A"
        }

    # --- Config and State Persistence ---
    def load_config(self):
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file, 'r', encoding="utf-8") as f:
                data = json.load(f)
            self.backup_schedules = data.get('schedules', {})
            self.ftp_settings.update(data.get('ftp_settings', {}))
            self.notification_settings.update(data.get('notification_settings', {}))
            self.log_settings.update(data.get('log_settings', {}))
        except (json.JSONDecodeError, OSError) as e:
            self.log(f"Config load error: {e}")

    def save_config(self):
        try:
            with open(self.config_file, 'w', encoding="utf-8") as f:
                json.dump({
                    'schedules': self.backup_schedules,
                    'ftp_settings': self.ftp_settings,
                    'notification_settings': self.notification_settings,
                    'log_settings': self.log_settings
                }, f, indent=2)
        except OSError as e:
            self.log(f"Config save error: {e}")

    def load_ftp_status(self):
        if not os.path.exists(self.ftp_status_file):
            return
        try:
            with open(self.ftp_status_file, 'r', encoding="utf-8") as f:
                self.ftp_sync_status = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            self.log(f"FTP status load error: {e}")

    def save_ftp_status(self):
        try:
            with open(self.ftp_status_file, 'w', encoding="utf-8") as f:
                json.dump(self.ftp_sync_status, f, indent=2)
        except OSError as e:
            self.log(f"FTP status save error: {e}")

    # --- Helper & Utility Methods ---
    def _compute_keep_seconds_from_info(self, info: dict) -> int:
        d = int(info.get('prune_keep_days', 0))
        h = int(info.get('prune_keep_hours', 0))
        m = int(info.get('prune_keep_minutes', 0))
        return d * 86400 + h * 3600 + m * 60

    def _interval_str(self, cfg: dict) -> str:
        parts = []
        if cfg.get('interval_days'): parts.append(f"{cfg['interval_days']}d")
        if cfg.get('interval_hours'): parts.append(f"{cfg['interval_hours']}h")
        if cfg.get('interval_minutes'): parts.append(f"{cfg['interval_minutes']}m")
        if cfg.get('interval_seconds'): parts.append(f"{cfg['interval_seconds']}s")
        return " ".join(parts) if parts else "N/A"

    def predict_storage_usage(self) -> dict:
        all_b = self.get_backups_structured()
        current = sum(b['size'] for items in all_b.values() for b in items)
        avg_sizes = {vol: (sum(b['size'] for b in items) / len(items)) if items else 10 * 1024 * 1024 for vol, items in
                     all_b.items()}
        predicted_add = 0
        horizon = datetime.now(timezone.utc) + timedelta(days=30)
        for vol, sched in self.backup_schedules.items():
            if not sched.get('enabled', True):
                continue
            interval_sec = (
                    sched.get('interval_days', 0) * 86400 +
                    sched.get('interval_hours', 0) * 3600 +
                    sched.get('interval_minutes', 0) * 60 +
                    sched.get('interval_seconds', 0)
            )
            if interval_sec <= 0:
                continue
            job = self.scheduler.get_job(f"backup_{vol}")
            if not (job and job.next_run_time):
                continue
            cur = job.next_run_time
            while cur < horizon:
                predicted_add += avg_sizes.get(vol, 10 * 1024 * 1024)
                cur += timedelta(seconds=interval_sec)
        return {'current_size_bytes': current, 'predicted_size_bytes_30d': current + predicted_add}

    def get_next_scheduled_overall(self) -> str:
        next_times = [job.next_run_time for job in self.scheduler.get_jobs() if
                      job.next_run_time and job.id.startswith('backup_')]
        return min(next_times).isoformat() if next_times else "N/A"

    def _ensure_remote_dirs(self, ftp, remote_path: str):
        parts = [p for p in remote_path.replace("\\\\", "/").split("/") if p]
        for i in range(len(parts)):
            path_to_check = "/" + "/".join(parts[:i + 1])
            try:
                ftp.cwd(path_to_check)
            except Exception:
                ftp.mkd(path_to_check)

    def _normalized_remote_base(self) -> str:
        base = self.ftp_settings.get("remote_base_path", "").strip().replace("\\\\", "/")
        if not base or base == "/":
            raise RuntimeError("Unsafe remote base path; must be a non-root path.")
        return base if base.startswith("/") else "/" + base


# --- Flask App Initialization ---
manager = DockerVolumeBackupManager()


# --- API Routes ---
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/state')
def api_state():
    volumes = [{'name': v, 'container': manager.get_container_using_volume(v)} for v in manager.get_docker_volumes()]
    backups = manager.get_backups_structured()
    for items in backups.values():
        for b in items:
            b['size_formatted'] = f"{b['size'] / (1024 * 1024):.2f} MB" if b[
                                                                               'size'] > 1024 * 1024 else f"{b['size'] / 1024:.1f} KB"
            b['download_url'] = f"/api/backups/download?path={b['path']}"
    ftp_public = {k: v for k, v in manager.ftp_settings.items() if k != 'password'}
    ftp_public['password_set'] = bool(manager.ftp_settings.get('password'))
    return jsonify({
        'volumes': volumes,
        'backups_structured': backups,
        'schedules': manager.get_schedule_status(),
        'sync_status': manager.get_sync_status(),
        'ftp_settings': ftp_public,
        'notification_settings': manager.notification_settings,
        'log_settings': manager.log_settings,
        'logs': manager.logs,
        'overview': {
            'backed_up_volumes_count': len(backups),
            'total_volumes_count': len(volumes),
            'next_scheduled_backup': manager.get_next_scheduled_overall(),
            'currently_running_backups': list(manager.active_backups)
        },
        'ftp_health': manager.ftp_health()
    })


@app.route('/api/backup', methods=['POST'])
def api_backup():
    data = request.json
    vol = data.get('volume')
    if not vol:
        return jsonify({'error': 'Volume name required'}), 400
    task_id = manager._run_task(manager.create_backup, vol, data.get('upload_to_ftp'))
    return jsonify({'message': f'Backup started for {vol}', 'task_id': task_id})


@app.route('/api/restore', methods=['POST'])
def api_restore():
    data = request.json
    if not all(k in data for k in ['volume', 'backup_path']):
        return jsonify({'error': 'Volume and backup_path required'}), 400
    task_id = manager._run_task(manager.restore_backup, data['volume'], data['backup_path'])
    return jsonify({'message': f"Restore started for {data['volume']}", 'task_id': task_id})


@app.route('/api/task_status/<task_id>')
def api_task_status(task_id):
    return jsonify(manager.get_task_status(task_id))


@app.route('/api/backups/logs')
def api_backup_logs():
    path = request.args.get('path')
    if not path:
        return jsonify({'error': 'path parameter is required'}), 400
    return jsonify(manager.get_backup_logs(path))


@app.route('/api/backups/download')
def api_download_backup():
    p = request.args.get('path', '')
    if not p:
        return jsonify({'error': 'path required'}), 400
    abs_p = os.path.abspath(p)
    if not abs_p.startswith(os.path.abspath(manager.backup_root_dir)) or not os.path.isfile(abs_p):
        return jsonify({'error': 'invalid or non-existent path'}), 400
    return send_file(abs_p, as_attachment=True)


@app.route('/api/upload_backup', methods=['POST'])
def api_upload_backup():
    success, message = manager.save_uploaded_backup(request.form.get('volume'), request.files.get('backup_file'))
    return jsonify({'message' if success else 'error': message}), 200 if success else 400


@app.route('/api/backups/<path:backup_path>', methods=['DELETE'])
def api_delete_backup(backup_path):
    full_path = f"/{backup_path}"
    if manager.delete_backup(full_path):
        return jsonify({'message': 'Backup deleted'})
    return jsonify({'error': 'Failed to delete backup'}), 500


@app.route('/api/prune_extended', methods=['POST'])
def api_prune_extended():
    data = request.json
    vol = data.get('volume')
    if not vol:
        return jsonify({'error': 'Volume is required'}), 400
    try:
        days, hours, minutes = int(data.get('days', 0)), int(data.get('hours', 0)), int(data.get('minutes', 0))
        keep_seconds = days * 86400 + hours * 3600 + minutes * 60
        if keep_seconds <= 0:
            return jsonify({'error': 'Retention must be greater than 0'}), 400
        res = manager.prune_backups_by_seconds(vol, keep_seconds)
        manager.send_notification(f"Pruned {res.get('deleted_count', 0)} backups on {vol}", "Prune")
        return jsonify(res)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid retention values'}), 400


@app.route('/api/sync_now', methods=['POST'])
def api_sync_now():
    task_id = manager._run_task(manager.sync_to_ftp)
    return jsonify({'message': 'FTP sync started.', 'task_id': task_id})


@app.route('/api/schedules', methods=['POST'])
def api_add_schedule():
    data = request.json
    vol = data.get('volume')
    if not vol:
        return jsonify({'error': 'Volume required'}), 400
    try:
        manager.add_or_update_schedule(vol, **data)
        return jsonify({'message': 'Schedule added'})
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid input values'}), 400


@app.route('/api/schedules/<volume>', methods=['PUT'])
def api_update_schedule(volume):
    if volume not in manager.backup_schedules:
        return jsonify({'error': 'Schedule not found'}), 404
    try:
        manager.add_or_update_schedule(volume, **request.json)
        return jsonify({'message': 'Schedule updated'})
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid values'}), 400


@app.route('/api/schedules/<volume>', methods=['DELETE'])
def api_remove_schedule(volume):
    if manager.remove_schedule(volume):
        return jsonify({'message': 'Schedule removed'})
    return jsonify({'error': 'Schedule not found'}), 404


@app.route('/api/schedules/toggle', methods=['POST'])
def api_toggle_schedule():
    data = request.json
    if 'volume' not in data or 'enabled' not in data:
        return jsonify({'error': 'Volume and enabled status required'}), 400
    if manager.toggle_schedule(data['volume'], bool(data['enabled'])):
        return jsonify({'message': f"Schedule for {data['volume']} updated"})
    return jsonify({'error': 'Schedule not found'}), 404


@app.route('/api/storage_prediction')
def api_storage_prediction():
    return jsonify(manager.predict_storage_usage())


@app.route('/api/logs/download')
def api_logs_download():
    return Response("\n".join(manager.logs), mimetype='text/plain',
                    headers={"Content-Disposition": "attachment; filename=backup_manager_logs.txt"})


@app.route('/api/settings/ftp', methods=['POST'])
def api_set_ftp_settings():
    data = request.json
    # Accept new keys as well (mode, nat_pasv_fix)
    for key in ["host", "port", "username", "password", "passive", "secure", "remote_base_path", "sync_enabled",
                "sync_interval_minutes", "mode", "nat_pasv_fix"]:
        if key in data:
            manager.ftp_settings[key] = data[key]
    manager.save_config()
    manager.schedule_ftp_sync()
    return jsonify({'message': 'FTP settings saved'})


@app.route('/api/settings/ftp/test', methods=['POST'])
def api_test_ftp_settings():
    # Temporarily apply provided settings for test
    old = dict(manager.ftp_settings)
    try:
        data = request.json or {}
        temp = dict(old)
        temp.update({k: data.get(k, temp.get(k)) for k in
                     ["host", "port", "username", "password", "passive", "secure", "remote_base_path", "mode",
                      "nat_pasv_fix"]})
        manager.ftp_settings = temp
        ok, msg = manager.test_ftp_connection()
        return jsonify({'ok': ok, 'message': msg})
    finally:
        manager.ftp_settings = old


@app.route('/api/settings/notifications', methods=['POST'])
def api_set_notifications():
    data = request.json
    if not (data.get('base_url') and data.get('topic')):
        return jsonify({'error': 'base_url and topic required'}), 400
    manager.notification_settings.update(data)
    manager.save_config()
    return jsonify({'message': 'Notification settings saved'})


@app.route('/api/settings/logs', methods=['POST'])
def api_set_log_settings():
    try:
        max_lines = int(request.json.get('max_lines'))
        if not 50 <= max_lines <= 5000:
            return jsonify({'error': 'Max lines must be between 50 and 5000'}), 400
        manager.log_settings['max_lines'] = max_lines
        if 'timezone' in request.json:
            manager.log_settings['timezone'] = request.json['timezone']
        manager.save_config()
        return jsonify({'message': 'Log settings saved'})
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid max_lines value'}), 400


if __name__ == '__main__':
    print("Starting Docker Volume Backup Manager")
    print("Open your browser and go to: http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000)
