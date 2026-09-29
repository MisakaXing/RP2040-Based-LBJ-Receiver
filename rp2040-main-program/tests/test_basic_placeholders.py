import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_receiver():
    machine = types.ModuleType("machine")
    rp2 = types.ModuleType("rp2")
    rp2.PIO = types.SimpleNamespace(SHIFT_LEFT=0, JOIN_RX=0)
    rp2.asm_pio = lambda **_kwargs: lambda function: function
    spec = importlib.util.spec_from_file_location("receiver_basic_test", ROOT / "lbj_receiver.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"machine": machine, "rp2": rp2}):
        spec.loader.exec_module(module)
    return module.LBJReceiver


class ParserHarness:
    LBJ_BLOCK_LEN = 47

    def __init__(self, receiver_class, block_start=-1):
        self.receiver_class = receiver_class
        self.block_start = block_start
        self._parse_train_data = types.MethodType(receiver_class._parse_train_data, self)
        self._has_usable_basic = types.MethodType(receiver_class._has_usable_basic, self)

    def _find_lbj_block(self, _message):
        return self.block_start

    def _parse_basic(self, message):
        return self.receiver_class._parse_basic(self, message)

    def _parse_ext(self, _message):
        return {"loco_type": "HXD3D-0324"}


class BasicPlaceholderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.receiver_class = load_receiver()

    def test_speed_or_km_only_is_a_basic_record(self):
        parser = ParserHarness(self.receiver_class)
        for message, speed, km in (
            ("--- 7 ---", "7", "---"),
            ("--- --- 7", "---", 0.7),
            ("---- 7 ---", "7", "---"),
            ("- 7 ---", "7", "---"),
            ("7 --- ----", "---", "---"),
        ):
            with self.subTest(message=message):
                result = parser._parse_train_data(message)
                self.assertEqual(result["type"], "basic_only")
                self.assertEqual(result["basic"]["train_no"], "7" if message.startswith("7 ") else "---")
                self.assertEqual(result["basic"]["speed_kmh"], speed)
                self.assertEqual(result["basic"]["km_post"], km)
                self.assertTrue(result["basic"]["partial"])

    def test_measurement_survives_same_message_extension(self):
        prefix = "--- 7 --- "
        parser = ParserHarness(self.receiver_class, len(prefix))
        result = parser._parse_train_data(prefix + "0" * 47)
        self.assertEqual(result["type"], "train_data_full")
        self.assertEqual(result["basic"]["speed_kmh"], "7")

    def test_empty_and_corrupt_placeholders_stay_rejected(self):
        parser = ParserHarness(self.receiver_class)
        for message in ("--- --- ---", "--- ---- -----", "- - ----"):
            with self.subTest(message=message):
                result = parser._parse_train_data(message)
                self.assertEqual(result["type"], "basic_only")
                self.assertTrue(result["basic"]["placeholder"])
        for message in ("--- BAD ---", "--- 501 ---",
                        "--- --- 1000001", "0D139 7 ---"):
            with self.subTest(message=message):
                self.assertEqual(parser._parse_train_data(message)["type"], "unknown")


if __name__ == "__main__":
    unittest.main()
