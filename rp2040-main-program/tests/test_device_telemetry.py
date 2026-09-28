import ast
import json
import pathlib
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wireless_portal import MAX_SSE_EVENT, MODE_SSE, WirelessPortal
from test_wireless_portal import StreamClient


ROOT = pathlib.Path(__file__).resolve().parents[1]


class DeviceTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.portal = WirelessPortal("test-password")

    def test_thresholds_and_unknown_values(self):
        for battery, temp, low, hot in (
            (19, 45.1, True, True), (20, 45, False, False),
            (0, 50, True, True), (100, 30, False, False),
            (None, None, False, False), (float("nan"), float("inf"), False, False),
            (-1, -101, False, False), (101, 201, False, False),
            ("ERR", "ERR", False, False),
        ):
            with self.subTest(battery=battery, temp=temp):
                self.portal.set_device_status(battery, temp)
                device = self.portal._view_model()["device"]
                self.assertEqual(device["low_battery"], low)
                self.assertEqual(device["high_temperature"], hot)
                json.dumps(device, allow_nan=False)

    def test_initial_page_and_warning_recovery(self):
        self.assertFalse(self.portal._view_model()["available"])
        self.assertIn("等待有效采样", self.portal._render_page())
        self.portal.set_device_status(19, 45.1)
        page = self.portal._render_page()
        self.assertIn('class="metric danger" id=batteryCard', page)
        self.assertIn('class="metric danger" id=tempCard', page)
        self.assertIn("<strong id=battery>19%</strong>", page)
        self.assertIn("<strong id=temperature>45.1°C</strong>", page)
        self.portal.set_device_status(20, 45)
        page = self.portal._render_page()
        self.assertNotIn('class="metric danger"', page)
        self.assertIn("随列车信息更新", page)

    def test_telemetry_alone_does_not_send_sse(self):
        client = StreamClient()
        self.portal._clients = [[client, 100, None, None, 0, MODE_SSE, 0, 100]]
        self.portal.set_device_status(10, 50)
        self.portal._service_http_client(101)
        self.assertIsNone(self.portal._clients[0][3])
        self.assertEqual(self.portal._latest_revision, 0)
        self.portal.set_latest({"t": "now", "d": {"basic": {"train_no": "57721"}}})
        self.portal._service_http_client(102)
        event = self.portal._clients[0][3]
        self.assertIn(b'"train_no": "57721"', event)
        self.assertIn(b'"battery_percent": 10', event)
        self.assertIn(b'"core_temp_c": 50.0', event)
        self.assertEqual(self.portal._latest_revision, 1)

    def test_sse_and_polling_use_same_payload(self):
        self.portal.set_device_status(15, 48.2)
        self.portal.set_latest({"d": {"type": "extended_only", "extended": {
            "route_hex": "BDF2C9BDCFDF2020", "loco_type": "HXD3C",
            "lon": "116°23.1234'", "lat": "39°54.1234'", "cab_end": "31"}}})
        event = self.portal._sse_event()
        self.assertLessEqual(len(event), MAX_SSE_EVENT)
        sse = json.loads(event.split(b"data: ", 1)[1])
        response = self.portal._build_http_response(b"GET /api/latest HTTP/1.1\r\n\r\n")
        self.assertEqual(sse, json.loads(response.split(b"\r\n\r\n", 1)[1]))

    def test_usb_transition_pushes_without_a_new_train(self):
        self.portal._clients = [[StreamClient(), 100, None, None, 0, MODE_SSE, 0, 100]]
        self.portal.set_device_status(0, 30, True, 3.44)
        self.portal._service_http_client(101)
        event = self.portal._clients[0][3]
        data = json.loads(event.split(b"data: ", 1)[1])
        self.assertEqual(data["update_id"], "0")
        self.assertTrue(data["device"]["usb_power"])
        self.assertIsNone(data["device"]["battery_percent"])
        self.assertFalse(data["device"]["low_battery"])

    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script test")
    def test_browser_script_thresholds_and_shared_fallback(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "portal_device_ui.cjs")],
            input=self.portal._render_page(), text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DeviceSamplingTests(unittest.TestCase):
    def test_old_saved_txpower_cannot_override_fixed_power(self):
        function = next(n for n in ast.parse((ROOT / "main.py").read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == "load_config")
        portal = WirelessPortal("test-password")
        ns = {"_read_config_dict": lambda: {"wifi_enabled": True, "wifi_txpower_dbm": 4},
              "SCR_OFF_OPTS": ("5s", "10s", "30s", "never", "on demand"),
              "PPM_CALIBRATION_VERSION": 1, "menu_items": [""] * 10,
              "wifi_portal": portal, "print": Mock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), ns)
        ns["load_config"]()
        self.assertTrue(ns["cfg_wifi_enabled"])
        self.assertEqual(portal.txpower_dbm, 31)
        ns["print"].assert_not_called()

    def test_hardware_bar_chrg_is_green_and_retains_voltage(self):
        function = next(n for n in ast.parse((ROOT / "main.py").read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == "draw_hardware_bar")
        display = SimpleNamespace(fill_rect=Mock(), draw_gbk=Mock())
        ns = {"last_hw_draw": None, "last_hw_update": 100,
              "time": SimpleNamespace(ticks_ms=lambda: 100), "sample_device_status": Mock(),
              "last_battery_v": "3.4V", "last_battery_p": "0%", "last_temp_str": "30C",
              "last_usb_power": True, "system_state": "DASHBOARD", "last_rssi_str": "N/A",
              "RED": 1, "WHITE": 2, "GREEN": 3, "BLACK": 0, "tft": display}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), ns)
        ns["draw_hardware_bar"]()
        self.assertEqual(display.draw_gbk.call_args_list[0].args, (b"3.4V", 45, 218, 2, 0))
        self.assertEqual(display.draw_gbk.call_args_list[1].args, (b"CHRG", 85, 218, 3, 0))

    def test_top_status_lowbat_suppressed_on_usb(self):
        function = next(node for node in ast.parse((ROOT / "main.py").read_text()).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "draw_battery_top_status")
        display = SimpleNamespace(fill_rect=Mock(), draw_gbk=Mock())
        scope = {
            "low_battery_shutdown": False,
            "last_battery_p": "9%", "current_status": b"READY",
            "current_status_color": 2, "RED": 1, "tft": display, "last_usb_power": False,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), scope)
        scope["draw_battery_top_status"]()
        self.assertEqual(display.draw_gbk.call_args.args[0], b"LOWBAT")
        self.assertEqual(display.draw_gbk.call_args.args[3], 1)
        scope["last_battery_p"] = "10%"
        scope["draw_battery_top_status"]()
        self.assertEqual(display.draw_gbk.call_args.args[0], b"READY")
        scope["last_battery_p"] = "0%"
        scope["last_usb_power"] = True
        scope["draw_battery_top_status"]()
        self.assertEqual(display.draw_gbk.call_args.args[0], b"READY")

    def test_zero_percent_stops_radio_and_turns_off_screen(self):
        function = next(node for node in ast.parse((ROOT / "main.py").read_text()).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "enter_low_battery_shutdown")

        class EndTest(Exception):
            pass

        def sleep(ms):
            if ms == 1000:
                raise EndTest

        state = [0, 0, 0, 0, True, True]
        power = Mock()
        portal = SimpleNamespace(set_enabled=Mock())
        cpu = SimpleNamespace(freq=Mock(side_effect=[None, 18000000]), reset=Mock())
        scope = {
            "low_battery_shutdown": False, "radio_state": state,
            "RADIO_RUNNING": 4, "RADIO_STOPPED": 5,
            "LOW_BATTERY_CPU_HZ": 18000000, "machine": cpu,
            "top_bar_ready": True,
            "last_battery_v": "3.7V", "last_battery_p": "0%",
            "wifi_portal": portal, "stop_buzzer": Mock(),
            "usb_power_present": lambda: False,
            "set_screen_power": power, "tft": SimpleNamespace(fill=Mock(), draw_gbk=Mock()),
            "cfg_buzzer": False, "time": SimpleNamespace(sleep_ms=sleep),
            "draw_battery_top_status": Mock(),
            "BLACK": 0, "RED": 1, "YELLOW": 2, "WHITE": 3, "GRAY": 4,
            "print": Mock(),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), scope)
        with self.assertRaises(EndTest):
            scope["enter_low_battery_shutdown"]()
        self.assertFalse(state[4])
        portal.set_enabled.assert_called_once_with(False)
        self.assertEqual([call.args[0] for call in power.call_args_list], [False])
        cpu.freq.assert_any_call(18000000)
        self.assertTrue(scope["low_battery_shutdown"])

    def test_w_board_usb_vsys_detection(self):
        function = next(node for node in ast.parse((ROOT / "main.py").read_text()).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "usb_power_present")
        readings = iter((31527, 31575, 31543, 22500, 22600, 22400))
        scope = {
            "vsys_adc": SimpleNamespace(read_u16=lambda: next(readings)),
            "VSYS_USB_PRESENT_RAW": 28200,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), scope)
        self.assertTrue(scope["usb_power_present"]())
        self.assertFalse(scope["usb_power_present"]())

    def test_screen_off_usb_power_reboots_after_two_checks(self):
        function = next(node for node in ast.parse((ROOT / "main.py").read_text()).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "enter_low_battery_shutdown")

        class Rebooted(Exception):
            pass

        power = Mock()
        cpu = SimpleNamespace(freq=Mock(return_value=18000000),
                              reset=Mock(side_effect=Rebooted))
        usb_values = iter((False, True, True))
        scope = {
            "low_battery_shutdown": False,
            "radio_state": [0, 0, 0, 0, True, True],
            "RADIO_RUNNING": 4, "RADIO_STOPPED": 5,
            "LOW_BATTERY_CPU_HZ": 18000000, "machine": cpu,
            "last_battery_v": "3.7V", "last_battery_p": "0%",
            "wifi_portal": SimpleNamespace(set_enabled=Mock()),
            "stop_buzzer": Mock(), "set_screen_power": power,
            "cfg_buzzer": False, "time": SimpleNamespace(sleep_ms=Mock()),
            "usb_power_present": lambda: next(usb_values), "print": Mock(),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), scope)
        with self.assertRaises(Rebooted):
            scope["enter_low_battery_shutdown"]()
        self.assertEqual(power.call_args.args, (False,))
        self.assertEqual(cpu.reset.call_count, 1)

    def test_zero_protection_is_immediate_and_usb_inhibits_shutdown(self):
        function = next(n for n in ast.parse((ROOT / "main.py").read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == "service_low_battery")
        power = [False]
        shutdown = Mock()
        ns = {"low_battery_shutdown": False, "last_battery_p": "0%",
              "usb_power_present": lambda: power[0], "enter_low_battery_shutdown": shutdown}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), ns)
        ns["service_low_battery"](0)
        shutdown.assert_called_once()
        shutdown.reset_mock()
        power[0] = True
        ns["service_low_battery"](1)
        shutdown.assert_not_called()
        power[0] = False
        ns["last_battery_p"] = "1%"
        ns["service_low_battery"](2)
        shutdown.assert_not_called()

    def setUp(self):
        self.tree = ast.parse((ROOT / "main.py").read_text())
        functions = [node for node in self.tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("sample_device_status", "get_battery_info")]
        battery_tree = ast.parse((ROOT / "boot_post.py").read_text())
        battery_function = next(node for node in battery_tree.body
                                if isinstance(node, ast.FunctionDef)
                                and node.name == "battery_voltage_from_raw")
        self.ns = {
            "last_hw_update": 0, "last_battery_v": None, "last_battery_p": None,
            "last_temp_str": None, "HW_SAMPLE_INTERVAL_MS": 5000,
            "top_bar_ready": False,
            "system_state": "DASHBOARD",
            "time": SimpleNamespace(ticks_diff=lambda a, b: a - b, sleep_ms=Mock()),
            "sensor_temp": SimpleNamespace(read_u16=Mock(return_value=13200)),
            "bat_en": SimpleNamespace(value=Mock()),
            "bat_adc": SimpleNamespace(read_u16=Mock(return_value=37000)),
            "BATTERY_EMPTY_V": 3.45, "BATTERY_FULL_V": 4.2,
            "wifi_portal": WirelessPortal("test-password"),
            "BATTERY_ADC_GAIN": 1.04, "last_usb_power": False,
            "usb_power_present": Mock(return_value=False),
        }
        exec(compile(ast.Module(body=[battery_function], type_ignores=[]),
                     "boot_post.py", "exec"), self.ns)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), self.ns)

    def test_battery_zero_begins_at_3_45v(self):
        raw = int(3.45 / (6.6 * 1.04) * 65535)
        self.ns["bat_adc"].read_u16.side_effect = [raw - 1000, raw, raw + 1000]
        shown_voltage, shown_percent = self.ns["get_battery_info"]()
        self.assertEqual(shown_voltage, "3.4V")
        self.assertEqual(shown_percent, "0%")
        self.ns["bat_en"].value.assert_called_with(1)

    def test_just_above_cutoff_remains_one_percent(self):
        raw = int(3.451 / (6.6 * 1.04) * 65535) + 1
        self.ns["bat_adc"].read_u16.return_value = raw
        self.assertEqual(self.ns["get_battery_info"]()[1], "1%")

    def test_sampling_usb_status_hides_web_percentage_and_keeps_voltage(self):
        self.ns["usb_power_present"].return_value = True
        self.ns["sample_device_status"](100, force=True)
        device = self.ns["wifi_portal"]._device_view()
        self.assertTrue(device["usb_power"])
        self.assertIsNone(device["battery_percent"])
        self.assertIsNotNone(device["battery_voltage"])
        self.assertFalse(device["low_battery"])
        page = self.ns["wifi_portal"]._render_page()
        self.assertIn("<strong id=battery>CHRG</strong>", page)
        self.assertIn('class="metric charging" id=batteryCard', page)
        self.ns["usb_power_present"].return_value = False
        self.ns["sample_device_status"](200, force=True)
        self.assertIsNotNone(self.ns["wifi_portal"]._device_view()["battery_percent"])

    def test_w_board_gain_matches_meter_reference(self):
        raw = 39737
        self.ns["bat_adc"].read_u16.return_value = raw
        shown_voltage, shown_percent = self.ns["get_battery_info"]()
        self.assertEqual(shown_voltage, "4.2V")
        self.assertGreaterEqual(int(shown_percent[:-1]), 90)
        self.assertAlmostEqual(self.ns["battery_voltage_from_raw"](raw), 4.162, places=2)

    def test_cache_does_not_resample_during_lcd_redraw(self):
        sample = self.ns["sample_device_status"]
        sample(100, force=True)
        sample(105)
        self.assertEqual(self.ns["bat_adc"].read_u16.call_count, 3)
        sample(200, force=True)  # A second train refreshes immediately.
        self.assertEqual(self.ns["bat_adc"].read_u16.call_count, 6)
        sample(5200)  # Low-battery monitoring refreshes every five seconds.
        self.assertEqual(self.ns["bat_adc"].read_u16.call_count, 9)

    def test_failed_adc_is_unknown_and_gate_is_restored(self):
        self.ns["bat_adc"].read_u16.side_effect = OSError("ADC failure")
        self.ns["sensor_temp"].read_u16.side_effect = OSError("ADC failure")
        self.ns["sample_device_status"](100, force=True)
        self.ns["bat_en"].value.assert_called_with(1)
        self.assertEqual(self.ns["last_battery_p"], "---")
        self.assertEqual(self.ns["last_temp_str"], "ERR")
        device = self.ns["wifi_portal"]._view_model()["device"]
        self.assertIsNone(device["battery_percent"])
        self.assertIsNone(device["core_temp_c"])
        self.ns["sample_device_status"](105)
        self.assertEqual(self.ns["bat_adc"].read_u16.call_count, 1)

    def test_train_samples_before_snapshot_without_independent_loop_sampler(self):
        process = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "process_ui_data")
        calls = sorted((n.lineno, ast.unparse(n)) for n in ast.walk(process)
                       if isinstance(n, ast.Call))
        sample = next(line for line, code in calls if code.startswith("sample_device_status("))
        publish = next(line for line, code in calls if code.startswith("wifi_portal.set_latest("))
        self.assertLess(sample, publish)
        loop = [n for n in self.tree.body if isinstance(n, ast.While)][-1]
        self.assertIn("sample_device_status(now)", ast.unparse(loop))


if __name__ == "__main__":
    unittest.main()
