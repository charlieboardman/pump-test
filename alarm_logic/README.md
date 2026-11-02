# Alarm logic

Home for locally derived alarm rules. The overnight leak rule is evaluated in the dashboard event log.

`leak.py` implements `LEAK_NO_OVERNIGHT_STOP`: after 04:00, raise a possible-leak result if no zero motor-frequency sample was observed during [02:00, 04:00). Missing coverage exceeding five seconds makes the result unknown. The dashboard uses the host local timezone. Results are reconstructed from retained samples, shown at 04:00 for each completed window, and expire with sample retention. Unknown results are shown explicitly; a passing window produces no alarm event.

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

Future event storage must use an additive migration that preserves existing
samples; do not bump the current schema version through its destructive reset
path merely to add an event table. History retains the project's seven-day,
RAM-backed storage policy unless explicitly changed.
