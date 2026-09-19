import ast
from pathlib import Path
import unittest

from rtc_ds3231 import DS3231, format_history_time
from history_store import make_history_record


class SnapshotI2C:
    def __init__(self, data):
        self.data = bytes(data)
        self.calls = []

    def readfrom_mem(self, addr, reg, count):
        self.calls.append((addr, reg, count))
        return self.data


class HistoryTimeTests(unittest.TestCase):
    def rtc(self, model, data):
        rtc = DS3231.__new__(DS3231)
        rtc.model = model
        rtc.addr = 0x68 if model == "DS3231" else 0x51
        rtc.i2c = SnapshotI2C(data)
        return rtc

    def test_snapshot_both_chips(self):
        for model, data, reg in (
            ("DS3231", [0x59, 0x34, 0x12, 6, 0x19, 9, 0x26], 0),
            ("PCF8563", [0x59, 0x34, 0x12, 0x19, 6, 9, 0x26], 2),
        ):
            rtc = self.rtc(model, data)
            self.assertEqual(rtc.get_history_time_str(), "2026-09-19 12:34")
            self.assertEqual(rtc.i2c.calls, [(rtc.addr, reg, 7)])

    def test_twelve_hour_and_midnight(self):
        for hour, expected in ((0x52, "00"), (0x72, "12"), (0x61, "13")):
            rtc = self.rtc("DS3231", [0, 0, hour, 1, 1, 1, 0x27])
            self.assertEqual(rtc.get_history_time_str(), "2027-01-01 " + expected + ":00")

    def test_invalid_or_missing_rtc(self):
        rtc = self.rtc("PCF8563", [0, 0x6A, 0, 0, 0, 0, 0])
        self.assertEqual(rtc.get_history_time_str(), "----/--/-- --:--")
        rtc.i2c.data = b""
        self.assertEqual(rtc.get_history_time_str(), "----/--/-- --:--")
        rtc.model = "UNKNOWN"
        self.assertEqual(rtc.get_history_time_str(), "----/--/-- --:--")

    def test_legacy_and_new_display(self):
        for source, expected in (
            ("12:34:59", "----/--/-- 12:34"),
            ("2026-09-19 12:34:59", "2026-09-19 12:34"),
            ("2026-09-19 12:34", "2026-09-19 12:34"),
            (None, "----/--/-- --:--"),
        ):
            self.assertEqual(format_history_time(source), expected)
            for page in ("HISTORY", "EXT ONLY"):
                header = f"{page} [9999/9999]  {expected}"
                self.assertLessEqual(5 + len(header) * 8, 320)

    def test_record_types_keep_date(self):
        for data in (
            {"type": "basic_only", "basic": {"train_no": "57721"}},
            {"type": "full", "basic": {"train_no": "57721"}},
            {"type": "extended_only", "extended": {"loco_type": "TEST"}},
        ):
            record = make_history_record("2026-09-19 12:34", data)
            self.assertEqual(record["t"], "2026-09-19 12:34")

    def test_main_wires_history_only(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        ast.parse(source)
        self.assertIn("make_history_record(rtc.get_history_time_str(), data)", source)
        self.assertIn("received_record = {\"t\": received_at, \"d\": data}", source)
        self.assertIn("{format_history_time(hist_time)}", source)


if __name__ == "__main__":
    unittest.main()
