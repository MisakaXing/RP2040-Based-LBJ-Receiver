import sys
import os
import re
import threading
import tempfile
import subprocess
import requests
import customtkinter as ctk
import serial.tools.list_ports
from tkinter import messagebox

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
        self.geometry("1080x740")
        self.minsize(920, 640)
        self.configure(fg_color=COLORS["bg"])

        # 仓库配置
        self.github_repo = "MisakaXing/RP2040-Based-LBJ-Receiver"
        self.target_dir = "rp2040-main-program"
        self.main_py_url = f"https://raw.githubusercontent.com/{self.github_repo}/main/{self.target_dir}/main.py"
        self.api_url = f"https://api.github.com/repos/{self.github_repo}/contents/{self.target_dir}"

        # 状态变量
        self.local_version = 0.0
        self.remote_version = 0.0
        self.is_working = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.setup_ui()
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
        self.device_status.grid(row=2, column=0, sticky="ew", pady=(8, 24))

        self._section_label(controls, "常用操作").grid(
            row=3, column=0, sticky="ew", pady=(0, 7)
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
        self.action_btn.grid(row=4, column=0, sticky="ew")

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
        self.test_btn.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        separator = ctk.CTkFrame(controls, height=1, fg_color=COLORS["border"])
        separator.grid(row=6, column=0, sticky="ew", pady=24)

        self._section_label(controls, "维护").grid(
            row=7, column=0, sticky="ew", pady=(0, 7)
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
        self.force_action_btn.grid(row=8, column=0, sticky="ew")

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

    def _on_port_selected(self, port):
        if port in ("未检测到设备", "请选择端口..."):
            self.connection_value.configure(text="未连接")
            self.device_status.configure(
                text="未检测到可用设备", text_color=COLORS["muted"]
            )
            return
        self.connection_value.configure(text="已选择")
        self.device_status.configure(
            text=port, text_color=COLORS["teal"]
        )

    def log(self, text):
        self.after(0, self._append_log, text)

    def _append_log(self, text):
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", text + "\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def set_ui_state(self, working):
        self.is_working = working
        state = "disabled" if working else "normal"
        self.action_btn.configure(
            state=state, text="正在处理" if working else "检查并更新"
        )
        self.test_btn.configure(
            state=state, text="正在处理" if working else "运行硬件自检"
        )
        self.force_action_btn.configure(
            state=state, text="正在处理" if working else "强制重刷固件"
        )
        self.refresh_btn.configure(state=state)
        self.port_menu.configure(state=state)
        self.clear_log_btn.configure(state=state)
        self.status_badge.configure(
            text="运行中" if working else "就绪",
            text_color=COLORS["amber"] if working else COLORS["green"],
        )

    def refresh_ports(self):
        PICO_VID = 0x2E8A
        ports = serial.tools.list_ports.comports()
        port_list = [port.device for port in ports]
        
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
                self.port_var.set(port_list[0])
                self.connection_value.configure(text="串口已选择")
                self.device_status.configure(
                    text=port_list[0], text_color=COLORS["amber"]
                )
                self.log("已刷新串口列表，但未发现标准 Pico 设备，请手动确认。")

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
                return True, "\n".join(full_output)
            else:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                        encoding='utf-8', errors='replace', timeout=timeout_sec, startupinfo=startupinfo)
                return True, result.stdout
        except subprocess.TimeoutExpired:
            return False, "命令执行超时 (可能 Pico 处于死循环，或文件传输时间过长)"
        except Exception as e:
            return False, str(e)

    def extract_version(self, text):
        ver = 0.0
        ver_match = re.search(r"Program_ver\s*=\s*([\d\.]+)", text)
        if ver_match:
            try: ver = float(ver_match.group(1))
            except ValueError: pass
        return ver
    
    # ================= 硬件自检逻辑 =================
    def start_hardware_test(self):
        port = self.port_var.get()
        if not port or port == "未检测到设备" or port == "请选择端口...":
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return
            
        if self.is_working: return
        self.set_ui_state(True)
        self.clear_log()
        self.set_progress(0, "硬件自检")
        
        threading.Thread(target=self._test_worker, args=(port,), daemon=True).start()

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
    def start_update_process(self, force=False):
        port = self.port_var.get()
        if not port or port == "未检测到设备" or port == "请选择端口...":
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return
            
        if force:
            confirm = messagebox.askyesno(
                "强制刷入警告",
                "您选择了强制刷入！\n\n这将无视版本是否最新，强行格式化 Pico 并重新拉取所有文件！\n\n保存在本机的所有【历史车次数据】将会永久消失！\n\n您确定要继续吗？",
                icon="warning"
            )
        else:
            confirm = messagebox.askyesno(
                "更新警告",
                "执行同步更新将会彻底清空 Pico 中的旧文件！\n\n保存在本机的所有【历史车次数据】将会永久消失！\n\n您确定要继续执行更新吗？",
                icon="warning"
            )
            
        if not confirm:
            self.log("用户已取消操作。")
            return

        if self.is_working: return
        self.set_ui_state(True)
        self.clear_log()
        self.set_progress(0, "固件更新")
        
        threading.Thread(target=self._update_worker, args=(port, force), daemon=True).start()

    def _update_worker(self, port, force):
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

            self.log("正在连接 GitHub 获取远程版本...")
            self.after(0, self.set_progress, 0.1, "获取远程版本")
            resp = requests.get(self.main_py_url, timeout=15)
            if resp.status_code == 200:
                self.remote_version = self.extract_version(resp.text)
                self.after(0, lambda rv=self.remote_version: self.remote_ver_label.configure(text=f"v{rv:g}"))
                self.log(f"成功获取远程版本: {self.remote_version}")
            else:
                self.log("获取远程文件失败，请检查网络！")
                return

            self.log("正在探测 Pico 文件系统...")
            self.after(0, self.set_progress, 0.2, "读取设备固件")
            
            success, ls_output = self.run_mpremote(port, ["exec", "import os; print('main.py' in os.listdir())"], timeout_sec=10)
            self.local_version = 0.0 
            
            if success and "True" in ls_output:
                self.log("正在读取本地版本...")
                success_cat, output = self.run_mpremote(port, ["cat", "main.py"], timeout_sec=15)
                if success_cat and "Program_ver" in output:
                    self.local_version = self.extract_version(output)
            else:
                self.log("未检测到 main.py，识别为全新开发板，将执行初次完整安装。")

            local_version_text = "未安装" if self.local_version == 0.0 else f"v{self.local_version:g}"
            self.after(0, lambda text=local_version_text: self.local_ver_label.configure(text=text))
            self.after(0, self.set_progress, 0.3, "准备更新文件")

            if not force:
                if self.local_version >= self.remote_version and self.local_version != 0.0:
                    self.log("\n[完成] 当前已是最新版本，无需更新。")
                    self.after(0, self.set_progress, 1.0, "已是最新版本")
                    return
                self.log("\n准备开始执行同步操作...")
            else:
                self.log("\n用户已选择强制刷入，跳过版本校验拦截...")

            self.log("正在解析远程仓库文件列表...")
            api_resp = requests.get(self.api_url, timeout=15)
            if api_resp.status_code != 200:
                self.log(f"获取目录失败: HTTP {api_resp.status_code}")
                return
                
            files_data = api_resp.json()
            downloadable_files = [f for f in files_data if f.get('type') == 'file']
            
            with tempfile.TemporaryDirectory() as temp_dir:
                total_files = len(downloadable_files)
                for i, file_info in enumerate(downloadable_files):
                    file_name = file_info['name']
                    dl_url = file_info['download_url']
                    self.log(f"正在下载: {file_name} ...")
                    
                    try:
                        file_resp = requests.get(dl_url, timeout=20)
                        file_resp.raise_for_status()
                    except Exception as e:
                        self.log(f"\n[失败] 下载 {file_name} 失败: {e}")
                        self.after(0, lambda fn=file_name: messagebox.showerror("网络错误", f"下载文件 {fn} 时发生网络错误！\n可能原因: 网络连接不稳定或超时。"))
                        return
                    
                    with open(os.path.join(temp_dir, file_name), 'wb') as f:
                        f.write(file_resp.content)
                        
                    self.after(
                        0,
                        self.set_progress,
                        0.3 + 0.3 * ((i+1)/total_files),
                        "下载固件文件",
                    )

                self.log("正在清空 Pico 中的旧文件...")
                wipe_script = "import os; [os.remove(f) for f in os.listdir() if not (os.stat(f)[0] & 0x4000)]"
                success, output = self.run_mpremote(port, ["exec", wipe_script], timeout_sec=20)
                if not success:
                    self.log(f"清空旧文件时出现警告: {output}")
                
                self.after(0, self.set_progress, 0.7, "写入设备")

                for i, file_info in enumerate(downloadable_files):
                    file_name = file_info['name']
                    local_path = os.path.join(temp_dir, file_name)
                    self.log(f"正在写入到 Pico: {file_name} ...")
                    
                    success, output = self.run_mpremote(port, ["fs", "cp", local_path, f":{file_name}"])
                    if not success:
                        self.log(f"\n[失败] 写入 {file_name} 失败: {output}")
                        self.after(0, lambda fn=file_name: messagebox.showerror("写入失败", f"写入文件 {fn} 时发生错误！"))
                        return
                    
                    self.after(
                        0,
                        self.set_progress,
                        0.7 + 0.25 * ((i+1)/total_files),
                        "写入设备",
                    )

            self.log("正在重启 Pico 生效固件...")
            self.run_mpremote(port, ["exec", "import machine; machine.reset()"], timeout_sec=10)
            
            self.after(0, self.set_progress, 1.0, "更新完成")
            msg_title = "强制刷入完成" if force else "初次/更新安装完成"
            self.log(f"\n[完成] {msg_title}，Pico 已加载最新程序。")
            self.after(0, lambda mt=msg_title: messagebox.showinfo(mt, f"{mt}！操作已成功完成！"))
            
        except Exception as e:
            error_text = str(e)
            self.log(f"\n[失败] 处理过程中发生错误: {error_text}")
            self.after(0, lambda err=error_text: messagebox.showerror("错误", f"发生意外错误: {err}"))
            
        finally:
            self.after(0, self.set_ui_state, False)

if __name__ == "__main__":
    app = PicoUpdaterApp()
    app.mainloop()
