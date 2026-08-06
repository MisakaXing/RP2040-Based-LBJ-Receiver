import importlib.util
import pathlib
import sys
import types
import unittest


class _FakePIO:
    SHIFT_LEFT = 0
    JOIN_RX = 1


fake_rp2 = types.ModuleType("rp2")
fake_rp2.PIO = _FakePIO
fake_rp2.asm_pio = lambda **_kwargs: (lambda func: func)
fake_rp2.StateMachine = object
sys.modules.setdefault("rp2", fake_rp2)
sys.modules.setdefault("machine", types.ModuleType("machine"))

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "standalone_receiver_test"
    / "lbj_receiver.py"
)
spec = importlib.util.spec_from_file_location(
    "lbj_receiver_standalone_under_test", MODULE_PATH
)
lbj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lbj)


class FakeClock:
    now = 0

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_add(value, delta):
        return value + delta


class StandaloneFragmentTests(unittest.TestCase):
    def setUp(self):
        FakeClock.now = 0
        lbj.time = FakeClock

    def make_receiver(self):
        receiver = lbj.LBJReceiver.__new__(lbj.LBJReceiver)
        receiver.pending_fragments = []
        receiver.callback_errors = 0
        receiver.callback = None
        return receiver

    @staticmethod
    def extended(group_id=7):
        return {
            "type": "extended_only",
            "ric": "1234002-F3",
            "_rx_group": group_id,
            "rssi": "-90.0dBm",
            "raw": "extended",
            "extended": {"loco_type": "HXD3D-0097"},
        }

    def test_unmatched_extension_emits_immediately(self):
        receiver = self.make_receiver()
        emitted = []
        receiver.set_callback(emitted.append)

        receiver._handle_parsed_msg(self.extended())

        self.assertEqual([msg["type"] for msg in emitted], ["extended_only"])
        self.assertEqual(len(receiver.pending_fragments), 0)

    def test_basic_still_waits_and_merges_with_following_extension(self):
        receiver = self.make_receiver()
        emitted = []
        receiver.set_callback(emitted.append)
        basic = {
            "type": "basic_only",
            "ric": "1234000-F1",
            "_rx_group": 7,
            "rssi": "-91.0dBm",
            "raw": "153 313 1233",
            "basic": {"train_no": "153", "speed_kmh": "313"},
        }

        receiver._handle_parsed_msg(basic)
        self.assertEqual(emitted, [])
        self.assertEqual(len(receiver.pending_fragments), 1)

        FakeClock.now = 100
        receiver._handle_parsed_msg(self.extended())

        self.assertEqual([msg["type"] for msg in emitted], ["train_data_merged"])
        self.assertEqual(emitted[0]["basic"]["train_no"], "153")
        self.assertEqual(emitted[0]["extended"]["loco_type"], "HXD3D-0097")
        self.assertEqual(len(receiver.pending_fragments), 0)

    def test_extension_before_basic_is_not_delayed_or_false_merged(self):
        receiver = self.make_receiver()
        emitted = []
        receiver.set_callback(emitted.append)

        receiver._handle_parsed_msg(self.extended())
        receiver._handle_parsed_msg({
            "type": "basic_only",
            "ric": "1234000-F1",
            "_rx_group": 7,
            "raw": "153 313 1233",
            "basic": {"train_no": "153"},
        })

        self.assertEqual([msg["type"] for msg in emitted], ["extended_only"])
        self.assertEqual(len(receiver.pending_fragments), 1)
        self.assertEqual(receiver.pending_fragments[0][1]["type"], "basic_only")


if __name__ == "__main__":
    unittest.main()
