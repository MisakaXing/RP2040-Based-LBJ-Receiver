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

if len(sys.argv) > 1 and sys.argv[1] == "mpremote_internal":
    # 伪造标准的 mpremote 命令行参数
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from mpremote.main import main
    try:
        main() # 直接调用 mpremote 的核心引擎
    except SystemExit as e:
        sys.exit(e.code)
    sys.exit(0) # 执行完毕立刻退出

# 配置 CustomTkinter 主题
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


def select_runtime_files(profile, files_data):
    by_name = {
        item.get("name"): item
        for item in files_data
        if isinstance(item, dict) and item.get("type") == "file"
    }
    required = tuple(profile["runtime_files"])
    missing = [name for name in required if name not in by_name]
    return [by_name[name] for name in required if name in by_name], missing


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

# ================= 嵌入的硬件自检脚本 =================
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
        allowed_names = set(profile["runtime_files"])

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
            extracted_by_name[name] for name in profile["runtime_files"]
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
    def start_offline_zip_update(self):
        if self.is_working:
            return
        if not self._confirm_selected_port("离线刷入"):
            return

        port = self.port_var.get()
        if not port or port == "未检测到设备" or port == "请选择端口...":
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return

        zip_path = filedialog.askopenfilename(
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
            self.run_mpremote(port, ["exec", "import machine; machine.reset()"], timeout_sec=10)

            self.after(0, self.set_progress, 1.0, "离线刷入完成")
            self.log("\n[完成] 离线 ZIP 刷入完成，Pico 已重启。")
            self.after(0, lambda: messagebox.showinfo("离线刷入完成", "离线 ZIP 固件刷入完成！"))

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
            self.run_mpremote(
                port,
                ["exec", "import machine; machine.reset()"],
                timeout_sec=10,
            )

            self.after(0, self.set_progress, 1.0, "更新完成")
            msg_title = "强制刷入完成" if force else "初次/更新安装完成"
            self.log(f"\n[完成] {msg_title}，Pico 已加载所选分支程序。")
            self.after(
                0,
                lambda mt=msg_title: messagebox.showinfo(
                    mt, f"{mt}！操作已成功完成！"
                ),
            )
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

if __name__ == "__main__":
    app = PicoUpdaterApp()
    app.mainloop()
