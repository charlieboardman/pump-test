"""One-function, read-only Aquavar CPC pump snapshot."""

from __future__ import annotations  # lets the type hints below run on Python 3.7+


def read_pump(
    port: str,
    *,
    slave: int = 1,
    baudrate: int = 9600,
    timeout: float = 0.75,
    retries: int = 2,
) -> dict:
    """Return the useful live Aquavar CPC readings as a labeled dictionary.

    The drive is expected to use Modbus RTU with eight data bits, no parity,
    and one stop bit (8N1), matching the field-confirmed settings (parameters
    3202-3204). This function only issues function-03 reads; it cannot start,
    stop, reset, or reconfigure the pump.

    Drive setup before first use (keypad, parameter lock opened with pass code
    66 in 10.02):
        3001 Comm Protocol Select = Std Modbus (default is Not Selected, and
             the drive will not answer at all until this is set).
        2409 Comm Fault Function = Not Selected (default Last Speed); this is
             read-only monitoring, so a dropped link should never change how
             the pump runs.

    Args:
        port: Serial device, for example ``/dev/ttyUSB0``.
        slave: VFD Modbus station ID (parameter 3202).
        baudrate: VFD Modbus baud rate (parameter 3203).
        timeout: Maximum seconds to wait for each reply.
        retries: Additional attempts after a timeout or damaged reply.

    Returns:
        A plain nested dictionary. Measurements contain ``value``, ``unit`` and
        ``raw`` keys (``raw`` is the unscaled register, for checking the scale
        factors against the keypad). Statuses, inputs, faults and alarms are
        already decoded.

    Verification notes:
        The Aquavar manual does not publish the Modbus scaling or bit layouts.
        Pressure scaling (0.1) is supported by the manual's 0...6553.5
        transducer ranges; the other factors follow ABB convention and live
        in ``SCALE`` below, so a value that is off by 10x is a one-line fix.
        ``faults.last_recorded`` (parameter 0116) is a direct fault code and
        is the authoritative fault reading. The ``active`` fault and alarm
        lists assume bit n means code n+1, which is not documented.

    Requires:
        ``python3 -m pip install pyserial``
    """
    import math
    import time
    from datetime import datetime

    try:
        import serial
    except ImportError as exc:
        raise RuntimeError(
            "read_pump() requires pyserial; install it with "
            "'python3 -m pip install pyserial'"
        ) from exc

    if not isinstance(port, str) or not port.strip():
        raise ValueError("port must be a serial-device name such as '/dev/ttyUSB0'")
    if isinstance(slave, bool) or not isinstance(slave, int) or not 1 <= slave <= 247:
        raise ValueError("slave must be an integer from 1 through 247")
    if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0:
        raise ValueError("baudrate must be a positive integer")
    if isinstance(timeout, bool):
        raise ValueError("timeout must be a finite number greater than zero")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a finite number greater than zero") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than zero")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("retries must be a non-negative integer")

    # Scale factor per parameter: displayed value = raw register * factor.
    # Check each against the keypad once; if one is off by 10x, change it here.
    SCALE = {
        "0101": 0.1,  # motor frequency, Hz
        "0102": 0.1,  # motor current, A
        "0103": 0.1,  # motor power, kW (signed)
        "0104": 1,  # DC bus voltage, V
        "0105": 1,  # motor voltage, V
        "0106": 0.1,  # drive temperature, °C; confirmed against keypad
        "0107": 1,  # VFD powered time, h
        "0108": 1,  # motor run time, h
        "0109": 1,  # energy, kWh
        "0111": 0.1,  # AI1, %
        "0112": 0.1,  # AI2 transducer feedback, %
        "0114": 0.1,  # AO1, mA
        "0115": 0.1,  # AO2, mA
        "0201": 0.1,  # set point, process unit
        "0202": 0.1,  # actual feedback (pressure), process unit
        "0203": 0.1,  # pump speed, Hz; raw matched 0101 in live snapshot
        "0204": 0.1,  # wire-to-water power, hp
        "0205": 0.1,  # set point 1, process unit
        "0206": 0.1,  # set point 2, process unit
        "0207": 1,  # energy savings, USD
        "0209": 0.1,  # used set point, process unit
    }

    def crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return crc

    def with_crc(payload: bytes) -> bytes:
        crc = crc16(payload)
        return payload + bytes((crc & 0xFF, crc >> 8))

    def valid_crc(frame: bytes) -> bool:
        return len(frame) >= 4 and int.from_bytes(frame[-2:], "little") == crc16(frame[:-2])

    def signed_16(raw: int) -> int:
        return raw - 0x10000 if raw & 0x8000 else raw

    def measurement(raw: int, parameter: str, unit: str, *, signed: bool = False) -> dict:
        number = signed_16(raw) if signed else raw
        factor = SCALE[parameter]
        value = number if factor == 1 else round(number * factor, 1)
        return {"value": value, "unit": unit, "raw": raw}

    fault_names = {
        0: "NO RECORD",
        1: "OVERCURRENT",
        2: "DC OVERVOLT",
        3: "DEV OVERTEMP",
        4: "SHORT CIRC",
        5: "OVERLOAD",
        6: "DC UNDERVOLT",
        7: "AI1 LOSS",
        8: "AI2 LOSS",
        9: "MOT OVERTEMP",
        10: "PANEL LOSS",
        11: "ID RUN FAIL",
        12: "MOTOR STALL",
        13: "RESERVED",
        14: "PUMP PROTECT",
        15: "LOW WATER",
        16: "EARTH FAULT",
        17: "UNDERLOAD",
        18: "THERM FAIL",
        19: "OPEX LINK",
        20: "OPEX PWR",
        21: "CURR MEAS",
        22: "SUPPLY PHASE",
        23: "RESERVED",
        24: "OVERSPEED",
        25: "RESERVED",
        26: "DRIVE ID",
        27: "CONFIG FILE",
        28: "SERIAL 1 ERR",
        29: "EFB CON FILE",
        30: "FORCE TRIP",
        31: "EFB 1",
        32: "EFB 2",
        33: "EFB 3",
        34: "MOTOR PHASE",
        35: "OUTP WIRING",
        101: "SERF CORRUPT",
        102: "SERF IITFILE",
        103: "SERF MACRO",
        104: "SERF EFBPROT",
        105: "SERF BPFILE",
        201: "DSP T1 OVERLOAD",
        202: "DSP T2 OVERLOAD",
        203: "DSP T3 OVERLOAD",
        204: "DSP STACK ERROR",
        205: "DSP REV ERROR",
        206: "OMIO ID ERROR",
        1000: "PAR HZRPM",
        1001: "PAR PFCREFNG",
        1003: "PAR AI SCALE",
        1004: "PAR AO SCALE",
        1005: "PAR PCU 2",
        1007: "PAR FBUS",
        1009: "PAR PCU 1",
    }
    alarm_names = {
        2004: "DIR LOCK",
        2005: "I/O COMM",
        2006: "AI1 LOSS",
        2007: "TRANSDUCER LOSS",
        2008: "PANEL LOSS",
        2010: "MOT OVERTEMP",
        2011: "UNDERLOAD",
        2012: "MOTOR STALL",
        2013: "AUTORESET",
        2015: "PUMP PROTECT",
        2016: "LOW WATER",
        2023: "E-STOP FAULT",
    }
    process_units = {0: "%", 1: "GPM", 2: "psig", 3: "ft", 4: "°F", 5: "°C",
                     25: "psi"}  # Code 25 confirmed against parameter 1501 on site.

    bus = None
    try:
        bus = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=min(0.02, timeout),
            write_timeout=timeout,
        )

        def read_block(register: int, count: int) -> list[int]:
            # Aquavar 4xxxx references map to zero-based Modbus addresses.
            address = register - 40001
            payload = bytes(
                (
                    slave,
                    3,
                    address >> 8,
                    address & 0xFF,
                    count >> 8,
                    count & 0xFF,
                )
            )
            request = with_crc(payload)
            expected_bytes = count * 2
            last_problem = "no reply"

            for attempt in range(retries + 1):
                # Modbus RTU requires a quiet interval between frames. Keep a
                # conservative 11-bit interval even with the drive using 8N1.
                time.sleep(max(3.5 * 11 / baudrate, 0.00175))
                bus.reset_input_buffer()
                bus.write(request)
                bus.flush()

                received = bytearray()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    waiting = bus.in_waiting
                    chunk = bus.read(waiting if waiting else 1)
                    if not chunk:
                        continue
                    received.extend(chunk)

                    # Scan past USB-adapter echoes and arbitrary leading noise.
                    for start in range(len(received)):
                        if received[start] != slave or start + 2 >= len(received):
                            continue
                        function = received[start + 1]

                        if function == 0x83 and start + 5 <= len(received):
                            frame = bytes(received[start : start + 5])
                            if valid_crc(frame):
                                exception_names = {
                                    1: "illegal function",
                                    2: "illegal data address",
                                    3: "illegal data value",
                                    4: "slave device failure",
                                    6: "slave device busy",
                                }
                                code = frame[2]
                                name = exception_names.get(code, "unknown exception")
                                raise RuntimeError(
                                    f"VFD returned Modbus exception {code} ({name}) "
                                    f"while reading {register}"
                                )

                        if function != 3 or received[start + 2] != expected_bytes:
                            continue
                        end = start + 3 + expected_bytes + 2
                        if end > len(received):
                            continue
                        frame = bytes(received[start:end])
                        if not valid_crc(frame):
                            last_problem = f"bad CRC in {frame.hex(' ')}"
                            continue
                        data = frame[3:-2]
                        return [
                            int.from_bytes(data[index : index + 2], "big")
                            for index in range(0, len(data), 2)
                        ]

                    if len(received) > 1024:
                        del received[:-512]

                if received and last_problem == "no reply":
                    last_problem = f"no valid response in {received.hex(' ')}"
                if attempt < retries:
                    time.sleep(0.05 * (attempt + 1))

            hint = ""
            if last_problem == "no reply":
                hint = (
                    ". Check: 3001 Comm Protocol Select = Std Modbus; A/B wires "
                    "(terminals 30/29) not swapped; station ID (3202), baud "
                    "(3203) and parity (3204) match"
                )
            raise TimeoutError(
                f"Unable to read Aquavar register {register} from slave {slave} "
                f"on {port} after {retries + 1} attempt(s): {last_problem}{hint}"
            )

        # These are separate blocks in the drive. Keeping the reads compact
        # avoids relying on undocumented/reserved addresses between groups.
        sample_started = datetime.now().astimezone()
        sample_started_monotonic = time.monotonic()
        group_01 = read_block(40101, 16)
        group_02 = read_block(40201, 9)
        group_03 = read_block(40303, 10)
        unit_code = read_block(41501, 1)[0]
        sample_finished = datetime.now().astimezone()
        sample_duration_ms = round(
            (time.monotonic() - sample_started_monotonic) * 1000,
            1,
        )
    finally:
        if bus is not None:
            bus.close()

    process_unit = process_units.get(unit_code, f"unit-code-{unit_code}")
    last_fault_code = group_01[15]
    fault_words = group_03[2:5]
    alarm_words = group_03[5:7]

    active_faults = []
    for word_index, word in enumerate(fault_words):
        for bit in range(16):
            if word & (1 << bit):
                code = word_index * 16 + bit + 1
                active_faults.append(
                    {"code": code, "name": fault_names.get(code, "UNKNOWN FAULT")}
                )

    active_alarms = []
    for word_index, word in enumerate(alarm_words):
        for bit in range(16):
            if word & (1 << bit):
                code = 2001 + word_index * 16 + bit
                active_alarms.append(
                    {"code": code, "name": alarm_names.get(code, "UNKNOWN ALARM")}
                )

    # Parameters 0303/0304 are the low and high halves of the drive's 32-bit
    # internal status word. They use this layout whatever the communication
    # profile (3205) is set to; the 16-bit "ABB Drives" profile word is a
    # different register and is not read here. Sanity check on site: with the
    # pump running, "running" (low word bit 3) should be True.
    status_low, status_high = group_03[0], group_03[1]
    low_names = {
        0: "ready",
        1: "enabled",
        2: "started",
        3: "running",
        4: "zero_speed",
        5: "accelerating",
        6: "decelerating",
        7: "at_setpoint",
        8: "at_limit",
        9: "supervision",
        10: "reverse_reference",
        11: "reverse_actual",
        12: "panel_local",
        13: "fieldbus_local",
        14: "external_set_2_active",
        15: "fault",
    }
    high_names = {
        0: "alarm",
        1: "notice",
        2: "direction_locked",
        3: "local_locked",
        4: "control_mode",
        7: "copy_control_word",
        8: "copy_reference_1",
        9: "copy_reference_2",
        10: "control_requested",
        11: "reference_1_requested",
        12: "reference_2_requested",
        13: "extended_reference_2_requested",
        14: "start_inhibit_acknowledged",
        15: "off_interlock_acknowledged",
    }
    status_bits = {name: bool(status_low & (1 << bit)) for bit, name in low_names.items()}
    status_bits.update(
        {name: bool(status_high & (1 << bit)) for bit, name in high_names.items()}
    )
    if status_bits["fault"]:
        summary = "faulted"
    elif status_bits["alarm"]:
        summary = "alarm"
    elif status_bits["zero_speed"]:
        # Field-confirmed: bit 3 stays set while the pump is at 0 Hz / 0 A.
        # Give zero speed precedence over that provisional running bit.
        summary = "stopped"
    elif status_bits["running"]:
        summary = "running"
    elif status_bits["started"]:
        summary = "started"
    elif status_bits["enabled"]:
        summary = "enabled"
    elif status_bits["ready"]:
        summary = "ready"
    else:
        summary = "stopped/not ready"

    return {
        "sample_started_at": sample_started.isoformat(timespec="milliseconds"),
        "sample_finished_at": sample_finished.isoformat(timespec="milliseconds"),
        "sample_duration_ms": sample_duration_ms,
        "pressure": measurement(group_02[1], "0202", process_unit),
        "setpoint": measurement(group_02[0], "0201", process_unit),
        "active_setpoint": measurement(group_02[8], "0209", process_unit),
        "setpoint_1": measurement(group_02[4], "0205", process_unit),
        "setpoint_2": measurement(group_02[5], "0206", process_unit),
        "pump_output_frequency": measurement(group_02[2], "0203", "Hz"),
        "motor_frequency": measurement(group_01[0], "0101", "Hz"),
        "motor_current": measurement(group_01[1], "0102", "A"),
        "motor_power": measurement(group_01[2], "0103", "kW", signed=True),
        "overall_system_power": measurement(group_02[3], "0204", "hp"),
        "motor_voltage": measurement(group_01[4], "0105", "V"),
        "dc_bus_voltage": measurement(group_01[3], "0104", "V"),
        "drive_temperature": measurement(group_01[5], "0106", "°C"),
        "analog_input_1": measurement(group_01[10], "0111", "%"),
        "transducer_feedback": measurement(group_01[11], "0112", "%"),
        "analog_output_1": measurement(group_01[13], "0114", "mA"),
        "analog_output_2": measurement(group_01[14], "0115", "mA"),
        "vfd_powered_time": measurement(group_01[6], "0107", "h"),
        "motor_run_time": measurement(group_01[7], "0108", "h"),
        "energy": measurement(group_01[8], "0109", "kWh"),
        "calculated_energy_savings": measurement(group_02[6], "0207", "USD"),
        "pump": {
            "code": group_02[7],
            "label": {0: "stopped", 1: "pump 1", 2: "pump 2", 3: "pump 3", 4: "pump 4"}.get(
                group_02[7], f"unknown pump {group_02[7]}"
            ),
        },
        "digital_inputs": {
            f"DI{number}": bool(group_01[9] & (1 << (number - 1)))
            for number in range(1, 7)
        },
        "relay_outputs": {
            f"RO{number}": bool(group_01[12] & (1 << (number - 1)))
            for number in range(1, 4)
        },
        "status": {
            "summary": summary,
            "bits": status_bits,
            "raw_low_word": f"0x{status_low:04X}",
            "raw_high_word": f"0x{status_high:04X}",
        },
        "faults": {
            # Authoritative: parameter 0116 is a direct fault code.
            "last_recorded": {
                "code": last_fault_code,
                "name": fault_names.get(last_fault_code, "UNKNOWN FAULT"),
            },
            # Provisional: assumes bit n of fault words 0305-0307 means code n+1.
            "active": active_faults,
            "raw_standard_words": [f"0x{word:04X}" for word in fault_words],
            "raw_aquavar_word": f"0x{group_03[8]:04X}",
        },
        "alarms": {
            # Provisional: assumes bit n of alarm words 0308-0309 means 2001+n.
            "active": active_alarms,
            "raw_standard_words": [f"0x{word:04X}" for word in alarm_words],
            "raw_aquavar_word": f"0x{group_03[7]:04X}",
        },
        "communication_relay_word": f"0x{group_03[9]:04X}",
    }


if __name__ == "__main__":
    # Quick field check: python3 read_pump.py /dev/ttyUSB0
    import json
    import sys

    print(json.dumps(read_pump(sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"),
                     indent=2, ensure_ascii=False))
