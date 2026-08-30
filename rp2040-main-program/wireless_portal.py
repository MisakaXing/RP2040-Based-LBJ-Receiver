"""Wi-Fi access point and captive portal for the LBJ receiver.

The radio decoder already owns core 1, so this module deliberately keeps all
network work cooperative and bounded.  Call ``service()`` frequently from the
core-0 UI loop.  The CYW43 MicroPython port supplies DHCP for AP mode; this
module supplies wildcard DNS and HTTP only.
"""

import json
import errno
import gc
import socket
import time

try:
    import network
except ImportError:  # Allows host-side tests of the protocol helpers.
    network = None


AP_IP = "192.168.4.1"
AP_NETMASK = "255.255.255.0"
AP_SSID = "PICO-LBJ-Receiver"
AP_CHANNEL = 6

DNS_PORT = 53
HTTP_PORT = 80
DNS_TTL_SECONDS = 30
MAX_DNS_PACKET = 512
MAX_HTTP_REQUEST = 1024
MAX_HTTP_CLIENTS = 2
HTTP_CLIENT_TIMEOUT_MS = 1500
MAX_HTTP_SEND = 512
SSE_HEARTBEAT_MS = 15000
SSE_SEND_TIMEOUT_MS = 6000
SSE_FIN_CHECK_MS = 1000
MAX_SSE_EVENT = 1536

# Compact per-client state.  Keeping this as a list avoids a dictionary for
# every socket on MicroPython's small heap.
CLIENT_SOCKET = 0
CLIENT_PROGRESS = 1
CLIENT_REQUEST = 2
CLIENT_RESPONSE = 3
CLIENT_SENT = 4
CLIENT_MODE = 5
CLIENT_REVISION = 6
CLIENT_HEARTBEAT = 7
MODE_HTTP = 0
MODE_SSE = 1

CAPTIVE_PATHS = (
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/connecttest.txt",
    "/ncsi.txt",
    "/redirect",
)


def _ticks_ms():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def _ticks_diff(new, old):
    try:
        return time.ticks_diff(new, old)
    except AttributeError:
        return new - old


def _sleep_ms(value):
    try:
        time.sleep_ms(value)
    except AttributeError:
        time.sleep(value / 1000.0)


def _is_would_block(exc):
    code = getattr(exc, "errno", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0]
    return code in (
        getattr(errno, "EAGAIN", 11),
        getattr(errno, "EWOULDBLOCK", 11),
        11,
        35,
    )


def _html_escape(value):
    value = str(value if value is not None else "---")
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _coordinate_decimal(value, axis):
    """Convert the receiver's degree/minute coordinate to decimal degrees.

    Corrupt POCSAG codewords are represented with ``X`` characters, so the
    web API must never treat a partly decoded coordinate as a real position.
    ``None`` is returned for malformed values, impossible minutes, or an
    out-of-range latitude/longitude.
    """
    limit = 180.0 if axis == "lon" else 90.0
    try:
        if isinstance(value, (int, float)):
            decimal = float(value)
        else:
            text = str(value).strip().upper()
            degree_at = text.find("°")
            if degree_at <= 0:
                return None
            minute_at = text.find("'", degree_at + 1)
            if minute_at < 0:
                minute_at = len(text)
            degrees = float(text[:degree_at].strip())
            minutes = float(text[degree_at + 1:minute_at].strip())
            if minutes < 0.0 or minutes >= 60.0:
                return None
            negative = degrees < 0.0 or "W" in text or "S" in text
            decimal = abs(degrees) + minutes / 60.0
            if negative:
                decimal = -decimal
        # NaN is the only numeric value that differs from itself.
        if decimal != decimal or decimal < -limit or decimal > limit:
            return None
        return round(decimal, 6)
    except (TypeError, ValueError, OverflowError):
        return None


def _dns_response(packet, ip=AP_IP):
    """Return a minimal wildcard-DNS response, or ``None`` if malformed.

    A and ANY questions receive the AP IPv4 address.  Other question types get
    a valid NOERROR response with no answers so clients can fall back to IPv4.
    """
    if not packet or len(packet) < 17 or len(packet) > MAX_DNS_PACKET:
        return None
    if packet[4:6] != b"\x00\x01":
        return None

    pos = 12
    packet_len = len(packet)
    while pos < packet_len:
        label_len = packet[pos]
        if label_len == 0:
            pos += 1
            break
        if label_len & 0xC0 or label_len > 63:
            return None
        pos += 1 + label_len
    else:
        return None

    if pos + 4 > packet_len:
        return None
    question_end = pos + 4
    qtype = (packet[pos] << 8) | packet[pos + 1]
    qclass = (packet[pos + 2] << 8) | packet[pos + 3]
    answer_count = 1 if qclass == 1 and qtype in (1, 255) else 0

    # QR=1, AA=1, copy the client's recursion-desired bit, no error.
    flags_hi = 0x84 | (packet[2] & 0x01)
    header = (
        packet[0:2]
        + bytes([flags_hi, 0x00])
        + b"\x00\x01"
        + bytes([0, answer_count])
        + b"\x00\x00\x00\x00"
    )
    response = header + packet[12:question_end]
    if answer_count:
        ip_bytes = bytes([int(part) for part in ip.split(".")])
        response += (
            b"\xc0\x0c\x00\x01\x00\x01"
            + DNS_TTL_SECONDS.to_bytes(4, "big")
            + b"\x00\x04"
            + ip_bytes
        )
    return response


