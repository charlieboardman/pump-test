#!/usr/bin/env python3
"""Aquavar CPC dashboard: 1 Hz sampler + uPlot web UI in one process.

The SQLite database lives in tmpfs (``/dev/shm/pump.db`` by default) so the Pi
never writes measurement data to its SD card.  A sampler thread calls
``read_pump()`` once per second and inserts decoded values; the main HTTP server
serves the dashboard and a small JSON API from the same database.  History is
volatile across reboots by design.

Run manually:
    python3 -B pump_dashboard.py
"""

from __future__ import annotations

import json
import math
import os
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import read_pump

DB_PATH = os.environ.get("PUMP_DB", "/dev/shm/pump.db")
HOST = os.environ.get("PUMP_HOST", "0.0.0.0")
PORT = int(os.environ.get("PUMP_PORT", "8080"))
RETENTION_DAYS = float(os.environ.get("PUMP_RETENTION_DAYS", "7"))
RETENTION_MS = int(RETENTION_DAYS * 86_400_000)

SAMPLE_TIMEOUT = float(os.environ.get("PUMP_TIMEOUT", "0.25"))
SAMPLE_RETRIES = int(os.environ.get("PUMP_RETRIES", "0"))
MAINTENANCE_S = 60
MAX_BUCKETS = 1500
MAX_ERROR_ROWS = 20000
SCHEMA_VERSION = 1

# Bucket widths that produce readable axis ticks.
NICE_BUCKETS_S = (
    1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400,
)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# key == database column for every measurement.  ``unit == "process"`` means the
# unit comes from the drive's process-unit register (usually psi) and is filled
# in from the latest sample.
FIELDS = [
    {"key": "pressure", "label": "Pressure", "unit": "process", "color": "#3b82f6", "group": "Pressure"},
    {"key": "setpoint", "label": "Setpoint", "unit": "process", "color": "#f59e0b", "group": "Pressure"},
    {"key": "active_setpoint", "label": "Active setpoint", "unit": "process", "color": "#fbbf24", "group": "Pressure"},
    {"key": "setpoint_1", "label": "Setpoint 1", "unit": "process", "color": "#f97316", "group": "Pressure"},
    {"key": "setpoint_2", "label": "Setpoint 2", "unit": "process", "color": "#fb7185", "group": "Pressure"},
    {"key": "motor_freq_hz", "label": "Motor frequency", "unit": "Hz", "color": "#22c55e", "group": "Motor"},
    {"key": "pump_freq_hz", "label": "Pump frequency", "unit": "Hz", "color": "#14b8a6", "group": "Motor"},
    {"key": "motor_current_a", "label": "Motor current", "unit": "A", "color": "#ef4444", "group": "Motor"},
    {"key": "motor_voltage_v", "label": "Motor voltage", "unit": "V", "color": "#a855f7", "group": "Motor"},
    {"key": "motor_power_kw", "label": "Motor power", "unit": "kW", "color": "#ec4899", "group": "Motor"},
    {"key": "system_power_hp", "label": "System power", "unit": "hp", "color": "#f43f5e", "group": "Motor"},
    {"key": "dc_bus_v", "label": "DC bus voltage", "unit": "V", "color": "#eab308", "group": "Drive"},
    {"key": "drive_temp_c", "label": "Drive temperature", "unit": "\u00b0C", "color": "#f97316", "group": "Drive"},
    {"key": "ai1_pct", "label": "Analog input 1", "unit": "%", "color": "#06b6d4", "group": "Analog I/O"},
    {"key": "transducer_pct", "label": "Transducer feedback", "unit": "%", "color": "#0ea5e9", "group": "Analog I/O"},
    {"key": "ao1_ma", "label": "Analog output 1", "unit": "mA", "color": "#8b5cf6", "group": "Analog I/O"},
    {"key": "ao2_ma", "label": "Analog output 2", "unit": "mA", "color": "#d946ef", "group": "Analog I/O"},
    {"key": "energy_kwh", "label": "Energy", "unit": "kWh", "color": "#84cc16", "group": "Energy & runtime"},
    {"key": "powered_h", "label": "VFD powered time", "unit": "h", "color": "#64748b", "group": "Energy & runtime"},
    {"key": "run_h", "label": "Motor run time", "unit": "h", "color": "#94a3b8", "group": "Energy & runtime"},
    {"key": "savings_usd", "label": "Energy savings", "unit": "USD", "color": "#10b981", "group": "Energy & runtime"},
]
FIELD_KEYS = {field["key"] for field in FIELDS}
DEFAULT_FIELDS = ["pressure", "setpoint", "motor_freq_hz", "motor_current_a"]

