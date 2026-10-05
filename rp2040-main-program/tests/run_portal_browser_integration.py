"""Optional real TCP + native EventSource + Chromium integration test.

Run with NODE_PATH pointing to a node_modules directory containing playwright.
LBJ_BROWSER_EXECUTABLE may select an installed Chrome/Chromium executable.
Uses disposable host history; never connects to or modifies a receiver.
"""
import pathlib
import sys
import tempfile
import threading
import time
import socket
import subprocess
import json

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from wireless_portal import WirelessPortal, _ticks_ms, _http_response, _parse_http_request
from history_store import HistoryStore, APPEND_OK
from test_history_store import fake_statvfs, record

def main():
    with tempfile.TemporaryDirectory(prefix="lbj-9999-native-browser-") as directory:
        store = HistoryStore(str(pathlib.Path(directory) / "history.jsonl"),
                             root=directory, statvfs_fn=fake_statvfs())
        store.scan()
        for i in range(9999):
            assert store.append(record(i)) == APPEND_OK
        portal = WirelessPortal("test-password")
        portal.set_history_store(store)
        # Test-only endpoint counts actual bounded Flash-line reads. It is
        # never part of the receiver's production portal.
        read_lines = [0]
        original_read = store._read_bounded_line
        def counted_read(source):
            read_lines[0] += 1
            return original_read(source)
        store._read_bounded_line = counted_read
        original_response = portal._build_http_response
        def test_response(request, peer_ip=None):
            if _parse_http_request(request)[1] == "/__test/history_cache":
                return _http_response("200 OK", "application/json", json.dumps({
                    "lines": read_lines[0], "cached": len(store._web_cache_records)}))
            return original_response(request, peer_ip=peer_ip)
        portal._build_http_response = test_response
        portal._http = socket.socket()
        portal._http.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        portal._http.bind(("127.0.0.1", 0))
        portal._http.listen(2)
        portal._http.setblocking(False)
        port = portal._http.getsockname()[1]
        stop = threading.Event()
        def serve():
            revision, last = 100000, 0
            while not stop.is_set():
                now = _ticks_ms()
                if now - last > 3000:
                    revision += 1
                    portal.set_latest(record(revision))
                    last = now
                portal._service_http_client(now)
                if len(portal._clients) < 2:
                    portal._accept_http(now)
                time.sleep(.001)
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            subprocess.run(["node", str(ROOT / "tests" / "portal_browser_integration.cjs"),
                            str(port)], check=True)
        finally:
            stop.set()
            worker.join(2)
            portal._stop()

if __name__ == "__main__":
    main()
