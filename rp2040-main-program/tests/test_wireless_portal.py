import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from wireless_portal import (
    AP_IP,
    AP_SSID,
    MODE_HTTP,
    MODE_SSE,
    SSE_FIN_CHECK_MS,
    SSE_HEARTBEAT_MS,
    SSE_SEND_TIMEOUT_MS,
    WirelessPortal,
    _coordinate_decimal,
    _dns_response,
)


def dns_query(name, qtype=1, txid=b"\x12\x34"):
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split("."))
    return txid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + labels + b"\x00" + qtype.to_bytes(2, "big") + b"\x00\x01"


class FakeWLAN:
    IF_STA = 0
    IF_AP = 1
    SEC_WPA_WPA2 = 7

    def __init__(self, interface):
        self.interface = interface
        self.is_active = False
        self.config_values = {}

    def active(self, value=None):
        if value is not None:
            self.is_active = bool(value)
        return self.is_active

    def config(self, **kwargs):
        self.config_values.update(kwargs)

    def ifconfig(self, value=None):
        return (AP_IP, "255.255.255.0", AP_IP, "0.0.0.0")


class FakeNetwork:
    WLAN = FakeWLAN


class DeadWLAN(FakeWLAN):
    def __init__(self, interface):
        raise OSError("injected CYW43 failure")


class DeadNetwork:
    WLAN = DeadWLAN


class NoSocketPortal(WirelessPortal):
    def _open_sockets(self):
        pass


class SlowClient:
    def __init__(self, max_send=17):
        self.max_send = max_send
        self.sent_chunks = []
        self.closed = False

    def send(self, data):
        self.sent_chunks.append(bytes(data))
        return min(self.max_send, len(data))

    def settimeout(self, _):
        raise AssertionError("HTTP service must remain non-blocking")

    def close(self):
        self.closed = True


class RequestClient:
    def __init__(self, request):
        self.request = request
        self.closed = False

    def recv(self, _size):
        request, self.request = self.request, b""
        return request

    def close(self):
        self.closed = True


class StreamClient(SlowClient):
    def __init__(self, request=b"", max_send=4096):
        super().__init__(max_send=max_send)
        self.request = request
        self.recv_calls = 0

    def recv(self, _size):
        self.recv_calls += 1
        if self.request:
            request, self.request = self.request, b""
            return request
        raise OSError(11)


class BlockingSendClient(StreamClient):
    def send(self, _data):
        raise OSError(11)


class ClosedStreamClient(StreamClient):
    def recv(self, _size):
        self.recv_calls += 1
        return b""


class MemorySendClient(StreamClient):
    def send(self, _data):
        raise MemoryError("injected send allocation failure")


class ExplodingRequestBuffer:
    def extend(self, _data):
        raise MemoryError("injected request allocation failure")


class ErrorSocket:
    def __init__(self, error_code):
        self.error_code = error_code

    def recvfrom(self, _):
        raise OSError(self.error_code)


