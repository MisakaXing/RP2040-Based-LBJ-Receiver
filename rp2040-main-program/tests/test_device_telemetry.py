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

    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser-script test")
    def test_browser_script_thresholds_and_shared_fallback(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "portal_device_ui.cjs")],
            input=self.portal._render_page(), text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DeviceSamplingTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse((ROOT / "main.py").read_text())
        functions = [node for node in self.tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("sample_device_status", "get_battery_info")]
        self.ns = {
            "last_hw_update": 0, "last_battery_v": None, "last_battery_p": None,
            "last_temp_str": None, "HW_SAMPLE_INTERVAL_MS": 30000,
            "time": SimpleNamespace(ticks_diff=lambda a, b: a - b, sleep_ms=Mock()),
            "sensor_temp": SimpleNamespace(read_u16=Mock(return_value=13200)),
            "bat_en": SimpleNamespace(value=Mock()),
            "bat_adc": SimpleNamespace(read_u16=Mock(return_value=37000)),
            "BAT_OFFSET": 0.174, "wifi_portal": WirelessPortal("test-password"),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), self.ns)

    def test_cache_does_not_resample_during_lcd_redraw(self):
        sample = self.ns["sample_device_status"]
        sample(100, force=True)
        sample(105)
        self.assertEqual(self.ns["bat_adc"].read_u16.call_count, 1)
        sample(200, force=True)  # A second train refreshes immediately.
        self.assertEqual(self.ns["bat_adc"].read_u16.call_count, 2)
        sample(30200)  # Preserve the LCD's existing idle sampling.
        self.assertEqual(self.ns["bat_adc"].read_u16.call_count, 3)

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
        self.assertNotIn("sample_device_status", ast.unparse(loop))


if __name__ == "__main__":
    unittest.main()
