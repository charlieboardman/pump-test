# Aquavar CPC dashboard

One Python process that samples the VFD at 1 Hz and serves a zoomable uPlot
dashboard.  History is stored in SQLite on tmpfs (`/dev/shm/pump.db`), so the
Pi's SD card is never written to.  History is therefore volatile across reboots.

## How it fits together

```
pump_dashboard.py
├─ sampler thread  → read_pump() @ 1 Hz → INSERT into /dev/shm/pump.db
└─ main thread     → ThreadingHTTPServer on 0.0.0.0:8080
                     ├─ /                 static/index.html
                     ├─ /static/uplot.*   vendored uPlot 1.6.31
                     ├─ /api/meta         field list + units + defaults
                     ├─ /api/latest       newest sample, recent read errors
                     ├─ /api/series       server-side bucketed history
                     └─ /api/errors       read failures
```

- Measurements are stored **decoded** (named REAL columns).  Status/fault raw
  words are kept alongside them, plus decoded active fault/alarm names.
- Retention is **7 days** (about 100 MB / 605k rows), trimmed once a minute.
- Failed reads are written to `read_errors` and skipped; the 1 Hz grid continues.
- The sampler uses `timeout=0.25, retries=0`, so even a failed read fits inside
  its one-second slot.

## Run manually

```sh
python3 -B pump_dashboard.py
# then open http://<host>:8080/
```

Environment overrides: `PUMP_DB`, `PUMP_HOST`, `PUMP_PORT`,
`PUMP_RETENTION_DAYS`, `PUMP_TIMEOUT`, `PUMP_RETRIES`.

## Install as a service

```sh
sudo cp pump-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pump-dashboard
systemctl status pump-dashboard
```

The unit runs as `pmops:dialout`, sets `PYTHONDONTWRITEBYTECODE=1`, disables
core dumps, gives the service a private `/tmp`, and writes logs to
`/dev/shm/pump-dashboard.log` (RAM) rather than the SD card.

## Web UI

- Range buttons: 1h / 6h / 24h / 48h / 72h / 7d.
- Drag to zoom; the visible window is re-fetched with a bucket size matched to
  the zoom (`max(1 s, window / 1500)`, rounded up to a readable interval).
- Double-click or **Reset zoom** returns to the selected range.
- Check any combination of measurements; each unit gets its own y-axis.
- Tiles and the status/fault banner poll `/api/latest` every 2 s.

## Storage notes

```sql
PRAGMA journal_mode=WAL;      -- readers never block the writer
PRAGMA synchronous=OFF;       -- RAM only, durability is irrelevant
PRAGMA temp_store=MEMORY;     -- never spill to /tmp
```

The writer checkpoints the WAL every minute.  Because everything lives in
`/dev/shm`, the database, WAL, shared-memory file and logs are all RAM-backed.