class WirelessPortalTests(unittest.TestCase):
    def test_dns_a_wildcard_response(self):
        response = _dns_response(dns_query("example.com"))
        self.assertIsNotNone(response)
        self.assertEqual(response[:2], b"\x12\x34")
        self.assertEqual(response[6:8], b"\x00\x01")
        self.assertEqual(response[-4:], bytes([192, 168, 4, 1]))

    def test_dns_aaaa_returns_empty_noerror(self):
        response = _dns_response(dns_query("example.com", qtype=28))
        self.assertIsNotNone(response)
        self.assertEqual(response[6:8], b"\x00\x00")

    def test_dns_rejects_malformed_packet(self):
        self.assertIsNone(_dns_response(b"short"))

    def test_coordinate_conversion_accepts_receiver_degree_minutes(self):
        self.assertEqual(_coordinate_decimal("117°12.3456' E", "lon"), 117.20576)
        self.assertEqual(_coordinate_decimal("39°01.2345' N", "lat"), 39.020575)
        self.assertEqual(_coordinate_decimal("73°30.0000' W", "lon"), -73.5)
        self.assertEqual(_coordinate_decimal("18°15.0000' S", "lat"), -18.25)

    def test_coordinate_conversion_rejects_corrupt_or_impossible_values(self):
        self.assertIsNone(_coordinate_decimal("117°12.34XX' E", "lon"))
        self.assertIsNone(_coordinate_decimal("117°60.0000' E", "lon"))
        self.assertIsNone(_coordinate_decimal("181°00.0000' E", "lon"))
        self.assertIsNone(_coordinate_decimal("91°00.0000' N", "lat"))
        self.assertIsNone(_coordinate_decimal("---", "lat"))

    def test_ap_lifecycle_uses_fixed_ssid_and_password(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        self.assertTrue(portal.set_enabled(True))
        self.assertTrue(portal.is_enabled())
        self.assertEqual(portal._ap.config_values["ssid"], AP_SSID)
        self.assertEqual(portal._ap.config_values["key"], "ABCDEF123456")
        portal.set_enabled(False)
        self.assertFalse(portal.is_enabled())

    def test_hardware_probe_is_nonfatal_and_reports_result(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        self.assertEqual(portal.probe_hardware(), (True, "CYW43 READY"))
        failed = NoSocketPortal("ABCDEF123456", network_module=DeadNetwork)
        self.assertEqual(failed.probe_hardware(), (False, "CYW43 FAILED"))

    def test_root_page_escapes_radio_data_and_hides_password(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal.set_latest({
            "t": "2026-08-29 13:00",
            "d": {
                "type": "train_data_full",
                "rssi": "-44dBm",
                "basic": {"train_no": "<script>alert(1)</script>", "speed_kmh": "80", "km_post": 123.4},
                "extended": {"loco_type": "测试&机车", "cab_end": "31"},
            },
        })
        response = portal._build_http_response(b"GET / HTTP/1.1\r\nHost: captive.apple.com\r\n\r\n")
        self.assertIn(b"200 OK", response)
        self.assertIn(b"&lt;script&gt;", response)
        self.assertNotIn(b"<script>alert(1)</script>", response)
        self.assertNotIn(b"ABCDEF123456", response)
        self.assertNotIn(b"http-equiv=refresh", response)
        self.assertIn(b"soundBtn", response)
        self.assertIn(b"id=longitude", response)
        self.assertIn(b"id=latitude", response)
        self.assertIn(b"POSITION /", response)
        self.assertIn(b'new TextDecoder("gbk"', response)
        self.assertIn(b'new EventSource("/api/events")', response)
        self.assertIn(b"renderCoordinates", response)
        self.assertIn(b"streamPrimed=false", response)
        self.assertIn(b"render(JSON.parse(event.data),true,first)", response)
        self.assertIn(b"pollPrimed=true", response)
        self.assertIn(b"const first=pollPrimed;pollPrimed=false", response)
        self.assertIn(b"render(d,true,first)", response)
        self.assertIn(b"if(stream&&stream.readyState===1)return;const first", response)
        self.assertIn(b'fetch("/api/latest?t="', response)
        self.assertNotIn(b"distanceValue", response)
        self.assertNotIn(b"haversineMeters", response)
        self.assertNotIn(b"navigator.geolocation", response)
        self.assertNotIn(b"manualPanel", response)
        self.assertNotIn(b"mapLink", response)
        self.assertNotIn(b"maps://", response)
        self.assertNotIn(b"openstreetmap", response)
        self.assertNotIn("接入地址".encode(), response)
        self.assertNotIn("若页面未自动打开".encode(), response)
        self.assertNotIn(b"192.168.4.1", response)
        self.assertIn(b'class="cell wide"', response)
        self.assertNotIn(b"id=lon>", response)
        self.assertNotIn(b"id=lat>", response)
        self.assertNotIn(b"class=plot", response)
        self.assertNotIn("全国经纬度范围".encode(), response)

    def test_view_matches_receiver_fields_and_revision_changes(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        first_revision = portal._view_model()["update_id"]
        portal.set_latest({
            "t": "2026-08-29 15:30:00",
            "d": {
                "type": "train_data_full",
                "basic": {"train_no": "1234", "speed_kmh": 80, "km_post": 12.3},
                "extended": {
                    "class_tag": "K",
                    "loco_type": "DF4-0012",
                    "cab_end": "31",
                    "route_hex": "BDF2C9BDCFDF2020",
                    "lon": "117°12.3456' E",
                    "lat": "39°01.2345' N",
                },
            },
        })
        view = portal._view_model()
        self.assertNotEqual(view["update_id"], first_revision)
        self.assertEqual(view["train_no"], "K1234")
        self.assertEqual(view["direction"], "上行")
        self.assertEqual(view["loco"], "DF4-0012A")
        self.assertEqual(view["cab"], "A端")
        self.assertEqual(view["route"], "BDF2C9BDCFDF2020")
        self.assertEqual(view["longitude"], 117.20576)
        self.assertEqual(view["latitude"], 39.020575)
        page = portal._render_page().encode("utf-8")
        self.assertIn(b'data-gbk="BDF2C9BDCFDF2020"', page)
        self.assertIn(b'data-lon="117.20576"', page)
        self.assertIn(b"renderCoordinates", page)
        self.assertIn(b'coordinateText(lon,"E","W")', page)
        self.assertIn(b'coordinateText(lat,"N","S")', page)
        self.assertIn(b"data-lon=\"117.20576\"", page)

    def test_position_requires_a_complete_coordinate_pair(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal.set_latest({
            "t": "2026-08-29 15:30:00",
            "d": {
                "type": "train_data_full",
                "basic": {"train_no": "1234", "speed_kmh": 80, "km_post": 12.3},
                "extended": {
                    "lon": "117°12.3456' E",
                    "lat": "39°01.XXXX' N",
                },
            },
        })
        view = portal._view_model()
        self.assertIsNone(view["longitude"])
        self.assertIsNone(view["latitude"])

    def test_extended_only_is_available_and_exposes_extension_fields(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal.set_latest({
            "t": "2026-08-29 18:01:00",
            "d": {
                "type": "extended_only",
                "rssi": -51,
                "extended": {
                    "class_tag": "K",
                    "route_hex": "BDF2C9BDCFDF2020",
                    "loco_type": "QJ-1234",
                    "cab_end": "31",
                    "lon": "117°12.3456' E",
                    "lat": "39°01.2345' N",
                },
            },
        })
        view = portal._view_model()
        self.assertTrue(view["available"])
        self.assertEqual(view["type"], "extended_only")
        self.assertEqual(view["train_no"], "---")
        self.assertEqual(view["direction"], "---")
        self.assertEqual(view["route"], "BDF2C9BDCFDF2020")
        self.assertEqual(view["loco"], "QJ-1234A")
        self.assertEqual(view["cab"], "A端")
        self.assertEqual(view["rssi"], -51)

    def test_basic_only_with_missing_measurements_is_still_displayed(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal.set_latest({
            "t": "2026-08-30 12:00:00",
            "d": {
                "type": "basic_only",
                "basic": {
                    "train_no": "57721",
                    "speed_kmh": "---",
                    "km_post": "---",
                },
            },
        })

        view = portal._view_model()
        self.assertTrue(view["available"])
        self.assertEqual(view["type"], "basic_only")
        self.assertEqual(view["train_no"], "57721")
        self.assertEqual(view["direction"], "下行")
        self.assertEqual(view["speed"], "---")
        self.assertEqual(view["km"], "---")

    def test_invalid_latest_record_is_treated_as_unavailable(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal.set_latest(["not", "a", "record"])
        self.assertFalse(portal._view_model()["available"])
        portal.set_latest({"t": "bad", "d": []})
        self.assertFalse(portal._view_model()["available"])
        portal.set_latest({"t": "partial", "d": {"basic": [], "extended": "bad"}})
        self.assertTrue(portal._view_model()["available"])
        self.assertEqual(portal._view_model()["train_no"], "---")

    def test_captive_probe_redirects_to_portal(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        response = portal._build_http_response(b"GET /generate_204 HTTP/1.1\r\nHost: connectivitycheck.gstatic.com\r\n\r\n")
        self.assertIn(b"302 Found", response)
        self.assertIn(b"Location: http://192.168.4.1/", response)

    def test_api_returns_only_display_fields(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        response = portal._build_http_response(b"GET /api/latest HTTP/1.1\r\nHost: 192.168.4.1\r\n\r\n")
        self.assertIn(b"application/json", response)
        self.assertIn(b'"available": false', response)
        self.assertIn(b'"longitude": null', response)
        self.assertIn(b'"latitude": null', response)
        self.assertNotIn(b"ABCDEF123456", response)

    def test_sse_headers_are_streaming_and_initial_event_has_snapshot(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        response = portal._build_http_response(
            b"GET /api/events HTTP/1.1\r\nHost: 192.168.4.1\r\n\r\n"
        )
        headers, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"text/event-stream", headers)
        self.assertIn(b"Connection: keep-alive", headers)
        self.assertNotIn(b"Content-Length", headers)
        self.assertNotIn(b"Connection: close", headers)
        self.assertIn(b"retry: 2000", body)
        self.assertIn(b"event: train", body)
        self.assertIn(b'"update_id": "0"', body)

    def test_sse_stream_stays_open_and_pushes_only_newest_revision(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        client = StreamClient(
            b"GET /api/events HTTP/1.1\r\nHost: 192.168.4.1\r\n\r\n"
        )
        portal._clients = [
            [client, 100, bytearray(), None, 0, MODE_HTTP, -1, 100]
        ]

        portal._service_http_client(101)  # Parse and build initial stream.
        portal._service_http_client(102)  # Send headers and initial snapshot.
        while portal._clients[0][3] is not None:
            portal._service_http_client(102)
        self.assertFalse(client.closed)
        self.assertEqual(portal._clients[0][5], MODE_SSE)

        portal.set_latest({"t": "first", "d": {"basic": {"train_no": "1"}}})
        portal.set_latest({"t": "newest", "d": {"basic": {"train_no": "2"}}})
        portal._service_http_client(103)  # Queue the current/latest revision.
        portal._service_http_client(104)  # Send it.
        while portal._clients[0][3] is not None:
            portal._service_http_client(104)
        sent = b"".join(client.sent_chunks)
        self.assertEqual(sent.count(b"event: train"), 2)
        self.assertIn(b"id: 2", sent)
        self.assertIn(b'"time": "newest"', sent)
        self.assertNotIn(b'"time": "first"', sent)

        before = len(client.sent_chunks)
        portal._service_http_client(105)
        self.assertEqual(len(client.sent_chunks), before)
        self.assertFalse(client.closed)

    def test_idle_sse_ignores_http_timeout_and_sends_heartbeat(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        client = StreamClient()
        portal._clients = [
            [client, 100, bytearray(), None, 0, MODE_SSE, 0, 100]
        ]

        portal._service_http_client(2000)
        self.assertFalse(client.closed)
        self.assertIsNone(portal._clients[0][3])

        portal._service_http_client(100 + SSE_HEARTBEAT_MS)
        self.assertEqual(portal._clients[0][3], b": keepalive\n\n")
        portal._service_http_client(101 + SSE_HEARTBEAT_MS)
        self.assertIn(b": keepalive\n\n", b"".join(client.sent_chunks))
        self.assertFalse(client.closed)

    def test_second_sse_keeps_healthy_stream_and_gets_busy_response(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        old_stream = StreamClient()
        old_state = [old_stream, 1, bytearray(), None, 0, MODE_SSE, 0, 1]
        portal._clients = [old_state]
        new_client = StreamClient()
        new_state = [new_client, 2, bytearray(), None, 0, MODE_HTTP, -1, 2]

        portal._start_sse(new_state, 2)

        self.assertFalse(old_stream.closed)
        self.assertEqual(portal._clients, [old_state])
        self.assertEqual(new_state[5], MODE_HTTP)
        self.assertIn(b"503 Service Unavailable", new_state[3])
        self.assertIsNone(new_state[2])

    def test_second_sse_reclaims_closed_stream_and_becomes_live(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        old_stream = ClosedStreamClient()
        old_state = [old_stream, 1, bytearray(), None, 0, MODE_SSE, 0, 1]
        portal._clients = [old_state]
        new_client = StreamClient()
        new_state = [new_client, 2, bytearray(), None, 0, MODE_HTTP, -1, 2]

        portal._start_sse(new_state, 2)

        self.assertTrue(old_stream.closed)
        self.assertEqual(portal._clients, [])
        self.assertEqual(new_state[5], MODE_SSE)
        self.assertIn(b"event: train", new_state[3])

    def test_idle_sse_checks_fin_at_most_once_per_second(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        client = StreamClient()
        portal._clients = [
            [client, 100, bytearray(), None, 0, MODE_SSE, 0, 100]
        ]

        for now in (101, 200, 500, 1099):
            portal._service_http_client(now)
        self.assertEqual(client.recv_calls, 0)
        portal._service_http_client(100 + SSE_FIN_CHECK_MS)
        self.assertEqual(client.recv_calls, 1)
        portal._service_http_client(100 + SSE_FIN_CHECK_MS + 999)
        self.assertEqual(client.recv_calls, 1)

    def test_sse_slow_send_times_out_without_stopping_portal(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._enabled = True
        client = BlockingSendClient()
        portal._clients = [
            [client, 100, bytearray(), b"pending", 0, MODE_SSE, 0, 100]
        ]

        portal._service_http_client(100 + SSE_SEND_TIMEOUT_MS - 1)
        self.assertFalse(client.closed)
        portal._service_http_client(100 + SSE_SEND_TIMEOUT_MS)
        self.assertTrue(client.closed)
        self.assertTrue(portal.is_enabled())
        self.assertEqual(portal._clients, [])

    def test_sse_fin_is_reclaimed_immediately(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        client = ClosedStreamClient()
        portal._clients = [
            [client, 100, bytearray(), None, 0, MODE_SSE, 0, 100]
        ]

        portal._service_http_client(100 + SSE_FIN_CHECK_MS)

        self.assertTrue(client.closed)
        self.assertEqual(portal._clients, [])

    def test_sse_event_allocation_failure_closes_only_stream(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._enabled = True
        client = StreamClient()
        portal._clients = [
            [client, 100, bytearray(), None, 0, MODE_SSE, 0, 100]
        ]
        portal.set_latest({"d": {}})

        def fail_event():
            raise MemoryError("injected")

        portal._sse_event = fail_event
        portal._service_http_client(101)
        self.assertTrue(client.closed)
        self.assertTrue(portal.is_enabled())
        self.assertEqual(portal._clients, [])

    def test_sse_send_memory_failure_closes_only_client(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._enabled = True
        client = MemorySendClient()
        portal._clients = [
            [client, 100, None, b"pending", 0, MODE_SSE, 0, 100]
        ]

        portal._service_http_client(101)

        self.assertTrue(client.closed)
        self.assertTrue(portal.is_enabled())
        self.assertEqual(portal._clients, [])

    def test_request_memory_failure_closes_only_client(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._enabled = True
        client = RequestClient(b"GET / HTTP/1.1\r\n")
        portal._clients = [
            [client, 100, ExplodingRequestBuffer(), None, 0, MODE_HTTP, -1, 100]
        ]

        portal._service_http_client(101)

        self.assertTrue(client.closed)
        self.assertTrue(portal.is_enabled())
        self.assertEqual(portal._clients, [])

    def test_service_frees_short_slot_before_accepting_next_client(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._enabled = True
        portal._health_at = 101
        short_client = SlowClient(max_send=10)
        stream_client = StreamClient()
        portal._clients = [
            [short_client, 100, None, b"x", 0, MODE_HTTP, -1, 100],
            [stream_client, 100, None, None, 0, MODE_SSE, 0, 100],
        ]
        calls = []
        portal._service_dns = lambda: calls.append("dns")

        def accept_after_service(_now):
            self.assertTrue(short_client.closed)
            calls.append("accept")

        portal._accept_http = accept_after_service
        portal.service(101)
        self.assertEqual(calls, ["dns", "accept"])
        self.assertEqual(len(portal._clients), 1)
        self.assertEqual(portal._clients[0][5], MODE_SSE)

    def test_oversized_sse_snapshot_is_rejected(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal.set_latest({
            "t": "test",
            "d": {
                "basic": {"train_no": "1"},
                "extended": {"route_hex": "A" * 3000},
            },
        })
        with self.assertRaises(ValueError):
            portal._sse_event()

    def test_slow_http_client_is_sent_incrementally_without_blocking(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        client = SlowClient()
        response = b"x" * 900
        portal._clients = [
            [client, 100, bytearray(), response, 0, MODE_HTTP, -1, 100]
        ]

        portal._service_http_client(101)

        self.assertFalse(client.closed)
        self.assertEqual(len(client.sent_chunks), 1)
        self.assertEqual(len(client.sent_chunks[0]), 512)
        self.assertEqual(portal._clients[0][4], 17)

    def test_http_render_failure_closes_only_that_client(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._enabled = True
        client = RequestClient(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
        portal._clients = [
            [client, 100, bytearray(), None, 0, MODE_HTTP, -1, 100]
        ]

        def fail_render(_request):
            raise MemoryError("injected")

        portal._build_http_response = fail_render
        portal._service_http_client(101)
        self.assertTrue(client.closed)
        self.assertTrue(portal.is_enabled())
        self.assertEqual(portal._clients, [])

    def test_error_stop_preserves_reason_for_retry_screen(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._enabled = True
        portal.stop_for_error(OSError("socket failed"))
        self.assertFalse(portal.is_enabled())
        self.assertIn("socket failed", portal.get_status()["error"])

    def test_dns_only_suppresses_would_block(self):
        portal = NoSocketPortal("ABCDEF123456", network_module=FakeNetwork)
        portal._dns = ErrorSocket(11)
        portal._service_dns()
        portal._dns = ErrorSocket(9)
        with self.assertRaises(OSError):
            portal._service_dns()


if __name__ == "__main__":
    unittest.main()
