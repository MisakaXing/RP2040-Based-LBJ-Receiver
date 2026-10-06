"""LBJ Manager: firmware, assembly inspection and train history.

Single desktop entry point and source of truth for the combined application.
The integration layer owns device selection, task leases and main-thread UI
dispatch. This module runs without either of the retired tool directories.
"""
import sys
if len(sys.argv) > 1 and sys.argv[1] == "mpremote_internal":
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from mpremote.main import main
    raise SystemExit(main())


# Verified history-transfer engine
"""Bounded, verified history downloads; no writes to the Pico filesystem."""


import ast


from dataclasses import dataclass


import hashlib


import os


import tempfile


CHUNK_SIZE = 4096


COMMAND_TIMEOUT = 20


class HistoryTransferError(RuntimeError):
    pass


@dataclass
class HistoryDownload:
    data: bytes
    sha256: str
    recovery_warning: str = ""


def _connect(port):
    # Also bundled by PyInstaller; no child process or command-line timeout.
    from mpremote.transport_serial import SerialTransport

    return SerialTransport(port)


def _execute(transport, command):
    output, error = transport.exec_raw(command, timeout=COMMAND_TIMEOUT)
    if error:
        raise HistoryTransferError(error.decode("utf-8", errors="replace").strip())
    return output.strip()


def download_history(port, progress=None, status=None, transport_factory=None):
    """Pause/reset the interpreter, download in small transactions, then reboot.

    Each block has its own EOF and timeout, independent of total file size.
    The initial raw-REPL reset stops the receiver's second core/timers too, so
    background radio prints cannot be mistaken for file data. main.py is not
    executed in this raw-REPL session. The final hardware reset runs it again.
    """
    def observe(callback, *args):
        # A closed/busy GUI must not prevent the finally block from rebooting
        # the receiver. Observers do not control the transport lifecycle.
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                pass

    def notify(text):
        observe(status, text)

    def report(done, total):
        observe(progress, done, total)

    transport = None
    failure = None
    recovery_warning = ""
    data = bytearray()
    digest = hashlib.sha256()
    synchronized = False
    try:
        notify("正在暂停接收程序...")
        transport = (transport_factory or _connect)(port)
        # Bound low-level reads/writes too (older mpremote defaults to None).
        transport.serial.timeout = 2
        transport.serial.write_timeout = 5
        transport.use_raw_paste = False
        transport.enter_raw_repl(soft_reset=True)
        synchronized = True
        size = ast.literal_eval(_execute(transport,
            "import os as _lv_os, gc as _lv_gc, hashlib as _lv_hashlib\n"
            "_lv_gc.collect()\n"
            "_lv_file = open('history.jsonl', 'rb')\n"
            "_lv_hash = _lv_hashlib.sha256()\n"
            "print(_lv_os.stat('history.jsonl')[6])"
        ).decode("ascii"))
        if type(size) is not int or size < 0:
            raise HistoryTransferError("设备返回了无效的文件长度。")
        report(0, size)
        while len(data) < size:
            requested = min(CHUNK_SIZE, size - len(data))
            # Decode a complete bytes literal, never individual UTF-8 chunks.
            # The device holds only one small block, not the whole history.
            chunk = ast.literal_eval(_execute(transport,
                "_lv_block = _lv_file.read(%d)\n"
                "_lv_hash.update(_lv_block)\n"
                "print(repr(_lv_block))\n"
                "del _lv_block\n_lv_gc.collect()" % requested
            ).decode("ascii"))
            if not isinstance(chunk, bytes) or len(chunk) != requested:
                raise HistoryTransferError("历史文件提前结束或数据块长度不符，请重新读取。")
            data.extend(chunk)
            digest.update(chunk)
            report(len(data), size)

        notify("正在校验文件完整性...")
        remote_size, tail, remote_digest = ast.literal_eval(_execute(transport,
            "print(repr((_lv_os.stat('history.jsonl')[6], "
            "_lv_file.read(1), _lv_hash.digest())))\n_lv_file.close()"
        ).decode("ascii"))
        if remote_size != size or tail != b"":
            raise HistoryTransferError("读取期间历史文件长度发生变化，请重新读取。")
        if remote_digest != digest.digest():
            raise HistoryTransferError("历史文件 SHA-256 校验失败，未采用不完整数据。")
    except Exception as exc:
        synchronized = False
        failure = exc
    finally:
        if transport is not None:
            try:
                notify("正在恢复设备运行...")
                if not synchronized:
                    # A timed-out command can still be printing. Interrupt it
                    # and reacquire raw REPL before issuing a reset command.
                    transport.enter_raw_repl(soft_reset=False)
                # A reset has no normal EOF; do not wait for one.
                transport.exec_raw_no_follow("import machine; machine.reset()")
            except Exception as exc:
                recovery_warning = "未能确认已发送设备重启指令，请手动复位。原因：%s" % exc
            finally:
                try:
                    transport.close()
                except Exception as exc:
                    recovery_warning = recovery_warning or "关闭串口失败：%s" % exc

    if failure is not None:
        detail = str(failure)
        if "EOF" in detail or "timeout" in detail.lower():
            detail = "传输某个数据块时设备未及时响应（已读取 %d 字节）。\n%s" % (len(data), detail)
        if recovery_warning:
            detail += "\n\n" + recovery_warning
        raise HistoryTransferError(detail) from failure
    return HistoryDownload(bytes(data), digest.hexdigest(), recovery_warning)


