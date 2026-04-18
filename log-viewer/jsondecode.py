import json
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk
import tkintermapview

# 配置主题
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class TrainLogApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("列车运行日志解析系统")
        self.geometry("1100x700")
        self.minsize(900, 600)
        
        self.log_data = [] # 存储解析后的数据

        # --- 布局框架 ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 1. 左侧侧边栏 (控制台与过滤器)
        self.sidebar_frame = ctk.CTkFrame(self, width=250, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(6, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="列车数据过滤器(支持模糊搜索）", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        # 筛选控件
        self.train_no_entry = ctk.CTkEntry(self.sidebar_frame, placeholder_text="搜索车次 (如: D70)")
        self.train_no_entry.grid(row=1, column=0, padx=20, pady=10, sticky="ew")

        self.time_entry = ctk.CTkEntry(self.sidebar_frame, placeholder_text="搜索时间 (如: 14:06)")
        self.time_entry.grid(row=2, column=0, padx=20, pady=10, sticky="ew")

        self.loco_entry = ctk.CTkEntry(self.sidebar_frame, placeholder_text="搜索车型 (如: CR400AF)")
        self.loco_entry.grid(row=3, column=0, padx=20, pady=10, sticky="ew")

        self.search_btn = ctk.CTkButton(self.sidebar_frame, text="应用筛选", command=self.apply_filter)
        self.search_btn.grid(row=4, column=0, padx=20, pady=20, sticky="ew")
        
        self.reset_btn = ctk.CTkButton(self.sidebar_frame, text="重置", fg_color="gray", command=self.reset_filter)
        self.reset_btn.grid(row=5, column=0, padx=20, pady=0, sticky="ew")

        self.load_btn = ctk.CTkButton(self.sidebar_frame, text="导入 JSON 日志文件", command=self.load_json_file)
        self.load_btn.grid(row=7, column=0, padx=20, pady=20, sticky="ew")

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
        self.map_widget.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.map_widget.set_position(39.9, 116.3) 
        self.map_widget.set_zoom(10)
        self.current_marker = None
        
        self.detail_text = ctk.CTkTextbox(self.map_frame, width=250)
        self.detail_text.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        self.detail_text.insert("0.0", "选择列表中的列车日志以查看详情和GPS定位...")
        self.detail_text.configure(state="disabled")

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
        
        self.log_data.clear()
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        data = json.loads(line)
                        parsed_entry = self.extract_log_info(data)
                        # 【核心修复】：为每条数据绑定其在全局列表中的真实索引
                        parsed_entry['_index'] = len(self.log_data)
                        self.log_data.append(parsed_entry)
                    except json.JSONDecodeError:
                        continue
            
            self.refresh_treeview(self.log_data)
            messagebox.showinfo("成功", f"成功解析 {len(self.log_data)} 条日志！")
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
        if speed.replace("-", "").strip() == "": speed = "0"
        
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
            # 使用绑定的真实索引作为节点ID，防止搜索后索引错位
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
        
        # 直接使用节点的 IID（也就是我们绑定的真实索引）获取正确数据
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

        # 地图防崩溃：确保经纬度在 Web Mercator 投影的合法范围内
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