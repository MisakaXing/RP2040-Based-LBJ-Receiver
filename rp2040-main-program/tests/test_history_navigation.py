import ast
import pathlib
import unittest

from history_store import APPEND_OK, storage_write_due


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"


class FakeTime:
    def __init__(self):
        self.now = 0

    def ticks_ms(self):
        return self.now

    @staticmethod
    def ticks_add(value, delta):
        return value + delta

    @staticmethod
    def ticks_diff(value, reference):
        return value - reference

    def sleep_ms(self, duration):
        self.now += duration


class FakePin:
    def __init__(self, value=1):
        self.level = value

    def value(self):
        return self.level


class FakeBuzzer:
    def __init__(self):
        self.values = []
        self.level = 0

    def value(self, level=None):
        if level is None:
            return self.level
        self.level = level
        self.values.append(level)


class FakeTimer:
    ONE_SHOT = 0

    def __init__(self):
        self.callback = None
        self.init_calls = 0

    def init(self, period, mode, callback):
        self.period = period
        self.mode = mode
        self.callback = callback
        self.init_calls += 1

    def deinit(self):
        self.callback = None

    def fire(self):
        callback = self.callback
        self.callback = None
        callback(self)


class FakeMachine:
    Timer = FakeTimer


class FakeQueue:
    def __init__(self, items=None):
        self.items = list(items or [])

    def __len__(self):
        return len(self.items)

    def peek(self):
        return self.items[0] if self.items else None

    def get(self):
        return self.items.pop(0) if self.items else None

    def clear(self):
        self.items.clear()


def navigation_namespace():
    tree = ast.parse(MAIN_PATH.read_text())
    wanted_assignments = {
        "BUTTON_REPEAT_DELAY_MS",
        "BUTTON_REPEAT_INTERVAL_MS",
        "BUTTON_BEEP_MS",
        "HISTORY_BEEP_GAP_MS",
        "HISTORY_REPEAT_DELAY_MS",
        "HISTORY_REPEAT_INTERVAL_MS",
    }
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & wanted_assignments:
                body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "ButtonTracker":
            body.append(node)
        elif (
            isinstance(node, ast.FunctionDef)
            and node.name in (
                "_buzzer_timer_off",
                "_cancel_buzzer_timer",
                "history_navigation_beep",
                "set_history_navigation_mode",
            )
        ):
            body.append(node)
    namespace = {"time": FakeTime(), "machine": FakeMachine}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MAIN_PATH), "exec"), namespace)
    return namespace


