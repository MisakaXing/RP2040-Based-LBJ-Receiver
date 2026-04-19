import sys
import os

# ================= ★ PyInstaller 幽灵窗口终极修复 ★ =================
# 必须放在所有图形化库导入的最前面！
# 拦截我们自定义的内部暗号 "mpremote_internal"
if len(sys.argv) > 1 and sys.argv[1] == "mpremote_internal":
    # 剥离暗号，伪造标准的 mpremote 命令行参数
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from mpremote.main import main
    try:
        main() # 直接调用 mpremote 的核心引擎
    except SystemExit as e:
        sys.exit(e.code)
    sys.exit(0) # 拿完数据立刻退出，绝对不加载图形界面
# =================================================================

import json
import re
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk
import tkintermapview
import serial.tools.list_ports

# 配置主题
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class TrainLogApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("列车运行日志解析系统 (最终完美版)")
        self.geometry("1100x750")
        self.minsize(900, 650)
        
        self.log_data = [] # 存储解析后的数据

        # --- 布局框架 ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 1. 左侧侧边栏 (控制台与过滤器)
        self.sidebar_frame = ctk.CTkFrame(self, width=260, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(8, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="列车数据过滤器", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        # 筛选控件
        self.train_no_entry = ctk.CTkEntry(self.sidebar_frame, placeholder_text="搜索车次 (如: D70)")
        self.train_no_entry.grid(row=1, column=0, padx=20, pady=10, sticky="ew")

        self.time_entry = ctk.CTkEntry(self.sidebar_frame, placeholder_text="搜索时间 (如: 14:06)")
        self.time_entry.grid(row=2, column=0, padx=20, pady=10, sticky="ew")

        self.loco_entry = ctk.CTkEntry(self.sidebar_frame, placeholder_text="搜索车型 (如: CR400AF)")
        self.loco_entry.grid(row=3, column=0, padx=20, pady=10, sticky="ew")

        self.search_btn = ctk.CTkButton(self.sidebar_frame, text="应用筛选", command=self.apply_filter)
        self.search_btn.grid(row=4, column=0, padx=20, pady=15, sticky="ew")
        
        self.reset_btn = ctk.CTkButton(self.sidebar_frame, text="重置", fg_color="gray", command=self.reset_filter)
        self.reset_btn.grid(row=5, column=0, padx=20, pady=0, sticky="ew")

        # 地图源自由切换
        self.map_source_label = ctk.CTkLabel(self.sidebar_frame, text="--- 地图源设置 ---", text_color="gray")
        self.map_source_label.grid(row=6, column=0, padx=20, pady=(15, 5))

        self.map_source_var = ctk.StringVar(value="高德地图 (极速)")
        self.map_source_menu = ctk.CTkOptionMenu(
            self.sidebar_frame, 
            variable=self.map_source_var, 
            values=["高德地图 (极速)", "OpenStreetMap (默认)", "CartoDB (海外极速)"],
            command=self.change_map_source 
        )
        self.map_source_menu.grid(row=7, column=0, padx=20, pady=(0, 10), sticky="ew")

        # Pico 串口直连区域
        self.pico_label = ctk.CTkLabel(self.sidebar_frame, text="--- Pico 串口直连 ---", text_color="gray")
        self.pico_label.grid(row=9, column=0, padx=20, pady=(10, 5))

        self.port_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.port_frame.grid(row=10, column=0, padx=20, pady=5, sticky="ew")

        self.port_var = ctk.StringVar(value="请选择端口...")
        self.port_menu = ctk.CTkOptionMenu(self.port_frame, variable=self.port_var, values=["请选择端口..."], width=130)
        self.port_menu.pack(side="left", fill="x", expand=True)

        self.refresh_port_btn = ctk.CTkButton(
            self.port_frame, text="🔄", width=45, height=30, 
            command=lambda: self.refresh_ports(show_prompt=True)
        )
        self.refresh_port_btn.pack(side="right", padx=(5, 0))

        self.read_pico_btn = ctk.CTkButton(self.sidebar_frame, text="📥 从 Pico 提取历史数据", fg_color="#2b8a3e", hover_color="#237032", command=self.start_pico_read)
        self.read_pico_btn.grid(row=11, column=0, padx=20, pady=(5, 10), sticky="ew")

        self.export_pico_btn = ctk.CTkButton(self.sidebar_frame, text="💾 导出日志到电脑", fg_color="#d97706", hover_color="#b45309", command=self.start_pico_export)
        self.export_pico_btn.grid(row=12, column=0, padx=20, pady=(0, 15), sticky="ew")

        # 本地文件读取
        self.local_label = ctk.CTkLabel(self.sidebar_frame, text="--- 本地文件读取 ---", text_color="gray")
        self.local_label.grid(row=13, column=0, padx=20, pady=(0, 5))

        self.load_btn = ctk.CTkButton(self.sidebar_frame, text="📂 导入 JSON 日志文件", command=self.load_json_file)
        self.load_btn.grid(row=14, column=0, padx=20, pady=(5, 20), sticky="ew")

        # 2. 右侧主内容区
        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(1, weight=2)

        # 2.1 数据表格区域
        self.tree_frame = ctk.CTkFrame(self.main_frame)
        self.tree_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        self.setup_treeview()

        # 2.2 详情与地图区域
        self.map_frame = ctk.CTkFrame(self.main_frame)
        self.map_frame.grid(row=1, column=0, sticky="nsew")
        self.map_frame.grid_columnconfigure(0, weight=1)
        self.map_frame.grid_rowconfigure(0, weight=1)

        self.map_widget = tkintermapview.TkinterMapView(self.map_frame, corner_radius=10)
        self.map_widget.set_tile_server("https://wprd01.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=7", max_zoom=19)
        self.map_widget.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.map_widget.set_position(39.9, 116.3) 
        self.map_widget.set_zoom(10)
        self.current_marker = None
        
        self.detail_text = ctk.CTkTextbox(self.map_frame, width=250)
        self.detail_text.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        self.detail_text.insert("0.0", "选择列表中的列车日志以查看详情和GPS定位...")
        self.detail_text.configure(state="disabled")

        self.refresh_ports(show_prompt=False)

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
            if show_prompt:
                messagebox.showwarning("提示", "未检测到任何串口设备，请检查数据线连接！")
        else:
            self.port_menu.configure(values=port_list)
            if auto_port:
                self.port_var.set(auto_port)
                if show_prompt:
                    messagebox.showinfo("成功", f"扫描完成！\n已自动识别并选中 Pico 设备：{auto_port}")
            else:
                self.port_var.set(port_list[0])
                if show_prompt:
                    messagebox.showwarning("提示", "已刷新列表，但未发现标准 Pico 设备。\n请展开下拉菜单手动选择正确的端口！")

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
        if not save_path:
            return 
            
        self.export_pico_btn.configure(state="disabled", text="导出中，请稍候...")
        self.read_pico_btn.configure(state="disabled")
        self.load_btn.configure(state="disabled")
        
        threading.Thread(target=self._export_worker, args=(port, save_path), daemon=True).start()

    def _pico_worker(self, port):
        # 动态判断运行环境
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "mpremote_internal", "connect", port, "cat", "history.jsonl"]
        else:
            cmd = [sys.executable, "-m", "mpremote", "connect", port, "cat", "history.jsonl"]
            
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            # 使用 utf-8 强制编码
            result = subprocess.run(cmd, capture_output=True, encoding='utf-8', timeout=30, startupinfo=startupinfo)
            output = result.stdout
            
            if result.returncode != 0:
                 err_msg = result.stderr if result.stderr else output
                 self.after(0, lambda err=err_msg: messagebox.showerror("读取失败", f"无法读取文件，可能 Pico 被占用或文件为空。\n\n{err}"))
                 return

            lines = output.split('\n')
            self.after(0, self._process_memory_lines, lines)
            
        except subprocess.TimeoutExpired:
            self.after(0, lambda: messagebox.showerror("超时", "读取超时，请确保串口未被 Thonny 等软件占用！"))
        except Exception as e:
            error_text = str(e)
            self.after(0, lambda err=error_text: messagebox.showerror("错误", f"发生意外错误: {err}"))
        finally:
            self.after(0, lambda: self.read_pico_btn.configure(state="normal", text="📥 从 Pico 提取历史数据"))
            self.after(0, lambda: self.export_pico_btn.configure(state="normal"))
            self.after(0, lambda: self.load_btn.configure(state="normal"))

    def _export_worker(self, port, save_path):
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "mpremote_internal", "connect", port, "cp", ":history.jsonl", save_path]
        else:
            cmd = [sys.executable, "-m", "mpremote", "connect", port, "cp", ":history.jsonl", save_path]
            
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(cmd, capture_output=True, encoding='utf-8', timeout=45, startupinfo=startupinfo)
            
            if result.returncode == 0:
                 self.after(0, lambda path=save_path: messagebox.showinfo("成功", f"日志文件已成功导出至：\n{path}"))
            else:
                 err_msg = result.stderr if result.stderr else result.stdout
                 self.after(0, lambda err=err_msg: messagebox.showerror("导出失败", f"无法导出文件，请确保串口未被占用。\n\n{err}"))
                 
        except subprocess.TimeoutExpired:
            self.after(0, lambda: messagebox.showerror("超时", "导出超时！日志文件可能太大或设备已断开连接。"))
        except Exception as e:
            error_text = str(e)
            self.after(0, lambda err=error_text: messagebox.showerror("错误", f"发生意外错误: {err}"))
        finally:
            self.after(0, lambda: self.export_pico_btn.configure(state="normal", text="💾 导出日志到电脑"))
            self.after(0, lambda: self.read_pico_btn.configure(state="normal"))
            self.after(0, lambda: self.load_btn.configure(state="normal"))

    def _process_memory_lines(self, lines):
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
                
        self.refresh_treeview(self.log_data)
        if valid_count > 0:
            messagebox.showinfo("成功", f"成功提取并解析了 {valid_count} 条记录！")
        else:
            messagebox.showwarning("提示", "Pico 连接成功，但未在 history.jsonl 中找到有效的历史数据。")

    def setup_treeview(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", 
                        background="#2b2b2b", foreground="white", rowheight=25, 
                        fieldbackground="#2b2b2b", bordercolor="#343638", borderwidth=0)
        style.map('Treeview', background=[('selected', '#1f538d')])
        style.configure("Treeview.Heading", background="#565b5e", foreground="white", relief="flat")
        style.map("Treeview.Heading", background=[('active', '#3484F0')])

        columns = ("time", "train_no", "speed", "loco_type", "gps_status")
        self.tree = ttk.Treeview(self.tree_frame, columns=columns, show="headings", style="Treeview")
        
        self.tree.heading("time", text="时间")
        self.tree.heading("train_no", text="车次")
        self.tree.heading("speed", text="速度 (km/h)")
        self.tree.heading("loco_type", text="车型/级别")
        self.tree.heading("gps_status", text="GPS状态")

        self.tree.column("time", width=100, anchor="center")
        self.tree.column("train_no", width=100, anchor="center")
        self.tree.column("speed", width=80, anchor="center")
        self.tree.column("loco_type", width=120, anchor="center")
        self.tree.column("gps_status", width=100, anchor="center")

        self.tree.pack(fill="both", expand=True, padx=5, pady=5)
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
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            self._process_memory_lines(lines) 
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
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        for entry in data_list:
            self.tree.insert("", "end", iid=entry['_index'], values=(
                entry["time"],
                entry["train_no"],
                entry["speed"],
                entry["loco_type"],
                entry["gps_status"]
            ))

    def apply_filter(self):
        filter_train = self.train_no_entry.get().strip().upper()
        filter_time = self.time_entry.get().strip()
        filter_loco = self.loco_entry.get().strip().upper()

        filtered_data = []
        for entry in self.log_data:
            match_train = filter_train in str(entry["train_no"]).upper() if filter_train else True
            match_time = filter_time in str(entry["time"]) if filter_time else True
            match_loco = filter_loco in str(entry["loco_type"]).upper() if filter_loco else True
            
            if match_train and match_time and match_loco:
                filtered_data.append(entry)
                
        self.refresh_treeview(filtered_data)

    def reset_filter(self):
        self.train_no_entry.delete(0, 'end')
        self.time_entry.delete(0, 'end')
        self.loco_entry.delete(0, 'end')
        self.refresh_treeview(self.log_data)

    def on_tree_select(self, event):
        selected_items = self.tree.selection()
        if not selected_items: return
        
        index = int(selected_items[0])
        entry = self.log_data[index]

        self.detail_text.configure(state="normal")
        self.detail_text.delete("0.0", "end")
        details = (
            f"=== 运行详情 ===\n"
            f"时间: {entry['time']}\n"
            f"车次: {entry['train_no']}\n"
            f"速度: {entry['speed']} km/h\n"
            f"车型: {entry['loco_type']}\n\n"
            f"=== GPS 坐标 ===\n"
            f"纬度: {entry['lat'] if entry['lat'] is not None else 'N/A'}\n"
            f"经度: {entry['lon'] if entry['lon'] is not None else 'N/A'}\n\n"
            f"=== 原始 JSON ===\n"
            f"{entry['raw']}"
        )
        self.detail_text.insert("0.0", details)
        self.detail_text.configure(state="disabled")

        if self.current_marker:
            self.current_marker.delete()
            self.current_marker = None

        lat = entry['lat']
        lon = entry['lon']

        if lat is not None and lon is not None:
            if -85.0 < lat < 85.0 and -180.0 <= lon <= 180.0:
                self.map_widget.set_position(lat, lon)
                self.map_widget.set_zoom(14)
                self.current_marker = self.map_widget.set_marker(
                    lat, lon, 
                    text=f"{entry['train_no']} ({entry['speed']}km/h)"
                )
            else:
                print(f"坐标非法被拦截 -> 车次: {entry['train_no']}, 坐标: ({lat}, {lon})")

if __name__ == "__main__":
    app = TrainLogApp()
    app.mainloop()