import ast
import pathlib
import unittest


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


class FakePin:
    def __init__(self):
        self.pressed = False

    def value(self):
        return 0 if self.pressed else 1


def load_button_tracker():
    main_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "rp2040-main-program"
        / "main.py"
    )
    tree = ast.parse(main_path.read_text(encoding="utf-8"), filename=str(main_path))
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ButtonTracker"
    )
    module = ast.fix_missing_locations(ast.Module(body=[class_node], type_ignores=[]))
    namespace = {"time": FakeClock}
    exec(compile(module, str(main_path), "exec"), namespace)
    return namespace["ButtonTracker"]


ButtonTracker = load_button_tracker()


class ButtonTrackerTests(unittest.TestCase):
    def setUp(self):
        FakeClock.now = 0
        self.pin = FakePin()

    def poll_at(self, tracker, milliseconds):
        FakeClock.now = milliseconds
        return tracker.poll(milliseconds)

    def test_short_press_fires_once_after_debounce(self):
        tracker = ButtonTracker(self.pin)
        self.pin.pressed = True
        self.assertFalse(self.poll_at(tracker, 1))
        self.assertFalse(self.poll_at(tracker, 12))
        self.assertTrue(self.poll_at(tracker, 13))
        self.assertFalse(self.poll_at(tracker, 30))
        self.pin.pressed = False
        self.assertFalse(self.poll_at(tracker, 31))
        self.assertFalse(self.poll_at(tracker, 50))

    def test_long_press_repeats_without_event_after_release(self):
        tracker = ButtonTracker(
            self.pin, repeat=True, debounce_ms=12,
            repeat_delay_ms=350, repeat_interval_ms=120,
        )
        self.pin.pressed = True
        self.assertFalse(self.poll_at(tracker, 1))
        self.assertTrue(self.poll_at(tracker, 13))
        self.assertFalse(self.poll_at(tracker, 362))
        self.assertTrue(self.poll_at(tracker, 363))
        self.pin.pressed = False
        self.assertFalse(self.poll_at(tracker, 483))
        self.assertFalse(self.poll_at(tracker, 500))


if __name__ == "__main__":
    unittest.main()