class HistoryNavigationTests(unittest.TestCase):
    def test_history_repeat_profile_is_faster_and_restores_defaults(self):
        namespace = navigation_namespace()
        tracker_class = namespace["ButtonTracker"]
        up = tracker_class(FakePin(), repeat=True)
        down = tracker_class(FakePin(), repeat=True)
        namespace["up_button"] = up
        namespace["down_button"] = down

        namespace["set_history_navigation_mode"](True, now=100)
        self.assertEqual((up.repeat_delay_ms, up.repeat_interval_ms), (220, 45))
        self.assertEqual((down.repeat_delay_ms, down.repeat_interval_ms), (220, 45))
        self.assertEqual(up.repeat_at, 320)

        namespace["set_history_navigation_mode"](
            False, suppress_until_release=True, now=200
        )
        self.assertEqual((up.repeat_delay_ms, up.repeat_interval_ms), (350, 120))
        self.assertEqual((down.repeat_delay_ms, down.repeat_interval_ms), (350, 120))
        self.assertEqual(up.repeat_at, 550)

    def test_held_history_key_is_suppressed_until_debounced_release(self):
        namespace = navigation_namespace()
        tracker_class = namespace["ButtonTracker"]
        fake_time = namespace["time"]
        pin = FakePin(1)
        tracker = tracker_class(pin, repeat=True)

        pin.level = 0
        self.assertFalse(tracker.poll(0))
        self.assertTrue(tracker.poll(12))
        tracker.set_repeat_profile(350, 120, 20, suppress_until_release=True)
        self.assertFalse(tracker.poll(500))

        pin.level = 1
        self.assertFalse(tracker.poll(501))
        self.assertFalse(tracker.poll(513))
        self.assertFalse(tracker.suppress_until_release)

        pin.level = 0
        fake_time.now = 514
        self.assertFalse(tracker.poll(514))
        self.assertTrue(tracker.poll(526))
        self.assertEqual(tracker.repeat_at, 876)

    def test_history_emits_a_separate_timed_pulse_for_every_page(self):
        namespace = navigation_namespace()
        buzzer = FakeBuzzer()
        timer = FakeTimer()
        namespace["cfg_buzzer"] = True
        namespace["buzzer"] = buzzer
        namespace["buzzer_timer"] = timer
        namespace["buzzer_off_at"] = None

        namespace["history_navigation_beep"]()
        self.assertEqual(buzzer.values, [1])
        self.assertEqual(timer.init_calls, 1)
        self.assertEqual(timer.period, 25)
        timer.fire()
        self.assertEqual(buzzer.values, [1, 0])

        namespace["history_navigation_beep"]()
        self.assertEqual(buzzer.values, [1, 0, 1])
        self.assertEqual(timer.init_calls, 2)

    def test_history_redraw_does_not_resample_full_hardware_bar(self):
        tree = ast.parse(MAIN_PATH.read_text())
        functions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        show_history = functions["show_history_index"]
        called_names = {
            node.func.id
            for node in ast.walk(show_history)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("draw_history_rssi", called_names)
        self.assertNotIn("draw_hardware_bar", called_names)

        for function_name in ("draw_menu", "move_menu_selection"):
            used_names = {
                node.id for node in ast.walk(functions[function_name])
                if isinstance(node, ast.Name)
            }
            self.assertNotIn("HISTORY_REPEAT_DELAY_MS", used_names)
            self.assertNotIn("HISTORY_REPEAT_INTERVAL_MS", used_names)

    def test_menu_keeps_page_number_without_scroll_arrows(self):
        tree = ast.parse(MAIN_PATH.read_text())
        draw_menu = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "draw_menu"
        )
        byte_literals = {
            node.value for node in ast.walk(draw_menu)
            if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
        }
        self.assertNotIn(b"^", byte_literals)
        self.assertNotIn(b"v", byte_literals)
        string_literals = {
            node.value for node in ast.walk(draw_menu)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertTrue(any("SYSTEM MENU" in value for value in string_literals))

    def test_web_snapshot_is_not_gated_by_internal_history_compaction(self):
        tree = ast.parse(MAIN_PATH.read_text())
        process_ui = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "process_ui_data"
        )

        def is_set_latest(call):
            return (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "set_latest"
            )

        latest_calls = [node for node in ast.walk(process_ui) if is_set_latest(node)]
        self.assertEqual(len(latest_calls), 1)
        for conditional in ast.walk(process_ui):
            if not isinstance(conditional, ast.If):
                continue
            test_names = {
                node.id for node in ast.walk(conditional.test)
                if isinstance(node, ast.Name)
            }
            if "compact_record" not in test_names:
                continue
            self.assertFalse(any(is_set_latest(node) for node in ast.walk(conditional)))

    def test_continuous_radio_event_is_eventually_written_by_main_service(self):
        tree = ast.parse(MAIN_PATH.read_text())
        service = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "service_history_storage"
        )
        fake_time = FakeTime()
        history_queue = FakeQueue([{"d": {"type": "extended_only"}}])
        sd_queue = FakeQueue()
        saved = []
        receiver = type("Receiver", (), {
            "last_word_time": 0,
            "raw_queue": FakeQueue(),
        })()
        namespace = {
            "time": fake_time,
            "storage_write_due": storage_write_due,
            "history_queue": history_queue,
            "sd_log_queue": sd_queue,
            "receiver": receiver,
            "last_storage_write": 0,
            "storage_pending_since": None,
            "storage_turn": 0,
            "history_write_retries": 0,
            "storage_forced_writes": 0,
            "history_dropped": 0,
            "sd_dropped": 0,
            "storage_errors": 0,
            "HISTORY_RADIO_QUIET_MS": 100,
            "STORAGE_MAX_DEFER_MS": 1500,
            "STORAGE_HARD_DEFER_MS": 5000,
            "STORAGE_WRITE_GAP_MS": 80,
            "MAX_STORAGE_RETRIES": 3,
            "APPEND_OK": APPEND_OK,
            "APPEND_FULL": 0,
            "APPEND_NO_SPACE": -3,
            "APPEND_INVALID": -2,
            "system_state": "DASHBOARD",
            "RED": 1,
            "current_status": b"",
            "current_status_color": 0,
            "history_store": type("Store", (), {
                "last_error": "", "read_only": False, "index_complete": True,
            })(),
            "save_history": lambda record: saved.append(record) or APPEND_OK,
            "log_to_sd": lambda record: True,
            "update_top_bar": lambda: None,
        }
        exec(
            compile(ast.Module(body=[service], type_ignores=[]), str(MAIN_PATH), "exec"),
            namespace,
        )

        fake_time.now = 100
        receiver.last_word_time = 99
        namespace["service_history_storage"](100)
        self.assertEqual(len(history_queue), 1)

        fake_time.now = 1600
        receiver.last_word_time = 1599
        namespace["service_history_storage"](1600)
        self.assertEqual(len(history_queue), 0)
        self.assertEqual(len(saved), 1)
        self.assertEqual(namespace["storage_forced_writes"], 1)
        self.assertIsNone(namespace["storage_pending_since"])

    def test_extended_only_history_page_has_explicit_header(self):
        tree = ast.parse(MAIN_PATH.read_text())
        functions = {
            node.name: node for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        display = functions["display_train_data"]
        argument_names = [argument.arg for argument in display.args.args]
        self.assertIn("record_type", argument_names)
        self.assertTrue(any(
            isinstance(node, ast.Constant) and node.value == "EXT ONLY"
            for node in ast.walk(display)
        ))
        show_history_source = ast.unparse(functions["show_history_index"])
        self.assertIn("data.get('type', '')", show_history_source)

    def test_history_page_beeps_after_validation_and_before_tft_draw(self):
        tree = ast.parse(MAIN_PATH.read_text())
        show_history = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "show_history_index"
        )
        self.assertIn(
            "announce", [argument.arg for argument in show_history.args.args]
        )
        calls = {
            node.func.id: node.lineno
            for node in ast.walk(show_history)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in (
                "load_history_entry",
                "history_navigation_beep",
                "display_train_data",
            )
        }
        self.assertLess(
            calls["load_history_entry"], calls["history_navigation_beep"]
        )
        self.assertLess(
            calls["history_navigation_beep"], calls["display_train_data"]
        )

        source = MAIN_PATH.read_text()
        self.assertEqual(source.count("announce=True"), 4)

    def test_history_inactivity_returns_to_dashboard_after_20_seconds(self):
        tree = ast.parse(MAIN_PATH.read_text())
        timeout_nodes = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_source = ast.unparse(node.test)
            if (
                "system_state == 'HISTORY'" in test_source
                and "history_last_input" in test_source
                and "20000" in test_source
            ):
                timeout_nodes.append(node)
        self.assertEqual(len(timeout_nodes), 1)
        timeout_node = timeout_nodes[0]
        body_source = "\n".join(ast.unparse(node) for node in timeout_node.body)
        self.assertIn("system_state = 'DASHBOARD'", body_source)
        self.assertIn("set_history_navigation_mode(False", body_source)
        self.assertIn("suppress_until_release=True", body_source)
        self.assertIn("draw_ui_skeleton()", body_source)

        process_ui = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "process_ui_data"
        )
        process_names = {
            node.id for node in ast.walk(process_ui) if isinstance(node, ast.Name)
        }
        self.assertNotIn("history_last_input", process_names)


if __name__ == "__main__":
    unittest.main()
