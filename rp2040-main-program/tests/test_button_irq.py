"""Production input capture under delayed UI/network work (host simulation)."""
import ast
import pathlib
import unittest
from unittest.mock import patch

from test_history_navigation import navigation_namespace, FakeTime
from wireless_portal import WirelessPortal, SERVICE_BUDGET_MS


MAIN_PATH = pathlib.Path(__file__).resolve().parents[1] / "main.py"


class IRQMachine:
    def __init__(self):
        self.masked = False
        self.guards = 0
        self.pending_pins = set()

    def disable_irq(self):
        previous = self.masked
        self.masked = True
        self.guards += 1
        return previous

    def enable_irq(self, previous):
        self.masked = previous
        if not previous:
            pending, self.pending_pins = self.pending_pins, set()
            for pin in pending:
                pin.handler(pin)


class IRQPin:
    IRQ_FALLING = 1
    IRQ_RISING = 2

    def __init__(self, clock, controller, initial=1):
        self.clock = clock
        self.controller = controller
        self.level = initial
        self.handler = None
        self.hard = None
        self.trigger = None

    def value(self):
        return self.level

    def irq(self, handler=None, trigger=None, hard=False):
        self.handler, self.trigger, self.hard = handler, trigger, hard

    def edge(self, at, level):
        self.clock.now = at
        if level != self.level:
            self.level = level
            if self.handler is not None:
                if self.controller.masked:
                    self.controller.pending_pins.add(self)
                else:
                    self.handler(self)


class WrappingTime(FakeTime):
    MODULUS = 1 << 30

    def ticks_ms(self):
        return self.now % self.MODULUS

    def ticks_add(self, tick, delta):
        return (tick + delta) % self.MODULUS

    def ticks_diff(self, new, old):
        half = self.MODULUS // 2
        return (new - old + half) % self.MODULUS - half


def input_environment(repeat=False, clock=None, initial=1, pin_class=IRQPin):
    ns = navigation_namespace()
    if clock is not None:
        ns["time"] = clock
    controller = IRQMachine()
    ns["machine"] = controller
    clock = ns["time"]
    pin = pin_class(clock, controller, initial=initial)
    tracker = ns["ButtonTracker"](pin, repeat=repeat)
    return ns, clock, controller, pin, tracker


def poll_at(clock, tracker, at):
    clock.now = at
    return tracker.poll(clock.ticks_ms())


