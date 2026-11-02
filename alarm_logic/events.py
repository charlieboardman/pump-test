"""Sampler-side event recording; HTTP readers never evaluate alarms."""
import json
from datetime import datetime, time
from .leak import evaluate


def initialize(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS alarm_events (
          id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, source TEXT NOT NULL,
          code TEXT NOT NULL, transition TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alarm_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')


def record(con, ts, sample=None, error=None):
    def previous(key, default=None):
        row = con.execute('SELECT value FROM alarm_state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save(key, value):
        con.execute('INSERT OR REPLACE INTO alarm_state VALUES (?,?)', (key, json.dumps(value)))

    def event(source, code, transition, message):
        con.execute('INSERT INTO alarm_events (ts,source,code,transition,message) VALUES (?,?,?,?,?)',
                    (ts, source, code, transition, message))

    failed = previous('comms', False)
    if error is not None:
        if not failed:
            event('comms', 'READ_FAILURE', 'raised', 'Communications failure: ' + error)
        save('comms', True)
    elif sample is not None:
        if failed:
            event('comms', 'READ_FAILURE', 'cleared', 'Communications restored')
        save('comms', False)
        for key, label in [('faults_active', 'Fault'), ('alarms_active', 'Alarm')]:
            current = set(json.loads(sample[key] or '[]'))
            old = set(previous(key, []))
            for name in sorted(current - old):
                event('VFD', label + ': ' + name, 'raised', label + ' raised: ' + name)
            for name in sorted(old - current):
                event('VFD', label + ': ' + name, 'cleared', label + ' cleared: ' + name)
            save(key, sorted(current))
        last = previous('last_fault')
        if last is not None and sample['fault_last'] and sample['fault_last'] != last:
            event('VFD', 'LAST_FAULT_CHANGED', 'observed', 'Last recorded fault changed: ' + sample['fault_last_name'])
        save('last_fault', sample['fault_last'])

    local = datetime.fromtimestamp(ts / 1000)
    day = local.date()
    if local.hour >= 4 and previous('leak_day') != day.isoformat():
        start = int(datetime.combine(day, time(2)).timestamp() * 1000)
        end = int(datetime.combine(day, time(4)).timestamp() * 1000)
        rows = con.execute('SELECT ts, motor_freq_hz FROM samples WHERE ts>=? AND ts<? ORDER BY ts', (start,end))
        result = evaluate([{'ts': r[0], 'motor_freq_hz': r[1]} for r in rows], day, None, ts)
        if result['state'] in ('raised', 'unknown'):
            event('local', result['code'], result['state'], result['message'])
        save('leak_day', day.isoformat())