SAMPLE_COLUMNS = [
    "ts", "dur_ms",
    "pressure", "setpoint", "active_setpoint", "setpoint_1", "setpoint_2",
    "pump_freq_hz", "motor_freq_hz", "motor_current_a", "motor_power_kw",
    "system_power_hp", "motor_voltage_v", "dc_bus_v", "drive_temp_c",
    "ai1_pct", "transducer_pct", "ao1_ma", "ao2_ma",
    "powered_h", "run_h", "energy_kwh", "savings_usd",
    "process_unit", "pump_label", "status",
    "status_low", "status_high", "fault_last", "fault_last_name",
    "fault_w0", "fault_w1", "fault_w2", "alarm_w0", "alarm_w1",
    "di_word", "ro_word", "faults_active", "alarms_active",
]
INSERT_SQL = (
    "INSERT OR REPLACE INTO samples ("
    + ", ".join(SAMPLE_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in SAMPLE_COLUMNS)
    + ")"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER PRIMARY KEY,
  dur_ms INTEGER,
  pressure REAL,
  setpoint REAL,
  active_setpoint REAL,
  setpoint_1 REAL,
  setpoint_2 REAL,
  pump_freq_hz REAL,
  motor_freq_hz REAL,
  motor_current_a REAL,
  motor_power_kw REAL,
  system_power_hp REAL,
  motor_voltage_v REAL,
  dc_bus_v REAL,
  drive_temp_c REAL,
  ai1_pct REAL,
  transducer_pct REAL,
  ao1_ma REAL,
  ao2_ma REAL,
  powered_h REAL,
  run_h REAL,
  energy_kwh REAL,
  savings_usd REAL,
  process_unit TEXT,
  pump_label TEXT,
  status TEXT,
  status_low INTEGER,
  status_high INTEGER,
  fault_last INTEGER,
  fault_last_name TEXT,
  fault_w0 INTEGER,
  fault_w1 INTEGER,
  fault_w2 INTEGER,
  alarm_w0 INTEGER,
  alarm_w1 INTEGER,
  di_word INTEGER,
  ro_word INTEGER,
  faults_active TEXT,
  alarms_active TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS read_errors (
  ts INTEGER PRIMARY KEY,
  error TEXT,
  consecutive INTEGER
) WITHOUT ROWID;
"""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def connect_writer() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def connect_reader() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def init_db(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        # Volatile data: a schema change just starts a fresh buffer.
        con.execute("DROP TABLE IF EXISTS samples")
        con.execute("DROP TABLE IF EXISTS read_errors")
    con.executescript(SCHEMA_SQL)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()


def maintain(con: sqlite3.Connection) -> None:
    cutoff = int(time.time() * 1000) - RETENTION_MS
    con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM read_errors WHERE ts < ?", (cutoff,))
    # A multi-day outage at 1 Hz would otherwise let the error table grow to
    # hundreds of megabytes; keep the newest rows only.
    oldest_kept = con.execute(
        "SELECT ts FROM read_errors ORDER BY ts DESC LIMIT 1 OFFSET ?",
        (MAX_ERROR_ROWS - 1,),
    ).fetchone()
    if oldest_kept:
        con.execute("DELETE FROM read_errors WHERE ts < ?", (oldest_kept[0],))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def nice_bucket(ms: int) -> int:
    """Round a bucket width up to a readable interval."""
    seconds = max(1.0, ms / 1000)
    for candidate in NICE_BUCKETS_S:
        if candidate >= seconds:
            return candidate * 1000
    return NICE_BUCKETS_S[-1] * 1000


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------

def _value(sample: dict, key: str):
    return sample[key]["value"]


def _hex_int(text) -> int:
    try:
        return int(text, 16)
    except (TypeError, ValueError):
        return 0


def _bits_word(bits: dict) -> int:
    word = 0
    for name, on in bits.items():
        if on:
            word |= 1 << (int(name[2:]) - 1)
    return word


def build_row(sample: dict, ts_ms: int) -> tuple:
    status = sample["status"]
    faults = sample["faults"]
    alarms = sample["alarms"]
    fault_words = [_hex_int(word) for word in faults["raw_standard_words"]]
    alarm_words = [_hex_int(word) for word in alarms["raw_standard_words"]]
    fault_words += [0] * (3 - len(fault_words))
    alarm_words += [0] * (2 - len(alarm_words))
    last_fault = faults["last_recorded"]

    return (
        ts_ms,
        int(sample["sample_duration_ms"]),
        _value(sample, "pressure"),
        _value(sample, "setpoint"),
        _value(sample, "active_setpoint"),
        _value(sample, "setpoint_1"),
        _value(sample, "setpoint_2"),
        _value(sample, "pump_output_frequency"),
        _value(sample, "motor_frequency"),
        _value(sample, "motor_current"),
        _value(sample, "motor_power"),
        _value(sample, "overall_system_power"),
        _value(sample, "motor_voltage"),
        _value(sample, "dc_bus_voltage"),
        _value(sample, "drive_temperature"),
        _value(sample, "analog_input_1"),
        _value(sample, "transducer_feedback"),
        _value(sample, "analog_output_1"),
        _value(sample, "analog_output_2"),
        _value(sample, "vfd_powered_time"),
        _value(sample, "motor_run_time"),
        _value(sample, "energy"),
        _value(sample, "calculated_energy_savings"),
        sample["pressure"]["unit"],
        sample["pump"]["label"],
        status["summary"],
        _hex_int(status["raw_low_word"]),
        _hex_int(status["raw_high_word"]),
        last_fault["code"],
        last_fault["name"],
        fault_words[0],
        fault_words[1],
        fault_words[2],
        alarm_words[0],
        alarm_words[1],
        _bits_word(sample["digital_inputs"]),
        _bits_word(sample["relay_outputs"]),
        json.dumps([item["name"] for item in faults["active"]]),
        json.dumps([item["name"] for item in alarms["active"]]),
    )


def sampler(stop: threading.Event, ready: threading.Event) -> None:
    con = connect_writer()
    init_db(con)
    ready.set()
    print(f"pump-dashboard: sampling into {DB_PATH}", flush=True)

    next_t = time.monotonic()
    last_maintenance = time.monotonic()
    consecutive = 0

    while not stop.is_set():
        next_t += 1.0
        started = time.time()
        try:
            # port=None re-runs the cheap filesystem auto-detect each second, so
            # an unplug/re-enumeration recovers without a restart.
            sample = read_pump.read_pump(
                port=None,
                timeout=SAMPLE_TIMEOUT,
                retries=SAMPLE_RETRIES,
            )
            con.execute(INSERT_SQL, build_row(sample, int(started * 1000)))
            con.commit()
            consecutive = 0
        except Exception as exc:  # keep the 1 Hz grid alive through any failure
            consecutive += 1
            message = f"{type(exc).__name__}: {exc}"[:300]
            try:
                con.execute(
                    "INSERT OR REPLACE INTO read_errors (ts, error, consecutive) "
                    "VALUES (?, ?, ?)",
                    (int(started * 1000), message, consecutive),
                )
                con.commit()
            except Exception:
                pass

        now = time.monotonic()
        if now - last_maintenance >= MAINTENANCE_S:
            try:
                maintain(con)
            except Exception:
                pass
            last_maintenance = now

        delay = next_t - time.monotonic()
        if delay > 0:
            stop.wait(delay)
        else:
            # Overran the slot: skip missed seconds instead of bunching reads.
            next_t = time.monotonic()

    try:
        maintain(con)
    finally:
        con.close()
    print("pump-dashboard: sampler stopped", flush=True)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

def _json_bytes(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _int_arg(query: dict, name: str, default: int) -> int:
    try:
        return int(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


def series_payload(con: sqlite3.Connection, from_ms: int, to_ms: int, fields: list[str]) -> dict:
    now_ms = int(time.time() * 1000)
    to_ms = min(to_ms, now_ms + 1000)
    from_ms = max(from_ms, to_ms - RETENTION_MS)
    if from_ms >= to_ms:
        raise ValueError("from must be before to")

    target = max(1000, int(math.ceil((to_ms - from_ms) / MAX_BUCKETS)))
    bucket = nice_bucket(target)
    start = (from_ms // bucket) * bucket

    select = ", ".join(f"avg({key})" for key in fields)
    sql = (
        f"SELECT (ts / {bucket}) * {bucket} AS b, {select} "
        "FROM samples WHERE ts >= ? AND ts < ? GROUP BY b ORDER BY b"
    )
    rows = con.execute(sql, (from_ms, to_ms)).fetchall()
    found = {row[0]: row[1:] for row in rows}

    grid = list(range(start, to_ms, bucket))
    series = {key: [] for key in fields}
    for stamp in grid:
        values = found.get(stamp)
        for index, key in enumerate(fields):
            value = values[index] if values else None
            if value is not None and not math.isfinite(value):
                value = None
            series[key].append(None if value is None else round(value, 3))

    return {
        "from": from_ms,
        "to": to_ms,
        "bucket_ms": bucket,
        "t": [round(stamp / 1000, 3) for stamp in grid],
        "series": series,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PumpDashboard/1.0"

    def log_message(self, fmt, *args):  # no per-request logging
        pass

    def _send(self, status: int, content_type: str, body: bytes, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, status: int = 200):
        self._send(status, "application/json; charset=utf-8", _json_bytes(payload))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", INDEX_HTML)
            elif path == "/static/uplot.iife.min.js":
                self._send(200, "text/javascript; charset=utf-8", UPLOT_JS, "public, max-age=86400")
            elif path == "/static/uplot.min.css":
                self._send(200, "text/css; charset=utf-8", UPLOT_CSS, "public, max-age=86400")
            elif path == "/api/meta":
                self._send_json({
                    "fields": FIELDS,
                    "default_fields": DEFAULT_FIELDS,
                    "retention_days": RETENTION_DAYS,
                    "sample_interval_s": 1,
                    "now": int(time.time() * 1000),
                })
            elif path == "/api/latest":
                self._send_json(self._latest())
            elif path == "/api/series":
                self._send_json(self._series(query))
            elif path == "/api/errors":
                self._send_json(self._errors(query))
            else:
                self._send_json({"error": "not found"}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except (sqlite3.Error, OSError) as exc:
            self._send_json({"error": f"database unavailable: {exc}"}, 503)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _latest(self) -> dict:
        con = connect_reader()
        try:
            row = con.execute(
                "SELECT * FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            bounds = con.execute(
                "SELECT MIN(ts), MAX(ts) FROM samples"
            ).fetchone()
            errors = [
                dict(item)
                for item in con.execute(
                    "SELECT ts, error, consecutive FROM read_errors "
                    "ORDER BY ts DESC LIMIT 5"
                ).fetchall()
            ]
        finally:
            con.close()
        return {
            "sample": dict(row) if row else None,
            "errors": errors,
            "stats": {
                "oldest_ts": bounds[0],
                "newest_ts": bounds[1],
                "retention_days": RETENTION_DAYS,
            },
            "now": int(time.time() * 1000),
        }

    def _series(self, query: dict) -> dict:
        now_ms = int(time.time() * 1000)
        to_ms = _int_arg(query, "to", now_ms)
        from_ms = _int_arg(query, "from", to_ms - 86_400_000)
        raw_fields = query.get("fields", [",".join(DEFAULT_FIELDS)])[0]
        fields = [name.strip() for name in raw_fields.split(",") if name.strip()]
        unknown = [name for name in fields if name not in FIELD_KEYS]
        if unknown:
            raise ValueError("unknown field(s): " + ", ".join(unknown))
        if not fields:
            raise ValueError("no fields requested")
        if len(fields) > len(FIELDS):
            raise ValueError("too many fields")
        con = connect_reader()
        try:
            return series_payload(con, from_ms, to_ms, fields)
        finally:
            con.close()

    def _errors(self, query: dict) -> dict:
        now_ms = int(time.time() * 1000)
        to_ms = _int_arg(query, "to", now_ms)
        from_ms = _int_arg(query, "from", to_ms - 86_400_000)
        con = connect_reader()
        try:
            rows = con.execute(
                "SELECT ts, error, consecutive FROM read_errors "
                "WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 500",
                (from_ms, to_ms),
            ).fetchall()
        finally:
            con.close()
        return {"from": from_ms, "to": to_ms, "errors": [dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_asset(name: str) -> bytes:
    with open(os.path.join(STATIC_DIR, name), "rb") as handle:
        return handle.read()


INDEX_HTML = _load_asset("index.html")
UPLOT_JS = _load_asset("uplot.iife.min.js")
UPLOT_CSS = _load_asset("uplot.min.css")


def main() -> None:
    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    ready = threading.Event()
    sampler_thread = threading.Thread(
        target=sampler, args=(stop, ready), name="sampler", daemon=True
    )
    sampler_thread.start()
    if not ready.wait(timeout=10):
        raise RuntimeError("database initialization did not complete")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    server_thread.start()
    print(f"pump-dashboard: http://{HOST}:{PORT}/", flush=True)

    stop.wait()

    server.shutdown()
    server.server_close()
    sampler_thread.join(timeout=10)
    print("pump-dashboard: stopped", flush=True)


if __name__ == "__main__":
    main()
