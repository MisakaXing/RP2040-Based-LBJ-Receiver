import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_receiver_module():
    machine = types.ModuleType("machine")
    rp2 = types.ModuleType("rp2")
    rp2.PIO = types.SimpleNamespace(
        SHIFT_LEFT=0,
        JOIN_RX=0,
    )
    rp2.asm_pio = lambda **_kwargs: lambda function: function
    spec = importlib.util.spec_from_file_location(
        "lbj_receiver_parser_under_test", PROJECT_ROOT / "lbj_receiver.py"
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules, {"machine": machine, "rp2": rp2}
    ):
        spec.loader.exec_module(module)
    return module


class ParserHarness:
    LBJ_BLOCK_LEN = 47

    def __init__(self, receiver_class, basic):
        self._parse_train_data = types.MethodType(
            receiver_class._parse_train_data, self
        )
        self.basic = basic

    def _find_lbj_block(self, _message):
        return 12

    def _parse_basic(self, _message):
        return self.basic

    def _parse_ext(self, _message):
        return {
            "route_hex": "BEA9B9E3CFDF2020",
            "loco_type": "HXD3D-0324",
        }


class BasicOnlyHarness:
    def __init__(self, receiver_class):
        self.receiver_class = receiver_class
        self._parse_train_data = types.MethodType(
            receiver_class._parse_train_data, self
        )

    def _find_lbj_block(self, _message):
        return -1

    def _parse_basic(self, message):
        return self.receiver_class._parse_basic(self, message)


class FullParserHarness(BasicOnlyHarness):
    LBJ_BLOCK_LEN = 47

    def _find_lbj_block(self, _message):
        return len("57721 --- --- ")

    def _parse_ext(self, _message):
        return {
            "route_hex": "BEA9B9E3CFDF2020",
            "loco_type": "HXD3D-0324",
        }


class LBJReceiverParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_receiver_module()

    def test_placeholder_basic_plus_extension_is_extended_only(self):
        parser = ParserHarness(self.module.LBJReceiver, {
            "train_no": "---",
            "speed_kmh": "---",
            "km_post": "---",
            "placeholder": True,
        })
        result = parser._parse_train_data("--- --- --- " + "0" * 47)
        self.assertEqual(result["type"], "extended_only")
        self.assertNotIn("basic", result)
        self.assertEqual(result["extended"]["loco_type"], "HXD3D-0324")

    def test_numeric_basic_plus_extension_remains_full(self):
        parser = FullParserHarness(self.module.LBJReceiver)
        result = parser._parse_train_data("57721 --- --- " + "0" * 47)
        self.assertEqual(result["type"], "train_data_full")
        self.assertEqual(result["basic"]["train_no"], "57721")
        self.assertEqual(result["basic"]["speed_kmh"], "---")
        self.assertEqual(result["basic"]["km_post"], "---")
        self.assertTrue(result["basic"]["partial"])

    def test_numeric_train_is_kept_when_both_measurements_are_missing(self):
        parser = BasicOnlyHarness(self.module.LBJReceiver)

        for message in ("57721 --- ---", "57721 ----- -----"):
            with self.subTest(message=message):
                basic = parser._parse_basic(message)
                self.assertEqual(basic["train_no"], "57721")
                self.assertEqual(basic["speed_kmh"], "---")
                self.assertEqual(basic["km_post"], "---")
                self.assertTrue(basic["partial"])

                result = parser._parse_train_data(message)
                self.assertEqual(result["type"], "basic_only")
                self.assertEqual(result["basic"], basic)

    def test_numeric_train_keeps_each_independent_measurement(self):
        parser = BasicOnlyHarness(self.module.LBJReceiver)
        cases = (
            ("57721 80 ---", "80", "---", True),
            ("57721 --- 1234", "---", 123.4, True),
            ("57721 500 1000000", "500", 100000.0, False),
            ("57721 501 1000001", "---", "---", True),
        )
        for message, speed, km, partial in cases:
            with self.subTest(message=message):
                basic = parser._parse_basic(message)
                self.assertEqual(basic["train_no"], "57721")
                self.assertEqual(basic["speed_kmh"], speed)
                self.assertEqual(basic["km_post"], km)
                self.assertEqual(basic.get("partial", False), partial)

    def test_placeholder_and_invalid_train_are_not_basic_records(self):
        parser = BasicOnlyHarness(self.module.LBJReceiver)

        self.assertEqual(
            parser._parse_train_data("--- --- ---")["type"], "unknown"
        )
        for message in ("ABC --- ---", "123456789 --- ---"):
            with self.subTest(message=message):
                self.assertEqual(parser._parse_basic(message), {})
                self.assertEqual(
                    parser._parse_train_data(message)["type"], "unknown"
                )

    def test_missing_measurements_survive_fragment_merge(self):
        parser = BasicOnlyHarness(self.module.LBJReceiver)
        receiver = object.__new__(self.module.LBJReceiver)
        basic = parser._parse_train_data("57721 --- ---")
        basic["ric"] = "1234000-F1"
        extended = {
            "type": "extended_only",
            "ric": "1234002-F1",
            "extended": {"class_tag": "K", "route_hex": "BEA9B9E3CFDF2020"},
        }

        merged = receiver._merge_lbj_fragments(basic, extended)
        self.assertEqual(merged["type"], "train_data_merged")
        self.assertEqual(merged["basic"]["train_no"], "57721")
        self.assertEqual(merged["basic"]["speed_kmh"], "---")
        self.assertEqual(merged["basic"]["km_post"], "---")
        self.assertEqual(merged["extended"]["class_tag"], "K")


if __name__ == "__main__":
    unittest.main()
