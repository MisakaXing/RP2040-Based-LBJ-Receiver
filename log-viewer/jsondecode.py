import sys
import os

if len(sys.argv) > 1 and sys.argv[1] == "mpremote_internal":
    # 伪造标准的 mpremote 命令行参数
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from mpremote.main import main
    try:
        main() # 直接调用 mpremote 的核心引擎
    except SystemExit as e:
        sys.exit(e.code)
    sys.exit(0) 

import json
import re
import time
import threading
import subprocess
import serial
import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk
import tkintermapview

# 视觉主题
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
    "amber": "#E9AD4A",
    "green": "#45B97C",
    "red": "#E46A6A",
}

class TrainLogApp(ctk.CTk):
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
        dialog.transient(self)

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
        PICO_VID = 0x2E8A
        ports = serial.tools.list_ports.comports()
        port_list = [p.device for p in ports]
        
        auto_port = None
        for p in ports:
            if p.vid == PICO_VID:
                auto_port = p.device
                break
                
        if not port_list:
            self.port_menu.configure(values=["未检测到设备"])
            self.port_var.set("未检测到设备")
            if show_prompt: messagebox.showwarning("提示", "未检测到任何串口设备，请检查数据线连接！")
        else:
            self.port_menu.configure(values=port_list)
            if auto_port:
                self.port_var.set(auto_port)
                if show_prompt: messagebox.showinfo("成功", f"扫描完成！\n已自动识别并选中 Pico 设备：{auto_port}")
            else:
                self.port_var.set(port_list[0])
                if show_prompt: messagebox.showwarning("提示", "已刷新列表，但未发现标准 Pico 设备。\n请展开下拉菜单手动选择正确的端口！")

    def _interrupt_pico(self, port):
        """发送 Ctrl+C 强行打断死循环"""
        try:
            with serial.Serial(port, 115200, timeout=1) as ser:
                ser.write(b'\x03\x03') 
                time.sleep(0.5) 
        except Exception as e:
            print(f"串口打断尝试失败: {e}")

    def _reboot_pico(self, port):
        """发送 Ctrl+D 触发软重启恢复工作"""
        try:
            with serial.Serial(port, 115200, timeout=1) as ser:
                ser.write(b'\x04')
        except Exception:
            pass

    def _run_mpremote_safe(self, cmd, timeout_sec=30):
        """抓取原始字节，宽容解码"""
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
            result = subprocess.run(cmd, capture_output=True, timeout=timeout_sec, startupinfo=startupinfo)
            
            stdout_bytes = result.stdout if result.stdout else b''
            stderr_bytes = result.stderr if result.stderr else b''
            
            try:
                out_str = stdout_bytes.decode('utf-8', errors='ignore')
            except:
                out_str = stdout_bytes.decode('gbk', errors='ignore')
                
            try:
                err_str = stderr_bytes.decode('utf-8', errors='ignore')
            except:
                err_str = stderr_bytes.decode('gbk', errors='ignore')
                
            return result.returncode, out_str, err_str
            
        except subprocess.TimeoutExpired:
            return -1, "", "TIMEOUT"
        except Exception as e:
            return -2, "", str(e)

    def start_pico_read(self):
        port = self.port_var.get()
        if not port or "未检测" in port or "请选择" in port:
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return

        self.read_pico_btn.configure(state="disabled", text="读取中，请稍候...")
        self.export_pico_btn.configure(state="disabled")
        self.load_btn.configure(state="disabled")
        
        threading.Thread(target=self._pico_worker, args=(port,), daemon=True).start()

    def start_pico_export(self):
        port = self.port_var.get()
        if not port or "未检测" in port or "请选择" in port:
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return
            
        save_path = filedialog.asksaveasfilename(
            defaultextension=".jsonl",
            initialfile="history.jsonl",
            title="保存 Pico 日志文件",
            filetypes=[("JSON Lines", "*.jsonl"), ("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if not save_path: return 
            
        self.export_pico_btn.configure(state="disabled", text="导出中，请稍候...")
        self.read_pico_btn.configure(state="disabled")
        self.load_btn.configure(state="disabled")
        
        threading.Thread(target=self._export_worker, args=(port, save_path), daemon=True).start()

    def _pico_worker(self, port):
        self.after(0, lambda: self.read_pico_btn.configure(text="正在中断设备..."))
        self._interrupt_pico(port)
        self.after(0, lambda: self.read_pico_btn.configure(text="正在读取数据..."))

        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "mpremote_internal", "connect", port, "cat", "history.jsonl"]
        else:
            cmd = [sys.executable, "-m", "mpremote", "connect", port, "cat", "history.jsonl"]
            
        try:
            returncode, output, err_msg = self._run_mpremote_safe(cmd, timeout_sec=30)
            
            if returncode == -1: 
                self.after(0, lambda: messagebox.showerror("超时", "读取超时，请确保串口未被占用且线缆连接正常！"))
                return
            elif returncode != 0:
                final_err = err_msg if err_msg else output
                self.after(0, lambda err=final_err: messagebox.showerror("读取失败", f"无法读取文件，可能设备忙或文件损坏。\n\n{err}"))
                return

            lines = output.split('\n')
            self.after(0, self._process_memory_lines, lines, "Pico 设备")
            
        except Exception as e:
            self.after(0, lambda err=str(e): messagebox.showerror("错误", f"发生意外错误: {err}"))
        finally:
            self.after(0, lambda: self.read_pico_btn.configure(text="正在恢复设备..."))
            self._reboot_pico(port)
            
            self.after(0, lambda: self.read_pico_btn.configure(state="normal", text="读取设备记录"))
            self.after(0, lambda: self.export_pico_btn.configure(state="normal"))
            self.after(0, lambda: self.load_btn.configure(state="normal"))

    def _export_worker(self, port, save_path):
        self.after(0, lambda: self.export_pico_btn.configure(text="正在中断设备..."))
        self._interrupt_pico(port)
        self.after(0, lambda: self.export_pico_btn.configure(text="正在导出文件..."))

        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "mpremote_internal", "connect", port, "cp", ":history.jsonl", save_path]
        else:
            cmd = [sys.executable, "-m", "mpremote", "connect", port, "cp", ":history.jsonl", save_path]
            
        try:
            returncode, output, err_msg = self._run_mpremote_safe(cmd, timeout_sec=45)
            
            if returncode == 0:
                 self.after(0, lambda p=save_path: messagebox.showinfo("成功", f"日志已成功导出至：\n{p}"))
            elif returncode == -1:
                 self.after(0, lambda: messagebox.showerror("超时", "导出超时！日志文件可能过大或连接断开。"))
            else:
                 final_err = err_msg if err_msg else output
                 self.after(0, lambda err=final_err: messagebox.showerror("导出失败", f"导出失败。\n\n{err}"))
                 
        except Exception as e:
            self.after(0, lambda err=str(e): messagebox.showerror("错误", f"发生意外错误: {err}"))
        finally:
            self.after(0, lambda: self.export_pico_btn.configure(text="正在恢复设备..."))
            self._reboot_pico(port)
            
            self.after(0, lambda: self.export_pico_btn.configure(state="normal", text="导出日志到电脑"))
            self.after(0, lambda: self.read_pico_btn.configure(state="normal"))
            self.after(0, lambda: self.load_btn.configure(state="normal"))

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

if __name__ == "__main__":
    app = TrainLogApp()
    app.mainloop()