def _http_response(status, content_type, body=b"", extra_headers="", head=False):
    if isinstance(body, str):
        body = body.encode("utf-8")
    headers = (
        "HTTP/1.1 %s\r\n"
        "Content-Type: %s\r\n"
        "Cache-Control: no-store, no-cache, must-revalidate\r\n"
        "Pragma: no-cache\r\n"
        "Connection: close\r\n"
        "%s"
        "Content-Length: %d\r\n\r\n"
        % (status, content_type, extra_headers, len(body))
    ).encode("utf-8")
    return headers if head else headers + body


def _parse_http_request(request):
    first_line = request.split(b"\r\n", 1)[0].decode("ascii")
    parts = first_line.split(" ")
    if len(parts) < 2:
        raise ValueError("bad request")
    return parts[0].upper(), parts[1].split("?", 1)[0]


def _sse_headers():
    # An SSE stream has no Content-Length and stays open until the browser
    # disconnects.  ``retry`` asks EventSource to reconnect after two seconds.
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream; charset=utf-8\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: keep-alive\r\n\r\n"
        b"retry: 2000\n\n"
    )


class WirelessPortal:
    def __init__(self, password, ssid=AP_SSID, network_module=None):
        self.ssid = ssid
        self.password = str(password)
        self._network = network if network_module is None else network_module
        self._ap = None
        self._dns = None
        self._http = None
        self._clients = []
        self._latest_record = None
        self._latest_revision = 0
        self._enabled = False
        self._last_error = ""
        self._ip = AP_IP
        self._health_at = 0

    def is_enabled(self):
        return self._enabled

    def set_latest(self, record):
        # The caller replaces the snapshot on core 0; HTTP never touches Flash.
        if isinstance(record, dict) and isinstance(record.get("d"), dict):
            self._latest_record = record
        else:
            self._latest_record = None
        self._latest_revision = (self._latest_revision + 1) & 0x7FFFFFFF

    def get_status(self):
        return {
            "enabled": self._enabled,
            "ssid": self.ssid,
            "ip": self._ip if self._enabled else "---",
            "clients": len(self._clients),
            "error": self._last_error,
        }

    def probe_hardware(self):
        """Briefly start the station interface to verify the CYW43 path."""
        if self._network is None:
            return False, "MODULE MISSING"
        station = None
        try:
            wlan_type = getattr(self._network.WLAN, "IF_STA", None)
            if wlan_type is None:
                wlan_type = getattr(self._network, "STA_IF", None)
            if wlan_type is None:
                raise OSError("STA interface missing")
            station = self._network.WLAN(wlan_type)
            station.active(True)
            deadline = _ticks_ms() + 1500
            while not station.active() and _ticks_diff(deadline, _ticks_ms()) > 0:
                _sleep_ms(25)
            if not station.active():
                raise OSError("CYW43 did not start")
            return True, "CYW43 READY"
        except Exception as exc:
            self._last_error = str(exc)[:48]
            return False, "CYW43 FAILED"
        finally:
            if station is not None:
                try:
                    station.active(False)
                    _sleep_ms(50)
                except Exception:
                    pass

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self._enabled:
            return True
        if enabled:
            return self._start()
        self._stop()
        return True

    def stop_for_error(self, exc):
        """Stop a failed AP instance without changing the user's preference."""
        self._last_error = str(exc)[:48]
        self._stop(clear_error=False)

    def _start(self):
        self._last_error = ""
        if self._network is None:
            self._last_error = "NETWORK MODULE MISSING"
            return False
        if len(self.password) < 8 or len(self.password) > 63:
            self._last_error = "INVALID SN PASSWORD"
            return False

        try:
            wlan_type = getattr(self._network.WLAN, "IF_AP", None)
            if wlan_type is None:
                wlan_type = self._network.AP_IF
            self._ap = self._network.WLAN(wlan_type)
            self._ap.active(False)
            _sleep_ms(100)

            try:
                security = getattr(self._network.WLAN, "SEC_WPA_WPA2")
                self._ap.config(
                    ssid=self.ssid,
                    key=self.password,
                    security=security,
                    channel=AP_CHANNEL,
                )
            except (AttributeError, TypeError, ValueError):
                # Compatibility with older CYW43 MicroPython builds.
                self._ap.config(
                    essid=self.ssid,
                    password=self.password,
                    channel=AP_CHANNEL,
                )

            self._ap.active(True)
            deadline = _ticks_ms() + 2500
            while not self._ap.active() and _ticks_diff(deadline, _ticks_ms()) > 0:
                _sleep_ms(25)
            if not self._ap.active():
                raise OSError("AP did not become active")

            current = self._ap.ifconfig()
            if not current or current[0] == "0.0.0.0":
                self._ap.ifconfig((AP_IP, AP_NETMASK, AP_IP, AP_IP))
                current = self._ap.ifconfig()
            if not current or current[0] == "0.0.0.0":
                raise OSError("AP has no IPv4 address")
            self._ip = current[0]

            self._open_sockets()
            self._enabled = True
            self._health_at = _ticks_ms()
            print("WIFI_AP_READY", self.ssid, self._ip)
            return True
        except Exception as exc:
            self._last_error = str(exc)[:48]
            print("WIFI_AP_ERROR", repr(exc))
            self._stop(clear_error=False)
            return False

    def _reuse_address(self, sock):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass

    def _open_sockets(self):
        self._dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._reuse_address(self._dns)
        self._dns.setblocking(False)
        self._dns.bind(("0.0.0.0", DNS_PORT))

        self._http = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._reuse_address(self._http)
        self._http.bind(("0.0.0.0", HTTP_PORT))
        self._http.listen(MAX_HTTP_CLIENTS)
        self._http.setblocking(False)

    def _close_socket(self, sock):
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _stop(self, clear_error=True):
        for client in self._clients:
            self._close_socket(client[0])
        self._clients = []
        self._close_socket(self._http)
        self._close_socket(self._dns)
        self._http = None
        self._dns = None
        if self._ap is not None:
            try:
                self._ap.active(False)
            except Exception:
                pass
        self._enabled = False
        self._ip = AP_IP
        if clear_error:
            self._last_error = ""
        print("WIFI_AP_OFF")

    def _service_dns(self):
        try:
            packet, address = self._dns.recvfrom(MAX_DNS_PACKET)
        except OSError as exc:
            if _is_would_block(exc):
                return
            raise
        response = _dns_response(packet, self._ip)
        if response is not None:
            try:
                self._dns.sendto(response, address)
            except OSError as exc:
                if not _is_would_block(exc):
                    raise

    def _accept_http(self, now):
        try:
            client, _ = self._http.accept()
        except OSError as exc:
            if _is_would_block(exc):
                return
            raise
        except Exception as exc:
            print("WIFI_ACCEPT_ERR", repr(exc))
            return
        try:
            client.setblocking(False)
        except Exception as exc:
            print("WIFI_CLIENT_ERR", repr(exc))
            self._close_socket(client)
            return
        if len(self._clients) >= MAX_HTTP_CLIENTS:
            self._close_socket(client)
            return
        # socket, progress, request, pending bytes, offset, mode, revision, heartbeat
        try:
            self._clients.append(
                [client, now, bytearray(), None, 0, MODE_HTTP, -1, now]
            )
        except Exception as exc:
            print("WIFI_CLIENT_ERR", repr(exc))
            self._close_socket(client)

    def _view_model(self):
        record = self._latest_record
        if not record:
            return {
                "available": False,
                "update_id": str(self._latest_revision),
                "time": "---",
                "type": "WAITING",
                "train_no": "---",
                "speed": "---",
                "km": "---",
                "loco": "---",
                "cab": "---",
                "direction": "---",
                "route": "---",
                "lon": "---",
                "lat": "---",
                "longitude": None,
                "latitude": None,
                "rssi": "---",
            }
        data = record.get("d", {})
        basic = data.get("basic", {}) or {}
        extended = data.get("extended", {}) or {}
        if not isinstance(basic, dict):
            basic = {}
        if not isinstance(extended, dict):
            extended = {}
        raw_train_no = basic.get("train_no", "---")
        class_tag = extended.get("class_tag", "")
        if class_tag == "?" or raw_train_no == "---":
            class_tag = ""
        train_no = (str(class_tag) + str(raw_train_no))[:8]
        digits = [char for char in str(raw_train_no) if char.isdigit()]
        if digits:
            direction = "上行" if int(digits[-1]) % 2 == 0 else "下行"
        else:
            direction = "---"
        cab_raw = str(extended.get("cab_end", ""))
        if cab_raw == "31":
            cab = "A端"
        elif cab_raw == "32":
            cab = "B端"
        elif cab_raw == "30":
            cab = "无端位"
        else:
            cab = cab_raw or "---"
        loco = str(extended.get("loco_type", "---"))
        if cab_raw == "31":
            loco += "A"
        elif cab_raw == "32":
            loco += "B"
        lon = extended.get("lon", "---")
        lat = extended.get("lat", "---")
        longitude = _coordinate_decimal(lon, "lon")
        latitude = _coordinate_decimal(lat, "lat")
        # A partial coordinate is not a usable train position.
        if longitude is None or latitude is None:
            longitude = None
            latitude = None
        return {
            "available": True,
            "update_id": str(self._latest_revision),
            "time": record.get("t", "---"),
            "type": data.get("type", "---"),
            "train_no": train_no,
            "speed": basic.get("speed_kmh", "---"),
            "km": basic.get("km_post", "---"),
            "loco": loco,
            "cab": cab,
            "direction": direction,
            "route": extended.get("route_hex", "---"),
            "lon": lon,
            "lat": lat,
            "longitude": longitude,
            "latitude": latitude,
            "rssi": data.get("rssi", "---"),
        }

    def _render_page(self):
        view = self._view_model()
        state = "最近一次列车信息" if view["available"] else "等待列车信号"
        values = {
            "record_id": _html_escape(view["update_id"]),
            "state": _html_escape(state),
            "train": _html_escape(view["train_no"]),
            "speed": _html_escape(view["speed"]),
            "km": _html_escape(view["km"]),
            "route": _html_escape(view["route"]),
            "direction": _html_escape(view["direction"]),
            "time": _html_escape(view["time"]),
            "loco": _html_escape(view["loco"]),
            "cab": _html_escape(view["cab"]),
            "longitude": "" if view["longitude"] is None else str(view["longitude"]),
            "latitude": "" if view["latitude"] is None else str(view["latitude"]),
            "rssi": _html_escape(view["rssi"]),
            "type": _html_escape(view["type"]),
        }
        return """<!doctype html>
<html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>LBJ Receiver W</title>
<style>
:root{--black:#000;--panel:#082033;--panel2:#102b40;--cyan:#29e7ff;--yellow:#ffe342;--green:#38ef82;--magenta:#ff58d0;--red:#ff4b55;--muted:#91a7b8;--white:#f3f8ff}
*{box-sizing:border-box}body{margin:0;background:var(--black);color:var(--white);font-family:ui-monospace,SFMono-Regular,Menlo,"PingFang SC",sans-serif}
header{background:#031927;border-bottom:3px solid var(--cyan);padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.logo{font-weight:900;letter-spacing:.08em}.logo b{color:var(--cyan)}.online{font-size:12px;color:var(--green);white-space:nowrap}.dot{display:inline-block;width:8px;height:8px;border-radius:50vw;background:var(--green);box-shadow:0 0 10px var(--green);margin-right:6px}
main{max-width:620px;margin:auto;padding:15px}.strip{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:12px;margin:2px 1px 10px}.strip strong{color:var(--green)}
.hero{position:relative;background:var(--panel);border:1px solid #24506b;border-left:5px solid var(--cyan);padding:16px;margin-bottom:11px;box-shadow:inset 0 0 28px #00131f}
.eyebrow{font-size:12px;color:var(--muted);letter-spacing:.12em}.train{font-size:48px;line-height:1.1;color:var(--cyan);font-weight:900;margin:6px 0 14px;word-break:break-all;text-shadow:0 0 12px #00c8e866}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric{background:#031927;border:1px solid #1d435a;padding:10px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;font-size:27px;margin-top:3px}.speed strong{color:var(--yellow)}.km strong{color:var(--green)}
.route{display:grid;grid-template-columns:70px 1fr auto;align-items:center;gap:10px;background:var(--panel2);border:1px solid #24506b;padding:13px 15px;margin-bottom:11px}.route label{color:var(--muted)}.route strong{font-size:25px;color:var(--white);word-break:break-all}.direction{color:var(--magenta);font-weight:800}
.location{background:var(--panel);border:1px solid #24506b;margin-bottom:11px}.loc-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 14px;border-bottom:1px solid #1d435a}.loc-head span{color:var(--muted);font-size:12px;letter-spacing:.08em}.loc-head strong{color:var(--green);font-size:13px}.coordinate-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:14px}.coordinate{min-width:0;background:#031927;border:1px solid #1d435a;padding:12px}.coordinate span{display:block;color:var(--muted);font-size:11px}.coordinate strong{display:block;color:var(--cyan);font-size:18px;margin-top:6px;overflow-wrap:anywhere}
.grid{display:grid;grid-template-columns:1fr 1fr;background:var(--panel);border:1px solid #23455c}.cell{min-width:0;padding:11px 13px;border-bottom:1px solid #1b3b50}.cell:nth-child(odd){border-right:1px solid #1b3b50}.cell.wide{grid-column:1/-1;border-right:0}.cell span{display:block;color:var(--muted);font-size:12px}.cell strong{display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.controls{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:11px}.sound{appearance:none;border:1px solid var(--yellow);background:#241f05;color:var(--yellow);padding:11px 13px;font:inherit;font-weight:800}.sound.on{border-color:var(--green);background:#092817;color:var(--green)}
.refresh{text-align:right;font-size:12px;color:var(--muted)}.refresh strong{display:block;color:var(--cyan);margin-bottom:3px}.notice{min-height:22px;color:var(--green);font-size:13px;margin-top:10px}
footer{color:#718795;font-size:11px;line-height:1.55;margin-top:8px}.flash{animation:flash 1.2s ease}@keyframes flash{0%%{border-color:var(--yellow);box-shadow:0 0 30px #ffe34299}100%%{border-color:#24506b;box-shadow:inset 0 0 28px #00131f}}
@media(max-width:430px){.train{font-size:40px}.metric strong{font-size:23px}.route{grid-template-columns:58px 1fr}.direction{grid-column:2}.coordinate-grid,.grid{grid-template-columns:1fr}.cell:nth-child(odd){border-right:0}.controls{align-items:stretch;flex-direction:column}.refresh{text-align:left}}
</style></head>
<body data-record-id="%(record_id)s"><header><div class=logo>LBJ RECEIVER <b>W</b></div><div class=online><i class=dot></i>AP ONLINE</div></header>
<main><div class=strip><span id=state>%(state)s</span><strong id=live>实时监视中</strong></div>
<section class=hero id=hero><div class=eyebrow>TRAIN NUMBER / 车次</div><div class=train id=train>%(train)s</div>
<div class=metrics><div class="metric speed"><span>速度 km/h</span><strong id=speed>%(speed)s</strong></div><div class="metric km"><span>公里标 km</span><strong id=km>%(km)s</strong></div></div></section>
<section class=route><label>线路</label><strong id=route data-gbk="%(route)s">%(route)s</strong><span class=direction id=direction>%(direction)s</span></section>
<section class=location id=location data-lon="%(longitude)s" data-lat="%(latitude)s"><div class=loc-head><span>POSITION / 列车经纬度</span><strong id=locationState>检查坐标中</strong></div><div class=coordinate-grid>
<div class=coordinate><span>经度</span><strong id=longitude>---</strong></div><div class=coordinate><span>纬度</span><strong id=latitude>---</strong></div></div></section>
<section class=grid>
<div class=cell><span>接收时间</span><strong id=received>%(time)s</strong></div><div class=cell><span>机车</span><strong id=loco>%(loco)s</strong></div>
<div class=cell><span>端位</span><strong id=cab>%(cab)s</strong></div><div class=cell><span>RSSI</span><strong id=rssi>%(rssi)s</strong></div>
<div class="cell wide"><span>数据类型</span><strong id=type>%(type)s</strong></div></section>
<div class=controls><button class=sound id=soundBtn type=button>声音提醒：关闭</button><div class=refresh><strong id=refreshState>正在建立实时通道</strong><span id=streamDetail>收到新报文即更新</span></div></div>
<div class=notice id=notice aria-live=polite></div>
<footer>页面通过 SSE 接收机实时推送，新报文到达即更新；断线时会自动重连并临时使用兼容轮询。声音默认关闭。</footer></main>
<script>
const el=id=>document.getElementById(id);let lastId=document.body.dataset.recordId||"",lastRevision=Number(lastId)||0,soundOn=false,audioCtx=null,stream=null,streamErrors=0,fallbackTimer=null,reopenTimer=null,pollPrimed=true;
function put(id,v){el(id).textContent=(v===undefined||v===null||v==="")?"---":v}
function decodeRoute(hex){if(!hex||hex==="---")return "---";if(hex.length%%2||!/^[0-9a-f]+$/i.test(hex))return "编码 "+hex;try{const pairs=hex.match(/[0-9a-f]{2}/gi),bytes=Uint8Array.from(pairs,x=>parseInt(x,16));const text=new TextDecoder("gbk",{fatal:true}).decode(bytes).replace(/\u0000/g,"").trim();return text||("编码 "+hex)}catch(e){return "编码 "+hex}}
function asCoordinate(value){if(value===null||value===undefined||value==="")return null;const number=Number(value);return Number.isFinite(number)?number:null}
function coordinateText(value,positive,negative){return Math.abs(value).toFixed(6)+"°"+(value<0?negative:positive)}
function renderCoordinates(latitude,longitude,available){const lat=asCoordinate(latitude),lon=asCoordinate(longitude);if(lat===null||lon===null||Math.abs(lat)>90||Math.abs(lon)>180){put("longitude",null);put("latitude",null);el("locationState").textContent=available?"本次无有效坐标":"等待定位报文";return}put("longitude",coordinateText(lon,"E","W"));put("latitude",coordinateText(lat,"N","S"));el("locationState").textContent="坐标有效"}
function tone(){if(!soundOn||!audioCtx)return;try{const t=audioCtx.currentTime,o=audioCtx.createOscillator(),g=audioCtx.createGain();o.frequency.setValueAtTime(880,t);o.frequency.setValueAtTime(1175,t+.09);g.gain.setValueAtTime(.0001,t);g.gain.exponentialRampToValueAtTime(.18,t+.015);g.gain.exponentialRampToValueAtTime(.0001,t+.20);o.connect(g);g.connect(audioCtx.destination);o.start(t);o.stop(t+.21)}catch(e){}}
function announce(){const hero=el("hero");hero.classList.remove("flash");void hero.offsetWidth;hero.classList.add("flash");el("notice").textContent="● 收到新的列车信息  "+new Date().toLocaleTimeString();tone()}
function render(d,fromLive,resetRevision){const id=String(d.update_id||""),revision=Number(id),numeric=Number.isFinite(revision),wrapped=numeric&&lastRevision>1879048192&&revision<268435456;if(resetRevision&&numeric&&revision<lastRevision&&!wrapped){lastRevision=revision;lastId=id}if(wrapped){lastRevision=-1;lastId=""}else if(fromLive&&!resetRevision&&numeric&&revision<lastRevision)return false;const changed=fromLive&&d.available&&(numeric?revision>lastRevision:id&&id!==lastId);put("state",d.available?"最近一次列车信息":"等待列车信号");put("train",d.train_no);put("speed",d.speed);put("km",d.km);put("route",decodeRoute(d.route));put("direction",d.direction);put("received",d.time);put("loco",d.loco);put("cab",d.cab);put("rssi",d.rssi);put("type",d.type);renderCoordinates(d.latitude,d.longitude,d.available);if(changed)announce();if(id)lastId=id;if(numeric)lastRevision=revision;return true}
function clearFallback(){if(fallbackTimer!==null){clearTimeout(fallbackTimer);fallbackTimer=null}}
function scheduleFallback(delay){if(fallbackTimer===null)fallbackTimer=setTimeout(fallbackPoll,delay)}
function fallbackPoll(){fallbackTimer=null;if(stream&&stream.readyState===1)return;el("refreshState").textContent="兼容模式轮询中";fetch("/api/latest?t="+Date.now(),{cache:"no-store"}).then(r=>{if(!r.ok)throw Error(r.status);return r.json()}).then(d=>{if(stream&&stream.readyState===1)return;const first=pollPrimed;pollPrimed=false;if(render(d,true,first))el("streamDetail").textContent="最近同步 "+new Date().toLocaleTimeString()}).catch(()=>{pollPrimed=true;el("streamDetail").textContent="连接失败，继续重试"}).then(()=>{if(!stream||stream.readyState!==1)scheduleFallback(5000)})}
function reconcile(){fetch("/api/latest?t="+Date.now(),{cache:"no-store"}).then(r=>{if(!r.ok)throw Error(r.status);return r.json()}).then(d=>{if(render(d,true))el("streamDetail").textContent="已对账 "+new Date().toLocaleTimeString()}).catch(()=>{})}
function scheduleStreamReopen(){if(reopenTimer===null)reopenTimer=setTimeout(()=>{reopenTimer=null;openStream()},30000)}
function openStream(){if(!window.EventSource){el("refreshState").textContent="浏览器兼容模式";el("streamDetail").textContent="此浏览器不支持实时流";pollPrimed=true;scheduleFallback(0);return}if(stream)stream.close();const opened=new EventSource("/api/events");let streamPrimed=false;stream=opened;streamErrors=0;opened.onopen=()=>{if(stream!==opened)return;streamPrimed=false;streamErrors=0;clearFallback();el("refreshState").textContent="实时推送已连接";el("streamDetail").textContent="收到新报文即更新";el("live").textContent="实时监视中";el("live").style.color="var(--green)"};opened.addEventListener("train",event=>{if(stream!==opened)return;try{const first=!streamPrimed;streamPrimed=true;if(render(JSON.parse(event.data),true,first))el("streamDetail").textContent="刚刚实时更新 "+new Date().toLocaleTimeString()}catch(e){el("streamDetail").textContent="收到异常数据，等待下一条"}});opened.onerror=()=>{if(stream!==opened)return;streamErrors=opened.readyState===2?3:streamErrors+1;el("refreshState").textContent="实时通道重连中";el("streamDetail").textContent="暂用兼容轮询";el("live").textContent="正在重连";el("live").style.color="var(--yellow)";pollPrimed=true;scheduleFallback(2000);if(streamErrors>=3){opened.close();stream=null;el("refreshState").textContent="兼容模式（实时流忙）";el("streamDetail").textContent="5 秒同步；30 秒后重试实时流";scheduleFallback(0);scheduleStreamReopen()}}}
el("soundBtn").onclick=function(){soundOn=!soundOn;if(soundOn){try{const button=this,A=window.AudioContext||window.webkitAudioContext;if(!A)throw Error();audioCtx=audioCtx||new A();button.textContent="正在启用声音…";const ready=audioCtx.state==="suspended"?audioCtx.resume():Promise.resolve();ready.then(()=>{if(!soundOn)return;tone();button.textContent="声音提醒：开启";button.classList.add("on");el("notice").textContent="声音提醒已开启（刚才是测试音）"}).catch(()=>{soundOn=false;button.textContent="声音提醒：关闭（未授权）";button.classList.remove("on");el("notice").textContent="浏览器阻止了声音，请再次点按重试"})}catch(e){soundOn=false;this.textContent="此浏览器不支持声音"}}else{this.textContent="声音提醒：关闭";this.classList.remove("on");el("notice").textContent="声音提醒已关闭"}};
put("route",decodeRoute(el("route").dataset.gbk));renderCoordinates(el("location").dataset.lat,el("location").dataset.lon,el("train").textContent!=="---");openStream();document.addEventListener("visibilitychange",()=>{if(!document.hidden){reconcile();if(!stream)scheduleFallback(0)}});window.addEventListener("pagehide",()=>{clearFallback();if(reopenTimer!==null)clearTimeout(reopenTimer);if(stream)stream.close()});
</script></body></html>""" % values

    def _sse_event(self):
        view = self._view_model()
        revision = view["update_id"]
        payload = json.dumps(view)
        event = ("id: %s\nevent: train\ndata: %s\n\n" % (revision, payload)).encode(
            "utf-8"
        )
        if len(event) > MAX_SSE_EVENT:
            raise ValueError("SSE event too large")
        return event

    def _sse_peer_closed(self, state):
        if state[CLIENT_RESPONSE] is not None:
            return False
        try:
            chunk = state[CLIENT_SOCKET].recv(1)
        except OSError as exc:
            return not _is_would_block(exc)
        except Exception:
            # Allocation pressure while probing is not proof that the peer is
            # gone.  Preserve the established stream and reject the newcomer.
            return False
        return chunk == b"" or bool(chunk)

    def _start_sse(self, state, now):
        # Only one persistent stream is allowed so a second TCP slot always
        # remains available for the portal, captive probes, and JSON fallback.
        # Never replace a healthy stream: two EventSource pages would otherwise
        # reconnect forever and repeatedly kick each other off.
        for other in self._clients:
            if other[CLIENT_MODE] == MODE_SSE:
                if self._sse_peer_closed(other):
                    self._finish_client(other)
                    self._clients.remove(other)
                    break
                state[CLIENT_RESPONSE] = _http_response(
                    "503 Service Unavailable",
                    "text/plain",
                    "Realtime stream busy",
                    "Retry-After: 3\r\n",
                )
                state[CLIENT_REQUEST] = None
                state[CLIENT_SENT] = 0
                state[CLIENT_PROGRESS] = now
                return False
        state[CLIENT_RESPONSE] = _sse_headers() + self._sse_event()
        state[CLIENT_REQUEST] = None
        state[CLIENT_SENT] = 0
        state[CLIENT_MODE] = MODE_SSE
        state[CLIENT_REVISION] = self._latest_revision
        state[CLIENT_PROGRESS] = now
        state[CLIENT_HEARTBEAT] = now
        return True

    def _build_http_response(self, request):
        try:
            method, path = _parse_http_request(request)
        except Exception:
            return _http_response("400 Bad Request", "text/plain", "Bad Request")

        head = method == "HEAD"
        if method not in ("GET", "HEAD"):
            return _http_response(
                "405 Method Not Allowed",
                "text/plain",
                "Method Not Allowed",
                "Allow: GET, HEAD\r\n",
                head=head,
            )
        if path == "/api/latest":
            body = json.dumps(self._view_model())
            return _http_response("200 OK", "application/json; charset=utf-8", body, head=head)
        if path == "/api/events":
            if head:
                return _http_response(
                    "200 OK", "text/event-stream; charset=utf-8", b"", head=True
                )
            # Normal socket servicing intercepts this path and keeps it open.
            # Returning a valid first frame here also keeps helper/unit callers
            # deterministic without adding a second response builder.
            return _sse_headers() + self._sse_event()
        if path in ("/", "/index.html"):
            # The full self-contained page is the largest temporary allocation
            # in the portal.  Reclaim completed UI/history objects first so a
            # long-running receiver can always serve a fresh captive page.
            gc.collect()
            return _http_response(
                "200 OK", "text/html; charset=utf-8", self._render_page(), head=head
            )

        # A redirect is intentionally returned for every OS connectivity probe
        # and unknown HTTP path.  HTTPS is never intercepted.
        return _http_response(
            "302 Found",
            "text/plain",
            b"",
            "Location: http://%s/\r\n" % self._ip,
            head=head,
        )

    def _finish_client(self, client_state):
        self._close_socket(client_state[0])

    def _service_http_client(self, now):
        if not self._clients:
            return
        state = self._clients.pop(0)
        client = state[CLIENT_SOCKET]
        request = state[CLIENT_REQUEST]
        response = state[CLIENT_RESPONSE]
        sent = state[CLIENT_SENT]

        if response is not None:
            try:
                count = client.send(response[sent : sent + MAX_HTTP_SEND])
            except OSError as exc:
                if _is_would_block(exc):
                    count = None
                else:
                    self._finish_client(state)
                    return
            except Exception as exc:
                # A slice/send allocation failure belongs to this client, not
                # to the AP.  Never turn one slow page into a hotspot restart.
                print("WIFI_CLIENT_ERR", repr(exc))
                self._finish_client(state)
                return
            if count is not None and count <= 0:
                self._finish_client(state)
                return
            if count is not None:
                state[CLIENT_PROGRESS] = now
                state[CLIENT_SENT] += count
            if state[CLIENT_SENT] >= len(response):
                if state[CLIENT_MODE] == MODE_SSE:
                    state[CLIENT_RESPONSE] = None
                    state[CLIENT_SENT] = 0
                    state[CLIENT_HEARTBEAT] = now
                    self._clients.append(state)
                else:
                    self._finish_client(state)
            elif _ticks_diff(now, state[CLIENT_PROGRESS]) >= (
                SSE_SEND_TIMEOUT_MS
                if state[CLIENT_MODE] == MODE_SSE
                else HTTP_CLIENT_TIMEOUT_MS
            ):
                self._finish_client(state)
            else:
                self._clients.append(state)
            return

        if state[CLIENT_MODE] == MODE_SSE:
            # Probe FIN once per second rather than allocating/catching an
            # EAGAIN exception on every ~1 ms UI loop iteration.
            if _ticks_diff(now, state[CLIENT_PROGRESS]) >= SSE_FIN_CHECK_MS:
                chunk = None
                try:
                    chunk = client.recv(1)
                except OSError as exc:
                    if not _is_would_block(exc):
                        self._finish_client(state)
                        return
                except Exception as exc:
                    print("WIFI_CLIENT_ERR", repr(exc))
                    self._finish_client(state)
                    return
                state[CLIENT_PROGRESS] = now
                if chunk == b"" or chunk:
                    self._finish_client(state)
                    return
            try:
                if state[CLIENT_REVISION] != self._latest_revision:
                    state[CLIENT_RESPONSE] = self._sse_event()
                    state[CLIENT_SENT] = 0
                    state[CLIENT_REVISION] = self._latest_revision
                    state[CLIENT_PROGRESS] = now
                elif _ticks_diff(now, state[CLIENT_HEARTBEAT]) >= SSE_HEARTBEAT_MS:
                    state[CLIENT_RESPONSE] = b": keepalive\n\n"
                    state[CLIENT_SENT] = 0
                    state[CLIENT_PROGRESS] = now
                    state[CLIENT_HEARTBEAT] = now
            except Exception as exc:
                print("WIFI_SSE_ERR", repr(exc))
                self._finish_client(state)
                return
            self._clients.append(state)
            return

        chunk = None
        try:
            chunk = client.recv(512)
        except OSError as exc:
            if not _is_would_block(exc):
                self._finish_client(state)
                return
        except Exception as exc:
            print("WIFI_CLIENT_ERR", repr(exc))
            self._finish_client(state)
            return

        if chunk:
            try:
                request.extend(chunk)
            except Exception as exc:
                print("WIFI_CLIENT_ERR", repr(exc))
                self._finish_client(state)
                return
            state[CLIENT_PROGRESS] = now
        if b"\r\n\r\n" in request or len(request) >= MAX_HTTP_REQUEST:
            try:
                limited_request = bytes(request[:MAX_HTTP_REQUEST])
                method, path = _parse_http_request(limited_request)
                if method == "GET" and path == "/api/events":
                    self._start_sse(state, now)
                else:
                    state[CLIENT_RESPONSE] = self._build_http_response(limited_request)
                    state[CLIENT_REQUEST] = None
            except Exception as exc:
                # A malformed request or transient allocation failure belongs
                # to this client.  It must not tear down the AP or persist the
                # user's Wi-Fi switch as OFF.
                print("WIFI_HTTP_ERR", repr(exc))
                self._finish_client(state)
                return
            state[CLIENT_SENT] = 0
            self._clients.append(state)
        elif chunk == b"" or _ticks_diff(now, state[CLIENT_PROGRESS]) >= HTTP_CLIENT_TIMEOUT_MS:
            self._finish_client(state)
        else:
            self._clients.append(state)

    def service(self, now=None):
        if not self._enabled:
            return
        now = _ticks_ms() if now is None else now
        self._service_dns()
        self._service_http_client(now)
        # Free a completed short request before accepting the next connection.
        # When both slots remain occupied, leave new TCP handshakes in the
        # listen backlog instead of accept+RST cycling them.
        if len(self._clients) < MAX_HTTP_CLIENTS:
            self._accept_http(now)

        if _ticks_diff(now, self._health_at) >= 5000:
            self._health_at = now
            try:
                if not self._ap.active():
                    raise OSError("AP became inactive")
            except Exception as exc:
                self._last_error = str(exc)[:48]
                self._stop(clear_error=False)
