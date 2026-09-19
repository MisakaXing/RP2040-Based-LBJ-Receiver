import ast
from pathlib import Path
import unittest

from rtc_ds3231 import DS3231, format_history_time


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

    def test_save_and_queue_capture_time(self):
        import array
        import json
        import tempfile
        from collections import deque
        from types import SimpleNamespace
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        tree = ast.parse(source)
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("save_history", "queue_history", "load_history_entry", "service_history_storage")]
        class Queue:
            def __init__(self):
                self.items = deque()
            def __len__(self):
                return len(self.items)
            def put(self, value):
                self.items.append(value)
            def get(self):
                return self.items.popleft() if self.items else None
        with tempfile.TemporaryDirectory() as directory:
            stamp = ["2026-09-19 23:59"]
            ns = dict(json=json, total_count=0, MAX_HIST=9999,
                      HISTORY_QUEUE_CAPACITY=8, history_queue=Queue(),
                      sd_log_queue=Queue(), HISTORY_RADIO_QUIET_MS=0,
                      STORAGE_WRITE_GAP_MS=0, last_storage_write=0,
                      receiver=SimpleNamespace(last_word_time=0, raw_queue=[]),
                      time=SimpleNamespace(ticks_diff=lambda a,b:a-b),
                      history_offsets=array.array("I"),
                      HIST_FILE=str(Path(directory) / "history.jsonl"),
                      rtc=SimpleNamespace(get_history_time_str=lambda:stamp[0]))
            exec(compile(ast.Module(body=funcs, type_ignores=[]), str(Path(directory)), "exec"), ns)
            data = {"type": "basic_only", "basic": {"train_no": "57721"}}
            self.assertTrue(ns["queue_history"](data))
            stamp[0] = "2026-09-20 00:00"
            ns["service_history_storage"](1000)
            self.assertEqual(ns["load_history_entry"](0), {"t":"2026-09-19 23:59", "d":data})
            self.assertEqual(data, {"type": "basic_only", "basic": {"train_no": "57721"}})
            self.assertTrue(ns["save_history"](data))
            self.assertEqual(ns["load_history_entry"](1)["t"], stamp[0])
            ns["total_count"] = 9999
            self.assertFalse(ns["queue_history"](data))
            self.assertFalse(ns["save_history"](data))

    def test_main_wires_history_only(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        ast.parse(source)
        self.assertIn('Program_ver = 5.4', source)
        self.assertIn('{format_history_time(hist_time)}', source)


if __name__ == "__main__":
    unittest.main()
