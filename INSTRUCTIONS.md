# Aquavar VFD Read Test — Instructions for the Coding Agent

Read this whole file before doing anything. It is the project brief, starting from zero.

## What we're doing

We're testing a Python function, `read_pump()` in `read_pump.py`, which reads live data from a water system's pressure-pump VFD and checking that it outputs the right values.

The VFD is a CentriPro **Aquavar CPC**. The system's pressure transducer is wired into the VFD (analog input AI2), so the VFD is where pressure, pump speed, motor current, faults and alarms all live. We read it over **Modbus RTU on RS485** with a USB-RS485 dongle plugged into a laptop at the drive.

This session's goal is a working, calibrated `read_pump()`. Nothing else is in scope.

## Files in this directory

| File | What it is |
|---|---|
| `INSTRUCTIONS.md` | This brief. Update the **Field results** and **Status** sections as you learn things. |
| `read_pump.py` | Draft read-only Modbus function for the Aquavar. **Not yet run against the real drive.** |
| `aquavar_cpc_im167r08.pdf` | Aquavar CPC Installation & Operation Manual (IM167 R08), 152 pages. |

### Where to look in the manual (PDF page numbers, not printed page numbers)

| Topic | PDF page |
|---|---|
| RS485 wiring, terminals 28–32, termination DIP switch | 31 |
| Group 01 VFD signals (0101–0128) | 52–53 |
| Group 02 pump signals (0201–0209) | 53 |
| Group 03 status, fault and alarm words (0301–0314) | 53–54 |
| 1501 transducer unit, 1502/1503 transducer range | 59 |
| 2409/2410 comm fault function and time | 69 |
| 3001 comm protocol select | 70 |
| 3202–3209 Modbus setup (station, baud, parity, profile, counters) | 71 |
| RS485 is shared with multipump; fieldbus can't be used with RS485 multipump | 88 |
| Fieldbus notes | 103 |
| Parameter lock, pass code | 104 |
| Fault listing (codes 1–1009) | 116–119 |
| Alarm listing (codes 2001–2023) | 120–121 |

## Hard rules

- **Read-only. Never write to the drive.** Only Modbus function 03 (read holding registers). Never send function 05, 06, 15 or 16, and never add code that can. The drive runs a live water system for people. Parameter changes are made by Charlie, by hand, at the keypad.
- **`read_pump()` stays stateless.** Each call opens the port, reads, closes the port and returns a dict. No background threads, no held-open port.
- **Keep dependencies minimal:** `pyserial` plus the standard library. No pymodbus unless the hand-rolled framing proves to be the problem.

## Drive setup (done by Charlie at the keypad, before the first read)

The drive will not answer Modbus until 3001 is set. Confirm each item with Charlie rather than assuming it's done.

1. Open the parameter lock: 10.02 Pass Code → **66**. The lock re-closes after power loss.
2. Write down the original values of 3001, 2409, 3202, 3203, 3204 and 3205 before changing anything.
3. **3001 Comm Protocol Select = Std Modbus.** The default is Not Selected, and the drive won't answer at all until this is set. Per the manual, this works only in the Single Pump, Constant Slave or Speed Control macros.
4. **2409 Comm Fault Function = Not Selected.** The default, Last Speed, holds the last speed reference if the drive thinks communication dropped. We only read, so a dropped link must never affect pump behavior.
5. Leave at their defaults unless there's a reason to change: 3202 Station ID = 1, 3203 Baud = 9.6 kbit/s, 3204 Parity = 8N2.
6. Wire the dongle per **Wiring** below. The RS485 terminals are shared with Aquavar multipump, so if this drive is in a multipump setup, Modbus isn't available.

## Wiring: Innomaker USB-RS485 dongle → Aquavar

