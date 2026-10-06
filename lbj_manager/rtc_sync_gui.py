import datetime as dt
import email.utils
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib import request

try:
    from serial.tools import list_ports
except Exception:
    list_ports = None


if len(sys.argv) > 1 and sys.argv[1] == "mpremote_internal":
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from mpremote.main import main

    try:
        main()
    except SystemExit as exc:
        sys.exit(exc.code)
    sys.exit(0)


BEIJING_OFFSET = dt.timedelta(hours=8)
NTP_EPOCH_DELTA = 2208988800

NTP_HOSTS = (
    "ntp.aliyun.com",
    "ntp.tencent.com",
    "cn.pool.ntp.org",
    "pool.ntp.org",
    "time.windows.com",
)

HTTP_TIME_URLS = (
    "http://www.baidu.com",
    "http://www.qq.com",
    "http://www.microsoft.com",
)


def format_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def fetch_ntp_beijing_time(timeout=3):
    packet = b"\x1b" + (b"\0" * 47)
    last_error = None

    for host in NTP_HOSTS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(packet, (host, 123))
                data, _ = sock.recvfrom(48)
            if len(data) < 48:
                raise OSError("short NTP packet")
            seconds, fraction = struct.unpack("!II", data[40:48])
            timestamp = seconds - NTP_EPOCH_DELTA + (fraction / 2**32)
            value = dt.datetime.utcfromtimestamp(timestamp) + BEIJING_OFFSET
            return value.replace(microsecond=0), "NTP " + host
        except Exception as exc:
            last_error = exc

    for url in HTTP_TIME_URLS:
        try:
            req = request.Request(url, method="HEAD")
            with request.urlopen(req, timeout=timeout) as resp:
                date_header = resp.headers.get("Date")
            if not date_header:
                raise OSError("missing Date header")
            value_utc = email.utils.parsedate_to_datetime(date_header)
            if value_utc.tzinfo is not None:
                value_utc = value_utc.astimezone(dt.timezone.utc).replace(tzinfo=None)
            value = value_utc + BEIJING_OFFSET
            return value.replace(microsecond=0), "HTTP Date " + url
        except Exception as exc:
            last_error = exc

    raise RuntimeError("network time failed: %s" % last_error)


def build_pico_rtc_script(value=None):
    if value is None:
        sync = "False"
        year = month = day = hour = minute = second = 0
    else:
        sync = "True"
        year = value.year
        month = value.month
        day = value.day
        hour = value.hour
        minute = value.minute
        second = value.second

    header = "\n".join(
        [
            "SYNC = %s" % sync,
            "TARGET_YEAR = %d" % year,
            "TARGET_MONTH = %d" % month,
            "TARGET_DAY = %d" % day,
            "TARGET_HOUR = %d" % hour,
            "TARGET_MINUTE = %d" % minute,
            "TARGET_SECOND = %d" % second,
            "",
        ]
    )

    body = r'''
from machine import Pin, I2C
import time

DS3231_ADDR = 0x68
PCF8563_ADDR = 0x51


def dec2bcd(value):
    return ((value // 10) << 4) | (value % 10)


def bcd2dec(value):
    return ((value >> 4) * 10) + (value & 0x0F)


def detect(i2c):
    devices = i2c.scan()
    print("I2C_DEVICES", ",".join([hex(addr) for addr in devices]))
    if DS3231_ADDR in devices:
        return "DS3231", DS3231_ADDR
    if PCF8563_ADDR in devices:
        return "PCF8563", PCF8563_ADDR
    return "UNKNOWN", None


def clear_ds3231_osf(i2c, addr):
    try:
        status = i2c.readfrom_mem(addr, 0x0F, 1)[0]
        i2c.writeto_mem(addr, 0x0F, bytes([status & 0x7F]))
    except Exception:
        pass


def start_pcf8563(i2c, addr):
    try:
        control = i2c.readfrom_mem(addr, 0x00, 1)[0]
        i2c.writeto_mem(addr, 0x00, bytes([control & 0xDF]))
    except Exception:
        pass


def write_ds3231(i2c, addr):
    data = bytes([
        dec2bcd(TARGET_SECOND),
        dec2bcd(TARGET_MINUTE),
        dec2bcd(TARGET_HOUR),
        1,
        dec2bcd(TARGET_DAY),
        dec2bcd(TARGET_MONTH),
        dec2bcd(TARGET_YEAR % 100),
    ])
    i2c.writeto_mem(addr, 0x00, data)
    clear_ds3231_osf(i2c, addr)


def write_pcf8563(i2c, addr):
    start_pcf8563(i2c, addr)
    data = bytes([
        dec2bcd(TARGET_SECOND),
        dec2bcd(TARGET_MINUTE),
        dec2bcd(TARGET_HOUR),
        dec2bcd(TARGET_DAY),
        0,
        dec2bcd(TARGET_MONTH),
        dec2bcd(TARGET_YEAR % 100),
    ])
    i2c.writeto_mem(addr, 0x02, data)


def read_ds3231(i2c, addr):
    data = i2c.readfrom_mem(addr, 0x00, 7)
    second = bcd2dec(data[0] & 0x7F)
    minute = bcd2dec(data[1] & 0x7F)
    hour_reg = data[2]
    if hour_reg & 0x40:
        hour = bcd2dec(hour_reg & 0x1F)
        if hour_reg & 0x20:
            hour = (hour % 12) + 12
        elif hour == 12:
            hour = 0
    else:
        hour = bcd2dec(hour_reg & 0x3F)
    day = bcd2dec(data[4] & 0x3F)
    month = bcd2dec(data[5] & 0x1F)
    year = 2000 + bcd2dec(data[6])
    return year, month, day, hour, minute, second


def read_pcf8563(i2c, addr):
    data = i2c.readfrom_mem(addr, 0x02, 7)
    second = bcd2dec(data[0] & 0x7F)
    minute = bcd2dec(data[1] & 0x7F)
    hour = bcd2dec(data[2] & 0x3F)
    day = bcd2dec(data[3] & 0x3F)
    month = bcd2dec(data[5] & 0x1F)
    year = 2000 + bcd2dec(data[6])
    return year, month, day, hour, minute, second


i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=400000)
model, addr = detect(i2c)
if addr is None:
    print("RTC_ERROR not_found")
else:
    print("RTC_MODEL", model)
    if SYNC:
        if model == "DS3231":
            write_ds3231(i2c, addr)
        else:
            write_pcf8563(i2c, addr)
        time.sleep_ms(100)
        print("RTC_SYNC_OK")
    if model == "DS3231":
        now = read_ds3231(i2c, addr)
    else:
        now = read_pcf8563(i2c, addr)
    print("RTC_NOW %04d-%02d-%02d %02d:%02d:%02d" % now)
'''
    return header + body


class RTCSyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pico RTC Sync")
        self.geometry("760x520")
        self.minsize(680, 460)

        self.port_var = tk.StringVar()
        self.machine_time_var = tk.StringVar()
        self.network_time_var = tk.StringVar(value="未获取")
        self.rtc_time_var = tk.StringVar(value="未读取")
        self.status_var = tk.StringVar(value="就绪")
        self.last_network_time = None

        self._build_ui()
        self.refresh_ports()
        self._tick_machine_time()

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        title = ttk.Label(root, text="Pico RTC 同步工具", font=("TkDefaultFont", 18, "bold"))
        title.grid(row=0, column=0, sticky="w", pady=(0, 14))

        conn = ttk.LabelFrame(root, text="设备")
        conn.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        conn.columnconfigure(1, weight=1)

        ttk.Label(conn, text="串口").grid(row=0, column=0, padx=(10, 8), pady=10)
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var)
        self.port_combo.grid(row=0, column=1, sticky="ew", pady=10)
        ttk.Button(conn, text="扫描", command=self.refresh_ports).grid(
            row=0, column=2, padx=10, pady=10
        )
        ttk.Button(conn, text="读取 RTC", command=self.read_rtc).grid(
            row=0, column=3, padx=(0, 10), pady=10
        )

        times = ttk.LabelFrame(root, text="时间")
        times.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        times.columnconfigure(1, weight=1)

        rows = (
            ("电脑时间", self.machine_time_var),
            ("联网时间(北京时间)", self.network_time_var),
            ("RTC 当前时间", self.rtc_time_var),
        )
        for row, (label, var) in enumerate(rows):
            ttk.Label(times, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=6)
            ttk.Label(times, textvariable=var).grid(row=row, column=1, sticky="w", padx=10, pady=6)

        actions = ttk.Frame(root)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        for col in range(4):
            actions.columnconfigure(col, weight=1)

        ttk.Button(actions, text="联网校时", command=self.fetch_network_time).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(actions, text="写入电脑时间", command=self.write_machine_time).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(actions, text="联网并写入北京时间", command=self.fetch_and_write_network_time).grid(
            row=0, column=2, sticky="ew", padx=8
        )
        ttk.Button(actions, text="清空日志", command=self.clear_log).grid(
            row=0, column=3, sticky="ew", padx=(8, 0)
        )

        log_frame = ttk.LabelFrame(root, text="日志")
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        ttk.Label(root, textvariable=self.status_var).grid(row=5, column=0, sticky="w", pady=(10, 0))

    def _tick_machine_time(self):
        self.machine_time_var.set(format_dt(dt.datetime.now().replace(microsecond=0)))
        self.after(500, self._tick_machine_time)

    def log(self, text):
        def append():
            self.log_text.insert("end", text + "\n")
            self.log_text.see("end")

        self.after(0, append)

    def clear_log(self):
        self.log_text.delete("1.0", "end")

    def set_status(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def refresh_ports(self):
        values = []
        if list_ports:
            values = [port.device for port in list_ports.comports()]
        else:
            self.log("未安装 pyserial，串口可手动输入，例如 COM3 或 /dev/cu.usbmodemXXXX。")

        self.port_combo.configure(values=values)
        if values and not self.port_var.get():
            self.port_var.set(values[0])
        self.log("串口扫描完成：" + (", ".join(values) if values else "未发现串口"))

    def selected_port(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("缺少串口", "请先选择或输入 Pico 串口。")
            return None
        return port

    def run_mpremote(self, port, args, timeout=30):
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "mpremote_internal", "connect", port] + args
        else:
            cmd = [sys.executable, "-m", "mpremote", "connect", port] + args

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode == 0, proc.stdout

    def run_pico_script(self, port, script_text, timeout=30):
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fp:
                fp.write(script_text)
                temp_path = fp.name
            return self.run_mpremote(port, ["run", temp_path], timeout=timeout)
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def start_worker(self, status, target):
        self.set_status(status)
        threading.Thread(target=target, daemon=True).start()

    def fetch_network_time(self):
        def worker():
            try:
                self.log("正在联网获取北京时间...")
                value, source = fetch_ntp_beijing_time()
                self.last_network_time = value
                self.after(0, lambda: self.network_time_var.set(format_dt(value) + "  (" + source + ")"))
                self.log("联网时间获取成功：" + format_dt(value) + " / " + source)
                self.set_status("联网校时完成")
            except Exception as exc:
                self.log("联网校时失败：" + str(exc))
                self.set_status("联网校时失败")

        self.start_worker("联网校时中", worker)

    def read_rtc(self):
        port = self.selected_port()
        if not port:
            return

        def worker():
            try:
                self.log("正在读取 RTC...")
                ok, output = self.run_pico_script(port, build_pico_rtc_script(None), timeout=30)
                self.log(output.strip())
                if not ok or "RTC_NOW" not in output:
                    raise RuntimeError("读取失败")
                rtc_line = [line for line in output.splitlines() if line.startswith("RTC_NOW")][-1]
                value = rtc_line.replace("RTC_NOW", "", 1).strip()
                self.after(0, lambda: self.rtc_time_var.set(value))
                self.set_status("RTC 读取完成")
            except Exception as exc:
                self.log("RTC 读取失败：" + str(exc))
                self.set_status("RTC 读取失败")

        self.start_worker("读取 RTC 中", worker)

    def write_datetime(self, value, source):
        port = self.selected_port()
        if not port:
            return

        if not messagebox.askyesno(
            "确认写入 RTC",
            "将把 %s 写入 Pico RTC：\n\n%s\n\n继续吗？" % (source, format_dt(value)),
        ):
            self.log("用户取消写入 RTC。")
            return

        def worker():
            try:
                self.log("正在写入 RTC：" + format_dt(value))
                ok, output = self.run_pico_script(port, build_pico_rtc_script(value), timeout=30)
                self.log(output.strip())
                if not ok or "RTC_SYNC_OK" not in output:
                    raise RuntimeError("写入失败")
                rtc_line = [line for line in output.splitlines() if line.startswith("RTC_NOW")][-1]
                rtc_value = rtc_line.replace("RTC_NOW", "", 1).strip()
                self.after(0, lambda: self.rtc_time_var.set(rtc_value))
                self.log("RTC 写入完成。")
                self.set_status("RTC 写入完成")
            except Exception as exc:
                self.log("RTC 写入失败：" + str(exc))
                self.set_status("RTC 写入失败")

        self.start_worker("写入 RTC 中", worker)

    def write_machine_time(self):
        self.write_datetime(dt.datetime.now().replace(microsecond=0), "电脑时间")

    def fetch_and_write_network_time(self):
        port = self.selected_port()
        if not port:
            return

        def worker():
            try:
                self.log("正在联网获取北京时间...")
                value, source = fetch_ntp_beijing_time()
                self.last_network_time = value
                self.after(0, lambda: self.network_time_var.set(format_dt(value) + "  (" + source + ")"))
                self.log("联网时间获取成功：" + format_dt(value) + " / " + source)
                self.after(0, lambda: self.write_datetime(value, "联网北京时间"))
                self.set_status("等待确认写入")
            except Exception as exc:
                self.log("联网并写入失败：" + str(exc))
                self.set_status("联网失败")

        self.start_worker("联网校时中", worker)


if __name__ == "__main__":
    app = RTCSyncApp()
    app.mainloop()
