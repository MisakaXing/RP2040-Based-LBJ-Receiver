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
    sys.exit(0) # 执行完毕立刻退出

import re
import threading
import tempfile
import subprocess
import requests
import customtkinter as ctk
import serial.tools.list_ports
from tkinter import messagebox

# 配置 CustomTkinter 主题
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class PicoUpdaterApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Pico LBJ-Receiver 自动更新助手")
        self.geometry("700x600")
        self.minsize(600, 550)

        # 仓库配置
        self.github_repo = "MisakaXing/RP2040-Based-LBJ-Receiver"
        self.target_dir = "rp2040-main-program"
        self.main_py_url = f"https://raw.githubusercontent.com/{self.github_repo}/main/{self.target_dir}/main.py"
        self.api_url = f"https://api.github.com/repos/{self.github_repo}/contents/{self.target_dir}"

        # 状态变量
        self.local_version = 0.0
        self.local_sn = "N/A"
        self.remote_version = 0.0
        self.is_working = False

        self.setup_ui()
        self.refresh_ports() 

    def setup_ui(self):
        # 顶部标题
        self.title_label = ctk.CTkLabel(self, text="RP2040 固件自动更新工具", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.pack(pady=(20, 5))

        # 硬件连接区域
        self.conn_frame = ctk.CTkFrame(self)
        self.conn_frame.pack(fill="x", padx=40, pady=5)
        
        ctk.CTkLabel(self.conn_frame, text="选择 Pico 串口:").pack(side="left", padx=10, pady=10)
        self.port_var = ctk.StringVar(value="请选择端口...")
        self.port_menu = ctk.CTkOptionMenu(self.conn_frame, variable=self.port_var, values=["请选择端口..."])
        self.port_menu.pack(side="left", padx=10, fill="x", expand=True)
        
        self.refresh_btn = ctk.CTkButton(self.conn_frame, text="🔄 刷新", width=60, command=self.refresh_ports)
        self.refresh_btn.pack(side="right", padx=10)

        # ★ SN 配置区域
        self.sn_frame = ctk.CTkFrame(self)
        self.sn_frame.pack(fill="x", padx=40, pady=5)
        
        ctk.CTkLabel(self.sn_frame, text="设备 SN 码:").pack(side="left", padx=10, pady=10)
        self.sn_entry = ctk.CTkEntry(self.sn_frame, placeholder_text="输入SN (留空则默认继承本机)")
        self.sn_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.force_sn_var = ctk.BooleanVar(value=False)
        self.force_sn_chk = ctk.CTkCheckBox(self.sn_frame, text="强制覆盖原机 SN", variable=self.force_sn_var)
        self.force_sn_chk.pack(side="right", padx=10)

        # 版本信息区域
        self.info_frame = ctk.CTkFrame(self)
        self.info_frame.pack(fill="x", padx=40, pady=5)
        
        self.local_ver_label = ctk.CTkLabel(self.info_frame, text="本地版本: 未知", font=ctk.CTkFont(size=16))
        self.local_ver_label.pack(side="left", padx=20, pady=10, expand=True)
        
        self.remote_ver_label = ctk.CTkLabel(self.info_frame, text="远程版本: 未知", font=ctk.CTkFont(size=16))
        self.remote_ver_label.pack(side="right", padx=20, pady=10, expand=True)

        # 进度与日志区域
        self.log_textbox = ctk.CTkTextbox(self, height=150, state="disabled")
        self.log_textbox.pack(fill="both", expand=True, padx=40, pady=10)

        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.pack(fill="x", padx=40, pady=5)
        self.progress_bar.set(0)

        # 动作按钮区域
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(pady=(10, 20), padx=40, fill="x")

        self.action_btn = ctk.CTkButton(self.btn_frame, text="检查更新并同步", font=ctk.CTkFont(size=16, weight="bold"), height=40, command=lambda: self.start_update_process(force=False))
        self.action_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.force_action_btn = ctk.CTkButton(self.btn_frame, text="强制重新刷入", fg_color="#b91c1c", hover_color="#7f1d1d", font=ctk.CTkFont(size=16, weight="bold"), height=40, command=lambda: self.start_update_process(force=True))
        self.force_action_btn.pack(side="right", fill="x", expand=True, padx=(5, 0))

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
        self.action_btn.configure(state=state, text="正在处理中..." if working else "检查更新并同步")
        self.force_action_btn.configure(state=state, text="正在处理中..." if working else "强制重新刷入")
        self.refresh_btn.configure(state=state)
        self.port_menu.configure(state=state)
        self.sn_entry.configure(state=state)
        self.force_sn_chk.configure(state=state)

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
            self.port_var.set(port_list[0])
            self.log("刷新完成：当前未连接任何串口设备。")
        else:
            self.port_menu.configure(values=port_list)
            
            if auto_detected_port:
                self.port_var.set(auto_detected_port)
                self.log(f"✅ 已自动识别并选中 Pico 设备: {auto_detected_port}")
            else:
                self.port_var.set(port_list[0])
                self.log("已刷新串口列表，但未发现标准 Pico 设备，请手动确认。")

    def run_mpremote(self, port, args_list, timeout_sec=60):
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "mpremote_internal", "connect", port] + args_list
        else:
            cmd = [sys.executable, "-m", "mpremote", "connect", port] + args_list
            
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
            result = subprocess.run(cmd, capture_output=True, encoding='utf-8', timeout=timeout_sec, startupinfo=startupinfo)
            return True, result.stdout
        except subprocess.TimeoutExpired:
            return False, "命令执行超时 (可能 Pico 处于死循环，或文件传输时间过长)"
        except Exception as e:
            return False, str(e)

    def extract_version_and_sn(self, text):
        ver = 0.0
        sn = "N/A"
        
        ver_match = re.search(r"Program_ver\s*=\s*([\d\.]+)", text)
        if ver_match:
            try: ver = float(ver_match.group(1))
            except ValueError: pass
            
        sn_match = re.search(r"Serial_Number\s*=\s*['\"](.*?)['\"]", text)
        if sn_match:
            sn = sn_match.group(1)
            
        return ver, sn
    
    def start_update_process(self, force=False):
        port = self.port_var.get()
        if not port or port == "未检测到设备" or port == "请选择端口...":
            messagebox.showwarning("警告", "请先选择有效的 Pico 串口！")
            return
            
        if force:
            confirm = messagebox.askyesno(
                "⚡ 强制刷入警告",
                "您选择了强制刷入！\n\n这将无视版本是否最新，强行格式化 Pico 并重新拉取所有文件！\n\n保存在本机的所有【历史车次数据】将会永久消失！\n\n您确定要继续吗？",
                icon="warning"
            )
        else:
            confirm = messagebox.askyesno(
                "⚠️ 严重警告",
                "执行同步更新将会彻底清空 Pico 中的旧文件！\n\n保存在本机的所有【历史车次数据】将会永久消失！\n\n您确定要继续执行更新吗？",
                icon="warning"
            )
            
        if not confirm:
            self.log("用户已取消操作。")
            return

        if self.is_working: return
        self.set_ui_state(True)
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("0.0", "end")
        self.log_textbox.configure(state="disabled")
        self.progress_bar.set(0)
        
        threading.Thread(target=self._update_worker, args=(port, force), daemon=True).start()

    def _update_worker(self, port, force):
        try:
            # ================= 0. 连通性预检 =================
            self.log(f"正在测试 Pico ({port}) 连接状态...")
            success, output = self.run_mpremote(port, ["exec", "print('PICO_OK')"], timeout_sec=10)
            if not success or "PICO_OK" not in output:
                self.log("\n❌ 严重错误: 无法与 Pico 建立通信！")
                self.log("可能的原因：")
                self.log("1. 串口正被其他软件占用。")
                self.log("2. Pico 陷入了无法被打断的死循环。")
                self.after(0, lambda: messagebox.showerror("连接失败", "无法与 Pico 通信，请确保串口未被占用！"))
                return
            self.log("✅ Pico 串口通信正常！")

            # ================= 1. 获取远程版本 =================
            self.log("正在连接 GitHub 获取远程版本...")
            self.after(0, self.progress_bar.set, 0.1)
            resp = requests.get(self.main_py_url, timeout=15)
            if resp.status_code == 200:
                self.remote_version, _ = self.extract_version_and_sn(resp.text)
                self.after(0, lambda rv=self.remote_version: self.remote_ver_label.configure(text=f"远程版本: {rv}"))
                self.log(f"成功获取远程版本: {self.remote_version}")
            else:
                self.log("获取远程文件失败，请检查网络！")
                return

            # ================= 2. 获取本地版本和原SN (★ 全新开发板检测) =================
            self.log("正在探测 Pico 文件系统...")
            self.after(0, self.progress_bar.set, 0.2)
            
            # 精准探测是否存在 main.py
            success, ls_output = self.run_mpremote(port, ["exec", "import os; print('main.py' in os.listdir())"], timeout_sec=10)
            
            self.local_sn = "N/A"
            self.local_version = 0.0 
            
            if success and "True" in ls_output:
                # 确认有旧程序，去读取版本
                self.log("正在读取本地版本与设备 SN...")
                success_cat, output = self.run_mpremote(port, ["cat", "main.py"], timeout_sec=15)
                if success_cat and "Program_ver" in output:
                    self.local_version, self.local_sn = self.extract_version_and_sn(output)
                    if self.local_sn != "N/A":
                        self.log(f"✅ 检测到原机保留 SN: {self.local_sn}")
                else:
                    self.log("⚠️ 存在 main.py 但无法解析版本号。")
            else:
                # 全新开发板，直接进安装逻辑
                self.log("🌟 未检测到 main.py，识别为全新开发板，将执行初次完整安装。")

            self.after(0, lambda lv=self.local_version: self.local_ver_label.configure(text=f"本地版本: {lv}"))
            self.after(0, self.progress_bar.set, 0.3)

            # ================= 3. 比较版本 (若强制更新则跳过判断) =================
            if not force:
                if self.local_version >= self.remote_version and self.local_version != 0.0:
                    self.log("\n✅ 当前已是最新版本，无需更新！")
                    self.after(0, self.progress_bar.set, 1.0)
                    return
                self.log("\n⚠️ 准备开始执行同步操作...")
            else:
                self.log("\n⚡ 用户已选择强制刷入，跳过版本校验拦截...")

            # ================= 4. 获取文件列表 =================
            self.log("正在解析远程仓库文件列表...")
            api_resp = requests.get(self.api_url, timeout=15)
            if api_resp.status_code != 200:
                self.log(f"获取目录失败: HTTP {api_resp.status_code}")
                return
                
            files_data = api_resp.json()
            downloadable_files = [f for f in files_data if f.get('type') == 'file']
            
            with tempfile.TemporaryDirectory() as temp_dir:
                # ================= 5. 下载文件到临时目录 =================
                total_files = len(downloadable_files)
                for i, file_info in enumerate(downloadable_files):
                    file_name = file_info['name']
                    dl_url = file_info['download_url']
                    self.log(f"正在下载: {file_name} ...")
                    
                    try:
                        file_resp = requests.get(dl_url, timeout=20)
                        file_resp.raise_for_status()
                    except Exception as e:
                        self.log(f"\n❌ 下载 {file_name} 失败: {e}")
                        self.after(0, lambda fn=file_name: messagebox.showerror("网络错误", f"下载文件 {fn} 时发生网络错误！\n可能原因: 网络连接不稳定或超时。"))
                        return
                    
                    with open(os.path.join(temp_dir, file_name), 'wb') as f:
                        f.write(file_resp.content)
                        
                    self.after(0, self.progress_bar.set, 0.3 + 0.3 * ((i+1)/total_files))

                # ================= 5.5 修改主程序的 SN 码 =================
                user_sn = self.sn_entry.get().strip()
                force_sn = self.force_sn_var.get()
                
                final_sn = "N/A"
                if force_sn and user_sn:
                    final_sn = user_sn
                    self.log(f"⚠️ 用户勾选强制覆盖，将烙印新 SN: {final_sn}")
                elif self.local_sn != "N/A" and not force_sn:
                    final_sn = self.local_sn
                    self.log(f"📝 将保留原机旧 SN: {final_sn}")
                elif user_sn:
                    final_sn = user_sn
                    self.log(f"📝 原机无可用 SN，将写入新 SN: {final_sn}")
                else:
                    self.log("ℹ️ 未提供且原机无有效 SN，默认保留 N/A。")
                    
                main_py_path = os.path.join(temp_dir, "main.py")
                if os.path.exists(main_py_path):
                    with open(main_py_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    
                    # 使用正则精准替换 Serial_Number 变量的值
                    content = re.sub(r'Serial_Number\s*=\s*["\'].*?["\']', f'Serial_Number = "{final_sn}"', content)
                    
                    with open(main_py_path, "w", encoding="utf-8") as f:
                        f.write(content)

                # ================= 6. 清空 Pico =================
                self.log("正在清空 Pico 中的旧文件...")
                wipe_script = "import os; [os.remove(f) for f in os.listdir() if not (os.stat(f)[0] & 0x4000)]"
                success, output = self.run_mpremote(port, ["exec", wipe_script], timeout_sec=20)
                if not success:
                    self.log(f"清空旧文件时出现警告: {output}")
                
                self.after(0, self.progress_bar.set, 0.7)

                # ================= 7. 写入新文件 =================
                for i, file_info in enumerate(downloadable_files):
                    file_name = file_info['name']
                    local_path = os.path.join(temp_dir, file_name)
                    self.log(f"正在写入到 Pico: {file_name} ...")
                    
                    success, output = self.run_mpremote(port, ["fs", "cp", local_path, f":{file_name}"])
                    if not success:
                        self.log(f"\n❌ 写入 {file_name} 失败: {output}")
                        self.after(0, lambda fn=file_name: messagebox.showerror("写入失败", f"写入文件 {fn} 时发生错误！"))
                        return
                    
                    self.after(0, self.progress_bar.set, 0.7 + 0.25 * ((i+1)/total_files))

            # ================= 8. 软重启 Pico =================
            self.log("正在重启 Pico 生效固件...")
            self.run_mpremote(port, ["exec", "import machine; machine.reset()"], timeout_sec=10)
            
            self.after(0, self.progress_bar.set, 1.0)
            msg_title = "强制刷入完成" if force else "初次/更新安装完成"
            self.log(f"\n🎉 {msg_title}！Pico 已加载最新程序。")
            self.after(0, lambda mt=msg_title: messagebox.showinfo(mt, f"{mt}！操作已成功完成！"))
            
        except Exception as e:
            error_text = str(e)
            self.log(f"\n❌ 处理过程中发生严重错误: {error_text}")
            self.after(0, lambda err=error_text: messagebox.showerror("错误", f"发生意外错误: {err}"))
            
        finally:
            self.after(0, self.set_ui_state, False)

if __name__ == "__main__":
    app = PicoUpdaterApp()
    app.mainloop()