The dongle is Innomaker's industrial USB-RS485 adapter. It has an FTDI FT230X chip and an isolated half-duplex transceiver (TD541S485H), and its DB9 connector goes to a green screw-terminal adapter board. On Charlie's unit the board is silkscreened **485A, GND, 485B**, in that order (left to right, looking into the wire entries). **GND is in the middle**, so don't assume A-B-GND order.

**Safety:** wire with the drive powered off, and wait at least 5 minutes before opening it (manual's capacitor-discharge warning). The comm terminals are low voltage, but they share an enclosure with mains and motor power.

| Dongle terminal | Aquavar X1 terminal | Notes |
|---|---|---|
| 485A (left) | 29 (B) | Data +. See polarity note. |
| GND (middle) | 31 (AGND) | Signal reference. Safe to connect because the dongle is isolated. |
| 485B (right) | 30 (A) | Data −. |
| (none) | 28 (Screen) | Cable shield, connected at the drive end only. |

- **Polarity:** RS485 A/B labels are not consistent between vendors. Transceiver chips, and most USB dongles, label A as the + line. ABB-style drives like the Aquavar label B as +. So the first guess above crosses the letters to match + to +. **If there's no reply, swap the two data wires.** Reversed polarity does no harm; it just won't communicate.
- **Termination:** with a short cable at 9600 baud it probably doesn't matter. To follow the manual (terminate both ends), turn on the drive's termination DIP switch (PDF page 31) and the dongle's termination. On the dongle, the black slide switch on the left edge of the green adapter board, next to a resistor marked "121" (120 Ω), looks like the termination switch. To check which position is on, unplug the adapter from the dongle and measure 485A to 485B with a multimeter: about 120 Ω means on, open means off.
- **Cable:** use a twisted pair for the two data wires (one pair from Cat5 works), plus a third wire for GND.
- **Direction control** is automatic in the FTDI chip. The code doesn't toggle RTS and shouldn't.

**Dongle LEDs:** PWR = USB power. TX blinks on each request. RX blinks when the drive answers.
- No TX blink: wrong port, or the script isn't sending.
- TX but no RX: the drive isn't answering. Check 3001, then swap A/B, then the station, baud and parity settings.

**Host side:**
- **Linux:** the dongle shows up as `/dev/ttyUSB0` (stable path under `/dev/serial/by-id/usb-FTDI_…`). The user needs to be in the `dialout` group (`sudo usermod -aG dialout $USER`, then log out and back in), or opening the port fails with "permission denied".
- **macOS:** it appears as `/dev/tty.usbserial-*`, with no driver install needed.

## What `read_pump.py` does and how sure we are

`read_pump(port, slave=1, baudrate=9600, timeout=0.75, retries=2)` reads four register blocks and returns a labeled dict:

- 40101–40116: parameters 0101–0116 (frequency, current, power, DC bus, voltage, temperature, hours, kWh, digital inputs, AI1, AI2, relays, AO1, AO2, last fault)
- 40201–40209: parameters 0201–0209 (set point, **actual pressure**, pump speed, wire-to-water power, set point 1 and 2, savings, pump number, used set point)
- 40303–40312: parameters 0303–0312 (status words, fault words, alarm words, Aquavar alarm and fault words, comm relay word)
- 41501: parameter 1501, the process unit (psig, etc.)

Each measurement is `{"value", "unit", "raw"}`. Running `python3 read_pump.py /dev/ttyUSB0` prints one snapshot as JSON.

**Verified:**
- The serial defaults match the manual: station 1, 9600 baud, 8N2.
- Every register offset lands on the right parameter, checked against the manual's tables.
- The Modbus RTU framing is correct. The CRC passes a standard test vector, and the function was tested against a simulated drive, including a USB adapter that echoes the request back.
- The fault and alarm names match the manual's tables.

**Not verified.** The manual has no Modbus register map, scaling or bit definitions. It refers to a separate "Standard Serial Communication" addendum we don't have. The drive appears to be built on an ABB ACS550 (the fault and alarm tables match), so the draft follows ABB's embedded-fieldbus conventions:
- **Address scheme** 4xxxx = parameter xxxx, PDU address = register − 40001. This is ABB standard and very likely right.
- **Scale factors** are all in the `SCALE` dict. Pressure ×0.1 is well supported, because the manual's transducer ranges go to 6553.5. The most doubtful are **0106 drive temperature** and **0203 pump speed**, both currently ×1; ABB often uses ×0.1 for these.
- **Status word bits (0303/0304)** are decoded with ABB's internal 32-bit status layout no matter what 3205 is set to. An earlier draft decoded them with the "ABB Drives" profile bits, which reported a running pump as faulted. Sanity check: with the pump running, `status.bits.running` (low word bit 3) should be True.
- **Fault and alarm word bits** assume bit n means code n+1 (faults) or 2001+n (alarms). Treat these as provisional. `faults.last_recorded` (0116) is a direct fault code and is authoritative.

If the addendum or ABB's ACS550 embedded-fieldbus documentation turns up, use it to settle the scaling and bit layouts.

## Test procedure

Charlie will be at the drive with a laptop and the USB-RS485 dongle.

1. Confirm the drive setup above is done. Find the dongle with `ls /dev/serial/by-id/` and use that path.
2. Run `python3 read_pump.py <port>`.
3. If it fails:
   - **TimeoutError, "no reply":** check 3001 first, then swap A and B (vendors label them inconsistently), then termination, then station, baud and parity.
   - **Bytes received but no valid response, or bad CRC:** look for a baud or parity mismatch, wiring noise, or a missing ground (AGND).
   - **Modbus exception 2 (illegal data address):** some register in that block isn't readable. Split the block into smaller reads to find which one, and leave it out.
4. **Calibrate against the keypad.** You can't see the keypad, so ask Charlie to read these from it at the same moment as a snapshot: pressure, set point, output frequency, motor current, drive temperature. Compare each with `raw` and fix `SCALE` where a value is off (usually by 10×). Check 0203 against 0101.
5. Check the status decode with the pump running, and again stopped if Charlie can safely do that.
6. Run a few snapshots in a row to confirm reads are reliable (no intermittent timeouts or CRC errors).
7. Record what you found under **Field results** below: confirmed scale factors, any blocks that had to be split, the drive firmware (parameter 9901), and anything surprising.

## Field results

- Session observations (2026-10-15): Charlie confirmed one pump; dongle-to-drive jumpers connected.
- Original keypad settings: 3001 = NOT SEL; 2409 = NOT SEL; 3202 = 1; 3203 = 9.6 kb/s; 3204 = 8N1; 3205 = AQUAVAR.
- Charlie stopped the pump, changed 3001 to STD MODBUS, and restarted it. 2409 remains NOT SEL.
- Host checks: Charlie's current session includes dialout; dongle is /dev/ttyUSB0.
- Reader changed to 8N1 to match the actual drive. No live read attempted yet.

- Date:
- Drive firmware (9901): A305 hex (keypad, reported by Charlie).
- Port path: `/dev/serial/by-id/usb-FTDI_FT230X_Basic_UART_DU0FAHYM-if00-port0`
- Worked on first try? If not, what fixed it: No. Enabling drive RS485 termination stopped continuous UART errors; restoring original polarity (485A → 29, 485B → 30) then yielded a successful snapshot in 191.7 ms.
- Confirmed scale factors: 0201 setpoint ×0.1 confirmed at 50.1; 0202 pressure ×0.1 and 0101 frequency ×0.1 agree approximately with fluctuating keypad readings. 0106 temperature ×0.1 confirmed (raw 324; keypad 32.4–32.5 °C). 0203 speed corrected to ×0.1 because raw 383 matched 0101.
- Status bits with pump running (raw low/high words): First successful snapshot 0x402F / 0x000C; running bit true, pending keypad comparison.
- Registers or blocks that had to be split or removed:
- Notes: First snapshot: pressure raw 490 (49.0), setpoint raw 501 (50.1), frequency 0101 and 0203 both raw 383, current raw 58, temperature raw 324. Process-unit raw code 25 confirmed by Charlie as psi at parameter 1501; reader mapping updated. Speed 0203 and temperature likely need ×0.1. Pump-number raw 0 currently misleadingly labeled stopped despite nonzero speed. Calibration pending.

## Status

- [x] `read_pump()` drafted and reviewed against the manual; tested on a simulated drive
- [x] Drive setup at keypad (3001, 2409)
- [x] First live read
- [ ] Scale factors calibrated against the keypad
- [ ] Status decode confirmed (running and stopped)
- [x] Repeated reads reliable (five consecutive snapshots completed successfully)

- Reliability check: five consecutive snapshots succeeded, 197.5–214.3 ms each. Pressure 49.5–50.5 psi; setpoint 50.1 psi; frequency 40.5–42.4 Hz; current 5.9–6.5 A; temperature 32.5–32.6 °C. All reported running. No surfaced communication errors; internal retries were not instrumented. Stopped-state status check remains pending.

## Observed stopped pump — 17:19:24 local time

Charlie reported the pump stopped; snapshot succeeded in 199.1 ms. Pressure 46.3 psi, setpoint 50.1 psi, both frequency readings 0.0 Hz, current 0.0 A, motor power and voltage zero. Raw status low/high: 0x401F / 0x000C. The draft still reports running=true and summary=running, while zero_speed=true. This demonstrates that the current running bit/summary cannot identify physical pump operation by itself; status decoding remains unresolved. Active fault/alarm words were zero. RO2 was false (true in the earlier running sample).

- Additional keypad observations: 0103 motor power fluctuated between 0.5 and 1.1 kW while running. Consistent with draft ×0.1 scale (earlier raw 5 → 0.5 kW), but not a simultaneous comparison.

## Confirmed restart settings and voltage scales

- 1405 Restart Value: bus raw 800; keypad 80%, confirming ×0.1 percent scaling.
- 1406 Restart Delay: bus raw 5; keypad 5 seconds, confirming ×1 second scaling.
- At the 50.1 psi setpoint, the normal-pressure-control restart threshold is 50.1 × 0.80 = **40.08 psi** (about **40.1 psi**). Per the manual, pressure must remain below the threshold longer than the restart delay before waking from demand-related sleep. Actual observed starting pressure may be lower as pressure continues to fall during the delay.
- 0104 DC bus voltage: keypad 322 V, consistent with earlier bus readings 304–326 V; ×1 supported.
- 0105 motor voltage: keypad 150–172 V while running, consistent with earlier bus reading 148 V; ×1 supported. Earlier keypad zero was not a useful scale comparison.

- One-minute reliability run: 60 snapshots at approximately one-second intervals, 60 successful and 0 failed, durations 189.4–220.5 ms, elapsed 59.2 s. Each snapshot reads four blocks (240 function-03 transactions total if no retries). Pre-run keypad counters: 3206=37, 3207=0, 3208=26663. Post-run keypad counters confirmed by Charlie: 3206=277, 3207=0, 3208=26663. Exactly 240 additional good messages, with no new CRC or UART errors. Internal retries not instrumented.

- Wire-to-water power 0204: Charlie observed 0.6–0.9 hp on the keypad. Fresh snapshot at 17:38:02 read raw 11 → 1.1 hp using ×0.1; motor power raw 8 → 0.8 kW, frequency 42.1 Hz. Charlie also observed 1.1 hp on the fluctuating keypad display, supporting the ×0.1 scale. Retained ×0.1 scale.

- Reader status summary corrected: fault/alarm retain precedence; otherwise zero_speed now yields "stopped" before considering the provisional running bit. Raw bits remain unchanged. This addresses the observed 0 Hz / 0 A samples incorrectly summarized as running. Change not tested against another live snapshot yet.
