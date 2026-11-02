# Alarm logic

Home for locally derived alarm rules. Rules are evaluated by the sampler. HTTP requests only read stored events.

`leak.py` implements `LEAK_NO_OVERNIGHT_STOP`: after 04:00, raise a possible-leak result if no zero motor-frequency sample was observed during [02:00, 04:00). Missing coverage exceeding five seconds makes the result unknown. The dashboard uses the host local timezone. The sampler evaluates the completed window once per day at or after 04:00 (on startup if it was down then). It saves the result and day marker atomically. Unknown results are recorded explicitly; a passing window produces no alarm event.

The dashboard's alarm history should combine these sources:

- **VFD bus:** fault/alarm codes and names observed in successful reads.
- **Local rule:** conditions evaluated from measurements, sample age, or communications health.
- **Communications:** failed reads and recovery.

Present history as a chronological table: time, source, condition, transition,
message. Record activation and clearance once per condition transition, rather
than repeating an event every second. Normal pump starts and stops are not alarm
events. A stored last-fault register is historical evidence, not an active fault.

Each local rule needs a stable ID, description, input measurements, activation
condition, activation delay, clearance condition, and clearance delay. Define
thresholds and hysteresis explicitly before enabling a rule. Missing or stale
measurements must not clear an alarm or imply a healthy state.

Rules must be read-only: they must never change drive parameters or control the
pump. Keep rule evaluation separate from Modbus acquisition and presentation.

`alarm_events` and `alarm_state` are created additively without changing the sample schema version. Events retain the latest 10,000 entries independently of sample retention; the UI shows the latest 100. Both tables survive service restarts in the RAM-backed SQLite file and are cleared on reboot. State and event writes commit with each sample or read failure. VFD activations and clearances and communications failure/recovery are recorded once per transition. Existing samples are not backfilled into events.
