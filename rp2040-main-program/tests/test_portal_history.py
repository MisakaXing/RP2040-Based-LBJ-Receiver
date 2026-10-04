import json
import pathlib
import subprocess
import shutil
import ast
from types import SimpleNamespace
import unittest
from wireless_portal import WirelessPortal, MODE_HTTP, MODE_HISTORY
from test_wireless_portal import StreamClient

class Store:
    index_complete = True
    def __init__(self):
        self.records = [
            {"t": "2026-10-04 10:01", "d": {"basic": {"train_no": "7"},
             "extended": {"loco_type": "轨道探伤车-04782", "cab_end": "31",
                          "lon": "116°17.1184' E", "lat": "39°53.6257' N"}}},
            {"t": "2026-10-04 10:02", "d": {"basic": {"train_no": "8"}}}]
        self.loads = []
    @property
    def count(self):
        return len(self.records)
    def load(self, index):
        self.loads.append(index)
        return self.records[index]
    def iter_load(self, index):
        yield None
        yield self.load(index)

class PortalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.portal = WirelessPortal("test-password")
        self.store = Store()
        self.portal.set_history_store(self.store)
        self.portal.set_latest({"t": "now", "d": {"basic": {"train_no": "99"}}})
        self.owner = [StreamClient(), 0, bytearray(), None, 0, MODE_HTTP, -1, 0]
        self.portal._start_sse(self.owner, 0)
        self.portal._clients.append(self.owner)
    def query(self, token=None, index="-1"):
        token = self.portal._history_token if token is None else token
        response = self.portal._build_http_response(
            ("GET /api/history?token=%s&index=%s HTTP/1.1\r\n\r\n" % (token, index)).encode())
        header, body = response.split(b"\r\n\r\n", 1)
        return header.split(b"\r\n", 1)[0], json.loads(body)
    def test_session_token_only_sent_on_owner_stream(self):
        self.assertIn(b"event: session", self.owner[3])
        self.assertNotIn(self.portal._history_token, self.portal._render_page())
        self.assertNotIn(self.portal._history_token, json.dumps(self.portal._view_model()))
    def test_polling_or_wrong_token_cannot_read_flash(self):
        for token in ("", "wrong"):
            status, body = self.query(token)
            self.assertIn(b"403", status)
            self.assertEqual(body["error"], "sse_required")
        self.assertEqual(self.store.loads, [])
    def test_head_never_triggers_synchronous_history_read(self):
        request = ("HEAD /api/history?token=%s HTTP/1.1\r\n\r\n" % self.portal._history_token).encode()
        response = self.portal._build_http_response(request)
        self.assertIn(b"405 Method Not Allowed", response)
        self.assertIn(b"Allow: GET", response)
        self.assertEqual(self.store.loads, [])
    def test_token_is_bound_to_sse_device_ip(self):
        self.portal._history_peer_ip = "192.168.4.2"
        request = ("GET /api/history?token=%s&index=0 HTTP/1.1\r\n\r\n" % self.portal._history_token).encode()
        response = self.portal._build_http_response(request, peer_ip="192.168.4.3")
        self.assertIn(b"403", response)
        self.assertEqual(self.store.loads, [])
        response = self.portal._build_http_response(request, peer_ip="192.168.4.2")
        self.assertIn(b"200", response)
    def test_broken_record_reports_error_without_altering_live(self):
        self.store.records[0] = None
        status, body = self.query(index="0")
        self.assertIn(b"503", status)
        self.assertEqual(body["error"], "history_read_failed")
        self.assertEqual(self.portal._view_model()["train_no"], "99")
    def test_one_record_latest_and_specific_do_not_change_live(self):
        status, body = self.query()
        self.assertIn(b"200", status)
        self.assertEqual((body["train_no"], body["history_index"], body["history_count"]), ("8", 1, 2))
        _, body = self.query(index="0")
        self.assertEqual(body["loco"], "轨道探伤车-04782A")
        self.assertEqual(body["latitude"], 39.893762)
        self.assertEqual(self.portal._view_model()["train_no"], "99")
    def test_history_uses_its_receipt_date_minute_not_live_timestamp(self):
        self.store.records[0]["t"] = "2026-09-30 23:59:47"
        _, body = self.query(index="0")
        self.assertEqual(body["time"], "2026-09-30 23:59")
        _, other = self.query(index="1")
        self.assertEqual(other["time"], "2026-10-04 10:02")
        self.assertEqual(self.portal._view_model()["time"], "now")
    def test_legacy_history_does_not_invent_receipt_date(self):
        self.store.records[0]["t"] = "12:34:56"
        self.assertEqual(self.query(index="0")[1]["time"], "----/--/-- 12:34")
        self.store.records[0].pop("t")
        self.assertEqual(self.query(index="0")[1]["time"], "----/--/-- --:--")
    def test_invalid_index_and_record_reset(self):
        self.assertIn(b"400", self.query(index="abc")[0])
        self.assertIn(b"409", self.query(index="9999")[0])
        self.assertIn(b"409", self.query(index="-2")[0])
        self.store.records.clear()
        self.assertEqual(self.query()[1], {"empty": True, "count": 0})
    def test_navigation_uses_new_count_when_train_arrives(self):
        self.store.records.append({"t": "new", "d": {"basic": {"train_no": "9"}}})
        request = ("GET /api/history?token=%s&index=1&step=1 HTTP/1.1\r\n\r\n" % self.portal._history_token).encode()
        body = json.loads(self.portal._build_http_response(request).split(b"\r\n\r\n",1)[1])
        self.assertEqual((body["train_no"], body["history_index"], body["history_count"]), ("9", 2, 3))
    def test_busy_and_unavailable_are_bounded_and_do_not_load(self):
        self.portal._history_ready = lambda: False
        self.assertEqual(self.query()[1]["error"], "receiver_busy")
        self.store.index_complete = False
        self.assertEqual(self.query()[1]["error"], "history_unavailable")
        self.assertEqual(self.store.loads, [])
    def test_second_stream_rejected_first_keeps_history_poll_still_works(self):
        token = self.portal._history_token
        other = [StreamClient(), 0, bytearray(), None, 0, MODE_HTTP, -1, 0]
        self.assertFalse(self.portal._start_sse(other, 1))
        self.assertIn(b"503", other[3])
        self.portal._finish_client(other)
        self.assertEqual(token, self.portal._history_token)
        response = self.portal._build_http_response(b"GET /api/latest HTTP/1.1\r\n\r\n")
        self.assertIn(b"200 OK", response)
        self.assertIn(b"200", self.query(token)[0])
    def test_disconnect_invalidates_token_reconnect_rotates_token(self):
        token = self.portal._history_token
        self.portal._finish_client(self.owner)
        self.portal._clients.remove(self.owner)
        self.assertIn(b"403", self.query(token)[0])
        next_owner = [StreamClient(), 0, bytearray(), None, 0, MODE_HTTP, -1, 0]
        self.portal._start_sse(next_owner, 1)
        self.assertNotEqual(token, self.portal._history_token)
        self.assertIn(b"403", self.query(token)[0])
    def test_stop_revokes_history(self):
        token = self.portal._history_token
        self.portal._stop()
        self.assertIn(b"403", self.query(token)[0])
    def start_deferred_request(self):
        request = ("GET /api/history?token=%s&index=0 HTTP/1.1\r\n\r\n" % self.portal._history_token).encode()
        state = [StreamClient(request), 0, bytearray(), None, 0, MODE_HTTP, -1, 0]
        self.portal._clients = [state, self.owner]
        self.portal._service_http_client(1)
        self.assertEqual(state[5], MODE_HISTORY)
        return state
    def test_history_steps_interleave_live_sse_and_obey_spacing(self):
        state = self.start_deferred_request()
        self.portal._clients = [state]
        self.portal._service_http_client(2)
        self.assertEqual(self.store.loads, [])
        self.portal._service_http_client(41)  # First iterator step, no result.
        self.assertEqual(self.store.loads, [])
        self.portal._clients.insert(0, self.owner)
        self.portal.set_latest({"t": "new", "d": {"basic": {"train_no": "100"}}})
        self.portal._service_http_client(42)  # Initial stream buffer remains deliverable.
        self.assertFalse(self.owner[0].closed)
        self.portal._clients = [state]
        self.portal._service_http_client(81)
        self.assertEqual(self.store.loads, [0])
        self.assertEqual(state[5], MODE_HTTP)
        self.assertIn(b"history_index", state[3])
    def test_pending_read_times_out_when_radio_busy_without_loading(self):
        state = self.start_deferred_request()
        self.portal._history_ready = lambda: False
        self.portal._clients = [state]
        self.portal._service_http_client(100)
        self.assertEqual(self.store.loads, [])
        self.portal._service_http_client(3001)
        self.assertIn(b"receiver_busy", state[3])
    def test_request_received_while_busy_waits_without_reading_flash(self):
        self.portal._history_ready = lambda: False
        state = self.start_deferred_request()
        self.portal._clients = [state]
        self.portal._service_http_client(100)
        self.assertEqual(state[5], MODE_HISTORY)
        self.assertEqual(self.store.loads, [])
        self.portal._history_ready = lambda: True
        self.portal._service_http_client(140)
        self.portal._service_http_client(180)
        self.assertEqual(state[5], MODE_HTTP)
        self.assertIn(b"200 OK", state[3])
        self.assertEqual(self.store.loads, [0])
    def test_pending_read_revoked_without_loading(self):
        state = self.start_deferred_request()
        self.portal._finish_client(self.owner)
        self.portal._clients = [state]
        self.portal._service_http_client(42)
        self.assertEqual(self.store.loads, [])
        self.assertIn(b"403", state[3])
    def test_one_record_at_capacity_does_not_read_whole_history(self):
        self.store.records *= 5000
        self.store.records = self.store.records[:9999]
        status, body = self.query()
        self.assertIn(b"200", status)
        self.assertEqual((body["history_index"], body["history_count"]), (9998, 9999))
        self.assertEqual(self.store.loads, [9998])
    def test_main_prioritizes_screen_and_buttons_before_network(self):
        tree = ast.parse(pathlib.Path(__file__).resolve().parents[1].joinpath("main.py").read_text())
        loop = next(node for node in tree.body if isinstance(node, ast.While))
        calls = [node for node in ast.walk(loop) if isinstance(node, ast.Call)]
        wifi = next(node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "service_wifi")
        redraw = next(node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "process_ui_data")
        polls = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "poll"]
        self.assertGreater(wifi.lineno, redraw.lineno)
        self.assertTrue(all(wifi.lineno > node.lineno for node in polls))
    def test_main_defers_web_flash_reads_for_any_priority_queue(self):
        tree = ast.parse(pathlib.Path(__file__).resolve().parents[1].joinpath("main.py").read_text())
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "set_history_store")
        env = {"receiver": SimpleNamespace(raw_queue=[], last_word_time=0, synced=True,
                                           input_pending=lambda:0, input_is_buffered=lambda:False),
               "radio_state": [0,0,0,0,True,False,False], "RADIO_BUSY":6,
               "ui_queue": [], "history_queue": [], "sd_log_queue": [],
               "last_storage_write": 0, "STORAGE_WRITE_GAP_MS": 80,
               "HISTORY_RADIO_QUIET_MS": 100,
               "time": SimpleNamespace(ticks_ms=lambda: 1000,ticks_diff=lambda a,b:a-b)}
        ready = eval(compile(ast.Expression(call.args[1]), "main.py", "eval"), env)
        self.assertTrue(ready())
        env["radio_state"][6] = True
        self.assertFalse(ready())
        env["radio_state"][6] = False
        for queue in (env["receiver"].raw_queue, env["ui_queue"], env["history_queue"], env["sd_log_queue"]):
            queue.append(1)
            self.assertFalse(ready())
            queue.clear()
        env["receiver"].last_word_time = 990
        self.assertFalse(ready())
        env["receiver"].input_is_buffered = lambda:True
        self.assertTrue(ready())  # DMA keeps sampling during bounded reads.
        env["radio_state"][6] = True
        self.assertTrue(ready())  # DMA permits concurrent bounded RX work.
        env["ui_queue"].append(1)
        self.assertFalse(ready())  # A pending screen update always wins.
        env["ui_queue"].clear()
        env["radio_state"][6] = False
        env["receiver"].input_pending = lambda:2
        self.assertFalse(ready())  # Never relax the backlog condition.
        env["receiver"].input_pending = lambda:0
        env["receiver"].input_is_buffered = lambda:False
        env["receiver"].synced = False
        self.assertTrue(ready())  # Background words must not starve all browsing.
        env["receiver"].input_pending = lambda: 2
        self.assertFalse(ready())
        env["receiver"].input_pending = lambda: 0
        env["receiver"].last_word_time = 0
        env["last_storage_write"] = 990
        self.assertFalse(ready())
    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_browser_history_state_machine(self):
        script = pathlib.Path(__file__).with_name("portal_history_ui.cjs")
        result = subprocess.run(["node", str(script)], input=self.portal._render_page(),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
