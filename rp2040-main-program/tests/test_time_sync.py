import ast
from pathlib import Path
import unittest

from rtc_ds3231 import DS3231

ROOT = Path(__file__).resolve().parents[1]


class FakeI2C:
    def __init__(self):
        self.registers = bytearray(32)
        self.writes = []

    def readfrom_mem(self, addr, reg, count):
        return bytes(self.registers[reg:reg + count])

    def writeto_mem(self, addr, reg, data):
        self.writes.append((addr, reg, bytes(data)))
        self.registers[reg:reg + len(data)] = data


class TimeSyncTests(unittest.TestCase):
    def namespace(self, model="DS3231"):
        rtc = DS3231.__new__(DS3231)
        rtc.model = model
        rtc.addr = 0x68 if model == "DS3231" else 0x51
        rtc.i2c = FakeI2C()
        rtc.i2c.registers[:7] = bytes([0x58, 0x59, 0x23, 5, 0x18, 9, 0x26])
        ns = dict(rtc=rtc, system_state="DASHBOARD",
                  current_status=b"IDLE", current_status_color=0, YELLOW=1,
                  updates=[], messages=[], need_post_train_gc=False)
        ns["update_top_bar"] = lambda: ns["updates"].append(True)
        ns["print"] = lambda *args: ns["messages"].append(args)
        tree = ast.parse((ROOT / "main.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "process_ui_data")
        exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), ns)
        return ns

    def test_midnight_never_writes_either_rtc(self):
        for model in ("DS3231", "PCF8563"):
            ns = self.namespace(model)
            before = bytes(ns["rtc"].i2c.registers)
            for state in ("DASHBOARD", "HISTORY", "MENU"):
                ns["system_state"] = state
                for _ in range(3):
                    ns["process_ui_data"]({"type":"time_sync", "time":"00:00"})
            self.assertEqual(ns["rtc"].i2c.writes, [])
            self.assertEqual(bytes(ns["rtc"].i2c.registers), before)
            self.assertEqual(ns["updates"], [])
            self.assertEqual(ns["current_status"], b"IDLE")
            self.assertEqual(len(ns["messages"]), 9)
            self.assertFalse(ns["need_post_train_gc"])

    def test_all_other_minutes_still_sync(self):
        for model in ("DS3231", "PCF8563"):
            ns = self.namespace(model)
            bus = ns["rtc"].i2c
            reg = 0 if model == "DS3231" else 2
            for hour in range(24):
                for minute in range(60):
                    if hour == minute == 0:
                        continue
                    bus.writes.clear()
                    ns["process_ui_data"]({"type":"time_sync", "time":f"{hour:02}:{minute:02}"})
                    time_writes = [w for w in bus.writes if w[1] == reg and len(w[2]) == 3]
                    self.assertEqual(len(time_writes), 1)
                    self.assertEqual(ns["rtc"].get_time(), (hour, minute, 0))
            self.assertEqual(len(ns["updates"]), 1439)

    def test_invalid_times_do_not_write(self):
        ns = self.namespace()
        for value in ("24:00", "00:60", "-1:00", "bad", None):
            ns["process_ui_data"]({"type":"time_sync", "time":value})
        self.assertEqual(ns["rtc"].i2c.writes, [])

    def test_manual_midnight_remains_available(self):
        ns = self.namespace()
        self.assertTrue(ns["rtc"].sync_time(0, 0))
        self.assertTrue(ns["rtc"].i2c.writes)

    def test_radio_0000_is_parsed_into_guarded_path(self):
        ns = self.namespace()
        tree = ast.parse((ROOT / "lbj_receiver.py").read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "_parse_train_data")
        exec(compile(ast.Module(body=[node], type_ignores=[]), "lbj_receiver.py", "exec"), ns)
        for raw in ("*0000", "-0000", "  *0000"):
            event = ns["_parse_train_data"](None, raw)
            self.assertEqual(event["time"], "00:00")
            ns["process_ui_data"](event)
        self.assertEqual(ns["rtc"].i2c.writes, [])


if __name__ == "__main__":
    unittest.main()