def save_history_atomic(path, data):
    """Only replace the chosen export after all verified bytes reach disk."""
    path = os.path.abspath(path)
    fd, temporary = tempfile.mkstemp(prefix=".lbj-history-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# Firmware-update engine
import sys


import os


import re


import threading


import tempfile


import subprocess


import zipfile


import shutil


from urllib.parse import quote


import requests


import customtkinter as ctk


import serial.tools.list_ports


from tkinter import messagebox, filedialog


ctk.set_appearance_mode("Dark")


ctk.set_default_color_theme("blue")


COLORS = {
    "bg": "#101317",
    "sidebar": "#15191E",
    "surface": "#1A1F25",
    "surface_alt": "#20262D",
    "border": "#303841",
    "text": "#F3F6F8",
    "muted": "#96A1AC",
    "teal": "#26B8A6",
    "teal_hover": "#209A8C",
    "blue": "#4C8DFF",
    "blue_hover": "#3C73D2",
    "amber": "#E9AD4A",
    "green": "#45B97C",
    "green_hover": "#389966",
    "red": "#E46A6A",
    "red_hover": "#BD5555",
}


GITHUB_REPO = "MisakaXing/RP2040-Based-LBJ-Receiver"


TARGET_DIR = "rp2040-main-program"


STANDARD_CHANNEL_LABEL = "标准版（main / RP2040）"


WIRELESS_CHANNEL_LABEL = "无线 W 版（Wireless-Enabled）"


DEFAULT_CHANNEL_LABEL = STANDARD_CHANNEL_LABEL


COMMON_RUNTIME_FILES = (
    "HZK16",
    "boot_post.py",
    "ili9341.py",
    "lbj_receiver.py",
    "locos.json",
    "rtc_ds3231.py",
    "sdcard.py",
)


FIRMWARE_BRANCHES = {
    STANDARD_CHANNEL_LABEL: {
        "label": STANDARD_CHANNEL_LABEL,
        "branch": "main",
        "family": "rp2040",
        "hardware_hint": "仅适用于 RP2040 Pico 标准接收器",
        "runtime_files": COMMON_RUNTIME_FILES + ("main.py",),
        "optional_runtime_files": ("boot.py", "pio_dma_rx.py", "device_protection.py"),
    },
    WIRELESS_CHANNEL_LABEL: {
        "label": WIRELESS_CHANNEL_LABEL,
        "branch": "Wireless-Enabled",
        "family": "waveshare_w",
        "hardware_hint": "仅适用于 Waveshare RP2350B-Plus-W",
        "runtime_files": COMMON_RUNTIME_FILES + (
            "history_store.py",
            "wireless_portal.py",
            "main.py",
        ),
        "optional_runtime_files": ("boot.py", "pio_dma_rx.py", "device_protection.py"),
    },
}


PROGRAM_VERSION_RE = re.compile(
    r"\bProgram_ver\s*=\s*(?P<value>[\"'][^\"'\r\n]+[\"']|[0-9][A-Za-z0-9._-]*)"
)


HARDWARE_PROBE_PREFIX = "LBJ_HW_PROBE_V1"


MIN_WIRELESS_FS_BYTES = 12 * 1024 * 1024


HARDWARE_PROBE_SCRIPT = """import sys
import os
import machine

def safe(value):
    return str(value).replace("|", " ").replace("\\r", " ").replace("\\n", " ")

implementation = sys.implementation
build = getattr(implementation, "_build", "")
impl_machine = getattr(implementation, "_machine", "")
try:
    uname_machine = getattr(os.uname(), "machine", "")
except Exception:
    uname_machine = ""

network_ok = 0
try:
    import network
    wlan_type = getattr(network.WLAN, "IF_STA", None)
    if wlan_type is None:
        wlan_type = getattr(network, "STA_IF", None)
    if wlan_type is None:
        raise OSError("STA interface missing")
    network.WLAN(wlan_type)
    network_ok = 1
except Exception:
    pass

pins_ok = 0
try:
    machine.Pin(41)
    machine.Pin(42)
    pins_ok = 1
except Exception:
    pass

fs_bytes = 0
try:
    values = os.statvfs("/")
    fs_bytes = int(values[0]) * int(values[2])
except Exception:
    pass

print("LBJ_HW_PROBE_V1|build=%s|impl_machine=%s|uname_machine=%s|platform=%s|network=%d|pins_41_42=%d|fs_bytes=%d" % (
    safe(build), safe(impl_machine), safe(uname_machine), safe(sys.platform),
    network_ok, pins_ok, fs_bytes
))
"""


def get_firmware_profile(selection):
    if selection in FIRMWARE_BRANCHES:
        return FIRMWARE_BRANCHES[selection]
    for profile in FIRMWARE_BRANCHES.values():
        if selection == profile["branch"]:
            return profile
    raise ValueError("未知固件分支: " + str(selection))


def build_github_urls(repo, target_dir, branch):
    branch_path = quote(str(branch), safe="")
    target_path = quote(str(target_dir).strip("/"), safe="/")
    return (
        f"https://raw.githubusercontent.com/{repo}/{branch_path}/{target_path}/main.py",
        f"https://api.github.com/repos/{repo}/contents/{target_path}?ref={branch_path}",
    )


def parse_program_version(text, file_names=()):
    match = PROGRAM_VERSION_RE.search(text or "")
    if not match:
        return {"label": "", "version": (), "branch": ""}

    label = match.group("value").strip().strip("\"'")
    number_match = re.match(r"(\d+(?:\.\d+)*)", label)
    version = tuple(
        int(part) for part in number_match.group(1).split(".")
    ) if number_match else ()

    names = {str(name).lower() for name in file_names}
    source_lower = (text or "").lower()
    is_wireless = (
        label.upper().endswith("-W")
        or "wireless_portal" in source_lower
        or "wireless_portal.py" in names
        or "history_store.py" in names
    )
    return {
        "label": label,
        "version": version,
        "branch": "Wireless-Enabled" if is_wireless else "main",
    }


def version_is_at_least(local_info, remote_info):
    local = tuple(local_info.get("version", ()))
    remote = tuple(remote_info.get("version", ()))
    return bool(local and remote and local >= remote)


def version_as_number(info):
    version = tuple(info.get("version", ()))
    if not version:
        return 0.0
    try:
        return float(".".join(str(part) for part in version[:2]))
    except (TypeError, ValueError):
        return 0.0


def version_display(info, missing="未知"):
    label = str(info.get("label", ""))
    return f"v{label}" if label else missing


def runtime_file_order(profile):
    required = tuple(profile["runtime_files"])
    optional = tuple(profile.get("optional_runtime_files", ()))
    return tuple(name for name in required if name != "main.py") + optional + ("main.py",)


def select_runtime_files(profile, files_data):
    by_name = {
        item.get("name"): item
        for item in files_data
        if isinstance(item, dict) and item.get("type") == "file"
    }
    required = tuple(profile["runtime_files"])
    missing = [name for name in required if name not in by_name]
    return [by_name[name] for name in runtime_file_order(profile) if name in by_name], missing


def parse_hardware_probe(output):
    for raw_line in reversed((output or "").splitlines()):
        line = raw_line.strip()
        if not line.startswith(HARDWARE_PROBE_PREFIX + "|"):
            continue
        info = {}
        for field in line.split("|")[1:]:
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            info[key] = value.strip()
        for key in ("network", "pins_41_42", "fs_bytes"):
            try:
                info[key] = int(info.get(key, 0))
            except (TypeError, ValueError):
                info[key] = 0
        return info
    return None


def hardware_identity(info):
    if not info:
        return "未知硬件"
    values = [
        info.get("build", ""),
        info.get("impl_machine", ""),
        info.get("uname_machine", ""),
    ]
    return next((str(value) for value in values if value), "未知硬件")


def evaluate_hardware_compatibility(profile, info):
    if not info:
        return False, "无法读取开发板身份，已阻止刷入"

    identity = " ".join(str(info.get(key, "")) for key in (
        "build", "impl_machine", "uname_machine", "platform"
    )).upper()
    compact = re.sub(r"[^A-Z0-9]+", "", identity)
    is_waveshare_w = "WAVESHARERP2350BPLUSW" in compact

    if profile["family"] == "rp2040":
        if is_waveshare_w:
            return False, "检测到 Waveshare RP2350B-Plus-W，不能刷入标准版"
        if "RP2040" not in compact:
            return False, "标准版仅支持 RP2040 Pico，当前板型不兼容"
        return True, "RP2040 Pico 与标准版兼容"

    if not is_waveshare_w:
        return False, (
            "无线 W 版仅支持 Waveshare RP2350B-Plus-W；"
            "请先刷入仓库提供的 Waveshare 专用 UF2"
        )
    if not info.get("network"):
        return False, "未检测到 CYW43 network.WLAN，无线固件不兼容"
    if not info.get("pins_41_42"):
        return False, "专板 GPIO41/42 不可用，无线固件不兼容"
    if int(info.get("fs_bytes", 0)) < MIN_WIRELESS_FS_BYTES:
        return False, "文件系统小于 12 MiB，不符合 16 MiB W 版布局"
    return True, "Waveshare RP2350B-Plus-W 与无线 W 版兼容"


HARDWARE_TEST_SCRIPT = """import machine
import time

# --- 全局测试状态记录 ---
test_results = {
    "RTC": False,
    "RTC_Model": "UNKNOWN",
    "SX1276_SPI": False,
    "SX1276_Signal": False
}

def get_serial_number():
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    try:
        n = int.from_bytes(machine.unique_id(), 'big')
        s = ""
        while n:
            n, r = divmod(n, 36)
            s = chars[r] + s
        s = s or "0"
        if len(s) < 12:
            s = ("0" * (12 - len(s))) + s
        return s[-12:]
    except Exception as e:
        return "S/N INVALID"

print("\\n" + "="*40)
print("开始执行硬件自检 (RTC & SX1276)")
print("="*40)

# --- 1. 检查 RTC ---
print("\\n[0] 正在测试 RTC 模块...")
try:
    i2c = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=400000)
    devices = i2c.scan()
    if 0x68 in devices:
        print("    [通过] DS3231 芯片检测成功 (I2C地址: 0x68)")
        test_results["RTC"] = True
        test_results["RTC_Model"] = "DS3231"
    elif 0x51 in devices:
        print("    [通过] PCF8563 芯片检测成功 (I2C地址: 0x51)")
        test_results["RTC"] = True
        test_results["RTC_Model"] = "PCF8563"
    else:
        print("    [失败] 未找到 DS3231 (0x68) 或 PCF8563 (0x51)！请检查接线或电源。")
except Exception as e:
    print("    [失败] RTC I2C 通信异常:", e)

# --- 2. 检查 SX1276 ---
SPI_ID = 0
SCK_PIN = 18
MOSI_PIN = 19
MISO_PIN = 16
CS_PIN = 17
RST_PIN = 15
DATA_PIN = 21
CLK_PIN = 20

class SX1276Validator:
    def __init__(self):
        self.spi = machine.SPI(SPI_ID, baudrate=2000000, polarity=0, phase=0,
                               sck=machine.Pin(SCK_PIN), mosi=machine.Pin(MOSI_PIN), miso=machine.Pin(MISO_PIN))
        self.cs = machine.Pin(CS_PIN, machine.Pin.OUT, value=1)
        self.rst = machine.Pin(RST_PIN, machine.Pin.OUT, value=1)

        self.data_in = machine.Pin(DATA_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
        self.clk_in = machine.Pin(CLK_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
        self.bit_samples = []

    def _read_reg(self, reg):
        self.cs.value(0)
        self.spi.write(bytearray([reg & 0x7F]))
        res = self.spi.read(1)[0]
        self.cs.value(1)
        return res

    def _write_reg(self, reg, val):
        self.cs.value(0)
        self.spi.write(bytearray([reg | 0x80, val]))
        self.cs.value(1)

    def hardware_reset(self):
        print("\\n[-] 正在复位 SX1276...")
        self.rst.value(0)
        time.sleep_ms(10)
        self.rst.value(1)
        time.sleep_ms(10)

    def check_spi(self):
        print("\\n[1] 正在测试 SPI 通信...")
        version = self._read_reg(0x42)
        print(f"    -> 读到芯片版本号 (RegVersion): 0x{version:02X}")
        if version in [0x00, 0xFF]:
            print("    [失败] SPI 通信失败！请检查 SCK, MISO, MOSI, CS 接线。")
            return False
        if version == 0x12:
            print("    [通过] 确认芯片为 SX1276/77/78/79 系列。")
        else:
            print("    [注意] 读到版本号正常，但可能不是标准 SX1276 (通常为 0x12)。")

        test_results["SX1276_SPI"] = True
        return True

    def setup_continuous_rx(self):
        print("\\n[2] 正在配置 SX1276 进入 FSK 连续接收模式...")
        self._write_reg(0x01, 0x00)
        time.sleep_ms(10)
        self._write_reg(0x01, 0x01)
        time.sleep_ms(10)

        self._write_reg(0x06, 0xD2)
        self._write_reg(0x07, 0x51)
        self._write_reg(0x08, 0x99)
        self._write_reg(0x31, 0x00)
        self._write_reg(0x40, 0x00)

        self._write_reg(0x01, 0x05)
        time.sleep_ms(50)
        print("    [通过] 射频芯片已启动。")

    def _clk_isr(self, pin):
        if len(self.bit_samples) < 2000:
            self.bit_samples.append(self.data_in.value())

    def analyze_bitstream(self):
        print("\\n[3] 正在挂载时钟中断，捕获比特流...")
        self.bit_samples = []
        self.clk_in.irq(trigger=machine.Pin.IRQ_RISING, handler=self._clk_isr)

        time.sleep(1)
        self.clk_in.irq(handler=None)

        sample_count = len(self.bit_samples)
        print(f"    -> 1秒内捕获到时钟上升沿次数: {sample_count}")

        if sample_count == 0:
            print("    [失败] 没有检测到时钟信号 (CLK)。")
            print("        可能是连续模式未生效，或者 DIO1 未正确连接到 Pico 的引脚 20。")
            return

        print("\\n[4] 开始分析数据流合法性...")
        count_0 = self.bit_samples.count(0)
        count_1 = self.bit_samples.count(1)

        if count_0 == sample_count:
            print("    [失败] 比特流【全为 0】。数据引脚可能接地短路，或射频前端未输出数据。")
        elif count_1 == sample_count:
            print("    [失败] 比特流【全为 1】。数据引脚可能被拉高，或处于死锁状态。")
        else:
            ratio = count_0 / sample_count
            if 0.4 < ratio < 0.6:
                print("    [通过] 比特流分布均匀（0和1各占约一半）。符合无信号时的【背景白噪声】特征。")
            else:
                print("    [通过] 存在 0/1 交替。可能有真实信号正在传输，或者存在定向干扰。")
            test_results["SX1276_Signal"] = True

validator = SX1276Validator()
validator.hardware_reset()
if validator.check_spi():
    validator.setup_continuous_rx()
    validator.analyze_bitstream()

# ================= 最终裁决报告 =================
print("\\n" + "="*40)
print("硬件自检最终报告")
print("="*40)

chip_id = get_serial_number()
print(f"芯片序列号 (S/N): {chip_id}")

failed_components = []
if not test_results["RTC"]:
    failed_components.append("RTC (DS3231/PCF8563 I2C通信失败或未找到设备)")
else:
    print(f"RTC型号: {test_results['RTC_Model']}")
if not test_results["SX1276_SPI"]:
    failed_components.append("SX1276 射频 (SPI通信验证失败)")
elif not test_results["SX1276_Signal"]:
    failed_components.append("SX1276 射频 (射频时钟或信号捕获异常)")

if not failed_components:
    print("\\n[通过] 最终结果: 【全部正常通过】")
    print("所有核心硬件模块均工作在最佳状态！")
else:
    print("\\n[失败] 最终结果: 【未通过】")
    print("报错部件清单:")
    for comp in failed_components:
        print(f"  - {comp}")

print("="*40 + "\\n")
"""


class PicoUpdaterApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Pico LBJ Receiver Updater")
        self.geometry("1080x790")
        self.minsize(920, 700)
        self.configure(fg_color=COLORS["bg"])

        # 仓库配置
        self.github_repo = GITHUB_REPO
        self.target_dir = TARGET_DIR
        self.active_profile = get_firmware_profile(DEFAULT_CHANNEL_LABEL)
        self.main_py_url, self.api_url = build_github_urls(
            self.github_repo, self.target_dir, self.active_profile["branch"]
        )

        # 状态变量
        self.local_version = 0.0
        self.remote_version = 0.0
        self.local_firmware_info = {"label": "", "version": (), "branch": ""}
        self.remote_firmware_info = {"label": "", "version": (), "branch": ""}
        self.is_working = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.setup_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_ports()

    def setup_ui(self):
        self.sidebar_frame = ctk.CTkFrame(
            self, width=292, corner_radius=0, fg_color=COLORS["sidebar"]
        )
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_propagate(False)
        self.sidebar_frame.grid_rowconfigure(1, weight=1)

        brand = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 18))
        ctk.CTkLabel(
            brand,
            text="PICO UPDATER",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            brand,
            text="LBJ Receiver 管理工具",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        controls = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="nsew", padx=18)
        controls.grid_columnconfigure(0, weight=1)

        self._section_label(controls, "设备连接").grid(
            row=0, column=0, sticky="ew", pady=(0, 7)
        )

        port_row = ctk.CTkFrame(controls, fg_color="transparent")
        port_row.grid(row=1, column=0, sticky="ew")
        port_row.grid_columnconfigure(0, weight=1)
        port_row.grid_columnconfigure(1, minsize=72)

        self.port_var = ctk.StringVar(value="请选择端口...")
        self.port_menu = ctk.CTkOptionMenu(
            port_row,
            variable=self.port_var,
            values=["请选择端口..."],
            height=38,
            corner_radius=6,
            fg_color=COLORS["surface_alt"],
            button_color=COLORS["border"],
            button_hover_color=COLORS["teal_hover"],
            dropdown_fg_color=COLORS["surface"],
            dropdown_hover_color=COLORS["surface_alt"],
            command=self._on_port_selected,
        )
        self.port_menu.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.refresh_btn = ctk.CTkButton(
            port_row,
            text="扫描",
            width=72,
            height=38,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            border_width=1,
            border_color=COLORS["border"],
            command=self.refresh_ports,
        )
        self.refresh_btn.grid(row=0, column=1, sticky="e")

        self.device_status = ctk.CTkLabel(
            controls,
            text="正在扫描设备",
            height=34,
            corner_radius=6,
            fg_color=COLORS["surface"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=12),
            anchor="w",
            padx=12,
        )
        self.device_status.grid(row=2, column=0, sticky="ew", pady=(8, 16))

        self._section_label(controls, "固件分支").grid(
            row=3, column=0, sticky="ew", pady=(0, 7)
        )

        self.branch_var = ctk.StringVar(value=DEFAULT_CHANNEL_LABEL)
        self.branch_menu = ctk.CTkOptionMenu(
            controls,
            variable=self.branch_var,
            values=list(FIRMWARE_BRANCHES),
            height=38,
            corner_radius=6,
            fg_color=COLORS["surface_alt"],
            button_color=COLORS["border"],
            button_hover_color=COLORS["teal_hover"],
            dropdown_fg_color=COLORS["surface"],
            dropdown_hover_color=COLORS["surface_alt"],
            command=self._on_branch_selected,
        )
        self.branch_menu.grid(row=4, column=0, sticky="ew")

        self.branch_hint = ctk.CTkLabel(
            controls,
            text=self.active_profile["hardware_hint"],
            height=38,
            corner_radius=6,
            fg_color=COLORS["surface"],
            text_color=COLORS["amber"],
            font=ctk.CTkFont(size=11),
            anchor="w",
            justify="left",
            wraplength=244,
            padx=10,
        )
        self.branch_hint.grid(row=5, column=0, sticky="ew", pady=(7, 16))

        self._section_label(controls, "常用操作").grid(
            row=6, column=0, sticky="ew", pady=(0, 7)
        )

        self.action_btn = ctk.CTkButton(
            controls,
            text="检查并更新",
            height=42,
            corner_radius=6,
            fg_color=COLORS["blue"],
            hover_color=COLORS["blue_hover"],
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self.start_update_process(force=False),
        )
        self.action_btn.grid(row=7, column=0, sticky="ew")

        self.offline_zip_btn = ctk.CTkButton(
            controls,
            text="离线 ZIP 刷入",
            height=42,
            corner_radius=6,
            fg_color=COLORS["teal"],
            hover_color=COLORS["teal_hover"],
            text_color="#061411",
            font=ctk.CTkFont(weight="bold"),
            command=self.start_offline_zip_update,
        )
        self.offline_zip_btn.grid(row=8, column=0, sticky="ew", pady=(8, 0))

        self.test_btn = ctk.CTkButton(
            controls,
            text="运行硬件自检",
            height=42,
            corner_radius=6,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            text_color="#07150D",
            font=ctk.CTkFont(weight="bold"),
            command=self.start_hardware_test,
        )
        self.test_btn.grid(row=9, column=0, sticky="ew", pady=(8, 0))

        separator = ctk.CTkFrame(controls, height=1, fg_color=COLORS["border"])
        separator.grid(row=10, column=0, sticky="ew", pady=18)

        self._section_label(controls, "维护").grid(
            row=11, column=0, sticky="ew", pady=(0, 7)
        )
        self.force_action_btn = ctk.CTkButton(
            controls,
            text="强制重刷固件",
            height=40,
            corner_radius=6,
            fg_color="transparent",
            hover_color="#332124",
            border_width=1,
            border_color=COLORS["red"],
            text_color=COLORS["red"],
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self.start_update_process(force=True),
        )
        self.force_action_btn.grid(row=12, column=0, sticky="ew")

        repo_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="MisakaXing / RP2040 LBJ",
            height=42,
            fg_color=COLORS["surface"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11),
            anchor="w",
            padx=22,
        )
        repo_label.grid(row=2, column=0, sticky="ew")

        self.main_frame = ctk.CTkFrame(
            self, corner_radius=0, fg_color=COLORS["bg"]
        )
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=22, pady=20)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header.grid_columnconfigure(0, weight=1)
        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_group,
            text="固件与硬件管理",
            font=ctk.CTkFont(size=27, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_group,
            text="RP2040 LBJ Receiver",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        self.status_badge = ctk.CTkLabel(
            header,
            text="就绪",
            width=70,
            height=28,
            corner_radius=5,
            fg_color=COLORS["surface_alt"],
            text_color=COLORS["green"],
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_badge.grid(row=0, column=1, sticky="e")

        summary = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        summary.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        for column in range(3):
            summary.grid_columnconfigure(column, weight=1)

        self.local_ver_label = self._metric(
            summary, 0, "设备固件", "未知", COLORS["teal"]
        )
        self.remote_ver_label = self._metric(
            summary, 1, "最新固件", "未知", COLORS["blue"]
        )
        self.connection_value = self._metric(
            summary, 2, "连接状态", "未连接", COLORS["amber"]
        )

        progress_panel = ctk.CTkFrame(
            self.main_frame,
            corner_radius=8,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        progress_panel.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        progress_panel.grid_columnconfigure(0, weight=1)

        progress_header = ctk.CTkFrame(progress_panel, fg_color="transparent")
        progress_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(13, 7))
        progress_header.grid_columnconfigure(0, weight=1)
        self.progress_label = ctk.CTkLabel(
            progress_header,
            text="任务进度",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        )
        self.progress_label.grid(row=0, column=0, sticky="w")
        self.progress_percent = ctk.CTkLabel(
            progress_header,
            text="0%",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
        )
        self.progress_percent.grid(row=0, column=1, sticky="e")

        self.progress_bar = ctk.CTkProgressBar(
            progress_panel,
            height=8,
            corner_radius=4,
            fg_color=COLORS["surface_alt"],
            progress_color=COLORS["teal"],
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 15))
        self.progress_bar.set(0)

        console_panel = ctk.CTkFrame(
            self.main_frame,
            corner_radius=8,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        console_panel.grid(row=3, column=0, sticky="nsew")
        console_panel.grid_columnconfigure(0, weight=1)
        console_panel.grid_rowconfigure(1, weight=1)
        self.console_panel = console_panel
        self.inspection_summary = summary
        self.inspection_progress = progress_panel
        self.inspection_window = None

        console_header = ctk.CTkFrame(console_panel, fg_color="transparent")
        console_header.grid(row=0, column=0, sticky="ew", padx=16)
        console_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            console_header,
            text="运行日志",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLORS["text"],
        ).grid(row=0, column=0, sticky="w", pady=11)
        self.clear_log_btn = ctk.CTkButton(
            console_header,
            text="清空",
            width=62,
            height=28,
            corner_radius=5,
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["muted"],
            command=self.clear_log,
        )
        self.clear_log_btn.grid(row=0, column=1, sticky="e")

        self.log_textbox = ctk.CTkTextbox(
            console_panel,
            state="disabled",
            corner_radius=0,
            border_width=0,
            fg_color=COLORS["surface_alt"],
            text_color=COLORS["text"],
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["muted"],
            font=ctk.CTkFont(family="Menlo", size=12),
            wrap="word",
        )
        self.log_textbox.grid(
            row=1, column=0, sticky="nsew", padx=1, pady=(0, 1)
        )

    def _section_label(self, parent, text):
        return ctk.CTkLabel(
            parent,
            text=text,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        )

    def _metric(self, parent, column, label, value, accent):
        padx = (0, 7) if column == 0 else ((7, 7) if column == 1 else (7, 0))
        panel = ctk.CTkFrame(
            parent,
            height=92,
            corner_radius=8,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        panel.grid(row=0, column=column, sticky="ew", padx=padx)
        panel.grid_propagate(False)
        ctk.CTkFrame(
            panel, width=4, height=44, corner_radius=2, fg_color=accent
        ).pack(side="left", padx=(14, 12))
        text_group = ctk.CTkFrame(panel, fg_color="transparent")
        text_group.pack(side="left", fill="both", expand=True, pady=14)
        ctk.CTkLabel(
            text_group,
            text=label,
            font=ctk.CTkFont(size=11),
            text_color=COLORS["muted"],
            anchor="w",
        ).pack(fill="x")
        value_label = ctk.CTkLabel(
            text_group,
            text=value,
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        )
        value_label.pack(fill="x", pady=(4, 0))
        return value_label

    def clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("0.0", "end")
        self.log_textbox.configure(state="disabled")

    def set_progress(self, value, label=None):
        value = max(0.0, min(1.0, float(value)))
        self.progress_bar.set(value)
        self.progress_percent.configure(text=f"{round(value * 100):d}%")
        if label is not None:
            self.progress_label.configure(text=label)

    def _selected_profile(self):
        profile = get_firmware_profile(self.branch_var.get())
        snapshot = dict(profile)
        snapshot["runtime_files"] = tuple(profile["runtime_files"])
        return snapshot

    def _on_branch_selected(self, selection):
        profile = get_firmware_profile(selection)
        self.active_profile = profile
        self.main_py_url, self.api_url = build_github_urls(
            self.github_repo, self.target_dir, profile["branch"]
        )
        self.remote_firmware_info = {
            "label": "", "version": (), "branch": ""
        }
        self.remote_version = 0.0
        self.remote_ver_label.configure(text="未知")
        self.branch_hint.configure(
            text=profile["hardware_hint"], text_color=COLORS["amber"]
        )
        self.log(
            f"已选择固件分支: {profile['branch']}；"
            f"{profile['hardware_hint']}。"
        )

    def _reset_selected_device_state(self):
        self.local_version = 0.0
        self.local_firmware_info = {
            "label": "", "version": (), "branch": ""
        }
        self.local_ver_label.configure(text="未知")
        profile = self._selected_profile()
        self.branch_hint.configure(
            text=profile["hardware_hint"], text_color=COLORS["amber"]
        )

    def _on_port_selected(self, port):
        self._reset_selected_device_state()
        self._sync_port_actions()
        if port in ("未检测到设备", "请选择端口..."):
            self.connection_value.configure(text="未连接")
            self.device_status.configure(
                text="未检测到可用设备", text_color=COLORS["muted"]
            )
            return
        known = port in getattr(self, "pico_candidate_ports", set())
        self.connection_value.configure(text="Pico 串口已选择" if known else "未识别的串口")
        self.device_status.configure(
            text=port if known else "非自动识别 Pico：" + port,
            text_color=COLORS["teal"] if known else COLORS["amber"]
        )

    def _sync_port_actions(self):
        selected = self.port_var.get()
        enabled = not self.is_working and selected not in (
            "", "未检测到设备", "请选择端口..."
        )
        for widget in (self.action_btn, self.force_action_btn,
                       self.offline_zip_btn, self.test_btn):
            widget.configure(state="normal" if enabled else "disabled")

    def _confirm_selected_port(self, action):
        """Re-enumerate immediately before starting, before touching any serial port."""
        port = self.port_var.get()
        if not port or port in ("未检测到设备", "请选择端口..."):
            messagebox.showwarning("未选择 Pico", "未检测到或尚未选择 Pico。请连接设备后扫描，不会开始" + action + "。")
            return False
        try:
            device = next((p for p in serial.tools.list_ports.comports()
                           if p.device == port), None)
        except Exception as exc:
            messagebox.showwarning("无法检查串口", str(exc))
            return False
        if device is None:
            self.refresh_ports()
            messagebox.showwarning("设备已断开", "所选串口已不存在，请重新连接并扫描。不会自动改用其他串口执行操作。")
            return False
        if device.vid != 0x2E8A:
            return messagebox.askyesno(
                "所选串口未识别为 Pico",
                f"串口：{port}\n设备描述：{device.description or '未知'}\n\n"
                "这可能是蓝牙或其他设备，不应当作 Pico 使用。\n"
                "只有确认它确实连接你的接收器时才继续。继续会尝试连接并可能软复位设备；"
                "刷入前仍必须通过硬件兼容检查。\n\n是否确认使用这个串口？",
                default="no",
            )
        return True

    def log(self, text):
        self.after(0, self._append_log, text)

    def _append_log(self, text):
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", text + "\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def set_ui_state(self, working):
        if working:
            panel = getattr(self, "inspection_window", None)
            if panel is not None and panel.finished:
                self.dismiss_inspection()
        self.is_working = working
        state = "disabled" if working else "normal"
        self.action_btn.configure(
            state=state, text="正在处理" if working else "检查并更新"
        )
        self.test_btn.configure(
            state=state, text="正在处理" if working else "运行硬件自检"
        )
        self.offline_zip_btn.configure(
            state=state, text="正在处理" if working else "离线 ZIP 刷入"
        )
        self.force_action_btn.configure(
            state=state, text="正在处理" if working else "强制重刷固件"
        )
        self.refresh_btn.configure(state=state)
        self.port_menu.configure(state=state)
        self.branch_menu.configure(state=state)
        self.clear_log_btn.configure(state=state)
        self.status_badge.configure(
            text="运行中" if working else "就绪",
            text_color=COLORS["amber"] if working else COLORS["green"],
        )
        self._sync_port_actions()

    def _on_close(self):
        if self.is_working:
            messagebox.showwarning(
                "任务正在进行",
                "当前正在检查或刷写固件。为避免设备只写入一部分文件，"
                "请等待任务完成或在确认窗口中取消后再关闭。",
            )
            return
        self.destroy()

    def refresh_ports(self):
        self._reset_selected_device_state()
        PICO_VID = 0x2E8A
        ports = serial.tools.list_ports.comports()
        port_list = [port.device for port in ports]
        self.pico_candidate_ports = {port.device for port in ports if port.vid == PICO_VID}

        auto_detected_port = None

        for port in ports:
            if port.vid == PICO_VID:
                auto_detected_port = port.device
                break

        if not port_list:
            port_list = ["未检测到设备"]
            self.port_menu.configure(values=port_list)
            self.port_var.set(port_list[0])
            self.connection_value.configure(text="未连接")
            self.device_status.configure(
                text="未检测到可用设备", text_color=COLORS["muted"]
            )
            self.log("刷新完成：当前未连接任何串口设备。")
        else:
            self.port_menu.configure(values=port_list)

            if auto_detected_port:
                self.port_var.set(auto_detected_port)
                self.connection_value.configure(text="Pico 已连接")
                self.device_status.configure(
                    text=auto_detected_port, text_color=COLORS["teal"]
                )
                self.log(f"已自动识别并选中 Pico 设备: {auto_detected_port}")
            else:
                self.port_menu.configure(values=["请选择端口..."] + port_list)
                self.port_var.set("请选择端口...")
                self.connection_value.configure(text="未检测到 Pico")
                self.device_status.configure(
                    text="未发现 Pico，请连接后扫描", text_color=COLORS["amber"]
                )
                self.log("未检测到 Pico：不会默认使用蓝牙或其他串口，刷入按钮已禁用。")
        self._sync_port_actions()

    # [改进版] 支持实时流式输出的 run_mpremote
    def run_mpremote(self, port, args_list, timeout_sec=60, live_stream=False):
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "mpremote_internal", "connect", port] + args_list
        else:
            cmd = [sys.executable, "-m", "mpremote", "connect", port] + args_list

        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            if live_stream:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding='utf-8', errors='replace', startupinfo=startupinfo)
                full_output = []
                for line in iter(process.stdout.readline, ''):
                    clean_line = line.strip('\r\n')
                    if clean_line:
                        self.log(clean_line)
                    full_output.append(clean_line)

                process.stdout.close()
                process.wait(timeout=timeout_sec)
                return process.returncode == 0, "\n".join(full_output)
            else:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        encoding='utf-8', errors='replace', timeout=timeout_sec, startupinfo=startupinfo)
                return result.returncode == 0, result.stdout
        except subprocess.TimeoutExpired:
            return False, "命令执行超时 (可能 Pico 处于死循环，或文件传输时间过长)"
        except Exception as e:
            return False, str(e)

    def extract_version(self, text):
        return version_as_number(parse_program_version(text))

    def _probe_hardware(self, port):
        success, output = self.run_mpremote(
            port,
            ["exec", HARDWARE_PROBE_SCRIPT],
            timeout_sec=15,
        )
        if not success:
            return None, "硬件身份探测命令失败: " + str(output)[:240]
        info = parse_hardware_probe(output)
        if info is None:
            return None, "硬件身份探测没有返回有效标识"
        return info, ""

    def _check_hardware_compatibility(self, port, profile, phase="刷入前检查"):
        self.log(
            f"{phase}: 正在核对 {profile['branch']} 与开发板硬件..."
        )
        info, probe_error = self._probe_hardware(port)
        if info is None:
            compatible, reason = False, probe_error
            identity = "未知硬件"
        else:
            compatible, reason = evaluate_hardware_compatibility(profile, info)
            identity = hardware_identity(info)

        self.log(f"检测到开发板: {identity}")
        if compatible:
            self.log(f"[通过] {reason}")
            self.after(
                0,
                lambda text=identity: self.connection_value.configure(
                    text=text[:26], text_color=COLORS["green"]
                ),
            )
            self.after(
                0,
                lambda text=reason: self.branch_hint.configure(
                    text=text, text_color=COLORS["green"]
                ),
            )
            return True

        self.log(f"[阻止刷入] {reason}")
        error_message = (
            f"所选分支：{profile['branch']}\n"
            f"检测硬件：{identity}\n\n{reason}\n\n"
            "未执行清空、写入或重启操作。"
        )
        self.after(
            0,
            lambda msg=error_message: messagebox.showerror(
                "硬件不兼容，已阻止刷入", msg
            ),
        )
        self.after(
            0,
            lambda text=reason: self.branch_hint.configure(
                text=text, text_color=COLORS["red"]
            ),
        )
        return False

    def show_confirm_dialog(self, title, message, yes_text, no_text, on_yes, on_no=None, icon="warning"):
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.configure(fg_color=COLORS["surface"])
        dialog.transient(self)
        dialog.grid_columnconfigure(0, weight=1)

        icon_text = "!" if icon == "warning" else "i"
        ctk.CTkLabel(
            dialog,
            text=icon_text,
            width=42,
            height=42,
            fg_color=COLORS["amber"] if icon == "warning" else COLORS["blue"],
            text_color="#15191E",
            corner_radius=21,
            font=ctk.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, pady=(24, 10))

        ctk.CTkLabel(
            dialog,
            text=title,
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=1, column=0, padx=28, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(
            dialog,
            text=message,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=13),
            justify="left",
            wraplength=420,
        ).grid(row=2, column=0, padx=28, sticky="ew")

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.grid(row=3, column=0, padx=24, pady=(22, 22), sticky="ew")
        buttons.grid_columnconfigure((0, 1), weight=1, uniform="dialog_buttons")

        finished = {"value": False}

        def _finish(value):
            if finished["value"]:
                return
            finished["value"] = True
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
            if value:
                on_yes()
            elif on_no:
                on_no()

        ctk.CTkButton(
            buttons,
            text=no_text,
            height=38,
            fg_color=COLORS["surface_alt"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            command=lambda: _finish(False),
        ).grid(row=0, column=0, padx=(0, 8), sticky="ew")

        ctk.CTkButton(
            buttons,
            text=yes_text,
            height=38,
            fg_color=COLORS["red"] if icon == "warning" else COLORS["blue"],
            hover_color=COLORS["red_hover"] if icon == "warning" else COLORS["blue_hover"],
            text_color=COLORS["text"],
            font=ctk.CTkFont(weight="bold"),
            command=lambda: _finish(True),
        ).grid(row=0, column=1, padx=(8, 0), sticky="ew")

        dialog.protocol("WM_DELETE_WINDOW", lambda: _finish(False))
        dialog.grab_set()

        try:
            dialog.update_idletasks()
            width = max(dialog.winfo_reqwidth(), 480)
            height = max(dialog.winfo_reqheight(), 260)
            x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
            dialog.geometry(f"{width}x{height}+{x}+{y}")
            self.lift()
            dialog.lift()
            dialog.focus_force()
            dialog.attributes("-topmost", True)
            dialog.after(
                250,
                lambda: dialog.winfo_exists() and dialog.attributes("-topmost", False),
            )
        except Exception:
            pass

    def _cleanup_temp_dir(self, temp_dir):
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _extract_zip_firmware(self, zip_path, dest_dir, profile):
        extracted_by_name = {}
        main_text = None
        target_marker = f"/{self.target_dir}/"
        allowed_names = set(runtime_file_order(profile))

        try:
            archive = zipfile.ZipFile(zip_path, "r")
        except zipfile.BadZipFile:
            raise ValueError("选择的文件不是有效 ZIP 压缩包。")

        with archive:
            for info in archive.infolist():
                raw_name = info.filename.replace("\\", "/")
                if info.is_dir() or raw_name.endswith("/"):
                    continue
                if raw_name.startswith("__MACOSX/") or "/__MACOSX/" in raw_name:
                    continue
                if raw_name.endswith(".DS_Store") or "/__pycache__/" in raw_name:
                    continue

                rel_name = ""
                if target_marker in f"/{raw_name}":
                    rel_name = f"/{raw_name}".split(target_marker, 1)[1]
                elif "/" not in raw_name:
                    rel_name = raw_name

                rel_name = os.path.normpath(rel_name).replace("\\", "/")
                if (
                    not rel_name
                    or rel_name == "."
                    or rel_name.startswith("../")
                    or rel_name.startswith("/")
                ):
                    continue
                if "/" in rel_name:
                    self.log(f"跳过子目录文件: {rel_name}")
                    continue
                if rel_name.startswith("._"):
                    continue
                if rel_name not in allowed_names:
                    self.log(f"跳过非运行文件: {rel_name}")
                    continue

                out_path = os.path.join(dest_dir, rel_name)
                with archive.open(info, "r") as src, open(out_path, "wb") as dst:
                    data = src.read()
                    dst.write(data)

                extracted_by_name[rel_name] = {
                    "name": rel_name,
                    "path": out_path,
                }
                if rel_name == "main.py":
                    main_text = data.decode("utf-8", errors="replace")

        if not extracted_by_name:
            raise ValueError(f"ZIP 中没有找到 {self.target_dir} 目录下的固件文件。")
        if main_text is None:
            raise ValueError(f"ZIP 中没有找到 {self.target_dir}/main.py，无法识别固件版本。")

        missing = [
            name for name in profile["runtime_files"]
            if name not in extracted_by_name
        ]
        if missing:
            raise ValueError("ZIP 缺少运行文件: " + ", ".join(missing))

        firmware_info = parse_program_version(main_text, extracted_by_name)
        if not firmware_info["version"]:
            raise ValueError("无法识别 ZIP 中的 Program_ver。")
        if firmware_info["branch"] != profile["branch"]:
            raise ValueError(
                f"ZIP 属于 {firmware_info['branch']}，"
                f"但当前选择的是 {profile['branch']}。"
            )

        firmware_files = [
            extracted_by_name[name] for name in runtime_file_order(profile)
            if name in extracted_by_name
        ]
        return firmware_files, firmware_info

    def _read_device_firmware_info(self, port):
        self.log("正在探测 Pico 文件系统...")
        success, ls_output = self.run_mpremote(
            port,
            ["exec", "import os; print('main.py' in os.listdir())"],
            timeout_sec=10
        )

        if success and "True" in ls_output:
            self.log("正在读取本地版本...")
            success_cat, output = self.run_mpremote(port, ["cat", "main.py"], timeout_sec=15)
            if success_cat and "Program_ver" in output:
                return parse_program_version(output)
            return {"label": "", "version": (), "branch": ""}

        self.log("未检测到 main.py，识别为全新开发板或空文件系统。")
        return {"label": "", "version": (), "branch": ""}

    def _read_device_version(self, port):
        return version_as_number(self._read_device_firmware_info(port))

    def _wipe_device_files(self, port, profile):
        if not self._check_hardware_compatibility(
            port, profile, phase="清空前最终复核"
        ):
            return False
        self.log("正在清空 Pico 中的旧文件...")
        wipe_script = "import os; [os.remove(f) for f in os.listdir() if not (os.stat(f)[0] & 0x4000)]"
        success, output = self.run_mpremote(port, ["exec", wipe_script], timeout_sec=20)
        if not success:
            self.log(f"[失败] 无法安全清空旧文件: {output}")
            self.after(
                0,
                lambda: messagebox.showerror(
                    "清空失败", "未能安全清空旧文件，已停止刷入。"
                ),
            )
            return False
        return True

    def _copy_firmware_files(self, port, firmware_files, progress_start=0.65, progress_span=0.30):
        total_files = max(1, len(firmware_files))
        for i, item in enumerate(firmware_files):
            file_name = item["name"]
            local_path = item["path"]
            self.log(f"正在写入到 Pico: {file_name} ...")

            success, output = self.run_mpremote(port, ["fs", "cp", local_path, f":{file_name}"])
            if not success:
                self.log(f"\n[失败] 写入 {file_name} 失败: {output}")
                self.after(0, lambda fn=file_name: messagebox.showerror("写入失败", f"写入文件 {fn} 时发生错误！"))
                return False

            self.after(
                0,
                self.set_progress,
                progress_start + progress_span * ((i + 1) / total_files),
                "写入设备",
            )
        return True

    # ================= 硬件自检逻辑 =================
    def dismiss_inspection(self):
        panel = self.inspection_window
        if panel is not None:
            if not panel.finished:
                return
            panel.destroy()
            self.inspection_window = None
        self.log_textbox.grid()
        self.inspection_summary.grid()
        self.inspection_progress.grid()
        self.clear_log_btn.grid()

    def start_hardware_test(self):
        if self.is_working: return
        if not self._confirm_selected_port("硬件检查"): return
        port = self.port_var.get()
        if not port or port == "未检测到设备" or port == "请选择端口...":
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return

        if self.is_working: return
        self.set_ui_state(True)
        try:
            from solder_check import InspectionWindow
            self.inspection_window = InspectionWindow(
                self, port, HARDWARE_TEST_SCRIPT, master=self.console_panel)
            self.log_textbox.grid_remove()
            self.inspection_summary.grid_remove()
            self.inspection_progress.grid_remove()
            self.clear_log_btn.grid_remove()
            self.inspection_window.grid(row=1, column=0, sticky="nsew", padx=1, pady=1)
        except Exception as exc:
            self.set_ui_state(False)
            self.log_textbox.grid()
            self.inspection_summary.grid()
            self.inspection_progress.grid()
            self.clear_log_btn.grid()
            messagebox.showerror("无法打开检查单", str(exc))

    def _test_worker(self, port):
        try:
            self.log(f"正在测试 Pico ({port}) 连接状态...")
            success, output = self.run_mpremote(port, ["exec", "print('PICO_OK')"], timeout_sec=10)
            if not success or "PICO_OK" not in output:
                self.log("[失败] 无法建立通信，请检查接线或串口占用。")
                return

            self.after(
                0, lambda: self.connection_value.configure(text="通信正常")
            )
            self.after(0, self.set_progress, 0.3, "正在执行硬件检查")
            self.log("正在将自检脚本注入 Pico 内存运行 (过程需要数秒，请勿断开连接)...\n")

            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
                f.write(HARDWARE_TEST_SCRIPT)
                temp_path = f.name

            # 开启实时流式输出 (live_stream=True)
            success, output = self.run_mpremote(port, ["run", temp_path], timeout_sec=20, live_stream=True)

            if not success:
                self.log(f"\n[失败] 自检执行超时或发生异常:\n{output}")

            self.after(0, self.set_progress, 1.0, "硬件自检完成")

            try: os.remove(temp_path)
            except: pass

        except Exception as e:
            self.log(f"[失败] 自检过程出错: {str(e)}")
        finally:
            self.after(0, self.set_ui_state, False)

    # ================= 固件更新逻辑 =================
    def start_offline_zip_update(self, zip_path=None):
        if self.is_working:
            return
        if not self._confirm_selected_port("离线刷入"):
            return

        port = self.port_var.get()
        if not port or port == "未检测到设备" or port == "请选择端口...":
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return

        zip_path = zip_path or filedialog.askopenfilename(
            title="选择 GitHub 下载的固件 ZIP",
            filetypes=[
                ("GitHub ZIP / 固件 ZIP", "*.zip"),
                ("所有文件", "*.*"),
            ],
        )
        if not zip_path:
            self.log("用户已取消选择离线 ZIP。")
            return

        profile = self._selected_profile()
        self.set_ui_state(True)
        self.clear_log()
        self.set_progress(0, "离线 ZIP 刷入")
        self.log(f"离线刷入目标分支: {profile['branch']}")
        threading.Thread(
            target=self._offline_zip_prepare_worker,
            args=(port, zip_path, profile),
            daemon=True
        ).start()

    def _offline_zip_prepare_worker(self, port, zip_path, profile):
        temp_dir = None
        try:
            self.log(f"离线刷入文件: {zip_path}")
            self.log(f"正在测试 Pico ({port}) 连接状态...")
            success, output = self.run_mpremote(port, ["exec", "print('PICO_OK')"], timeout_sec=10)
            if not success or "PICO_OK" not in output:
                self.log("\n[失败] 无法与 Pico 建立通信！")
                self.after(0, lambda: messagebox.showerror("连接失败", "无法与 Pico 通信，请确保串口未被占用！"))
                self.after(0, self.set_ui_state, False)
                return

            self.after(0, lambda: self.connection_value.configure(text="通信正常"))
            self.log("[完成] Pico 串口通信正常。")

            temp_dir = tempfile.mkdtemp(prefix="pico_offline_zip_")
            self.after(0, self.set_progress, 0.15, "解析 ZIP 固件")
            firmware_files, zip_info = self._extract_zip_firmware(
                zip_path, temp_dir, profile
            )
            self.remote_firmware_info = zip_info
            self.remote_version = version_as_number(zip_info)
            remote_text = "ZIP " + version_display(zip_info)
            self.after(0, lambda text=remote_text: self.remote_ver_label.configure(text=text))
            self.log(f"ZIP 解析完成，找到 {len(firmware_files)} 个固件文件。")
            self.log(
                f"ZIP 固件: {version_display(zip_info)} / "
                f"{zip_info['branch']}"
            )

            if not self._check_hardware_compatibility(
                port, profile, phase="离线包刷入前检查"
            ):
                self._cleanup_temp_dir(temp_dir)
                temp_dir = None
                self.after(0, self.set_ui_state, False)
                return

            self.after(0, self.set_progress, 0.28, "读取设备固件")
            local_info = self._read_device_firmware_info(port)
            self.local_firmware_info = local_info
            self.local_version = version_as_number(local_info)
            local_text = version_display(local_info, missing="未安装")
            self.after(0, lambda text=local_text: self.local_ver_label.configure(text=text))
            self.log(
                "设备当前固件: "
                + (
                    f"{local_text} / {local_info['branch']}"
                    if local_info["version"] else "未安装/未知"
                )
            )

            self.after(
                0,
                self._confirm_offline_zip_update,
                port,
                temp_dir,
                firmware_files,
                zip_info,
                local_info,
                profile,
            )
            temp_dir = None

        except Exception as e:
            self._cleanup_temp_dir(temp_dir)
            error_text = str(e)
            self.log(f"\n[失败] 离线刷入过程中发生错误: {error_text}")
            self.after(0, lambda err=error_text: messagebox.showerror("离线刷入失败", f"发生错误: {err}"))
            self.after(0, self.set_ui_state, False)

    def _confirm_offline_zip_update(
        self, port, temp_dir, firmware_files, zip_info, local_info, profile
    ):
        zip_text = version_display(zip_info)
        local_text = version_display(local_info, missing="未知/未安装")
        same_branch = local_info.get("branch") == profile["branch"]

        if (
            local_info["version"]
            and same_branch
            and not version_is_at_least(local_info, zip_info)
        ):
            self.log("等待用户确认：ZIP 版本较新，刷入会清空 Pico 数据。")
            title = "离线刷入确认"
            message = (
                f"目标分支：{profile['branch']}\n"
                f"ZIP 固件 {zip_text} 高于机器版本 {local_text}。\n\n"
                "刷入过程会清空 Pico 内所有旧文件，历史车次数据将会永久消失。\n\n"
                "请选择继续刷入，或取消操作。"
            )
            yes_text = "继续刷入"
            cancel_log = "用户已取消离线刷入。"
            continue_log = "版本较新，用户确认后开始正常刷入。"
        elif not local_info["version"]:
            self.log("等待用户确认：即将离线刷入并清空 Pico 数据。")
            title = "离线刷入确认"
            message = (
                f"目标分支：{profile['branch']}\n"
                f"将刷入 ZIP 固件 {zip_text}。\n\n"
                "刷入过程会清空 Pico 内所有旧文件，历史车次数据将会永久消失。\n\n"
                "请选择继续刷入，或取消操作。"
            )
            yes_text = "继续刷入"
            cancel_log = "用户已取消离线刷入。"
            continue_log = "设备未安装或版本未知，用户确认后开始刷入。"
        elif not same_branch:
            self.log("等待用户确认：设备中的固件属于另一分支。")
            title = "切换固件分支"
            message = (
                f"设备当前是 {local_info.get('branch') or '未知分支'} "
                f"{local_text}，将切换到 {profile['branch']} {zip_text}。\n\n"
                "刷入过程会清空 Pico 内所有旧文件，历史车次数据将会永久消失。\n\n"
                "请选择切换分支，或取消操作。"
            )
            yes_text = "切换并刷入"
            cancel_log = "用户取消切换固件分支。"
            continue_log = "用户确认切换固件分支。"
        else:
            self.log("等待用户确认：ZIP 版本小于或等于机器版本，需要选择是否强制刷入。")
            title = "版本较低或相同"
            message = (
                f"要刷入的 ZIP 版本 {zip_text} 小于或等于机器版本 {local_text}。\n\n"
                "继续会强制刷入并清空 Pico 内所有旧文件，历史车次数据将会永久消失。\n\n"
                "请选择强制刷入，或取消操作。"
            )
            yes_text = "强制刷入"
            cancel_log = "用户取消：ZIP 版本小于或等于机器版本，未执行刷入。"
            continue_log = "用户确认强制刷入离线 ZIP。"

        self.set_progress(0.34, "等待用户确认")

        def _cancel():
            self.log(cancel_log)
            self._cleanup_temp_dir(temp_dir)
            self.set_progress(1.0, "已取消")
            self.set_ui_state(False)

        def _continue():
            self.log(continue_log)
            try:
                threading.Thread(
                    target=self._offline_zip_flash_worker,
                    args=(port, temp_dir, firmware_files, profile),
                    daemon=True,
                ).start()
            except Exception as exc:
                self.log(f"[失败] 无法启动离线刷入线程: {exc}")
                self._cleanup_temp_dir(temp_dir)
                self.set_ui_state(False)
                messagebox.showerror("离线刷入失败", str(exc))

        try:
            self.show_confirm_dialog(
                title,
                message,
                yes_text=yes_text,
                no_text="取消",
                on_yes=_continue,
                on_no=_cancel,
                icon="warning",
            )
        except Exception as exc:
            self.log(f"[失败] 无法显示离线刷入确认窗口: {exc}")
            self._cleanup_temp_dir(temp_dir)
            self.set_ui_state(False)
            messagebox.showerror("离线刷入失败", str(exc))

    def _offline_zip_flash_worker(
        self, port, temp_dir, firmware_files, profile
    ):
        try:
            self.after(0, self.set_progress, 0.55, "准备刷入")
            if not self._wipe_device_files(port, profile):
                return
            self.after(0, self.set_progress, 0.65, "写入设备")

            if not self._copy_firmware_files(port, firmware_files, 0.65, 0.30):
                return

            self.log("正在重启 Pico 生效固件...")
            self._complete_flash(port, "离线 ZIP 刷入")

        except Exception as e:
            error_text = str(e)
            self.log(f"\n[失败] 离线刷入过程中发生错误: {error_text}")
            self.after(0, lambda err=error_text: messagebox.showerror("离线刷入失败", f"发生错误: {err}"))
        finally:
            self._cleanup_temp_dir(temp_dir)
            self.after(0, self.set_ui_state, False)

    def start_update_process(self, force=False):
        if self.is_working:
            return
        if not self._confirm_selected_port("强制刷入" if force else "在线更新"):
            return
        port = self.port_var.get()
        if not port or port == "未检测到设备" or port == "请选择端口...":
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return

        if self.is_working:
            return

        profile = self._selected_profile()
        self.set_ui_state(True)
        self.clear_log()
        self.set_progress(0, "在线更新预检")
        self.log(f"在线更新目标分支: {profile['branch']}")
        self.log(f"目标硬件: {profile['hardware_hint']}")

        threading.Thread(
            target=self._update_worker,
            args=(port, force, profile),
            daemon=True,
        ).start()

    def _update_worker(self, port, force, profile):
        temp_dir = None
        handed_to_dialog = False
        try:
            self.log(f"正在测试 Pico ({port}) 连接状态...")
            success, output = self.run_mpremote(port, ["exec", "print('PICO_OK')"], timeout_sec=10)
            if not success or "PICO_OK" not in output:
                self.log("\n[失败] 无法与 Pico 建立通信！")
                self.log("可能的原因：\n1. 串口占用。\n2. Pico 死机。")
                self.after(0, lambda: messagebox.showerror("连接失败", "无法与 Pico 通信，请确保串口未被占用！"))
                return
            self.after(
                0, lambda: self.connection_value.configure(text="通信正常")
            )
            self.log("[完成] Pico 串口通信正常。")

            if not self._check_hardware_compatibility(
                port, profile, phase="在线更新刷入前检查"
            ):
                return

            main_py_url, api_url = build_github_urls(
                self.github_repo, self.target_dir, profile["branch"]
            )
            self.log("正在连接 GitHub 获取远程版本...")
            self.after(0, self.set_progress, 0.1, "获取远程版本")
            resp = requests.get(main_py_url, timeout=15)
            if resp.status_code == 200:
                remote_info = parse_program_version(resp.text)
                if not remote_info["version"]:
                    raise ValueError("无法从远程 main.py 识别 Program_ver。")
                if remote_info["branch"] != profile["branch"]:
                    raise ValueError(
                        f"远程文件属于 {remote_info['branch']}，"
                        f"与所选 {profile['branch']} 不一致，已阻止刷入。"
                    )
                self.remote_firmware_info = remote_info
                self.remote_version = version_as_number(remote_info)
                remote_text = version_display(remote_info)
                self.after(
                    0,
                    lambda text=remote_text: self.remote_ver_label.configure(text=text),
                )
                self.log(
                    f"成功获取远程固件: {remote_text} / "
                    f"{remote_info['branch']}"
                )
            else:
                raise RuntimeError(
                    f"获取远程 main.py 失败: HTTP {resp.status_code}"
                )

            self.after(0, self.set_progress, 0.2, "读取设备固件")
            local_info = self._read_device_firmware_info(port)
            self.local_firmware_info = local_info
            self.local_version = version_as_number(local_info)
            local_text = version_display(local_info, missing="未安装")
            self.after(
                0,
                lambda text=local_text: self.local_ver_label.configure(text=text),
            )
            self.log(
                "设备当前固件: "
                + (
                    f"{local_text} / {local_info['branch']}"
                    if local_info["version"] else "未安装/未知"
                )
            )
            self.after(0, self.set_progress, 0.3, "准备更新文件")

            same_branch = local_info.get("branch") == profile["branch"]
            if not force and same_branch and version_is_at_least(
                local_info, remote_info
            ):
                self.log("\n[完成] 当前分支已是最新版本，无需更新。")
                self.after(0, self.set_progress, 1.0, "已是最新版本")
                return

            if force:
                self.log("\n已选择强制刷入；硬件兼容检查仍然有效。")
            elif local_info["version"] and not same_branch:
                self.log(
                    "\n检测到设备固件属于另一分支，将按分支切换处理。"
                )
            else:
                self.log("\n准备开始执行同步操作...")

            self.log("正在解析远程仓库文件列表...")
            api_resp = requests.get(api_url, timeout=15)
            if api_resp.status_code != 200:
                raise RuntimeError(
                    f"获取远程目录失败: HTTP {api_resp.status_code}"
                )

            files_data = api_resp.json()
            if not isinstance(files_data, list):
                raise ValueError("GitHub 返回的固件目录格式无效。")

            downloadable_files, missing = select_runtime_files(
                profile, files_data
            )
            if missing:
                raise ValueError(
                    "远程分支缺少必需运行文件: " + ", ".join(missing)
                )

            temp_dir = tempfile.mkdtemp(prefix="pico_online_update_")
            firmware_files = []
            total_files = len(downloadable_files)
            for i, file_info in enumerate(downloadable_files):
                file_name = file_info["name"]
                dl_url = file_info.get("download_url")
                if not dl_url:
                    raise ValueError(f"{file_name} 缺少下载地址。")
                self.log(f"正在下载: {file_name} ...")

                try:
                    file_resp = requests.get(dl_url, timeout=20)
                    file_resp.raise_for_status()
                except Exception as exc:
                    raise RuntimeError(
                        f"下载 {file_name} 失败: {exc}"
                    ) from exc

                local_path = os.path.join(temp_dir, file_name)
                with open(local_path, "wb") as file_handle:
                    file_handle.write(file_resp.content)
                firmware_files.append({"name": file_name, "path": local_path})

                self.after(
                    0,
                    self.set_progress,
                    0.3 + 0.3 * ((i + 1) / total_files),
                    "下载固件文件",
                )

            downloaded_main = next(
                item["path"] for item in firmware_files
                if item["name"] == "main.py"
            )
            with open(downloaded_main, "r", encoding="utf-8") as file_handle:
                downloaded_info = parse_program_version(
                    file_handle.read(),
                    (item["name"] for item in firmware_files),
                )
            if (
                downloaded_info["branch"] != profile["branch"]
                or downloaded_info["label"] != remote_info["label"]
            ):
                raise ValueError(
                    "远程分支在下载过程中发生变化，固件快照不一致；"
                    "请重新检查更新。"
                )

            self.after(
                0,
                self._confirm_online_update,
                port,
                force,
                temp_dir,
                firmware_files,
                remote_info,
                local_info,
                profile,
            )
            handed_to_dialog = True
            temp_dir = None

        except Exception as e:
            error_text = str(e)
            self.log(f"\n[失败] 处理过程中发生错误: {error_text}")
            self.after(0, lambda err=error_text: messagebox.showerror("错误", f"发生意外错误: {err}"))

        finally:
            self._cleanup_temp_dir(temp_dir)
            if not handed_to_dialog:
                self.after(0, self.set_ui_state, False)

    def _confirm_online_update(
        self,
        port,
        force,
        temp_dir,
        firmware_files,
        remote_info,
        local_info,
        profile,
    ):
        remote_text = version_display(remote_info)
        local_text = version_display(local_info, missing="未知/未安装")
        same_branch = local_info.get("branch") == profile["branch"]

        if force:
            title = "强制刷入警告"
            lead = (
                f"将强制刷入 {profile['branch']} {remote_text}。"
            )
            yes_text = "强制刷入"
            continue_log = "用户确认强制在线刷入。"
        elif local_info["version"] and not same_branch:
            title = "切换固件分支"
            lead = (
                f"设备当前是 {local_info.get('branch') or '未知分支'} "
                f"{local_text}，将切换到 {profile['branch']} {remote_text}。"
            )
            yes_text = "切换并刷入"
            continue_log = "用户确认在线切换固件分支。"
        elif not local_info["version"]:
            title = "初次安装确认"
            lead = f"将安装 {profile['branch']} {remote_text}。"
            yes_text = "安装固件"
            continue_log = "用户确认在线初次安装。"
        else:
            title = "固件更新确认"
            lead = (
                f"将从 {local_text} 更新到 {profile['branch']} {remote_text}。"
            )
            yes_text = "继续更新"
            continue_log = "用户确认在线更新。"

        message = (
            f"{lead}\n\n"
            f"目标硬件：{profile['hardware_hint']}\n\n"
            "已通过硬件兼容检查。继续后会再次复核，然后清空 Pico 内的旧文件。\n"
            "所有历史车次数据将会永久消失。"
        )
        self.set_progress(0.62, "等待用户确认")

        def _cancel():
            self.log("用户已取消在线刷入。")
            self._cleanup_temp_dir(temp_dir)
            self.set_progress(1.0, "已取消")
            self.set_ui_state(False)

        def _continue():
            self.log(continue_log)
            try:
                threading.Thread(
                    target=self._online_flash_worker,
                    args=(port, force, temp_dir, firmware_files, profile),
                    daemon=True,
                ).start()
            except Exception as exc:
                self.log(f"[失败] 无法启动在线刷入线程: {exc}")
                self._cleanup_temp_dir(temp_dir)
                self.set_ui_state(False)
                messagebox.showerror("在线刷入失败", str(exc))

        try:
            self.show_confirm_dialog(
                title,
                message,
                yes_text=yes_text,
                no_text="取消",
                on_yes=_continue,
                on_no=_cancel,
                icon="warning",
            )
        except Exception as exc:
            self.log(f"[失败] 无法显示在线刷入确认窗口: {exc}")
            self._cleanup_temp_dir(temp_dir)
            self.set_ui_state(False)
            messagebox.showerror("在线刷入失败", str(exc))

    def _online_flash_worker(
        self, port, force, temp_dir, firmware_files, profile
    ):
        try:
            self.after(0, self.set_progress, 0.64, "准备刷入")
            if not self._wipe_device_files(port, profile):
                return

            self.after(0, self.set_progress, 0.70, "写入设备")
            if not self._copy_firmware_files(
                port, firmware_files, 0.70, 0.25
            ):
                return

            self.log("正在重启 Pico 生效固件...")
            msg_title = "强制刷入完成" if force else "初次/更新安装完成"
            self._complete_flash(port, msg_title)
        except Exception as e:
            error_text = str(e)
            self.log(f"\n[失败] 刷入过程中发生错误: {error_text}")
            self.after(
                0,
                lambda err=error_text: messagebox.showerror(
                    "刷入失败", f"发生意外错误: {err}"
                ),
            )
        finally:
            self._cleanup_temp_dir(temp_dir)
            self.after(0, self.set_ui_state, False)

    def _complete_flash(self, port, title):
        confirmed, detail = self.run_mpremote(
            port, ['exec', 'import machine; machine.reset()'], timeout_sec=10)
        text = ('文件写入完成，设备 USB 已断开并重新连接。\n'
                '请确认接收器屏幕与接收状态正常。\n\n' + detail) if confirmed else (
                '文件写入完成，但无法确认设备已重启并重新连接。\n'
                '不要据此判断固件已正常运行；请检查 USB、屏幕，必要时手动复位。\n\n' + detail)
        self._reset_notice_posted = True
        self._reset_warning = '' if confirmed else text
        self.after(0, self.set_progress, 1.0,
                   '文件写入完成，USB 已重连' if confirmed else '文件写入完成，重启待确认')
        self.log(('\n[完成] ' if confirmed else '\n[警告] ') + text)
        self.after(0, messagebox.showinfo if confirmed else messagebox.showwarning,
                   title if confirmed else '文件写入完成，重启待确认', text)


# History-view engine
import sys


import os


import json


import re


import threading


import serial


import serial.tools.list_ports


import tkinter as tk


from tkinter import ttk, filedialog, messagebox


import customtkinter as ctk


import tkintermapview


ctk.set_appearance_mode("Dark")


ctk.set_default_color_theme("blue")


def is_pico_port(port):
    description = " ".join(str(getattr(port, name, "") or "")
                           for name in ("description", "product", "interface")).lower()
    return port.vid == 0x2E8A and "debug" not in description and "cmsis-dap" not in description


def require_pico_port(port):
    if not any(p.device == port and is_pico_port(p)
               for p in serial.tools.list_ports.comports()):
        raise ValueError("所选串口未识别为 Pico 或设备已断开，请连接 Pico 后重新扫描。")


class _HistoryViewBase(ctk.CTkFrame):
    def __init__(self):
        super().__init__()

        self.title("LBJ Log Viewer")
        self.geometry("1320x860")
        self.minsize(1080, 720)
        self.configure(fg_color=COLORS["bg"])

        self.log_data = []
        self.displayed_data = []
        self.current_marker = None
        self.current_raw_json = ""
        self._pico_busy = False
        self.protocol("WM_DELETE_WINDOW", self._close_app)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main_view()
        self._bind_shortcuts()
        self.refresh_ports(show_prompt=False)

    def _build_sidebar(self):
        self.sidebar_frame = ctk.CTkFrame(
            self, width=286, corner_radius=0, fg_color=COLORS["sidebar"]
        )
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_propagate(False)
        self.sidebar_frame.grid_rowconfigure(1, weight=1)

        brand = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 14))
        ctk.CTkLabel(
            brand,
            text="LBJ LOG VIEWER",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            brand,
            text="列车运行记录",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        controls = ctk.CTkScrollableFrame(
            self.sidebar_frame,
            fg_color="transparent",
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["muted"],
        )
        controls.grid(row=1, column=0, sticky="nsew", padx=(14, 8), pady=0)

        self._sidebar_section(controls, "筛选")
        self.train_no_entry = self._sidebar_entry(controls, "车次，例如 D70")

        time_row = ctk.CTkFrame(controls, fg_color="transparent")
        time_row.pack(fill="x", pady=(0, 8))
        self.time_start_entry = self._sidebar_entry(
            time_row, "开始 14:00", side="left", padx=(0, 4), pady=0
        )
        self.time_end_entry = self._sidebar_entry(
            time_row, "结束 15:30", side="left", padx=(4, 0), pady=0
        )

        self.loco_entry = self._sidebar_entry(controls, "车型，例如 CR400AF")

        filter_actions = ctk.CTkFrame(controls, fg_color="transparent")
        filter_actions.pack(fill="x", pady=(2, 18))
        self.search_btn = ctk.CTkButton(
            filter_actions,
            text="应用筛选",
            height=36,
            corner_radius=6,
            fg_color=COLORS["teal"],
            hover_color=COLORS["teal_hover"],
            text_color="#081713",
            font=ctk.CTkFont(weight="bold"),
            command=self.apply_filter,
        )
        self.search_btn.pack(side="left", fill="x", expand=True)
        self.reset_btn = ctk.CTkButton(
            filter_actions,
            text="重置",
            width=68,
            height=36,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            border_width=1,
            border_color=COLORS["border"],
            command=self.reset_filter,
        )
        self.reset_btn.pack(side="left", padx=(8, 0))

        self._sidebar_section(controls, "地图")
        self.map_source_var = ctk.StringVar(value="高德地图 (极速)")
        self.map_source_menu = ctk.CTkOptionMenu(
            controls,
            variable=self.map_source_var,
            values=["高德地图 (极速)", "OpenStreetMap (默认)", "CartoDB (海外极速)"],
            height=36,
            corner_radius=6,
            fg_color=COLORS["surface_alt"],
            button_color=COLORS["border"],
            button_hover_color=COLORS["teal_hover"],
            command=self.change_map_source,
        )
        self.map_source_menu.pack(fill="x", pady=(0, 18))

        self._sidebar_section(controls, "Pico 设备")
        self.port_frame = ctk.CTkFrame(controls, fg_color="transparent")
        self.port_frame.pack(fill="x", pady=(0, 8))
        self.port_frame.grid_columnconfigure(0, weight=1)
        self.port_frame.grid_columnconfigure(1, weight=0, minsize=76)
        self.port_var = ctk.StringVar(value="请选择端口...")
        self.port_menu = ctk.CTkOptionMenu(
            self.port_frame,
            variable=self.port_var,
            values=["请选择端口..."],
            width=140,
            height=36,
            corner_radius=6,
            fg_color=COLORS["surface_alt"],
            button_color=COLORS["border"],
            button_hover_color=COLORS["teal_hover"],
        )
        self.port_menu.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.refresh_port_btn = ctk.CTkButton(
            self.port_frame,
            text="扫描",
            width=76,
            height=36,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            border_width=1,
            border_color=COLORS["border"],
            command=lambda: self.refresh_ports(show_prompt=True),
        )
        self.refresh_port_btn.grid(row=0, column=1, sticky="e")

        self.read_pico_btn = ctk.CTkButton(
            controls,
            text="读取设备记录",
            height=38,
            corner_radius=6,
            fg_color=COLORS["green"],
            hover_color="#389966",
            text_color="#07150D",
            font=ctk.CTkFont(weight="bold"),
            command=self.start_pico_read,
        )
        self.read_pico_btn.pack(fill="x", pady=(0, 8))

        self.export_pico_btn = ctk.CTkButton(
            controls,
            text="导出日志到电脑",
            height=38,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            border_width=1,
            border_color=COLORS["amber"],
            text_color=COLORS["amber"],
            command=self.start_pico_export,
        )
        self.export_pico_btn.pack(fill="x", pady=(0, 20))

        self.source_status = ctk.CTkLabel(
            self.sidebar_frame,
            text="尚未加载日志",
            height=38,
            text_color=COLORS["muted"],
            fg_color=COLORS["surface"],
            corner_radius=0,
            anchor="w",
            padx=22,
        )
        self.source_status.grid(row=2, column=0, sticky="ew")

    def _build_main_view(self):
        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color=COLORS["bg"])
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=18)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(2, weight=3, minsize=330)
        self.main_frame.grid_rowconfigure(3, weight=2, minsize=175)

        header = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.grid_columnconfigure(0, weight=1)
        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_group,
            text="列车运行日志",
            font=ctk.CTkFont(size=27, weight="bold"),
            text_color=COLORS["text"],
        ).pack(anchor="w")
        self.header_subtitle = ctk.CTkLabel(
            title_group,
            text="选择记录以查看列车位置与运行信息",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
        )
        self.header_subtitle.pack(anchor="w", pady=(2, 0))

        self.load_btn = ctk.CTkButton(
            header,
            text="导入日志",
            width=112,
            height=38,
            corner_radius=6,
            fg_color=COLORS["blue"],
            hover_color="#3C73D2",
            font=ctk.CTkFont(weight="bold"),
            command=self.load_json_file,
        )
        self.load_btn.grid(row=0, column=1, sticky="e")

        stats = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        stats.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        for column in range(3):
            stats.grid_columnconfigure(column, weight=1)
        self.total_value = self._metric(stats, 0, "当前记录", "0", COLORS["teal"])
        self.gps_value = self._metric(stats, 1, "有效位置", "0", COLORS["green"])
        self.latest_value = self._metric(stats, 2, "最新时间", "--:--", COLORS["amber"])

        focus_area = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        focus_area.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
        focus_area.grid_columnconfigure(0, weight=3)
        focus_area.grid_columnconfigure(1, weight=0, minsize=330)
        focus_area.grid_rowconfigure(0, weight=1)

        self.map_frame = ctk.CTkFrame(
            focus_area,
            corner_radius=8,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self.map_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.map_frame.grid_columnconfigure(0, weight=1)
        self.map_frame.grid_rowconfigure(1, weight=1)

        map_header = ctk.CTkFrame(self.map_frame, height=44, fg_color="transparent")
        map_header.grid(row=0, column=0, sticky="ew", padx=14)
        map_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            map_header,
            text="列车位置",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLORS["text"],
        ).grid(row=0, column=0, sticky="w", pady=10)
        self.map_location_label = ctk.CTkLabel(
            map_header,
            text="等待选择",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
        )
        self.map_location_label.grid(row=0, column=1, sticky="e")

        self.map_widget = tkintermapview.TkinterMapView(
            self.map_frame, corner_radius=0
        )
        self.map_widget.set_tile_server("https://wprd01.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=7", max_zoom=19)
        self.map_widget.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        self.map_widget.set_position(35.8, 104.2)
        self.map_widget.set_zoom(4)

        self.detail_panel = ctk.CTkFrame(
            focus_area,
            width=330,
            corner_radius=8,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self.detail_panel.grid(row=0, column=1, sticky="nsew")
        self.detail_panel.grid_propagate(False)
        self.detail_panel.grid_columnconfigure(0, weight=1)

        detail_header = ctk.CTkFrame(self.detail_panel, fg_color="transparent")
        detail_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 0))
        detail_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            detail_header,
            text="当前列车",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="w")
        self.gps_badge = ctk.CTkLabel(
            detail_header,
            text="未定位",
            width=62,
            height=24,
            corner_radius=5,
            fg_color=COLORS["surface_alt"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.gps_badge.grid(row=0, column=1, sticky="e")

        self.raw_button = ctk.CTkButton(
            detail_header,
            text="查看原始日志",
            width=100,
            height=24,
            corner_radius=5,
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["muted"],
            state="disabled",
            command=self.show_raw_record,
        )
        self.raw_button.grid(row=0, column=2, sticky="e", padx=(6, 0))

        self.train_no_value = ctk.CTkLabel(
            self.detail_panel,
            text="---",
            font=ctk.CTkFont(size=34, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        )
        self.train_no_value.grid(row=1, column=0, sticky="ew", padx=18, pady=(5, 12))

        primary_info = ctk.CTkFrame(
            self.detail_panel,
            height=70,
            fg_color=COLORS["surface_alt"],
            corner_radius=6,
        )
        primary_info.grid(row=2, column=0, sticky="ew", padx=18)
        primary_info.grid_propagate(False)
        primary_info.grid_columnconfigure(0, weight=1)
        primary_info.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            primary_info, text="速度", text_color=COLORS["muted"], font=ctk.CTkFont(size=11)
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        ctk.CTkLabel(
            primary_info, text="车型", text_color=COLORS["muted"], font=ctk.CTkFont(size=11)
        ).grid(row=0, column=1, sticky="w", padx=12, pady=(10, 0))
        self.speed_value = ctk.CTkLabel(
            primary_info, text="-- km/h", text_color=COLORS["teal"], font=ctk.CTkFont(size=20, weight="bold")
        )
        self.speed_value.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))
        self.loco_value = ctk.CTkLabel(
            primary_info, text="未知", text_color=COLORS["text"], font=ctk.CTkFont(size=15, weight="bold")
        )
        self.loco_value.grid(row=1, column=1, sticky="w", padx=12, pady=(0, 10))

        ctk.CTkLabel(
            self.detail_panel,
            text="记录时间",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=3, column=0, sticky="ew", padx=18, pady=(14, 0))
        self.detail_time_value = ctk.CTkLabel(
            self.detail_panel,
            text="--",
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=13),
            anchor="w",
        )
        self.detail_time_value.grid(row=4, column=0, sticky="ew", padx=18)

        ctk.CTkLabel(
            self.detail_panel,
            text="经纬度",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=5, column=0, sticky="ew", padx=18, pady=(10, 0))
        self.coordinate_value = ctk.CTkLabel(
            self.detail_panel,
            text="无有效坐标",
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=13),
            anchor="w",
        )
        self.coordinate_value.grid(row=6, column=0, sticky="ew", padx=18)

        self.tree_frame = ctk.CTkFrame(
            self.main_frame,
            corner_radius=8,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self.tree_frame.grid(row=3, column=0, sticky="nsew")
        self.tree_frame.grid_columnconfigure(0, weight=1)
        self.tree_frame.grid_rowconfigure(1, weight=1)

        table_header = ctk.CTkFrame(self.tree_frame, height=42, fg_color="transparent")
        table_header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14)
        table_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            table_header,
            text="运行记录",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLORS["text"],
        ).grid(row=0, column=0, sticky="w", pady=9)
        self.result_count_label = ctk.CTkLabel(
            table_header,
            text="0 条",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
        )
        self.result_count_label.grid(row=0, column=1, sticky="e")
        self.setup_treeview()

    def _sidebar_section(self, parent, text):
        ctk.CTkLabel(
            parent,
            text=text,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        ).pack(fill="x", pady=(2, 7))

    def _sidebar_entry(self, parent, placeholder, side=None, padx=0, pady=(0, 8)):
        entry = ctk.CTkEntry(
            parent,
            placeholder_text=placeholder,
            width=96 if side else 200,
            height=36,
            corner_radius=6,
            fg_color=COLORS["surface_alt"],
            border_color=COLORS["border"],
            border_width=1,
        )
        if side:
            entry.pack(side=side, fill="x", expand=True, padx=padx, pady=pady)
        else:
            entry.pack(fill="x", padx=padx, pady=pady)
        return entry

    def _metric(self, parent, column, label, value, accent):
        card = ctk.CTkFrame(
            parent,
            height=68,
            corner_radius=7,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        card.grid(
            row=0,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 6, 0 if column == 2 else 6),
        )
        card.grid_propagate(False)
        card.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(
            card, width=4, height=46, corner_radius=2, fg_color=accent
        ).grid(row=0, column=0, rowspan=2, padx=(0, 14), pady=11)
        ctk.CTkLabel(
            card,
            text=label,
            font=ctk.CTkFont(size=11),
            text_color=COLORS["muted"],
            anchor="w",
        ).grid(row=0, column=1, sticky="sw", pady=(8, 0))
        value_label = ctk.CTkLabel(
            card,
            text=value,
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        )
        value_label.grid(row=1, column=1, sticky="nw", pady=(0, 7))
        return value_label

    def _bind_shortcuts(self):
        self.bind("<Return>", lambda event: self.apply_filter())
        self.bind("<Escape>", lambda event: self.reset_filter())
        self.bind("<Control-o>", lambda event: self.load_json_file())
        self.bind("<Command-o>", lambda event: self.load_json_file())

    def show_raw_record(self):
        if not self.current_raw_json:
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("原始日志记录")
        dialog.geometry("760x560")
        dialog.minsize(560, 400)
        dialog.configure(fg_color=COLORS["bg"])
        dialog.transient(self.winfo_toplevel())

        ctk.CTkLabel(
            dialog,
            text="原始日志记录",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 10))

        text = ctk.CTkTextbox(
            dialog,
            corner_radius=6,
            fg_color="#111519",
            border_width=1,
            border_color=COLORS["border"],
            text_color="#C7D0D8",
            font=ctk.CTkFont(family="Menlo", size=12),
            wrap="none",
        )
        text.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        text.insert("0.0", self.current_raw_json)
        text.configure(state="disabled")
        dialog.after(100, dialog.focus_force)

    # ==================== 核心逻辑与功能函数 ====================
    def change_map_source(self, choice):
        if "高德" in choice:
            self.map_widget.set_tile_server("https://wprd01.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=7", max_zoom=19)
        elif "OpenStreetMap" in choice:
            self.map_widget.set_tile_server("https://a.tile.openstreetmap.org/{z}/{x}/{y}.png", max_zoom=19)
        elif "CartoDB" in choice:
            self.map_widget.set_tile_server("https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png", max_zoom=19)

    def refresh_ports(self, show_prompt=False):
        if self._pico_busy:
            return
        try:
            port_list = [p.device for p in serial.tools.list_ports.comports() if is_pico_port(p)]
        except Exception as exc:
            port_list = []
            if show_prompt:
                messagebox.showerror("扫描失败", str(exc))
                show_prompt = False
        selected = self.port_var.get()
        self.port_menu.configure(values=port_list or ["未识别到 Pico"],
                                 state="normal" if port_list else "disabled")
        self.port_var.set(selected if selected in port_list else
                          (port_list[0] if port_list else "未识别到 Pico"))
        state = "normal" if port_list else "disabled"
        self.read_pico_btn.configure(state=state)
        self.export_pico_btn.configure(state=state)
        if show_prompt:
            if port_list:
                messagebox.showinfo("成功", f"已识别 Pico：{self.port_var.get()}")
            else:
                messagebox.showwarning("未识别到 Pico", "读取和导出已禁用。请连接运行 MicroPython 的 Pico 后重新扫描。")

    def _validated_pico_port(self):
        port = self.port_var.get()
        try:
            require_pico_port(port)
        except Exception as exc:
            self.refresh_ports()
            messagebox.showwarning("无法读取 Pico", str(exc))
            return None
        return port

    def start_pico_read(self):
        if self._pico_busy:
            return
        port = self._validated_pico_port()
        if port is None:
            return

        self._pico_busy = True
        self.read_pico_btn.configure(state="disabled", text="读取中，请稍候...")
        self.export_pico_btn.configure(state="disabled")
        self.load_btn.configure(state="disabled")

        threading.Thread(target=self._pico_worker, args=(port,), daemon=True).start()

    def start_pico_export(self):
        if self._pico_busy:
            return
        port = self._validated_pico_port()
        if port is None:
            return

        save_path = filedialog.asksaveasfilename(
            defaultextension=".jsonl",
            initialfile="history.jsonl",
            title="保存 Pico 日志文件",
            filetypes=[("JSON Lines", "*.jsonl"), ("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if not save_path: return

        self._pico_busy = True
        self.export_pico_btn.configure(state="disabled", text="导出中，请稍候...")
        self.read_pico_btn.configure(state="disabled")
        self.load_btn.configure(state="disabled")

        threading.Thread(target=self._export_worker, args=(port, save_path), daemon=True).start()

    def _pico_worker(self, port):
        self._device_history_worker(port)

    def _export_worker(self, port, save_path):
        self._device_history_worker(port, save_path)

    def _device_history_worker(self, port, save_path=None):
        button = self.export_pico_btn if save_path else self.read_pico_btn
        last_percent = -1

        def status(text):
            self.after(0, lambda text=text: button.configure(text=text))

        def progress(done, total):
            nonlocal last_percent
            percent = done * 100 // total if total else 100
            # At most 101 queued UI updates, even for large histories.
            if percent != last_percent:
                last_percent = percent
                status(f"传输 {percent}% · {done / 1048576:.2f}/{total / 1048576:.2f} MB")

        try:
            require_pico_port(port)
            result = download_history(port, progress=progress, status=status)
            if result.recovery_warning:
                self.after(0, lambda warning=result.recovery_warning: messagebox.showwarning("设备恢复提示", warning))
            if save_path:
                save_history_atomic(save_path, result.data)
                self.after(0, lambda p=save_path: messagebox.showinfo("成功", f"日志已完整导出并通过 SHA-256 校验：\n{p}"))
            else:
                # Decode only after reassembling all blocks; never silently
                # discard damaged UTF-8 or load a partially received file.
                lines = result.data.decode("utf-8").splitlines()
                self.after(0, self._process_memory_lines, lines, "Pico 设备（已校验）")
        except Exception as e:
            self.after(0, lambda err=str(e): messagebox.showerror("设备日志传输失败", f"未加载或覆盖日志文件。\n请确认数据线连接且串口未被其他程序占用。\n\n{err}"))
        finally:
            self.after(0, self._finish_device_transfer)

    def _finish_device_transfer(self):
        self._pico_busy = False
        self.read_pico_btn.configure(state="normal", text="读取设备记录")
        self.export_pico_btn.configure(state="normal", text="导出日志到电脑")
        self.load_btn.configure(state="normal")
        self.refresh_ports()

    def _close_app(self):
        if self._pico_busy:
            messagebox.showwarning("传输尚未完成", "正在读取设备日志，请等待设备恢复运行后再关闭软件。")
            return
        self.destroy()

    def _process_memory_lines(self, lines, source_name="日志文件"):
        self.log_data.clear()
        valid_count = 0
        for line in lines:
            line = line.strip()
            if not line: continue
            try:
                data = json.loads(line)
                parsed_entry = self.extract_log_info(data)
                parsed_entry['_index'] = len(self.log_data)
                self.log_data.append(parsed_entry)
                valid_count += 1
            except json.JSONDecodeError:
                continue

        self.source_status.configure(text=f"{source_name} · {valid_count} 条记录")
        self.header_subtitle.configure(text=f"数据来源：{source_name}")
        self.refresh_treeview(self.log_data)
        if valid_count > 0:
            messagebox.showinfo("成功", f"成功提取并解析了 {valid_count} 条记录！")
        else:
            messagebox.showwarning("提示", "未找到有效的 JSON 历史数据。")

    def setup_treeview(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            style.theme_use("default")
        style.configure(
            "Rail.Treeview",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["surface"],
            borderwidth=0,
            relief="flat",
            rowheight=31,
            font=("TkDefaultFont", 12),
        )
        style.map(
            "Rail.Treeview",
            background=[("selected", "#244B49")],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Rail.Treeview.Heading",
            background=COLORS["surface_alt"],
            foreground=COLORS["muted"],
            borderwidth=0,
            relief="flat",
            padding=(8, 8),
            font=("TkDefaultFont", 11, "bold"),
        )
        style.map(
            "Rail.Treeview.Heading",
            background=[("active", COLORS["surface_alt"])],
            foreground=[("active", COLORS["text"])],
        )
        style.configure(
            "Rail.Vertical.TScrollbar",
            troughcolor=COLORS["surface"],
            background=COLORS["border"],
            bordercolor=COLORS["surface"],
            arrowcolor=COLORS["muted"],
            darkcolor=COLORS["border"],
            lightcolor=COLORS["border"],
        )
        style.map(
            "Rail.Vertical.TScrollbar",
            background=[("active", COLORS["muted"])],
        )

        columns = ("time", "train_no", "speed", "loco_type", "gps_status")
        self.tree = ttk.Treeview(
            self.tree_frame,
            columns=columns,
            show="headings",
            style="Rail.Treeview",
            selectmode="browse",
            height=5,
        )

        self.tree.heading("time", text="时间")
        self.tree.heading("train_no", text="车次")
        self.tree.heading("speed", text="速度")
        self.tree.heading("loco_type", text="车型")
        self.tree.heading("gps_status", text="位置")

        self.tree.column("time", width=175, minwidth=130, anchor="w")
        self.tree.column("train_no", width=120, minwidth=90, anchor="w")
        self.tree.column("speed", width=100, minwidth=80, anchor="center")
        self.tree.column("loco_type", width=180, minwidth=120, anchor="w")
        self.tree.column("gps_status", width=100, minwidth=80, anchor="center")

        self.tree.tag_configure("even", background=COLORS["surface"])
        self.tree.tag_configure("odd", background="#181D22")
        self.tree.tag_configure("no_gps", foreground="#808A93")

        scrollbar = ttk.Scrollbar(
            self.tree_frame,
            orient="vertical",
            command=self.tree.yview,
            style="Rail.Vertical.TScrollbar",
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, 8), pady=(0, 10))
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

    def parse_coordinate(self, coord_str):
        if not coord_str: return None
        try:
            match = re.match(r"(\d+)°([\d.]+)'\s*([NSEW])", coord_str)
            if match:
                deg, minute, direction = match.groups()
                decimal = float(deg) + float(minute) / 60.0
                if direction in ['S', 'W']: decimal = -decimal
                return round(decimal, 6)
        except Exception:
            pass
        return None

    def load_json_file(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON Lines", "*.json *.jsonl *.txt"), ("All Files", "*.*")])
        if not filepath: return
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            self._process_memory_lines(lines, os.path.basename(filepath))
        except Exception as e:
            messagebox.showerror("错误", f"读取文件失败: {str(e)}")

    def extract_log_info(self, raw_json):
        d_block = raw_json.get("d", {})
        basic_block = d_block.get("basic", {})
        ext_block = d_block.get("extended", {})

        class_tag = ext_block.get("class_tag", "").strip()
        t_time = raw_json.get("t", "未知")

        train_no_raw = basic_block.get("train_no", "---").strip()
        if train_no_raw.replace("-", "") == "":
            train_no = "未知"
        else:
            train_no = f"{class_tag}{train_no_raw}"

        speed = basic_block.get("speed_kmh", "0")
        if str(speed).replace("-", "").strip() == "": speed = "0"

        loco_type = ext_block.get("loco_type", "未知")
        lat_raw = ext_block.get("lat", "")
        lon_raw = ext_block.get("lon", "")

        lat_dec = self.parse_coordinate(lat_raw)
        lon_dec = self.parse_coordinate(lon_raw)

        gps_status = "有坐标" if lat_dec is not None and lon_dec is not None else "无"

        return {
            "time": t_time,
            "train_no": train_no,
            "speed": speed,
            "loco_type": loco_type,
            "lat": lat_dec,
            "lon": lon_dec,
            "gps_status": gps_status,
            "raw": json.dumps(raw_json, indent=2, ensure_ascii=False)
        }

    def refresh_treeview(self, data_list):
        self.displayed_data = list(data_list)
        for item in self.tree.get_children():
            self.tree.delete(item)

        for row_index, entry in enumerate(data_list):
            tags = ["even" if row_index % 2 == 0 else "odd"]
            if entry["gps_status"] == "无":
                tags.append("no_gps")
            self.tree.insert("", "end", iid=entry['_index'], values=(
                entry["time"],
                entry["train_no"],
                f"{entry['speed']} km/h",
                entry["loco_type"],
                "已定位" if entry["gps_status"] == "有坐标" else "无坐标"
            ), tags=tuple(tags))

        visible_count = len(data_list)
        gps_count = sum(1 for entry in data_list if entry["gps_status"] == "有坐标")
        latest_time = str(data_list[-1]["time"]) if data_list else "--:--"
        self.total_value.configure(text=str(visible_count))
        self.gps_value.configure(text=str(gps_count))
        self.latest_value.configure(text=latest_time[-8:] if latest_time else "--:--")
        self.result_count_label.configure(
            text=f"{visible_count} / {len(self.log_data)} 条"
        )

        self._clear_selection_detail()
        children = self.tree.get_children()
        if children:
            latest_item = children[-1]
            self.tree.selection_set(latest_item)
            self.tree.focus(latest_item)
            self.tree.see(latest_item)
            self.on_tree_select(None)

    def _clear_selection_detail(self):
        self.train_no_value.configure(text="---")
        self.speed_value.configure(text="-- km/h")
        self.loco_value.configure(text="未知")
        self.detail_time_value.configure(text="--")
        self.coordinate_value.configure(text="无有效坐标")
        self.gps_badge.configure(
            text="未定位",
            fg_color=COLORS["surface_alt"],
            text_color=COLORS["muted"],
        )
        self.map_location_label.configure(text="等待选择")
        self.current_raw_json = ""
        self.raw_button.configure(state="disabled")
        if self.current_marker:
            self.current_marker.delete()
            self.current_marker = None

    # ================== ★ 核心筛选逻辑大改 ==================
    def apply_filter(self):
        filter_train = self.train_no_entry.get().strip().upper()
        filter_loco = self.loco_entry.get().strip().upper()

        # 获取起止时间并补齐秒数
        start_time = self.time_start_entry.get().strip()
        end_time = self.time_end_entry.get().strip()

        if start_time and len(start_time) <= 5: start_time += ":00"
        if end_time and len(end_time) <= 5: end_time += ":59"

        filtered_data = []
        for entry in self.log_data:
            match_train = filter_train in str(entry["train_no"]).upper() if filter_train else True
            match_loco = filter_loco in str(entry["loco_type"]).upper() if filter_loco else True

            # 时间范围比对逻辑
            log_time = str(entry["time"])
            time_only = log_time.split(' ')[-1] if ' ' in log_time else log_time

            match_time = True
            if start_time and end_time:
                match_time = start_time <= time_only <= end_time
            elif start_time:
                match_time = time_only >= start_time
            elif end_time:
                match_time = time_only <= end_time

            if match_train and match_time and match_loco:
                filtered_data.append(entry)

        self.refresh_treeview(filtered_data)
        self.header_subtitle.configure(
            text=f"筛选结果：{len(filtered_data)} 条记录"
        )

    def reset_filter(self):
        # 1. 加了安全判断：只有框里确实有内容时，才去执行 delete，完美避开 ctk 的占位符 Bug
        if self.train_no_entry.get():
            self.train_no_entry.delete(0, 'end')

        if self.time_start_entry.get():
            self.time_start_entry.delete(0, 'end')

        if self.time_end_entry.get():
            self.time_end_entry.delete(0, 'end')

        if self.loco_entry.get():
            self.loco_entry.delete(0, 'end')

        # 2. 强制主窗口拿回焦点，让那些被删掉内容的输入框重新显示出占位符
        self.focus_set()

        # 3. 刷新表格恢复全量数据
        self.refresh_treeview(self.log_data)
        self.header_subtitle.configure(text="已显示全部记录")
    # =========================================================

    def on_tree_select(self, event):
        selected_items = self.tree.selection()
        if not selected_items: return

        index = int(selected_items[0])
        entry = self.log_data[index]

        self.train_no_value.configure(text=entry["train_no"])
        self.speed_value.configure(text=f"{entry['speed']} km/h")
        self.loco_value.configure(text=entry["loco_type"])
        self.detail_time_value.configure(text=entry["time"])
        self.current_raw_json = entry["raw"]
        self.raw_button.configure(state="normal")

        if self.current_marker:
            self.current_marker.delete()
            self.current_marker = None

        lat = entry['lat']
        lon = entry['lon']

        if lat is not None and lon is not None:
            if -85.0 < lat < 85.0 and -180.0 <= lon <= 180.0:
                self.coordinate_value.configure(text=f"{lat:.6f}, {lon:.6f}")
                self.gps_badge.configure(
                    text="已定位",
                    fg_color="#173B31",
                    text_color=COLORS["green"],
                )
                self.map_location_label.configure(text=f"{lat:.4f}, {lon:.4f}")
                self.map_widget.set_position(lat, lon)
                self.map_widget.set_zoom(14)
                self.current_marker = self.map_widget.set_marker(
                    lat, lon,
                    text=f"{entry['train_no']} ({entry['speed']}km/h)"
                )
            else:
                self.coordinate_value.configure(text="坐标超出范围")
                self.gps_badge.configure(
                    text="无效坐标",
                    fg_color="#402729",
                    text_color=COLORS["red"],
                )
                self.map_location_label.configure(text="坐标无效")
                print(f"坐标非法被拦截 -> 车次: {entry['train_no']}, 坐标: ({lat}, {lon})")
        else:
            self.coordinate_value.configure(text="无有效坐标")
            self.gps_badge.configure(
                text="未定位",
                fg_color=COLORS["surface_alt"],
                text_color=COLORS["muted"],
            )
            self.map_location_label.configure(text="该记录无坐标")


# Unified integration layer
"""Unified window, device ownership and integration overrides."""
import collections
import ast
import math
import queue
import time
from dataclasses import dataclass

MANAGER_VERSION = '3.0.5-preview'
PLACEHOLDER_PORT = '未选择 Pico'
TrainLogApp = _HistoryViewBase  # Compatibility for engine regression tests.


@dataclass(frozen=True)
class RebootResult:
    sent: bool
    reconnected: bool
    port: str = ''
    message: str = ''


def same_usb_receiver(original, candidate):
    """Never mistake another attached receiver for the one being rebooted."""
    if not is_pico_port(candidate) or (candidate.vid, candidate.pid) != (original.vid, original.pid):
        return False
    serial_number = getattr(original, 'serial_number', None)
    if serial_number:
        return serial_number == getattr(candidate, 'serial_number', None)
    if candidate.device != original.device:
        return False  # A renamed port requires a stable USB serial number.
    location = getattr(original, 'location', None)
    candidate_location = getattr(candidate, 'location', None)
    return not (location and candidate_location) or location == candidate_location


def reboot_receiver(port, timeout=10, transport_factory=None, enumerate_ports=None,
                    clock=None, sleep=None):
    """Send a reset without following EOF, close, observe the same USB device.

    USB re-enumeration is not proof that main.py or RF reception is healthy.
    No post-reset REPL command is issued, so verification does not stop the
    newly started receiver. All operations belong in the device worker.
    """
    enumerate_ports = enumerate_ports or serial.tools.list_ports.comports
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    try:
        original = [item for item in enumerate_ports() if item.device == port and is_pico_port(item)]
    except Exception as exc:
        return RebootResult(False, False, message='重启前无法枚举设备：' + str(exc))
    if len(original) != 1:
        return RebootResult(False, False, message='重启前未找到所选 Pico，未发送重启指令。')
    original = original[0]
    transport, sent, error, close_error = None, False, '', ''
    try:
        transport = (transport_factory or _connect)(port)
        transport.serial.timeout = 2
        transport.serial.write_timeout = 3
        transport.use_raw_paste = False
        transport.enter_raw_repl(soft_reset=False, timeout_overall=5)
        # Brief delay lets the raw-REPL acknowledgement reach Windows and
        # the host close its old handle before USB disappears. Never follow
        # command output / EOF, and never exit raw REPL after issuing reset.
        transport.exec_raw_no_follow('import machine, time; time.sleep_ms(150); machine.reset()')
        sent = True
    except Exception as exc:
        error = '未能确认重启指令已发送：' + str(exc)
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                close_error = '释放旧串口失败：' + str(exc)
    if not sent:
        return RebootResult(False, False, message=error + ('；' + close_error if close_error else ''))
    deadline, disappeared = clock() + max(0, timeout), False
    while clock() < deadline:
        try:
            matches = [item for item in enumerate_ports() if same_usb_receiver(original, item)]
        except Exception as exc:
            return RebootResult(True, False, message='重启指令已发送，USB 枚举失败：' + str(exc))
        if not matches:
            disappeared = True
        elif disappeared and len(matches) == 1:
            if close_error:
                return RebootResult(True, False, matches[0].device, close_error)
            return RebootResult(True, True, matches[0].device,
                                '同一接收器已重新连接：' + matches[0].device)
        sleep(min(.1, max(0, deadline - clock())))
    detail = '未在限定时间内重新检测到同一接收器。' if disappeared else '未观察到 USB 断开重连，不能确认重启。'
    return RebootResult(True, False, message='重启指令已发送；' + detail + ('；' + close_error if close_error else ''))


def dropped_file(tcl, data, extensions):
    """Decode the OS's Tcl file list, preserving spaces, braces and Unicode."""
    try:
        paths = tcl.splitlist(data)
    except (tk.TclError, ValueError, TypeError) as exc:
        raise ValueError('无法识别拖入的文件。') from exc
    if len(paths) != 1:
        raise ValueError('请每次只拖入一个文件。')
    path = os.path.abspath(paths[0])
    if not os.path.isfile(path):
        raise ValueError('请拖入本地文件，不支持文件夹或已移除的文件。')
    if os.path.splitext(path)[1].lower() not in extensions:
        raise ValueError('此区域支持：' + '、'.join(sorted(extensions)) + ' 文件。')
    return path


def validate_firmware_bundle(profile, files):
    """Reject missing local imports before any destructive flash operation.

    Old firmware without the newer imports remains supported. A modern
    receiver that imports PioDmaRx must include it, even though its firmware
    has a runtime FIFO fallback for boards without hardware DMA.
    """
    names = [item['name'] for item in files]
    if len(set(names)) != len(names):
        raise ValueError('固件包包含重复的运行文件。')
    by_name = {item['name']: item for item in files}
    allowed = set(runtime_file_order(profile))
    missing = set(profile['runtime_files']) - set(names)
    if missing:
        raise ValueError('固件包缺少运行文件：' + ', '.join(sorted(missing)))
    if set(names) - allowed:
        raise ValueError('固件包包含当前分支不允许的文件。')
    modules = {name[:-3]: name for name in allowed if name.endswith('.py')}
    for name, item in by_name.items():
        if not name.endswith('.py'):
            continue
        try:
            with open(item['path'], 'r', encoding='utf-8') as source:
                tree = ast.parse(source.read(), filename=name)
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise ValueError('无法验证固件文件 ' + name + '：' + str(exc)) from exc
        # boot.py runs implicitly at power-on, not via a Python import.
        # A main.py using its display hand-off must not silently lose the
        # startup frame during a wipe-and-copy update. Older applications
        # with no hand-off remain compatible without boot.py.
        if (name == 'main.py' and 'boot.py' not in by_name
                and any(isinstance(node, ast.Name) and node.id == '_boot_display'
                        and isinstance(node.ctx, ast.Load) for node in ast.walk(tree))):
            raise ValueError('main.py 使用早期开机画面，但固件包缺少 boot.py，已阻止刷入。')
        for node in ast.walk(tree):
            imported = ([alias.name.split('.')[0] for alias in node.names]
                        if isinstance(node, ast.Import) else
                        [node.module.split('.')[0]]
                        if isinstance(node, ast.ImportFrom) and node.module else [])
            for module in imported:
                dependency = modules.get(module)
                if dependency and dependency not in by_name:
                    raise ValueError(name + ' 依赖缺失文件 ' + dependency + '，已阻止刷入。')
    return names


class TaskBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceLease:
    serial: int
    owner: str
    port: str


class DeviceTasks:
    """One serial owner for the entire application; stale releases do nothing."""
    def __init__(self):
        self._lock = threading.Lock()
        self._lease = None
        self._serial = 0

    @property
    def active(self):
        with self._lock:
            return self._lease

    def acquire(self, owner, port):
        if not owner or not port or port in (PLACEHOLDER_PORT, '未检测到设备', '请选择端口...'):
            raise ValueError('必须明确选择设备和操作。')
        with self._lock:
            if self._lease is not None:
                raise TaskBusyError('设备正在执行' + self._lease.owner)
            self._serial += 1
            self._lease = DeviceLease(self._serial, owner, port)
            return self._lease

    def release(self, lease):
        with self._lock:
            if lease is not self._lease or lease is None:
                return False
            self._lease = None
            return True


class UiDispatch:
    """Workers enqueue Python objects, never call Tcl/Tk directly."""
    def __init__(self):
        self._queue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self.closed = False

    def post(self, delay, callback, args=()):
        with self._lock:
            if self.closed:
                return False
            self._queue.put((delay, callback, args))
            return True

    def drain(self, schedule, limit=100, budget_ms=6):
        started = time.monotonic()
        count = 0
        while count < limit and (time.monotonic() - started) * 1000 < budget_ms:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                closed = self.closed
            if not closed:
                schedule(*item)
            count += 1
        return count

    def close(self):
        with self._lock:
            self.closed = True


def choose_port(ports, selected, initial=False, explicit=False):
    """Single-device auto-selection needs startup or an explicit scan click."""
    candidates = [p.device for p in ports if is_pico_port(p)]
    if selected in candidates:
        return candidates, selected
    return candidates, candidates[0] if (initial or explicit) and len(candidates) == 1 else PLACEHOLDER_PORT


def decimal_coordinate(value, kind=None):
    if value in ('', None) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.fullmatch(r"\s*(\d{1,3})°([\d.]+)'\s*([NSEW])\s*", str(value))
        if match is None:
            return None
        degrees, minutes, direction = match.groups()
        try:
            minutes = float(minutes)
            if not 0 <= minutes < 60:
                return None
            if kind == 'lat' and direction not in 'NS' or kind == 'lon' and direction not in 'EW':
                return None
            number = float(degrees) + minutes / 60
            if direction in 'SW':
                number = -number
        except ValueError:
            return None
    bound = 90 if kind == 'lat' else 180
    return round(number, 6) if math.isfinite(number) and -bound <= number <= bound else None


def normalise_record(record):
    if not isinstance(record, dict):
        raise ValueError('记录不是 JSON 对象')
    d = record.get('d', {})
    if not isinstance(d, dict):
        raise ValueError('数据字段不是对象')
    basic, ext = d.get('basic') or {}, d.get('extended') or {}
    if not isinstance(basic, dict) or not isinstance(ext, dict):
        raise ValueError('基础或扩展字段不是对象')
    number = basic.get('train_no')
    train = '---' if number is None or number == '' else str(number).strip()
    prefix = str(ext.get('class_tag') or '').strip()
    if not prefix.strip('- '):
        prefix = ''
    train = '未知' if not train.strip('- ') else prefix + train
    speed = str(basic.get('speed_kmh', '---')).strip()
    if not speed.strip('- ') or speed == 'None':
        speed = '---'
    lat = decimal_coordinate(ext.get('lat'), 'lat')
    lon = decimal_coordinate(ext.get('lon'), 'lon')
    return {'time': str(record.get('t') or '未知'), 'train_no': train, 'speed': speed,
            'loco_type': str(ext.get('loco_type') or '未知'), 'lat': lat, 'lon': lon,
            'gps_status': '有坐标' if lat is not None and lon is not None else '无',
            'raw': json.dumps(record, indent=2, ensure_ascii=False)}


def parse_history_lines(lines):
    records, invalid = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = normalise_record(json.loads(line))
        except (ValueError, TypeError):
            invalid += 1
            continue
        entry['_index'] = len(records)
        records.append(entry)
    return records, invalid


def time_filter_value(value, end=False):
    value = value.strip()
    if not value:
        return None
    match = re.fullmatch(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', value)
    if match is None:
        raise ValueError('时间请使用 HH:MM 或 HH:MM:SS。')
    hour, minute, second = match.groups()
    hour, minute, second = int(hour), int(minute), int(second) if second else (59 if end else 0)
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError('时间范围不正确。')
    return hour * 3600 + minute * 60 + second


def filter_records(records, train='', loco='', start='', end=''):
    lower, upper = time_filter_value(start), time_filter_value(end, True)
    result = []
    for record in records:
        if train.strip().upper() not in record['train_no'].upper():
            continue
        if loco.strip().upper() not in record['loco_type'].upper():
            continue
        if lower is not None or upper is not None:
            match = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)$', record['time'])
            if match is None:
                continue
            try:
                stamp = time_filter_value(match.group(1))
            except ValueError:
                continue
            if lower is not None and upper is not None and lower > upper:
                if not (stamp >= lower or stamp <= upper):
                    continue
            elif (lower is not None and stamp < lower) or (upper is not None and stamp > upper):
                continue
        result.append(record)
    return result


def run_command(command, timeout, on_line=None, popen=None):
    """Drain output independently so an idle/partial line cannot defeat timeout."""
    child = (popen or subprocess.Popen)(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, bufsize=0)
    output = queue.SimpleQueue()

    def read_output():
        try:
            while True:
                line = child.stdout.readline()
                if not line:
                    break
                output.put(line)
        finally:
            output.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout
    chunks = []
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                line = output.get(timeout=min(remaining, .1))
            except queue.Empty:
                continue
            if line is None:
                break
            chunks.append(line)
            if on_line:
                try:
                    on_line(line.decode('utf-8', 'replace').rstrip())
                except Exception:
                    pass  # UI observers cannot interrupt the device command.
        child.wait(timeout=max(.01, deadline - time.monotonic()))
        return child.returncode == 0, b''.join(chunks).decode('utf-8', 'replace')
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
        return False, '命令超时，已停止子进程。\n' + b''.join(chunks).decode('utf-8', 'replace')
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()


class HistoryPanel(_HistoryViewBase):
    def __init__(self, master, owner):
        ctk.CTkFrame.__init__(self, master, fg_color=COLORS['bg'], corner_radius=0)
        self.owner = owner
        self.log_data, self.displayed_data = [], []
        self.current_marker, self.current_raw_json = None, ''
        self._pico_busy, self._loading_data = False, False
        self._render_generation = 0
        self._selection_key = None
        self._selecting = False
        self.port_var = owner.port_var
        self.grid_columnconfigure(0, minsize=240)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main_view()
        self.main_frame.grid_configure(padx=(12, 16), pady=16)
        self.main_frame.grid_rowconfigure(2, minsize=270)
        self.header_subtitle.configure(text='导入本地记录，或从当前接收器读取完整历史')
        self.latest_value.configure(font=ctk.CTkFont(size=18, weight='bold'))
        # Keep time and coordinates visible even at the minimum window size.
        self.gps_badge.master.grid_configure(pady=(8, 0))
        self.train_no_value.configure(height=32, font=ctk.CTkFont(size=30, weight='bold'))
        self.train_no_value.grid_configure(pady=(2, 4))
        self.speed_value.master.configure(height=58)
        for label in self.speed_value.master.winfo_children():
            if isinstance(label, ctk.CTkLabel):
                label.configure(height=22)
                label.grid_configure(pady=(6, 0) if int(label.grid_info()['row']) == 0 else (0, 6))
        for label in self.detail_panel.winfo_children():
            if isinstance(label, ctk.CTkLabel) and int(label.grid_info()['row']) >= 3:
                label.configure(height=22)
                if int(label.grid_info()['row']) in (3, 5):
                    label.grid_configure(pady=(6, 0))

    def after(self, ms, func=None, *args):
        if func is None:
            return super().after(ms)
        return self.owner.after(ms, func, *args)

    def _build_sidebar(self):
        self.sidebar_frame = ctk.CTkFrame(self, width=240, fg_color=COLORS['sidebar'], corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky='nsew')
        self.sidebar_frame.grid_propagate(False)
        self.sidebar_frame.grid_columnconfigure(0, weight=1)
        self.sidebar_frame.grid_rowconfigure(0, weight=1)
        controls = ctk.CTkScrollableFrame(self.sidebar_frame, fg_color='transparent', corner_radius=0)
        controls.grid(row=0, column=0, sticky='nsew', padx=12, pady=16)
        controls.grid_columnconfigure(0, weight=1)
        self._sidebar_section(controls, '筛选记录').pack(fill='x', pady=(0, 8))
        self.train_no_entry = self._sidebar_entry(controls, '车次，例如 0D139')
        self.loco_entry = self._sidebar_entry(controls, '车型，例如 FXD1BA')
        self.time_start_entry = self._sidebar_entry(controls, '开始时间 14:00')
        self.time_end_entry = self._sidebar_entry(controls, '结束时间 15:30')
        self.owner.make_button(controls, '应用筛选', self.apply_filter).pack(fill='x', pady=(4, 8))
        self.owner.make_button(controls, '显示全部', self.reset_filter, secondary=True).pack(fill='x')
        self._sidebar_section(controls, '地图').pack(fill='x', pady=(24, 8))
        self.map_source = ctk.CTkOptionMenu(controls, values=['高德地图', 'OpenStreetMap', 'CartoDB'],
                                          command=self.change_map_source, height=36)
        self.map_source.pack(fill='x')
        self._sidebar_section(controls, '当前接收器').pack(fill='x', pady=(24, 8))
        ctk.CTkLabel(controls, text='使用窗口顶部选择的同一台设备\n读取时会短暂停收，结束后恢复',
                     justify='left', anchor='w', wraplength=196, text_color=COLORS['muted']).pack(fill='x')
        self.read_pico_btn = self.owner.make_button(controls, '读取设备记录', self.start_pico_read)
        self.read_pico_btn.pack(fill='x', pady=(10, 8))
        self.export_pico_btn = self.owner.make_button(controls, '导出到电脑', self.start_pico_export, secondary=True)
        self.export_pico_btn.pack(fill='x')
        self.log_drop_zone = ctk.CTkFrame(self.sidebar_frame, fg_color=COLORS['surface_alt'],
                                         border_width=1, border_color=COLORS['border'])
        self.log_drop_zone.grid(row=1, column=0, sticky='ew', padx=12, pady=(0, 4))
        self.log_drop_label = ctk.CTkLabel(self.log_drop_zone, text='↓ 拖入日志文件\nJSON / JSONL / TXT / LOG',
                                          text_color=COLORS['muted'], justify='center', height=62)
        self.log_drop_label.pack(fill='x', padx=8, pady=4)
        self.source_status = ctk.CTkLabel(self.sidebar_frame, text='尚未加载记录', anchor='w',
                                        wraplength=210, text_color=COLORS['muted'])
        self.source_status.grid(row=2, column=0, sticky='ew', padx=14, pady=12)

    def _sidebar_section(self, parent, text):
        return ctk.CTkLabel(parent, text=text, anchor='w', text_color=COLORS['muted'],
                            font=ctk.CTkFont(size=12, weight='bold'))

    def sync_controls(self):
        available = self.port_var.get() in self.owner.pico_candidate_ports
        enabled = (available and not self.owner.is_working and not self.owner._zip_loading
                   and not self._loading_data)
        for button in (self.read_pico_btn, self.export_pico_btn):
            button.configure(state='normal' if enabled else 'disabled')
        self.load_btn.configure(state='disabled' if self._loading_data or self._pico_busy
                                or self.owner.is_working or self.owner._zip_loading else 'normal')

    def refresh_ports(self, show_prompt=False):
        self.owner.refresh_ports(silent=True)

    def _validated_pico_port(self):
        return self.port_var.get() if self.owner._confirm_selected_port('读取历史') else None

    def start_pico_read(self):
        self._begin_transfer()

    def start_pico_export(self):
        if self.owner.is_working or self._loading_data:
            return
        port = self._validated_pico_port()
        if port is None:
            return
        path = filedialog.asksaveasfilename(parent=self.owner, defaultextension='.jsonl',
                                          initialfile='history.jsonl', title='导出完整历史',
                                          filetypes=[('JSON Lines', '*.jsonl'), ('所有文件', '*.*')])
        if path:
            self._begin_transfer(path, port)

    def _begin_transfer(self, path=None, expected_port=None):
        if self.owner.is_working or self._loading_data:
            return
        port = self._validated_pico_port()
        if port is None or expected_port is not None and port != expected_port:
            return
        if not messagebox.askyesno('读取设备历史',
                '将暂停当前接收程序，少量未落盘记录可能丢失。\n不删除已保存历史；完成后重启恢复接收。\n\n继续吗？',
                parent=self.owner, default='no'):
            return
        lease = self.owner.begin_task('导出历史' if path else '读取历史', port)
        self._pico_busy = True
        self.sync_controls()
        try:
            threading.Thread(target=self._transfer_worker, args=(lease, path), daemon=True).start()
        except Exception as exc:
            self._finish_device_transfer(lease)
            messagebox.showerror('无法启动读取', str(exc), parent=self.owner)

    def _transfer_worker(self, lease, path):
        previous = -1
        def progress(done, total):
            nonlocal previous
            percent = done * 100 // total if total else 100
            if percent != previous:
                previous = percent
                self.after(0, self.owner.task_progress, lease, percent / 100,
                           f'{lease.owner} {percent}% · {done / 1048576:.2f}/{total / 1048576:.2f} MB')
        def status(text):
            self.after(0, self.owner.task_progress, lease, None, text)
        try:
            require_pico_port(lease.port)
            result = download_history(lease.port, progress=progress, status=status)
            if result.recovery_warning:
                self.after(0, messagebox.showwarning, '设备恢复提示', result.recovery_warning)
            if path:
                save_history_atomic(path, result.data)
                self.after(0, self.owner.notice, '已导出并通过 SHA-256 校验：' + path)
            else:
                rows, invalid = parse_history_lines(result.data.decode('utf-8-sig').splitlines())
                self.after(0, self._accept_records, rows, invalid, '设备历史（SHA-256 已校验）')
        except Exception as exc:
            self.after(0, messagebox.showerror, '历史传输失败', '未加载或覆盖文件。\n' + str(exc))
        finally:
            self.after(0, self._finish_device_transfer, lease)

    def _finish_device_transfer(self, lease):
        if self.owner.tasks.release(lease):
            self._pico_busy = False
            self.owner.is_working = False
            self.owner.notice('设备历史任务结束；请留意设备恢复提示。')
            self.owner.sync_controls()
            self.owner.refresh_ports(silent=True)

    def load_json_file(self, path=None):
        if (self._loading_data or self._pico_busy or self.owner.is_working
                or self.owner._zip_loading):
            return
        path = path or filedialog.askopenfilename(parent=self.owner, title='导入历史记录',
                                         filetypes=[('历史记录', '*.json *.jsonl *.txt *.log'), ('所有文件', '*.*')])
        if not path:
            return
        self._loading_data = True
        self.sync_controls()
        self.source_status.configure(text='正在解析本地文件…')
        try:
            threading.Thread(target=self._file_worker, args=(path,), daemon=True).start()
        except Exception as exc:
            self._file_error(str(exc))

    def _file_worker(self, path):
        try:
            with open(path, encoding='utf-8-sig') as f:
                rows, invalid = parse_history_lines(f)
            self.after(0, self._accept_records, rows, invalid, os.path.basename(path))
        except Exception as exc:
            self.after(0, self._file_error, str(exc))

    def _file_error(self, error):
        self._loading_data = False
        self.sync_controls()
        self.source_status.configure(text='导入失败；原有记录未改变')
        messagebox.showerror('读取文件失败', error, parent=self.owner)

    def _accept_records(self, rows, invalid, source):
        if not rows:
            self._file_error(f'未发现有效记录；跳过 {invalid} 条无效数据。')
            return
        self.log_data = rows
        self.source_status.configure(text=f'{source} · {len(rows)} 条' + (f' · 跳过 {invalid} 条无效数据' if invalid else ''))
        self.header_subtitle.configure(text='数据来源：' + source)
        self.refresh_treeview(rows)

    def _process_memory_lines(self, lines, source_name='日志文件'):
        rows, invalid = parse_history_lines(lines)
        self._accept_records(rows, invalid, source_name)

    def extract_log_info(self, record):
        return normalise_record(record)

    def parse_coordinate(self, value):
        return decimal_coordinate(value)

    def apply_filter(self):
        if self._loading_data:
            return
        try:
            rows = filter_records(self.log_data, self.train_no_entry.get(), self.loco_entry.get(),
                                  self.time_start_entry.get(), self.time_end_entry.get())
        except ValueError as exc:
            messagebox.showwarning('筛选时间不正确', str(exc), parent=self.owner)
            return
        self.header_subtitle.configure(text=f'筛选结果：{len(rows)} 条记录')
        self.refresh_treeview(rows)

    def refresh_treeview(self, rows):
        self._render_generation += 1
        generation = self._render_generation
        self._loading_data = True
        self.sync_controls()
        self.displayed_data = list(rows)
        old = list(self.tree.get_children())
        self._clear_selection_detail()
        def clear(offset=0):
            if generation != self._render_generation:
                return
            batch = old[offset:offset + 256]
            if batch:
                self.tree.delete(*batch)
                self.after(1, clear, offset + len(batch))
            else:
                insert(0)
        def insert(offset):
            if generation != self._render_generation:
                return
            for index in range(offset, min(offset + 128, len(rows))):
                row = rows[index]
                tags = ('even' if index % 2 == 0 else 'odd',) + (('no_gps',) if row['gps_status'] == '无' else ())
                self.tree.insert('', 'end', iid=row['_index'], tags=tags,
                                 values=(row['time'], row['train_no'], row['speed'] + ' km/h',
                                         row['loco_type'], '已定位' if row['gps_status'] == '有坐标' else '无坐标'))
            offset += 128
            if offset < len(rows):
                self.result_count_label.configure(text=f'加载 {min(offset, len(rows))}/{len(rows)}')
                self.after(1, insert, offset)
            else:
                self.total_value.configure(text=str(len(rows)))
                self.gps_value.configure(text=str(sum(r['gps_status'] == '有坐标' for r in rows)))
                self.latest_value.configure(text=rows[-1]['time'] if rows else '--:--')
                self.result_count_label.configure(text=f'{len(rows)} / {len(self.log_data)} 条')
                self._loading_data = False
                self.sync_controls()
                if rows:
                    last = str(rows[-1]['_index'])
                    self.tree.selection_set(last)
                    self.tree.focus(last)
                    self.tree.see(last)
                    # Populate details immediately; queued virtual events are
                    # deduplicated by on_tree_select's generation/selection key.
                    self.on_tree_select(None)
        clear()

    def _clear_selection_detail(self):
        self._selection_key = None
        self._selecting = True
        try:
            # Marker.delete calls canvas.update(), which can re-enter pending
            # selection events. Detach first and suppress selection while clear.
            marker, self.current_marker = self.current_marker, None
            if marker:
                marker.delete()
            super()._clear_selection_detail()
        finally:
            self._selecting = False

    def on_tree_select(self, event):
        if self._loading_data or self._selecting:
            return
        selected = self.tree.selection()
        if not selected:
            return
        key = (self._render_generation, selected[0])
        if key == self._selection_key:
            return
        self._selection_key = key
        self._selecting = True
        try:
            super().on_tree_select(event)
        finally:
            self._selecting = False
        if self.tree.selection() != selected:
            self.after(0, self.on_tree_select, None)


class LBJManager(PicoUpdaterApp):
    def __init__(self):
        self.tasks = DeviceTasks()
        self.dispatch = UiDispatch()
        self._ui_thread = threading.get_ident()
        self._task_context = threading.local()
        self._updater_lease = None
        self._device_touched = self._reset_done = self._flash_partial = False
        self._reset_attempted = self._reset_notice_posted = False
        self._reset_warning = ''
        self._finishing = self._closing = False
        self._scan_initialized = False
        self._auto_selected_port = None
        self._zip_loading = False
        self._pending_zip_path = None
        self._dnd_available = False
        self.active_page = '设备管理'
        self.history = None
        super().__init__()
        self.title('LBJ Manager · 设备与列车记录')
        self.geometry('1320x900')
        self.minsize(1120, 800)
        self._enable_file_drop()
        self.bind('<Command-o>', lambda event: self.open_history_file())
        self.bind('<Control-o>', lambda event: self.open_history_file())
        self._bind_navigation_shortcuts()
        self.bind('<Return>', lambda event: self.history.apply_filter() if self.active_page == '历史记录' else None)
        self.bind('<Escape>', lambda event: self.history.reset_filter() if self.active_page == '历史记录' else None)
        super().after(15, self._pump_ui)
        super().after(2000, self._scan_loop)

    def _bind_navigation_shortcuts(self):
        modifier = 'Command' if sys.platform == 'darwin' else 'Control'
        # A bare numeric detail such as <Command-1> is a mouse-button
        # binding in Tk, not the digit key. Always name the event type.
        self.bind(f'<{modifier}-KeyPress-1>', lambda event: self.show_page('设备管理'))
        self.bind(f'<{modifier}-KeyPress-2>', lambda event: self.show_page('历史记录'))

    def after(self, ms, func=None, *args):
        if threading.get_ident() == self._ui_thread:
            return super().after(ms, func, *args)
        if func is None:
            raise RuntimeError('后台线程不能阻塞 Tcl/Tk。')
        lease = getattr(self._task_context, 'lease', None)
        if func == self.set_ui_state and args == (False,):
            func, args = self._finish_updater, (lease,)
        elif lease is not None:
            original, original_args = func, args
            def invoke():
                if self.tasks.active is lease:
                    original(*original_args)
            func, args = invoke, ()
        self.dispatch.post(ms, func, args)
        return None

    def _pump_ui(self):
        if self._closing:
            return
        self.dispatch.drain(lambda delay, callback, args: super(LBJManager, self).after(delay, callback, *args))
        super().after(15, self._pump_ui)

    def make_button(self, parent, text, command, secondary=False):
        return ctk.CTkButton(parent, text=text, command=command, height=40, corner_radius=8,
                            fg_color=COLORS['surface_alt'] if secondary else COLORS['teal'],
                            hover_color=COLORS['border'] if secondary else COLORS['teal_hover'],
                            text_color=COLORS['text'])

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color=COLORS['sidebar'], corner_radius=0)
        header.grid(row=0, column=0, sticky='ew')
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text='LBJ MANAGER', font=ctk.CTkFont(size=23, weight='bold'),
                     text_color=COLORS['text']).grid(row=0, column=0, padx=(20, 28), pady=(15, 8))
        self.nav = ctk.CTkSegmentedButton(header, values=['设备管理', '历史记录'], command=self.show_page,
                                         height=36, selected_color=COLORS['teal'],
                                         selected_hover_color=COLORS['teal_hover'])
        self.nav.grid(row=0, column=1, sticky='w', pady=(15, 8))
        self.nav.set('设备管理')
        self.status_badge = ctk.CTkLabel(header, text='就绪', text_color=COLORS['green'], width=80)
        self.status_badge.grid(row=0, column=2, padx=20)
        device = ctk.CTkFrame(header, fg_color='transparent')
        device.grid(row=1, column=0, columnspan=3, sticky='ew', padx=20, pady=(0, 14))
        device.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(device, text='当前设备', text_color=COLORS['muted']).grid(row=0, column=0, padx=(0, 10))
        self.port_var = ctk.StringVar(value=PLACEHOLDER_PORT)
        self.port_menu = ctk.CTkOptionMenu(device, variable=self.port_var, values=[PLACEHOLDER_PORT],
                                          command=self._on_port_selected, width=255, height=36,
                                          fg_color=COLORS['surface_alt'], button_color=COLORS['border'])
        self.port_menu.grid(row=0, column=1)
        self.device_status = ctk.CTkLabel(device, text='仅扫描，不自动暂停接收器', anchor='w',
                                        justify='left', height=40, corner_radius=8, padx=12,
                                        font=ctk.CTkFont(size=13, weight='bold'),
                                        text_color=COLORS['muted'], fg_color=COLORS['surface_alt'])
        self.device_status.grid(row=0, column=2, sticky='ew', padx=16)
        self.refresh_btn = self.make_button(device, '扫描设备', self.scan_devices, True)
        self.refresh_btn.grid(row=0, column=3)
        self.body = ctk.CTkFrame(self, fg_color=COLORS['bg'], corner_radius=0)
        self.body.grid(row=1, column=0, sticky='nsew')
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)
        self.device_page = ctk.CTkFrame(self.body, fg_color=COLORS['bg'], corner_radius=0)
        self.device_page.grid(row=0, column=0, sticky='nsew')
        self.device_page.grid_columnconfigure(1, weight=1)
        self.device_page.grid_rowconfigure(0, weight=1)
        controls = ctk.CTkScrollableFrame(self.device_page, width=238,
                                         fg_color=COLORS['sidebar'], corner_radius=0)
        controls.grid(row=0, column=0, sticky='nsew')
        ctk.CTkLabel(controls, text='固件与硬件', font=ctk.CTkFont(size=20, weight='bold')).pack(fill='x', padx=12, pady=(18, 16))
        self._section_label(controls, '固件分支').pack(fill='x', padx=12)
        self.branch_var = ctk.StringVar(value=DEFAULT_CHANNEL_LABEL)
        self.branch_menu = ctk.CTkOptionMenu(controls, variable=self.branch_var, values=list(FIRMWARE_BRANCHES),
                                            command=self._on_branch_selected, height=38, width=218)
        self.branch_menu.pack(fill='x', padx=12, pady=8)
        self.branch_hint = ctk.CTkLabel(controls, text=self.active_profile['hardware_hint'],
                                      wraplength=212, justify='left', text_color=COLORS['amber'])
        self.branch_hint.pack(fill='x', padx=12, pady=(0, 16))
        self.action_btn = self.make_button(controls, '检查并更新固件', self.start_update_process)
        self.action_btn.pack(fill='x', padx=12, pady=(0, 10))
        self.zip_drop_zone = ctk.CTkFrame(controls, fg_color=COLORS['surface_alt'],
                                         border_width=1, border_color=COLORS['border'])
        self.zip_drop_zone.pack(fill='x', padx=12, pady=(0, 8))
        self.zip_drop_label = ctk.CTkLabel(self.zip_drop_zone, text='↓ 拖入固件 ZIP\n载入后点击下方刷入',
                                          wraplength=200, justify='center', text_color=COLORS['muted'], height=68)
        self.zip_drop_label.pack(fill='x', padx=8, pady=4)
        self.offline_zip_btn = self.make_button(controls, '从 ZIP 刷入', self.start_offline_zip_update, True)
        self.offline_zip_btn.pack(fill='x', padx=12, pady=(0, 22))
        self._section_label(controls, '装配检查').pack(fill='x', padx=12)
        self.test_btn = self.make_button(controls, '运行硬件检查单', self.start_hardware_test)
        self.test_btn.pack(fill='x', padx=12, pady=10)
        ctk.CTkLabel(controls, text='核心硬件 · 五键 · 屏幕\n蜂鸣器 · SD 卡 · 检查报告', justify='left',
                     text_color=COLORS['muted']).pack(fill='x', padx=12, pady=(0, 24))
        self._section_label(controls, '高级操作').pack(fill='x', padx=12)
        self.force_action_btn = self.make_button(controls, '强制重新刷入', lambda: self.start_update_process(True), True)
        self.force_action_btn.pack(fill='x', padx=12, pady=10)
        ctk.CTkLabel(controls, text='刷入前会检查硬件兼容性。\n刷入会清除历史，请先到\n“历史记录”导出保存。', justify='left',
                     wraplength=212, text_color=COLORS['amber']).pack(fill='x', padx=12, pady=10)
        self.main_frame = ctk.CTkFrame(self.device_page, fg_color='transparent')
        self.main_frame.grid(row=0, column=1, sticky='nsew', padx=20, pady=18)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(self.main_frame, text='设备管理', font=ctk.CTkFont(size=27, weight='bold'),
                     anchor='w').grid(row=0, column=0, sticky='ew', pady=(0, 18))
        self.inspection_summary = ctk.CTkFrame(self.main_frame, fg_color='transparent')
        self.inspection_summary.grid(row=1, column=0, sticky='ew', pady=(0, 14))
        for column in range(3):
            self.inspection_summary.grid_columnconfigure(column, weight=1)
        self.local_ver_label = self._metric(self.inspection_summary, 0, '机内固件', '未知', COLORS['teal'])
        self.remote_ver_label = self._metric(self.inspection_summary, 1, '目标固件', '未知', COLORS['blue'])
        self.connection_value = self._metric(self.inspection_summary, 2, '连接', '未选择设备', COLORS['amber'])
        self.inspection_progress = ctk.CTkFrame(self.main_frame, fg_color=COLORS['surface'], corner_radius=10)
        self.inspection_progress.grid(row=2, column=0, sticky='ew', pady=(0, 14))
        self.inspection_progress.grid_columnconfigure(0, weight=1)
        self.progress_label = ctk.CTkLabel(self.inspection_progress, text='尚未开始任务', anchor='w')
        self.progress_label.grid(row=0, column=0, sticky='ew', padx=16, pady=10)
        self.progress_percent = ctk.CTkLabel(self.inspection_progress, text='0%')
        self.progress_percent.grid(row=0, column=1, padx=16)
        self.progress_bar = ctk.CTkProgressBar(self.inspection_progress, progress_color=COLORS['teal'])
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky='ew', padx=16, pady=(0, 16))
        self.progress_bar.set(0)
        self.console_panel = ctk.CTkFrame(self.main_frame, fg_color=COLORS['surface'], corner_radius=10)
        self.console_panel.grid(row=3, column=0, sticky='nsew')
        self.console_panel.grid_columnconfigure(0, weight=1)
        self.console_panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self.console_panel, text='运行日志 / 硬件检查单', anchor='w').grid(row=0, column=0, sticky='w', padx=16, pady=10)
        self.clear_log_btn = self.make_button(self.console_panel, '清空日志', self.clear_log, True)
        self.clear_log_btn.grid(row=0, column=1, padx=12, pady=8)
        self.log_textbox = ctk.CTkTextbox(self.console_panel, state='disabled', wrap='word',
                                        fg_color=COLORS['surface_alt'], font=ctk.CTkFont(family='Menlo', size=12))
        self.log_textbox.grid(row=1, column=0, columnspan=2, sticky='nsew', padx=1, pady=(0, 1))
        self.inspection_window = None
        self.history = HistoryPanel(self.body, self)
        self.history.grid(row=0, column=0, sticky='nsew')
        self.history.grid_remove()
        footer = ctk.CTkFrame(self, fg_color=COLORS['sidebar'], corner_radius=0)
        footer.grid(row=2, column=0, sticky='ew')
        footer.grid_columnconfigure(0, weight=1)
        self.footer_status = ctk.CTkLabel(footer, text='连接数据 USB 后扫描；不会自动刷入或暂停接收。',
                                        anchor='w', text_color=COLORS['muted'])
        self.footer_status.grid(row=0, column=0, sticky='ew', padx=20, pady=8)
        ctk.CTkLabel(footer, text=MANAGER_VERSION, text_color=COLORS['muted']).grid(row=0, column=1, padx=20)

    def show_page(self, page):
        if page not in ('设备管理', '历史记录'):
            return False
        panel = self.inspection_window
        if page != '设备管理' and panel is not None and not panel.finished:
            self.nav.set('设备管理')
            self.notice('硬件检查需要操作确认，请先完成或结束检查。')
            return False
        self.active_page = page
        self.nav.set(page)
        if page == '设备管理':
            self.history.grid_remove()
            self.device_page.grid()
        else:
            self.device_page.grid_remove()
            self.history.grid()
        return True

    def open_history_file(self):
        if self.show_page('历史记录'):
            self.history.load_json_file()

    def notice(self, text):
        self.footer_status.configure(text=text)

    def _enable_file_drop(self):
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
            self._dnd_version = TkinterDnD.require(self)
            # CTk composites include canvases/labels which can obscure their
            # parent target. Register every descendant, not just the frame.
            for kind, surfaces in (
                ('zip', (self.zip_drop_zone, self.offline_zip_btn)),
                ('log', (self.history.log_drop_zone, self.history.load_btn)),
            ):
                for surface in surfaces:
                    self._register_drop_surface(surface, kind, DND_FILES)
            self._dnd_available = True
        except (ImportError, RuntimeError, tk.TclError) as exc:
            self._dnd_available = False
            self.zip_drop_label.configure(text='文件拖放不可用\n请点击下方选择 ZIP')
            self.history.log_drop_label.configure(text='文件拖放不可用\n请点击“导入日志”')
            self.log('文件拖放未启用（按钮仍可使用）：' + str(exc))

    def _register_drop_surface(self, widget, kind, file_type):
        widget.drop_target_register(file_type)
        widget.dnd_bind('<<DropEnter>>', lambda event: self._drop_hover(kind, True))
        widget.dnd_bind('<<DropLeave>>', lambda event: self._drop_hover(kind, False))
        widget.dnd_bind('<<Drop>>', lambda event: self._on_file_drop(kind, event))
        for child in widget.winfo_children():
            self._register_drop_surface(child, kind, file_type)

    def _drop_busy(self):
        return (self.is_working or self._zip_loading or self._closing or
                self.history._loading_data or self.history._pico_busy)

    def _drop_hover(self, kind, entered):
        zone = self.zip_drop_zone if kind == 'zip' else self.history.log_drop_zone
        allowed = not self._drop_busy()
        zone.configure(border_color=COLORS['teal'] if entered and allowed else COLORS['border'])
        return 'copy' if allowed else 'refuse_drop'

    def _on_file_drop(self, kind, event):
        self._drop_hover(kind, False)
        if self._drop_busy():
            self.notice('当前任务尚未结束，请完成后再拖入文件。')
            return 'refuse_drop'
        try:
            path = dropped_file(self.tk, event.data,
                                {'.zip'} if kind == 'zip' else {'.json', '.jsonl', '.txt', '.log'})
        except ValueError as exc:
            self.notice(str(exc))
            return 'refuse_drop'
        # Return to the native drag loop before parsing or showing a dialog.
        self.after(0, self._accept_dropped_file, kind, path)
        return 'copy'

    def _accept_dropped_file(self, kind, path):
        if self._drop_busy():
            self.notice('当前任务尚未结束，未导入拖入的文件。')
            return
        if kind == 'log':
            if self.show_page('历史记录'):
                self.history.load_json_file(path)
        else:
            self._stage_zip(path)

    def _stage_zip(self, path):
        if self._drop_busy():
            self.notice('当前任务尚未结束，未载入 ZIP。')
            return
        self._zip_loading = True
        self._pending_zip_path = None
        self.zip_drop_label.configure(text='正在校验 ZIP…', text_color=COLORS['amber'])
        self.notice('仅在电脑上校验 ZIP；接收器继续运行。')
        self.sync_controls()
        profile = self._selected_profile()
        try:
            threading.Thread(target=self._stage_zip_worker, args=(path, profile), daemon=True).start()
        except Exception as exc:
            self._finish_zip_stage(path, None, str(exc))

    def _stage_zip_worker(self, path, profile):
        try:
            with tempfile.TemporaryDirectory(prefix='lbj_drop_zip_') as directory:
                _, info = self._extract_zip_firmware(path, directory, profile)
            self.after(0, self._finish_zip_stage, path, info, '')
        except Exception as exc:
            self.after(0, self._finish_zip_stage, path, None, str(exc))

    def _finish_zip_stage(self, path, info, error):
        self._zip_loading = False
        if error:
            self._pending_zip_path = None
            self.zip_drop_label.configure(text='ZIP 未通过校验\n可重新拖入', text_color=COLORS['amber'])
            self.notice('ZIP 载入失败：' + error)
            self.log('ZIP 载入失败（未连接或改写设备）：' + error)
        else:
            self._pending_zip_path = path
            self.zip_drop_label.configure(text=os.path.basename(path) + '\n' + version_display(info),
                                           text_color=COLORS['green'])
            self.notice('ZIP 已载入并校验；点击“刷入已载入 ZIP”后仍需确认，刷入会清除历史。')
        self.offline_zip_btn.configure(text='刷入已载入 ZIP' if self._pending_zip_path else '从 ZIP 刷入')
        self.sync_controls()

    def _on_branch_selected(self, selection):
        if self._zip_loading:
            self.branch_var.set(self.active_profile['label'])
            return
        super()._on_branch_selected(selection)
        self._pending_zip_path = None
        self.zip_drop_label.configure(text='↓ 拖入固件 ZIP\n载入后点击下方刷入', text_color=COLORS['muted'])
        self.offline_zip_btn.configure(text='从 ZIP 刷入')

    def _scan_loop(self):
        if self._closing:
            return
        if not self.is_working:
            self.refresh_ports(silent=True)
        super().after(2000, self._scan_loop)

    def scan_devices(self):
        return self.refresh_ports(silent=False, select_single=True)

    def refresh_ports(self, silent=True, select_single=False):
        if self.is_working:
            if not silent:
                self.notice('设备任务尚未结束，请等待完成后再扫描。')
            return False
        try:
            ports = list(serial.tools.list_ports.comports())
        except Exception as exc:
            self.pico_candidate_ports = set()
            self._auto_selected_port = None
            self.connection_value.configure(text='扫描失败')
            self.device_status.configure(text='扫描失败，请重试', text_color=COLORS['red'],
                                         fg_color=COLORS['surface_alt'])
            self.sync_controls()
            if not silent:
                self.notice('扫描失败：' + str(exc))
            return False
        old = self.port_var.get()
        initial = not self._scan_initialized
        candidates, selected = choose_port(ports, old, initial=initial,
                                           explicit=select_single)
        self._scan_initialized = True
        self.pico_candidate_ports = set(candidates)
        self.port_menu.configure(values=[PLACEHOLDER_PORT] + candidates)
        self.port_var.set(selected)
        if old != selected:
            self._reset_selected_device_state()
        if selected not in candidates:
            self._auto_selected_port = None
        elif select_single and len(candidates) == 1 or initial and old != selected:
            self._auto_selected_port = selected
        automatic = selected in candidates and getattr(self, '_auto_selected_port', None) == selected
        chosen_message = '已自动选中 Pico' if automatic else '已选中 Pico'
        self.connection_value.configure(text=chosen_message if selected in candidates else '未选择 Pico')
        status = ('✓ ' + chosen_message + '\n' + selected if selected in candidates else
                  f'发现 {len(candidates)} 台 Pico，请在左侧选择' if len(candidates) > 1 else
                  '发现 1 台 Pico，点击扫描即可选择' if candidates else
                  '未发现 Pico，请检查数据线 / 连接')
        self.device_status.configure(text=status,
                                      text_color=COLORS['green'] if selected in candidates else COLORS['muted'],
                                      fg_color='#17362B' if selected in candidates else COLORS['surface_alt'])
        self.sync_controls()
        if not silent or initial and automatic:
            self.notice('扫描完成：' + (chosen_message + '：' + selected if selected in candidates else status) + '。')
        return True

    def _on_port_selected(self, port):
        if self.is_working:
            self.port_var.set(self.tasks.active.port)
            return
        self._auto_selected_port = None
        self._reset_selected_device_state()
        self.refresh_ports(silent=True)

    def _confirm_selected_port(self, action):
        if self.is_working:
            self.notice('设备正在执行其他任务，请等待完成。')
            return False
        port = self.port_var.get()
        try:
            require_pico_port(port)
        except Exception:
            self.refresh_ports(silent=True)
            messagebox.showwarning('请连接并选择 Pico', '未识别到所选 Pico，不会开始' + action + '。', parent=self)
            return False
        return True

    def _sync_port_actions(self):
        # Called by the original init before all integration widgets exist.
        selected = self.port_var.get()
        enabled = (not self.is_working and not self._zip_loading and
                   selected in getattr(self, 'pico_candidate_ports', set()))
        for button in (self.action_btn, self.force_action_btn, self.offline_zip_btn, self.test_btn):
            button.configure(state='normal' if enabled else 'disabled')

    def sync_controls(self):
        self.is_working = self.tasks.active is not None
        self._sync_port_actions()
        state = 'disabled' if self.is_working or self._zip_loading else 'normal'
        for widget in (self.port_menu, self.refresh_btn, self.branch_menu):
            widget.configure(state=state)
        self.clear_log_btn.configure(state=state)
        busy = self.is_working or self._zip_loading
        self.status_badge.configure(text='校验 ZIP' if self._zip_loading else ('运行中' if busy else '就绪'),
                                    text_color=COLORS['amber'] if busy else COLORS['green'])
        if self.history:
            self.history.sync_controls()

    def begin_task(self, owner, port):
        if self._zip_loading:
            raise TaskBusyError('ZIP 正在校验，请等待完成')
        lease = self.tasks.acquire(owner, port)
        self.is_working = True
        self.sync_controls()
        self.notice(owner + ' · ' + port + ' · 请勿拔线')
        return lease

    def task_progress(self, lease, value, text):
        if self.tasks.active is not lease:
            return
        self.notice(text)
        if value is not None:
            self.set_progress(value, text)

    def set_ui_state(self, working):
        if working:
            if self.tasks.active and self.tasks.active is not self._updater_lease:
                raise TaskBusyError('设备被历史任务占用')
            if self._updater_lease is None:
                self._updater_lease = self.begin_task('更新 / 硬件检查', self.port_var.get())
                self._device_touched = self._reset_done = self._flash_partial = False
                self._reset_attempted = self._reset_notice_posted = False
                self._reset_warning = ''
                self._finishing = False
            panel = self.inspection_window
            if panel is not None and panel.finished:
                self.dismiss_inspection()
            self.sync_controls()
        else:
            self._finish_updater(self._updater_lease)

    def _finish_updater(self, lease):
        if lease is None or self.tasks.active is not lease or self._finishing:
            return
        if (self._device_touched and not self._reset_done and not self._flash_partial
                and not self.__dict__.get('_reset_attempted', False)):
            self._finishing = True
            self.notice('正在恢复接收程序，请勿拔线…')
            def restore():
                self._task_context.lease = lease
                try:
                    success, output = self.run_mpremote(lease.port,
                        ['resume', 'exec', 'import machine; machine.reset()'], 10)
                    warning = '' if success else '恢复状态待确认：' + output
                except Exception as exc:
                    warning = '恢复失败，请手动复位设备：' + str(exc)
                self.dispatch.post(0, self._release_updater, (lease, warning))
            try:
                threading.Thread(target=restore, daemon=True).start()
            except Exception as exc:
                self._release_updater(lease, '无法启动恢复任务，请手动复位：' + str(exc))
            return
        self._release_updater(lease, '刷入未完成，请重新刷入；不要将设备视为已恢复接收。'
                              if self._flash_partial else self.__dict__.get('_reset_warning', ''))

    def _release_updater(self, lease, warning=''):
        if not self.tasks.release(lease):
            return
        self._updater_lease = None
        self._finishing = False
        self.sync_controls()
        self.notice(warning or '设备任务结束。')
        self.refresh_ports(silent=True)
        if warning and not self.__dict__.get('_reset_notice_posted', False):
            messagebox.showwarning('设备状态提示', warning, parent=self)

    def run_mpremote(self, port, args_list, timeout_sec=60, live_stream=False):
        lease = getattr(self._task_context, 'lease', None) or self._updater_lease
        if self.tasks.active is not lease or lease is None or lease.port != port:
            return False, '串口任务已失效，未执行设备命令。'
        self._device_touched = True
        arguments = list(args_list)
        reset = 'exec' in arguments and any('machine.reset()' in arg for arg in arguments)
        if reset:
            self._reset_attempted = True
            try:
                result = reboot_receiver(port, timeout=timeout_sec)
            except Exception as exc:
                self._reset_warning = '重启状态检查异常：' + str(exc)
                return False, self._reset_warning
            self._reset_done = result.reconnected
            self._reset_warning = '' if result.reconnected else result.message
            return result.reconnected, result.message
        if getattr(sys, 'frozen', False):
            command = [sys.executable, 'mpremote_internal', 'connect', port] + arguments
        else:
            command = [sys.executable, '-m', 'mpremote', 'connect', port] + arguments
        try:
            success, output = run_command(command, timeout_sec, self.log if live_stream else None)
        except Exception as exc:
            return False, str(exc)
        return success, output

    def _wipe_device_files(self, port, profile):
        if not self._check_hardware_compatibility(port, profile, phase='清空前最终复核'):
            return False
        # Mark only after compatibility succeeds, but before an interrupted wipe.
        self._flash_partial = True
        self.log('正在清空 Pico 中的旧文件…')
        script = "import os; [os.remove(f) for f in os.listdir() if not (os.stat(f)[0] & 0x4000)]"
        success, output = self.run_mpremote(port, ['exec', script], timeout_sec=20)
        if not success:
            self.log('[失败] 清空未完成：' + output)
        return success

    def _copy_firmware_files(self, *args, **kwargs):
        complete = super()._copy_firmware_files(*args, **kwargs)
        if complete:
            self._flash_partial = False
        return complete

    def _extract_zip_firmware(self, zip_path, dest_dir, profile):
        files, info = super()._extract_zip_firmware(zip_path, dest_dir, profile)
        validate_firmware_bundle(profile, files)
        self.log('ZIP 运行文件与依赖检查通过（含 DMA / 保护模块）。')
        return files, info

    def _confirm_online_update(self, port, force, temp_dir, firmware_files,
                               remote_info, local_info, profile):
        try:
            validate_firmware_bundle(profile, firmware_files)
        except ValueError as exc:
            self.log('[失败] 固件完整性检查：' + str(exc))
            self._cleanup_temp_dir(temp_dir)
            self.set_ui_state(False)
            messagebox.showerror('固件包不完整', str(exc), parent=self)
            return
        self.log('在线固件运行文件与依赖检查通过（含 DMA / 保护模块）。')
        return super()._confirm_online_update(port, force, temp_dir, firmware_files,
                                              remote_info, local_info, profile)

    def start_update_process(self, force=False):
        if self.__dict__.get('_zip_loading', False):
            return
        try:
            super().start_update_process(force)
        except Exception as exc:
            self.set_ui_state(False)
            messagebox.showerror('无法启动更新', str(exc), parent=self)

    def start_offline_zip_update(self):
        if self.__dict__.get('_zip_loading', False):
            return
        try:
            super().start_offline_zip_update(self.__dict__.get('_pending_zip_path'))
        except Exception as exc:
            self.set_ui_state(False)
            messagebox.showerror('无法启动离线刷入', str(exc), parent=self)

    def start_hardware_test(self):
        if self._zip_loading:
            return
        self.show_page('设备管理')
        super().start_hardware_test()

    def _on_close(self):
        if self.tasks.active:
            messagebox.showwarning('设备任务尚未结束', '请先等待刷入 / 读取完成，或在硬件检查单中结束检查。\n软件会先释放串口并恢复设备。', parent=self)
            return
        if self.history._loading_data or self._zip_loading:
            messagebox.showwarning('文件正在加载', '请等待文件加载完成后关闭。', parent=self)
            return
        self._closing = True
        self.dispatch.close()
        self.destroy()


def _bind_engine_worker(name):
    original = getattr(PicoUpdaterApp, name)
    def worker(self, *args, **kwargs):
        lease = self._updater_lease
        if lease is None or self.tasks.active is not lease:
            return
        self._task_context.lease = lease
        try:
            return original(self, *args, **kwargs)
        finally:
            self._task_context.lease = None
    worker.__name__ = name
    return worker


for _worker_name in ('_update_worker', '_offline_zip_prepare_worker',
                     '_online_flash_worker', '_offline_zip_flash_worker', '_test_worker'):
    setattr(LBJManager, _worker_name, _bind_engine_worker(_worker_name))

if __name__ == "__main__":
    app = LBJManager()
    app.mainloop()