class ButtonIRQTests(unittest.TestCase):
    def test_three_thousand_random_pulses_with_delayed_scanning(self):
        import random
        randomizer = random.Random(2350)
        _, clock, controller, pin, tracker = input_environment()
        expected, at = 0, 0
        for _ in range(3000):
            at += randomizer.randint(15, 40)
            duration = randomizer.randint(1, 160)
            pin.edge(at, 0)
            at += duration
            pin.edge(at, 1)
            at += randomizer.randint(15, 1500)
            valid = duration >= tracker.debounce_ms
            expected += int(valid)
            self.assertEqual(poll_at(clock, tracker, at), valid)
            self.assertFalse(poll_at(clock, tracker, at + 1))
            self.assertFalse(controller.masked)
        self.assertEqual(tracker.captured_presses, expected)
        self.assertEqual(tracker.delivered_events, expected)

    def test_requires_hard_irq_on_both_edges(self):
        _, _, _, pin, tracker = input_environment()
        self.assertTrue(tracker.irq_enabled)
        self.assertTrue(pin.hard)
        self.assertEqual(pin.trigger, pin.IRQ_FALLING | pin.IRQ_RISING)

    def test_complete_short_press_survives_one_second_scan_gap(self):
        _, clock, _, pin, tracker = input_environment()
        pin.edge(50, 0)
        pin.edge(150, 1)
        self.assertTrue(tracker.input_pending())
        self.assertTrue(poll_at(clock, tracker, 1000))
        self.assertFalse(poll_at(clock, tracker, 1010))
        self.assertEqual((tracker.captured_presses, tracker.delivered_events), (1, 1))
        self.assertFalse(tracker.raw_pressed)
        self.assertFalse(tracker.stable_pressed)

    def test_all_five_keys_capture_through_ten_second_busy_period(self):
        ns = navigation_namespace()
        clock, controller = ns["time"], IRQMachine()
        ns["machine"] = controller
        pins = [IRQPin(clock, controller) for _ in range(5)]
        trackers = [ns["ButtonTracker"](pin, repeat=(i in (1, 2)))
                    for i, pin in enumerate(pins)]
        for at in range(50, 10000, 1000):
            for pin in pins:
                pin.edge(at, 0)
            for pin in pins:
                pin.edge(at + 100, 1)
        self.assertTrue(all(poll_at(clock, tracker, 10000) for tracker in trackers))
        self.assertTrue(all(not poll_at(clock, tracker, 10020) for tracker in trackers))
        for tracker in trackers:
            self.assertEqual(tracker.captured_presses, 10)
            self.assertEqual(tracker.coalesced_presses, 9)
            self.assertEqual(tracker.delivered_events, 1)

    def test_sub_debounce_pulses_are_rejected(self):
        for duration in (1, 5, 11):
            with self.subTest(duration=duration):
                _, clock, _, pin, tracker = input_environment()
                pin.edge(50, 0)
                pin.edge(50 + duration, 1)
                self.assertFalse(poll_at(clock, tracker, 1000))
        _, clock, _, pin, tracker = input_environment()
        pin.edge(50, 0)
        pin.edge(62, 1)
        self.assertTrue(poll_at(clock, tracker, 1000))

    def test_press_and_release_bounce_produce_one_command(self):
        _, clock, _, pin, tracker = input_environment()
        for at, level in ((1, 0), (3, 1), (6, 0), (10, 1), (13, 0),
                          (100, 1), (103, 0), (107, 1)):
            pin.edge(at, level)
        self.assertTrue(poll_at(clock, tracker, 200))
        self.assertFalse(poll_at(clock, tracker, 300))
        self.assertEqual(tracker.captured_presses, 1)

    def test_held_key_repeat_timing_and_no_duplicate_on_release(self):
        _, clock, _, pin, tracker = input_environment(repeat=True)
        pin.edge(0, 0)
        self.assertFalse(poll_at(clock, tracker, 11))
        self.assertTrue(poll_at(clock, tracker, 12))
        self.assertFalse(poll_at(clock, tracker, 361))
        self.assertTrue(poll_at(clock, tracker, 362))
        self.assertFalse(poll_at(clock, tracker, 481))
        self.assertTrue(poll_at(clock, tracker, 482))
        pin.edge(500, 1)
        self.assertFalse(poll_at(clock, tracker, 520))
        self.assertEqual(tracker.captured_presses, 1)

    def test_delayed_hold_does_not_replay_missed_repeats(self):
        _, clock, _, pin, tracker = input_environment(repeat=True)
        pin.edge(50, 0)
        self.assertTrue(poll_at(clock, tracker, 10000))
        self.assertFalse(poll_at(clock, tracker, 10001))
        self.assertFalse(poll_at(clock, tracker, 10349))
        self.assertTrue(poll_at(clock, tracker, 10350))

    def test_menu_ok_and_power_do_not_auto_repeat(self):
        _, clock, _, pin, tracker = input_environment()
        pin.edge(0, 0)
        self.assertTrue(poll_at(clock, tracker, 12))
        for at in (350, 1000, 5000):
            self.assertFalse(poll_at(clock, tracker, at))

    def test_history_repeat_and_exit_suppression_preserved(self):
        _, clock, _, pin, tracker = input_environment(repeat=True)
        tracker.set_repeat_profile(220, 45, 0)
        pin.edge(0, 0)
        self.assertTrue(poll_at(clock, tracker, 12))
        self.assertTrue(poll_at(clock, tracker, 232))
        self.assertTrue(poll_at(clock, tracker, 277))
        tracker.set_repeat_profile(350, 120, 280, suppress_until_release=True)
        self.assertFalse(poll_at(clock, tracker, 1000))
        pin.edge(1001, 1)
        self.assertFalse(poll_at(clock, tracker, 1013))
        self.assertFalse(tracker.suppress_until_release)
        pin.edge(1100, 0)
        self.assertTrue(poll_at(clock, tracker, 1112))

    def test_history_exit_clears_buffered_direction_press(self):
        _, clock, _, pin, tracker = input_environment(repeat=True)
        pin.edge(50, 0)
        pin.edge(150, 1)
        tracker.set_repeat_profile(350, 120, 200, suppress_until_release=True)
        self.assertFalse(poll_at(clock, tracker, 201))
        self.assertFalse(poll_at(clock, tracker, 1000))
        pin.edge(1100, 0)
        pin.edge(1200, 1)
        self.assertTrue(poll_at(clock, tracker, 1300))

    def test_ticks_wrap_does_not_lose_complete_press(self):
        clock = WrappingTime()
        clock.now = clock.MODULUS - 100
        _, clock, _, pin, tracker = input_environment(clock=clock)
        pin.edge(clock.MODULUS - 50, 0)
        pin.edge(clock.MODULUS + 30, 1)
        self.assertTrue(poll_at(clock, tracker, clock.MODULUS + 100))
        self.assertFalse(poll_at(clock, tracker, clock.MODULUS + 110))

    def test_registration_failure_retains_explicit_polling_fallback(self):
        class NoHardPin(IRQPin):
            def irq(self, handler=None, trigger=None, hard=False):
                if hard:
                    raise TypeError("hard IRQ unsupported")
                self.handler = handler
        _, clock, _, pin, tracker = input_environment(pin_class=NoHardPin)
        self.assertFalse(tracker.irq_enabled)
        self.assertIn("unsupported", tracker.irq_error)
        pin.edge(0, 0)
        self.assertFalse(poll_at(clock, tracker, 0))
        self.assertTrue(poll_at(clock, tracker, 12))

    def test_counters_stay_small_and_wrap_without_changing_events(self):
        _, clock, _, pin, tracker = input_environment()
        tracker.irq_edges = tracker.captured_presses = tracker.delivered_events = 65535
        pin.edge(50, 0)
        pin.edge(150, 1)
        self.assertTrue(poll_at(clock, tracker, 200))
        self.assertEqual((tracker.irq_edges, tracker.captured_presses,
                          tracker.delivered_events), (1, 0, 0))

    def test_foreground_guard_is_released(self):
        _, clock, controller, pin, tracker = input_environment()
        pin.edge(50, 0)
        self.assertTrue(poll_at(clock, tracker, 100))
        self.assertFalse(controller.masked)
        tracker.set_repeat_profile(350, 120, 101, suppress_until_release=True)
        self.assertFalse(controller.masked)
        self.assertGreaterEqual(controller.guards, 2)

    def test_release_latched_while_irq_masked_preserves_completed_press(self):
        _, clock, controller, pin, tracker = input_environment()
        pin.edge(50, 0)
        previous = controller.disable_irq()
        pin.edge(150, 1)
        self.assertTrue(poll_at(clock, tracker, 150))
        controller.enable_irq(previous)
        self.assertFalse(poll_at(clock, tracker, 200))
        self.assertEqual(tracker.captured_presses, 1)

    def test_short_release_latched_while_irq_masked_is_still_debounced(self):
        _, clock, controller, pin, tracker = input_environment()
        pin.edge(50, 0)
        previous = controller.disable_irq()
        pin.edge(55, 1)
        self.assertFalse(poll_at(clock, tracker, 55))
        controller.enable_irq(previous)
        self.assertFalse(poll_at(clock, tracker, 100))
        self.assertEqual(tracker.captured_presses, 0)

    def test_network_yield_does_not_starve_a_stable_held_key(self):
        _, clock, _, pin, tracker = input_environment(repeat=True)
        pin.edge(50, 0)
        self.assertTrue(tracker.input_pending())
        self.assertTrue(poll_at(clock, tracker, 62))
        self.assertFalse(tracker.input_pending())
        pin.edge(100, 1)
        self.assertTrue(tracker.input_pending())
        self.assertFalse(poll_at(clock, tracker, 112))
        self.assertFalse(tracker.input_pending())

    def test_startup_held_key_requires_release_before_new_press(self):
        _, clock, _, pin, tracker = input_environment(initial=0)
        self.assertFalse(poll_at(clock, tracker, 100))
        pin.edge(200, 1)
        self.assertFalse(poll_at(clock, tracker, 220))
        pin.edge(300, 0)
        pin.edge(400, 1)
        self.assertTrue(poll_at(clock, tracker, 500))

    def test_irq_path_has_no_allocating_syntax_or_external_work(self):
        tree = ast.parse(MAIN_PATH.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "ButtonTracker")
        methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        allowed = {"_ticks_ms", "_read_pin", "_qualify", "_ticks_diff"}
        for name in ("_capture_edge", "_qualify_state"):
            for node in ast.walk(methods[name]):
                self.assertNotIsInstance(node, (ast.List, ast.Dict, ast.Set, ast.Tuple,
                                               ast.ListComp, ast.DictComp, ast.SetComp))
                if isinstance(node, ast.Call):
                    self.assertIsInstance(node.func, ast.Attribute)
                    self.assertIn(node.func.attr, allowed)

    def test_rf_event_precedes_input_but_input_precedes_optional_work(self):
        tree = ast.parse(MAIN_PATH.read_text())
        loop = next(n for n in tree.body if isinstance(n, ast.While))
        calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)]
        polls = [n for n in calls if isinstance(n.func, ast.Attribute)
                 and n.func.attr == "poll"]
        train = next(n for n in calls if isinstance(n.func, ast.Name)
                     and n.func.id == "process_ui_data")
        self.assertTrue(all(train.lineno < n.lineno for n in polls))
        for name in ("service_history_storage", "service_wifi"):
            call = next(n for n in calls if isinstance(n.func, ast.Name)
                        and n.func.id == name)
            self.assertTrue(all(n.lineno < call.lineno for n in polls))
        first = min(n.lineno for n in polls)
        recent = [n for n in loop.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "now" for t in n.targets)
                  and n.lineno < first]
        self.assertGreater(recent[-1].lineno, next(n.lineno for n in calls
                          if isinstance(n.func, ast.Name) and n.func.id == "service_low_battery"))

    def test_rf_backlog_yields_network_without_consuming_capture_words(self):
        tree = ast.parse(MAIN_PATH.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "network_should_yield")
        from types import SimpleNamespace
        receiver = SimpleNamespace(raw_queue=[], input_pending=lambda: 0)
        env = dict(receiver=receiver, ui_queue=[], buttons_pending=lambda: False)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), env)
        check = env["network_should_yield"]
        self.assertFalse(check())
        receiver.raw_queue.append(1)
        self.assertTrue(check())
        self.assertEqual(receiver.raw_queue, [1])
        receiver.raw_queue.clear()
        env["ui_queue"].append(1)
        self.assertTrue(check())
        env["ui_queue"].clear()
        receiver.input_pending = lambda: 2
        self.assertTrue(check())
        receiver.input_pending = lambda: 1
        self.assertFalse(check())

    def test_menu_wins_over_buffered_confirmation_and_direction(self):
        loop = next(n for n in ast.parse(MAIN_PATH.read_text()).body if isinstance(n, ast.While))
        block = next(n for n in loop.body if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Name) and n.test.id == "menu_event")
        env = dict(menu_event=True, up_event=True, down_event=True, ok_event=True)
        exec(compile(ast.Module(body=[block], type_ignores=[]), "main.py", "exec"), env)
        self.assertEqual([env[k] for k in ("menu_event", "up_event", "down_event", "ok_event")],
                         [True, False, False, False])


