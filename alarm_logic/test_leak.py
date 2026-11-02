import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
from alarm_logic.leak import evaluate


class LeakCheck(unittest.TestCase):
    def test_window_and_missing_data(self):
        day = date(2026, 11, 2)
        zone = 'America/Los_Angeles'
        start = int(datetime(2026, 11, 2, 2, tzinfo=ZoneInfo(zone)).timestamp()*1000)
        end = start + 7200000
        rows = [{'ts': ts, 'motor_freq_hz': 40} for ts in range(start, end, 1000)]
        check = lambda data, now=end: evaluate(data, day, zone, now)['state']
        self.assertEqual(check(rows), 'raised')
        self.assertEqual(check(rows, end-1), 'pending')
        self.assertEqual(check([]), 'unknown')
        self.assertEqual(check(rows[10:]), 'unknown')
        self.assertEqual(check(rows[:100]+rows[110:]), 'unknown')
        self.assertEqual(check(rows+[{'ts': end, 'motor_freq_hz': 0}]), 'raised')
        self.assertEqual(check(rows+[{'ts': start, 'motor_freq_hz': 0}]), 'passed')
        self.assertEqual(check([{'ts': start+1000, 'motor_freq_hz': 0}]), 'passed')
