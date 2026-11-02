"""Evaluate a completed overnight window using stored pump samples."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

LEAK_CODE = "LEAK_NO_OVERNIGHT_STOP"


def evaluate(samples, day, timezone, now, max_gap_seconds=5):
    """Return a daily result for [02:00, 04:00) in the explicit IANA timezone.

    samples: iterable of mappings with epoch-millisecond ``ts`` and measured
    ``motor_freq_hz``. Zero frequency is stopped; the provisional VFD running
    bit is deliberately ignored. Pass all retained samples for the window.
    Results are declarative: callers persist one event per (code, day), rather
    than emitting an event on every evaluation.
    """
    zone = ZoneInfo(timezone) if timezone else None
    start = datetime.combine(day, time(2), zone)
    end = datetime.combine(day, time(4), zone)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    result = {"code": LEAK_CODE, "day": day.isoformat(), "timezone": timezone or "host local",
              "window_start": start_ms, "window_end": end_ms}
    if now < end_ms:
        return dict(result, state="pending")
    rows = sorted((r for r in samples if start_ms <= r["ts"] < end_ms), key=lambda r: r["ts"])
    if any(r.get("motor_freq_hz") == 0 for r in rows):
        return dict(result, state="passed", message="Pump stopped during the overnight window")
    valid = [r for r in rows if r.get("motor_freq_hz") is not None and r["motor_freq_hz"] > 0]
    timestamps = [start_ms] + [r["ts"] for r in valid] + [end_ms]
    if not valid or any(b - a > max_gap_seconds * 1000 for a, b in zip(timestamps, timestamps[1:])):
        return dict(result, state="unknown", message="Insufficient readings to determine overnight pump operation")
    return dict(result, state="raised", message="Possible leak: pump never stopped between 02:00 and 04:00")