class BudgetPortal(WirelessPortal):
    def __init__(self, clock):
        super().__init__("test-password")
        self.clock = clock
        self.calls = []
        self._enabled = True
        self.dns_delay = self.http_delay = 0
        self.dns_action = self.http_action = None
        self._ap = type("AP", (), {"active": lambda _: True})()

    def _service_dns(self):
        self.calls.append("dns")
        if self.dns_action:
            self.dns_action()
        self.clock.now += self.dns_delay

    def _service_http_client(self, now):
        self.calls.append("http")
        if self.http_action:
            self.http_action()
        self.clock.now += self.http_delay

    def _accept_http(self, now):
        self.calls.append("accept")


class NetworkInputBudgetTests(unittest.TestCase):
    def test_inactive_ap_stops_before_resuming_other_phases(self):
        clock = FakeTime()
        clock.now = 6000
        portal = BudgetPortal(clock)
        portal._service_phase = 3
        portal._ap = type("AP", (), {"active": lambda _: False})()
        def stopped(clear_error=False):
            portal._enabled = False
            portal._service_phase = 0
        portal._stop = stopped
        with patch("wireless_portal._ticks_ms", clock.ticks_ms):
            portal.service(clock.now)
        self.assertFalse(portal._enabled)
        self.assertEqual(portal.calls, [])
        self.assertEqual(portal._last_error, "AP became inactive")

    def test_pending_input_yields_without_touching_clients_or_live_record(self):
        clock = FakeTime()
        portal = BudgetPortal(clock)
        owner, live = object(), {"t": "now", "d": {"basic": {"train_no": "7"}}}
        portal._clients = [owner]
        portal.set_latest(live)
        portal.set_input_yield(lambda: True)
        with patch("wireless_portal._ticks_ms", clock.ticks_ms):
            portal.service(0)
        self.assertEqual(portal.calls, [])
        self.assertEqual(portal._clients, [owner])
        self.assertIs(portal._latest_record, live)

    def test_dns_budget_resumes_at_http_instead_of_starving_it(self):
        clock, portal = FakeTime(), None
        portal = BudgetPortal(clock)
        portal.dns_delay = SERVICE_BUDGET_MS
        with patch("wireless_portal._ticks_ms", clock.ticks_ms):
            portal.service(0)
            self.assertEqual(portal.calls, ["dns"])
            self.assertEqual(portal.last_service_ms, SERVICE_BUDGET_MS)
            portal.service(clock.now)
        self.assertEqual(portal.calls, ["dns", "http", "accept", "dns"])

    def test_input_edge_during_dns_defers_http_until_button_is_serviced(self):
        _, clock, _, pin, tracker = input_environment()
        portal = BudgetPortal(clock)
        portal.set_input_yield(tracker.input_pending)
        portal.dns_action = lambda: pin.edge(1, 0)
        with patch("wireless_portal._ticks_ms", clock.ticks_ms):
            portal.service(0)
            self.assertEqual(portal.calls, ["dns"])
            self.assertTrue(poll_at(clock, tracker, 13))
            portal.dns_action = None
            portal.service(13)
        self.assertIn("http", portal.calls)

    def test_short_press_inside_slow_http_is_latched_and_skips_further_network_work(self):
        _, clock, _, pin, tracker = input_environment()
        portal = BudgetPortal(clock)
        portal.set_input_yield(tracker.input_pending)
        portal.http_delay = 500
        def pulse():
            pin.edge(50, 0)
            pin.edge(150, 1)
        portal.http_action = pulse
        with patch("wireless_portal._ticks_ms", clock.ticks_ms):
            portal.service(0)
        self.assertEqual(portal.calls, ["dns", "http"])
        self.assertEqual(portal.max_service_ms, 650)
        self.assertTrue(poll_at(clock, tracker, clock.now))
        self.assertFalse(poll_at(clock, tracker, clock.now + 1))

    def test_network_metrics_record_failure_without_swallowing_it(self):
        clock = FakeTime()
        portal = BudgetPortal(clock)
        def failure():
            clock.now += 20
            raise OSError("injected DNS failure")
        portal.dns_action = failure
        with patch("wireless_portal._ticks_ms", clock.ticks_ms):
            with self.assertRaises(OSError):
                portal.service(0)
        self.assertEqual(portal.max_service_ms, 20)


if __name__ == "__main__":
    unittest.main()
