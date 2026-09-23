"""Interactive, non-destructive assembly inspection for both receiver variants."""
import datetime
import codecs
import html
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

# Physical key names follow the user's observed assembly, not firmware aliases.
BUTTONS = (("MENU", 2), ("UP", 4), ("DOWN", 3), ("OK", 5), ("POWER", 28))
BUTTON_PINS = tuple(pin for _, pin in BUTTONS)
STEPS = ("核心硬件", "五键检查", "屏幕检查", "蜂鸣器", "SD 卡检查")
PREFIX = "LBJ_CHECK_JSON:"
CORE_PREFIX = "LBJ_CORE_STEP:"
CORE_ITEMS = (("RTC","RTC 时钟"),("SX1276_SPI","射频 SPI 通信"),
              ("SX1276_Signal","射频时钟 / 数据线"),("Battery","电池电压"))
LABELS = {"pending": "未检查", "pass": "通过", "fail": "失败",
          "skip": "已跳过", "running": "检查中"}

IDENTITY_SCRIPT = """import machine,json,os
_uid=machine.unique_id()
_n=int.from_bytes(_uid,'big')
_sn=''
while _n:
    _sn='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'[_n%36]+_sn
    _n//=36
# MicroPython does not provide str.rjust; match the firmware's padding.
print('LBJ_CHECK_JSON:'+json.dumps({'SN':('000000000000'+_sn)[-12:],
    'board':os.uname().machine,'uid':_uid.hex(),
    'micropython':os.uname().release,'cpu_mhz':machine.freq()//1000000}))
"""


def identity_text(device):
    return (f"S/N：{device.get('SN', '待读取')}  ·  {device.get('board', '等待连接设备')}"
            f"  ·  MicroPython {device.get('micropython', '—')}"
            f"  ·  CPU {device.get('cpu_mhz', '—')} MHz")


def result_json(output):
    for line in reversed(output.splitlines()):
        if line.startswith(PREFIX):
            value = json.loads(line[len(PREFIX):])
            if not isinstance(value, dict):
                raise ValueError("设备结果不是对象")
            return value
    raise ValueError("设备未返回完整检查结果")


class ButtonProgress:
    """Require a stable release then stable press, not a permanently grounded pin."""
    def __init__(self):
        self.last = [None] * 5
        self.since = [0.] * 5
        self.armed = [False] * 5
        self.passed = [False] * 5

    def feed(self, levels, now):
        if len(levels) != 5 or any(v not in (0, 1) for v in levels):
            raise ValueError("按键采样格式错误")
        for i, value in enumerate(levels):
            if value != self.last[i]:
                self.last[i], self.since[i] = value, now
            elif now - self.since[i] >= .06:
                if value == 1:
                    self.armed[i] = True
                elif self.armed[i]:
                    self.passed[i] = True
        return list(self.passed)


def overall(results):
    if any(r["status"] == "fail" for r in results):
        return "检查未通过"
    if len(results) == len(STEPS) and all(r["status"] == "pass" for r in results):
        return "全部检查通过"
    return "检查未完成（含跳过或未检查项目）"


def report_text(port, device, results, restore):
    lines = ["LBJ Receiver 焊接检查报告", datetime.datetime.now().isoformat(timespec="seconds"),
             "串口：" + port, "设备：" + json.dumps(device, ensure_ascii=False),
             "结论：" + overall(results), "运行恢复：" + restore, ""]
    for title, result in zip(STEPS, results):
        lines += [f"{title}：{LABELS[result['status']]}", result.get("detail", ""), ""]
    lines += ["说明：屏幕与蜂鸣器由用户确认；电压沿用固件换算，非校准仪表测量。SD 检查仅验证 SPI 初始化、容量和扇区读取，",
              "不验证写入能力，不格式化；射频噪声/时钟检查不代表列车接收灵敏度合格。"]
    return "\n".join(lines)


def report_html(port, device, results, restore):
    esc=lambda value:html.escape(str(value))
    cards=[]
    for name,result in zip(STEPS,results):
        status=result['status']
        cards.append(f'<section class="{esc(status)}"><h2>{esc(name)} <span>{esc(LABELS[status])}</span></h2><pre>{esc(result.get("detail") or "未执行检查")}</pre></section>')
    return ('<!doctype html><meta charset="utf-8"><title>LBJ 检查报告</title><style>'
            'body{font:16px system-ui;background:#101317;color:#eef3f7;max-width:900px;margin:40px auto;padding:20px}'
            'section{background:#1a1f25;border-left:4px solid #8493a3;border-radius:10px;padding:16px 22px;margin:14px 0}'
            '.pass{border-color:#45b97c}.fail{border-color:#e46a6a}.skip{border-color:#e9ad4a}'
            'h2{font-size:18px}span{float:right;font-size:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px system-ui;color:#adb9c6}'
            '</style><h1>LBJ · 焊接检查报告</h1><h2>'+esc(overall(results))+'</h2><p>'+esc(port)+' · '+esc(identity_text(device))+'</p><p>'+esc(restore)+'</p>'+
            ''.join(cards)+'<p>人工确认项目不等于仪器测量；SD 只读检查不验证写入能力。</p>')


def driver_source(name):
    return (Path(__file__).resolve().parent / "diagnostic_drivers" / name).read_text(encoding="utf-8")


BATTERY_SCRIPT = '''
_battery_gate=machine.Pin(14,machine.Pin.OUT,value=1)
try:
 _battery_gate.value(0)
 time.sleep_ms(5)
 _battery_adc=machine.ADC(machine.Pin(27))
 _battery_raw=sum(_battery_adc.read_u16() for _ in range(8))/8
 _battery_voltage=_battery_raw/65535*3.3*2+0.174
 test_results['Battery']=0<_battery_raw<65535 and 0.5<_battery_voltage<5.0
 test_results['Battery_V']=round(_battery_voltage,3)
 test_results['Battery_ADC']=_battery_raw
except Exception as _battery_error:
 test_results['Battery']=False
 test_results['Battery_Error']=str(_battery_error)
finally:
 _battery_gate.value(1)
'''

BUZZER_SCRIPT = '''
import machine,time,json
_test_buzzer=machine.Pin(22,machine.Pin.OUT,value=0)
try:
 _test_buzzer.value(1)
 time.sleep_ms(120)
finally:
 _test_buzzer.value(0)
print('LBJ_CHECK_JSON:'+json.dumps({'pulse_ms':120}))
'''


def core_script(legacy):
    # Keep the existing RTC/radio checks, but replace their old "all passed"
    # footer with one structured result that also includes the battery.
    checks=legacy.split("# ================= 最终裁决报告",1)[0]
    checks=checks.replace('# --- 1. 检查 RTC ---',"_core_step('RTC','running')\n# --- 1. 检查 RTC ---")
    checks=checks.replace('# --- 2. 检查 SX1276 ---',"_core_step('RTC','pass' if test_results.get('RTC') else 'fail',test_results.get('RTC_Model',''))\n# --- 2. 检查 SX1276 ---\n_core_step('SX1276_SPI','running')")
    checks=checks.replace('if validator.check_spi():',"_core_spi_ok=validator.check_spi()\n_core_step('SX1276_SPI','pass' if _core_spi_ok else 'fail')\nif _core_spi_ok:\n    _core_step('SX1276_Signal','running')")
    checks+="\n_core_step('SX1276_Signal',('pass' if test_results.get('SX1276_Signal') else 'fail') if _core_spi_ok else 'skip')\n"
    return ("import machine,time,json\ndef _core_step(key,state,detail=''):\n print('LBJ_CORE_STEP:'+json.dumps({'key':key,'state':state,'detail':detail}))\ntest_results={}\ntry:\n" +
            "\n".join(" "+line for line in checks.splitlines()) +
            "\nexcept Exception as _core_error:\n test_results['Core_Error']=str(_core_error)\n" +
            "\n_core_step('Battery','running')\n"+BATTERY_SCRIPT +
            "\n_core_step('Battery','pass' if test_results.get('Battery') else 'fail',str(test_results.get('Battery_V','读取失败'))+' V')\n"+
            "\ntry: test_results['SN']=get_serial_number()\nexcept Exception: pass\n" +
            "print('LBJ_CHECK_JSON:'+json.dumps(test_results))")


def screen_script():
    # Drivers run in RAM, including on an unflashed assembly. No board files written.
    return "import machine,time,json\n_ns={}\nexec(%r,_ns)\n" % driver_source("ili9341.py") + '''
machine.Pin(7,machine.Pin.OUT,value=1)
_spi=machine.SPI(1,baudrate=20000000,sck=machine.Pin(10),mosi=machine.Pin(11),miso=machine.Pin(8))
_lcd=_ns['ILI9341'](_spi,cs=9,dc=12,rst=13)
machine.Pin(6,machine.Pin.OUT,value=0)
for _color in (0xF800,0x07E0,0x001F,0xFFFF,0):
 _lcd.fill(_color)
 time.sleep_ms(800)
_lcd.fill(0)
for _x in range(0,320,20): _lcd.fill_rect(_x,0,1,240,0xFFFF)
for _y in range(0,240,20): _lcd.fill_rect(0,_y,320,1,0xFFFF)
time.sleep_ms(1500)
_lcd.fill(0)
_lcd.fill_rect(0,0,320,2,0xFFFF)
_lcd.fill_rect(0,238,320,2,0xFFFF)
_lcd.fill_rect(0,0,2,240,0xFFFF)
_lcd.fill_rect(318,0,2,240,0xFFFF)
_lcd.draw_gbk(b'LBJ LCD TEST',60,75,0x07E0,0,scale=2)
_lcd.draw_gbk(b'320 x 240',88,130,0xFFFF,0,scale=2)
if _lcd.hzk_file: _lcd.hzk_file.close()
print('LBJ_CHECK_JSON:'+json.dumps({'patterns':True}))
'''


def sd_script():
    return "import machine,json\n_sd_ns={}\nexec(%r,_sd_ns)\n" % driver_source("sdcard.py") + '''
machine.Pin(9,machine.Pin.OUT,value=1)
_sd_spi=machine.SPI(1,baudrate=100000,sck=machine.Pin(10),mosi=machine.Pin(11),miso=machine.Pin(8,machine.Pin.IN,machine.Pin.PULL_UP))
_sd_cs=machine.Pin(7,machine.Pin.OUT,value=1)
try:
 _card=_sd_ns['SDCard'](_sd_spi,_sd_cs)
 _sectors=_card.ioctl(4,None)
 assert _sectors>0
 _block=bytearray(512)
 _card.readblocks(0,_block)
 print('LBJ_CHECK_JSON:'+json.dumps({'sectors':_sectors,'bytes':_sectors*512,'read_bytes':len(_block)}))
finally:
 _sd_cs.value(1)
 _sd_spi.init(baudrate=20000000,polarity=0,phase=0)
'''


class InspectionWorker(threading.Thread):
    """One serial owner. Events, never Tk calls, cross the thread boundary."""
    def __init__(self, port, legacy_script, transport_factory=None):
        super().__init__(daemon=True)
        self.port, self.legacy_script = port, legacy_script
        self.jobs, self.events = queue.Queue(), queue.Queue()
        self.cancel = threading.Event()
        self.transport = None
        self.transport_factory = transport_factory

    def execute(self, script, timeout=20, live=False):
        chunks=[]
        pending=['']
        log_buffer=['']
        last_log=[time.monotonic()]
        decoder=codecs.getincrementaldecoder('utf-8')('replace')
        def flush_log():
            if log_buffer[0]:
                self.events.put(('log',log_buffer[0]))
                log_buffer[0]=''
                last_log[0]=time.monotonic()
        def consume(data):
            # mpremote calls this once per BYTE, including the raw-REPL EOF.
            # Never forward individual characters to Tk.
            data=data.replace(b'\x04',b'')
            if not data:return
            chunks.append(data)
            text=decoder.decode(data)
            log_buffer[0]+=text
            if len(log_buffer[0])>=1024 or (text and time.monotonic()-last_log[0]>=.05):
                flush_log()
            pending[0]+=text
            while '\n' in pending[0]:
                line,pending[0]=pending[0].split('\n',1)
                if line.startswith(CORE_PREFIX):
                    try:
                        event=json.loads(line[len(CORE_PREFIX):])
                        if event.get('key') in dict(CORE_ITEMS) and event.get('state') in LABELS:
                            flush_log()
                            self.events.put(('core_step',event))
                    except (ValueError,AttributeError): pass
        if live:
            try:
                out,err=self.transport.exec_raw(script,timeout=timeout,data_consumer=consume)
            finally:
                log_buffer[0]+=decoder.decode(b'',final=True)
                flush_log()
            if chunks:out=b''.join(chunks)
        else:
            out, err = self.transport.exec_raw(script, timeout=timeout)
        if err:
            raise RuntimeError(err.decode("utf-8", "replace"))
        return out.decode("utf-8", "replace")

    def run(self):
        while True:
            action = self.jobs.get()
            try:
                if action == "close":
                    restored = "未连接，无需恢复"
                    if self.transport is not None:
                        try:
                            self.transport.exec_raw_no_follow("import machine; machine.reset()")
                            restored = "已发送硬重启，恢复设备原程序（空板无主程序）"
                        except Exception as exc:
                            restored = "恢复失败，请手动复位设备：" + str(exc)
                        finally:
                            try:
                                self.transport.close()
                            except Exception as exc:
                                restored += "；串口关闭异常：" + str(exc)
                    self.events.put(("closed", restored))
                    return
                if action == "connect":
                    if self.transport is not None:
                        self.transport.close()
                    factory = self.transport_factory
                    if factory is None:
                        from mpremote.transport_serial import SerialTransport
                        factory = SerialTransport
                    self.transport = factory(self.port, timeout=2)
                    self.transport.enter_raw_repl(soft_reset=True)
                    out = self.execute(IDENTITY_SCRIPT)
                    device = result_json(out)
                    if "RP2040" not in device["board"] and "RP2350" not in device["board"]:
                        raise RuntimeError("只支持本项目 RP2040/RP2350 接线，不运行引脚测试")
                    self.events.put((action, device))
                elif self.transport is None:
                    raise RuntimeError("设备尚未连接")
                elif action == "core":
                    out = self.execute(core_script(self.legacy_script), 20, live=True)
                    self.events.put((action, (result_json(out), out)))
                elif action == "buttons":
                    self.execute("import machine\n_check_keys=[machine.Pin(n,machine.Pin.IN,machine.Pin.PULL_UP) for n in %r]" % (BUTTON_PINS,))
                    progress = ButtonProgress()
                    deadline = time.monotonic() + 90
                    while not self.cancel.is_set() and time.monotonic() < deadline:
                        text = self.execute("print(''.join(str(p.value()) for p in _check_keys))", 3).strip()
                        passed = progress.feed([int(x) for x in text], time.monotonic())
                        self.events.put(("keys", passed))
                        if all(passed):
                            break
                        self.cancel.wait(.08)
                    self.events.put((action, progress.passed))
                elif action == "screen":
                    self.events.put((action, result_json(self.execute(screen_script(), 20))))
                elif action == "buzzer":
                    self.events.put((action, result_json(self.execute(BUZZER_SCRIPT, 3))))
                elif action == "sd":
                    self.events.put((action, result_json(self.execute(sd_script(), 20))))
            except Exception as exc:
                self.events.put(("error", (action, str(exc))))


class CoreChecklist(ctk.CTkScrollableFrame):
    def __init__(self,parent):
        super().__init__(parent,fg_color="#15191e")
        self.rows={}
        self.expanded=False
        for key,title in CORE_ITEMS:
            row=ctk.CTkFrame(self,fg_color="#202830",corner_radius=10)
            row.pack(fill='x',padx=8,pady=5)
            row.grid_columnconfigure(1,weight=1)
            ctk.CTkLabel(row,text=title,font=ctk.CTkFont(size=14,weight='bold'),width=145,anchor='w').grid(row=0,column=0,padx=8,pady=9)
            bar=ctk.CTkProgressBar(row,mode='indeterminate',width=60,height=4)
            bar.grid(row=0,column=1,sticky='ew',padx=8);bar.set(0)
            status=ctk.CTkLabel(row,text='等待检查',width=130,anchor='e',text_color='#96a1ac')
            status.grid(row=0,column=2,padx=8)
            self.rows[key]=(bar,status)
        self.toggle=ctk.CTkButton(self,text='▸ 展开详细日志',anchor='w',fg_color='#29343e',command=self.toggle_log)
        self.toggle.pack(fill='x',padx=8,pady=(12,6))
        self.logs=ctk.CTkTextbox(self,height=160,wrap='word')
        self.logs.configure(state='disabled')

    def toggle_log(self):
        self.expanded=not self.expanded
        if self.expanded:self.logs.pack(fill='x',padx=8,pady=(0,8))
        else:self.logs.pack_forget()
        self.toggle.configure(text='▾ 收起详细日志' if self.expanded else '▸ 展开详细日志')

    def append_log(self,text):
        self.logs.configure(state='normal');self.logs.insert('end',text)
        self.logs.see('end');self.logs.configure(state='disabled')

    def reset(self):
        self.logs.configure(state='normal');self.logs.delete('1.0','end');self.logs.configure(state='disabled')
        for key,_ in CORE_ITEMS:self.update_step(key,'pending')

    def update_step(self,key,state,detail=''):
        if key not in self.rows:return
        bar,label=self.rows[key]
        bar.stop()
        colors={'pass':'#45b97c','fail':'#e46a6a','skip':'#e9ad4a','running':'#4c8dff','pending':'#96a1ac'}
        color=colors.get(state,'#96a1ac')
        bar.configure(progress_color=color)
        if state=='running':bar.start()
        else:bar.set(1 if state=='pass' else 0)
        label.configure(text=({'pending':'等待检查','running':'检查中…'}.get(state,LABELS.get(state,state)))+(' · '+detail if detail else ''),text_color=color)

    def finish(self,result):
        for key,_ in CORE_ITEMS:
            detail=str(result.get('Battery_V',''))+' V' if key=='Battery' and 'Battery_V' in result else ''
            if key=='RTC':detail=result.get('RTC_Model','')
            state='pass' if result.get(key) is True else 'fail'
            if key=='SX1276_Signal' and result.get('SX1276_SPI') is False:state='skip'
            self.update_step(key,state,detail)

    def interrupt(self):
        for bar,label in self.rows.values():
            bar.stop()
            if label.cget('text').startswith('检查中'):
                label.configure(text='检查中断',text_color='#e46a6a')


class CaseDiagram(tk.Canvas):
    """V13 orthographic outline; topology-derived dimensions, not a stock board icon."""
    def __init__(self, parent):
        super().__init__(parent, width=550, height=285, bg="#15191e", highlightthickness=0)
        self.passed = [False] * 5
        self.bind("<Configure>", lambda event: self.draw())

    def draw(self):
        self.delete("all")
        # Supplied STL body: X -2.3..97.535, Y -2.3..64.3 (99.84 x 66.6 mm).
        # Landscape use; four keys on the left and POWER on the right at OK height,
        # as explicitly clarified by the user rather than inferred from EXIF.
        scale = min((self.winfo_width()-180)/99.84, (self.winfo_height()-60)/66.6)
        scale = max(scale, 1)
        w, h = 99.84*scale, 66.6*scale
        x, y = (self.winfo_width()-w)/2+35, 15
        self.create_rectangle(x+7,y+8,x+w+7,y+h+8,fill="#0c1014",outline="#303841")
        self.create_rectangle(x,y,x+w,y+h,fill="#303b45",outline="#8499aa",width=2)
        self.create_rectangle(x+12*scale,y+8*scale,x+82*scale,y+57*scale,fill="#101317",outline="#4c8dff",width=2)
        self.create_text(x+w/2,y+h/2,text="LBJ\nRECEIVER\n\n屏幕窗口",fill="#96a1ac",font=("Arial",12),justify="center")
        for dx in (7,87):
            for dy in (6,59):
                self.create_oval(x+dx*scale-3,y+dy*scale-3,x+dx*scale+3,y+dy*scale+3,outline="#96a1ac")
        for position,i in enumerate((3,1,2,0,4)):
            name,pin=BUTTONS[i]
            color = "#45b97c" if self.passed[i] else "#6a7683"
            if position<4:
                cy=y+(10,24,43,55)[position]*scale
                self.create_rectangle(x-7,cy-7,x+5,cy+7,fill=color,outline=color)
                self.create_text(x-16,cy,text=f"{name} · GP{pin}",anchor="e",fill=color,font=("Arial",11))
            else:
                cy=y+10*scale
                self.create_rectangle(x+w-4,cy-7,x+w+7,cy+7,fill=color,outline=color)
                self.create_text(x+w+14,cy,text="POWER\nGP28",anchor="w",fill=color,font=("Arial",11))


class InspectionWindow(ctk.CTkFrame):
    """Embedded inspection panel; the historical name is retained for callers."""
    def __init__(self, parent, port, legacy_script, transport_factory=None, master=None):
        self._scheduled = set()
        super().__init__(master if master is not None else parent)
        self.parent, self.port = parent, port
        self.configure(fg_color="#101317")
        self.results = [{"status":"pending","detail":""} for _ in STEPS]
        self.device = {}
        self.index, self.busy, self.connected = 0, False, False
        self.skip_pending, self.closing = False, False
        self.finished = False
        self.buzzer_count = 0
        self.core_log = ""
        self.restore = "检查期间主程序暂停"
        self.worker = InspectionWorker(port, legacy_script, transport_factory)
        self.grid_columnconfigure(0,weight=1)
        self.identity=ctk.CTkLabel(self,text=identity_text({}),wraplength=520,justify="left",anchor="w",text_color="#8bc7ed")
        self.identity.grid(row=0,column=0,sticky="ew",padx=12,pady=(6,0))
        self.subtitle=ctk.CTkLabel(self,text=port+"  ·  每项可跳过，跳过不会算作通过",text_color="#96a1ac")
        self.subtitle.grid(row=1,column=0,sticky="w",padx=12)
        self.steps=ctk.CTkFrame(self,fg_color="#1a1f25")
        self.steps.grid(row=2,column=0,sticky="ew",padx=12,pady=5)
        self.step_labels=[]
        for i,title in enumerate(STEPS):
            self.steps.grid_columnconfigure(i,weight=1)
            label=ctk.CTkLabel(self.steps,text=f"{i+1}. {title}\n未检查",height=38)
            label.grid(row=0,column=i,padx=3,pady=3)
            self.step_labels.append(label)
        self.heading=ctk.CTkLabel(self,font=ctk.CTkFont(size=19,weight="bold"))
        self.heading.grid(row=3,column=0,sticky="w",padx=12)
        self.hint=ctk.CTkLabel(self,wraplength=520,justify="left",anchor="w")
        self.hint.grid(row=4,column=0,sticky="ew",padx=12,pady=3)
        self.content=ctk.CTkFrame(self,fg_color="#15191e",height=200)
        self.content.grid(row=5,column=0,sticky="nsew",padx=12)
        self.content.pack_propagate(False)
        self.grid_rowconfigure(5,weight=1)
        self.diagram=CaseDiagram(self.content)
        self.output=ctk.CTkTextbox(self.content,wrap="word",font=ctk.CTkFont(size=13))
        self.core_panel=CoreChecklist(self.content)
        self.buzzer_panel=ctk.CTkFrame(self.content,fg_color="transparent")
        self.buzzer_panel.grid_columnconfigure(0,weight=1)
        self.buzzer_panel.grid_rowconfigure((0,3),weight=1)
        self.buzzer_big=ctk.CTkButton(self.buzzer_panel,text="响一下",width=320,height=145,corner_radius=24,font=ctk.CTkFont(size=32,weight="bold"),fg_color="#229c8c",hover_color="#18786d",command=self.start_step)
        self.buzzer_big.grid(row=1,column=0,padx=30,pady=15)
        self.buzzer_caption=ctk.CTkLabel(self.buzzer_panel,text="单次短响 120 ms · 不自动循环",text_color="#96a1ac")
        self.buzzer_caption.grid(row=2,column=0,pady=10)
        self.report_panel=ctk.CTkScrollableFrame(self.content,fg_color="#15191e")
        self.note=ctk.CTkLabel(self,text="",text_color="#e9ad4a",wraplength=520,justify="left")
        self.note.grid(row=6,column=0,sticky="ew",padx=12,pady=3)
        self.actions=ctk.CTkFrame(self,fg_color="transparent")
        self.actions.grid(row=7,column=0,sticky="ew",padx=12,pady=(0,8))
        self.primary=ctk.CTkButton(self.actions,text="开始检查",width=112,command=self.start_step)
        self.primary.pack(side="left",padx=(0,10))
        self.skip_btn=ctk.CTkButton(self.actions,text="跳过本项",width=85,fg_color="#46515f",command=self.skip)
        self.skip_btn.pack(side="left",padx=(0,10))
        self.fail_btn=ctk.CTkButton(self.actions,text="画面异常",width=85,fg_color="#b65050",command=lambda:self.confirm_screen(False))
        self.confirm_btn=ctk.CTkButton(self.actions,text="听到了",width=90,fg_color="#288a60",command=lambda:self.confirm_buzzer(True))
        self.close_btn=ctk.CTkButton(self.actions,text="结束并查看报告",width=115,fg_color="#46515f",command=self.close)
        self.close_btn.pack(side="right")
        self.render()
        self.bind("<Configure>",self.resize_text)
        self.worker.start()
        self.after(80,self.pump)

    def resize_text(self,event):
        if event.widget is self:
            width=max(240,event.width-30)
            for label in (self.identity,self.hint,self.note):
                label.configure(wraplength=width)

    def after(self,ms,func=None,*args):
        if func is None:
            return super().after(ms)
        def invoke():
            self._scheduled.discard(token)
            func(*args)
        token=super().after(ms,invoke)
        self._scheduled.add(token)
        return token

    def destroy(self):
        for token in tuple(self._scheduled):
            self.after_cancel(token)
        self._scheduled.clear()
        super().destroy()

    def render(self):
        for i,label in enumerate(self.step_labels):
            status=self.results[i]["status"]
            label.configure(text=f"{i+1}. {STEPS[i]}\n{LABELS[status]}",text_color={"pass":"#45b97c","fail":"#e46a6a","skip":"#e9ad4a"}.get(status,"#f3f6f8"))
        self.fail_btn.pack_forget()
        self.confirm_btn.pack_forget()
        self.diagram.pack_forget()
        self.output.pack_forget()
        self.buzzer_panel.pack_forget()
        self.report_panel.pack_forget()
        self.core_panel.pack_forget()
        if not self.primary.winfo_manager(): self.primary.pack(side="left",padx=(0,10),before=self.skip_btn)
        if self.index>=len(STEPS):
            self.show_report()
            return
        self.heading.configure(text=f"{self.index+1} / {len(STEPS)}  ·  {STEPS[self.index]}")
        hints=("自动检查 RTC、SX1276 SPI、时钟/数据线及电池电压（GP27，沿用固件换算）。读数不是精密校准结果，射频信号检查不代表灵敏度合格。",
               "先松开全部按键，再依次按住各键约半秒。识别成功后保持绿色；五个全绿自动进入屏幕检查。90 秒未完成可重试或跳过。",
               "进入本项后自动显示红、绿、蓝、白、黑、网格和边框文字。观察缺色、坏点、横纹及显示范围，完成后请确认画面。",
               "点击“响一下”，蜂鸣器短响一次（120 ms），可重复点击。听到声音后点击“听到了”；未听到请标记异常。不会自动循环鸣响。",
               "请插入 SD 卡，进入本项 5 秒后自动开始只读检查。未插卡可跳过，检测失败可插卡后重试；不格式化、不写卡。")
        self.hint.configure(text=hints[self.index])
        if self.index==0:
            self.core_panel.pack(fill='both',expand=True,padx=4,pady=4)
            self.note.configure(text='各项结果实时更新；详细日志可在检查单底部展开。')
        elif self.index==1:
            self.diagram.pack(fill="both",expand=True)
            self.diagram.draw()
            self.note.configure(text="V13 外壳横屏 · 左侧：OK、上翻、下翻、MENU；右侧 POWER 与 OK 对齐。按键通过后保持绿色。")
        elif self.index==3:
            self.buzzer_panel.pack(fill="both",expand=True)
            self.buzzer_big.configure(state="normal",text="响一下")
            self.buzzer_caption.configure(text=f"已试响 {self.buzzer_count} 次 · 每次 120 ms")
            self.note.configure(text="点击中央按钮试响，再在下方确认是否听到。")
            ready="normal" if self.buzzer_count else "disabled"
            self.confirm_btn.configure(state=ready)
            self.confirm_btn.pack(side="left",padx=5)
            self.fail_btn.configure(text="未听到",width=90,state=ready,command=lambda:self.confirm_buzzer(False))
            self.fail_btn.pack(side="left",padx=5)
        else:
            self.output.pack(fill="both",expand=True,padx=8,pady=8)
            self.set_output(self.results[self.index]["detail"] or "等待开始。可跳过此项。")
            self.note.configure(text="测试不写入设备程序或历史；结束时重启恢复原程序。")
        self.primary.configure(text="重试本项" if self.results[self.index]["status"]=="fail" else "开始检查",state="normal",command=self.start_step)
        if self.index==3: self.primary.pack_forget()
        self.skip_btn.configure(state="normal")

    def set_output(self,text):
        self.output.configure(state="normal")
        self.output.delete("1.0","end")
        self.output.insert("end",text)
        self.output.configure(state="disabled")

    def dispatch(self,action):
        self.busy=True
        self.worker.cancel.clear()
        self.primary.configure(state="disabled")
        self.buzzer_big.configure(state="disabled")
        if action=="buzzer":
            self.buzzer_big.configure(text="正在试响…")
            self.confirm_btn.configure(state="disabled")
            self.fail_btn.configure(state="disabled")
        if action=="core":
            self.core_log=""
            self.core_panel.reset()
        self.results[self.index]["status"]="running"
        self.step_labels[self.index].configure(text=f"{self.index+1}. {STEPS[self.index]}\n检查中")
        self.worker.jobs.put(action)

    def start_step(self):
        if self.busy or self.closing or self.index>=len(STEPS): return
        if not self.connected:
            if not messagebox.askokcancel("开始硬件检查", "将暂停接收并软复位，未落盘的临时数据可能丢失。\n设备必须采用本项目接线。不会擦除固件、历史或 SD 卡。\n结束后自动重启恢复程序，是否继续？",parent=self): return
            self.dispatch("connect")
        else:
            if self.index==1: self.diagram.passed=[False]*5; self.diagram.draw()
            self.dispatch(("core","buttons","screen","buzzer","sd")[self.index])

    def advance(self):
        self.index+=1
        self.render()
        if self.index>=len(STEPS): self.close()
        elif self.connected:
            target=self.index
            if target in (1,2,4):
                self.note.configure(text="请插入 SD 卡，5 秒后自动检查…" if target==4 else "即将自动开始本项检查…")
                def auto_start():
                    if self.index==target and not self.busy and not self.closing and self.results[target]['status']=='pending':
                        self.start_step()
                self.after(5000 if target==4 else 350,auto_start)

    def continue_core(self):
        if self.index==0 and not self.busy and not self.closing and self.results[0]['status']=='pass':
            self.advance()

    def skip(self):
        if self.index>=len(STEPS) or self.closing: return
        if self.busy:
            self.skip_pending=True
            self.worker.cancel.set()
            self.skip_btn.configure(state="disabled")
            self.note.configure(text="跳过请求已记录，等待当前串口操作安全结束…")
        else:
            old=self.results[self.index]
            # A failed test remains a failure even if the user moves on.
            if old["status"]!="fail": old.update(status="skip",detail="用户跳过")
            self.advance()

    def confirm_screen(self,normal):
        self.results[2]={"status":"pass" if normal else "fail","detail":"测试图案已输出；用户目视确认："+("正常" if normal else "异常")}
        if normal: self.advance()
        else: self.render()

    def confirm_buzzer(self,heard):
        if self.busy or self.closing: return
        self.results[3]={"status":"pass" if heard else "fail","detail":f"已点击试响 {self.buzzer_count} 次；用户确认："+("听到声音" if heard else "未听到声音")}
        if heard: self.advance()
        else: self.render()

    def pump(self):
        deadline=time.monotonic()+.008
        handled=0
        try:
            while handled<20 and time.monotonic()<deadline:
                action,value=self.worker.events.get_nowait()
                self.handle(action,value)
                handled+=1
        except queue.Empty:
            pass
        if self.winfo_exists() and not getattr(self,'finished',False):
            self.after(16 if not self.worker.events.empty() else 50,self.pump)

    def handle(self,action,value):
        if action=='core_step':
            if not self.closing and self.index==0:
                self.core_panel.update_step(value['key'],value['state'],value.get('detail',''))
            return
        if action=="log":
            self.core_log+=value
            if self.index==0 and not self.closing:
                self.core_panel.append_log(value)
            return
        if action=="closed":
            self.restore=value
            self.finished=True
            self.parent.set_ui_state(False)
            self.parent.log("焊接检查："+overall(self.results)+"；"+value)
            self.index=len(STEPS)
            self.show_report()
            return
        if action=="keys":
            self.diagram.passed=value
            self.diagram.draw()
            return
        self.busy=False
        if self.closing: return
        if action=="connect":
            self.connected=True
            self.device=value
            self.identity.configure(text=identity_text(value))
        if self.skip_pending:
            self.core_panel.interrupt()
            self.skip_pending=False
            self.results[self.index]={"status":"skip","detail":"用户中途跳过"}
            self.advance()
            return
        if action=="error":
            stage,error=value
            self.connected=False
            detail=stage+"："+error
            if stage=='core': detail+='\n'+self.core_log
            if stage in ('core','connect'):self.core_panel.interrupt()
            self.results[self.index]={"status":"fail","detail":detail}
            self.render()
            self.note.configure(text="检查失败，可重试或跳过。若串口断连，请结束后重新连接设备。")
        elif action=="connect": self.start_step()
        elif action=="core":
            result,log=value
            self.core_panel.finish(result)
            passed=not result.get('Core_Error') and all(result.get(k) is True for k in ("RTC","SX1276_SPI","SX1276_Signal","Battery"))
            voltage=result.get('Battery_V','读取失败')
            self.results[0]={"status":"pass" if passed else "fail","detail":f"电池电压：{voltage} V（固件换算，需万用表核对）\n"+json.dumps(result,ensure_ascii=False)+"\n"+log}
            self.subtitle.configure(text=f"{self.port}  ·  电池：{voltage} V")
            # Keep checklist/log widgets mounted through final confirmation.
            self.step_labels[0].configure(text="1. 核心硬件\n"+("通过" if passed else "失败"),text_color="#45b97c" if passed else "#e46a6a")
            self.primary.configure(state="normal",text="重试本项",command=self.start_step)
            self.skip_btn.configure(state="normal")
            if not passed:
                self.note.configure(text="核心检查存在异常，请查看各项结果及日志，可重试或跳过。")
            if passed:
                self.primary.configure(text="确认结果，继续",command=self.continue_core)
                self.skip_btn.configure(state="disabled")
                self.note.configure(text="核心硬件检查通过。可展开下方日志查看详情，确认后继续按键检查。")
        elif action=="buttons":
            passed=all(value)
            self.results[1]={"status":"pass" if passed else "fail","detail":"；".join(f"{name}/GP{pin}：{'通过' if ok else '未识别'}" for (name,pin),ok in zip(BUTTONS,value))}
            if passed:
                self.busy=True
                self.skip_btn.configure(state="disabled")
                self.note.configure(text="五个按键全部通过，正在进入屏幕检查…")
                def next_screen():
                    self.busy=False
                    if not self.closing: self.advance()
                self.after(650,next_screen)
            else: self.render(); self.note.configure(text=self.results[1]["detail"])
        elif action=="screen":
            self.results[2]={"status":"pending","detail":"图案输出完成，等待用户确认；尚未判定通过。"}
            self.render()
            self.primary.configure(text="画面正常",command=lambda:self.confirm_screen(True))
            self.fail_btn.configure(text="画面异常",state="normal",command=lambda:self.confirm_screen(False))
            self.fail_btn.pack(side="left",padx=8)
        elif action=="buzzer":
            self.buzzer_count+=1
            self.results[3]={"status":"pending","detail":f"已试响 {self.buzzer_count} 次，每次 {value['pulse_ms']} ms。等待用户确认。"}
            # Update existing controls in place. Repacking the entire page here
            # triggers a full layout/paint cycle and can flash on macOS.
            self.buzzer_big.configure(state="normal",text="响一下")
            self.buzzer_caption.configure(text=f"已试响 {self.buzzer_count} 次 · 每次 120 ms")
            self.confirm_btn.configure(state="normal")
            self.fail_btn.configure(state="normal")
            self.step_labels[3].configure(text="4. 蜂鸣器\n等待确认")
        elif action=="sd":
            self.results[4]={"status":"pass","detail":f"容量 {value['bytes']/1024/1024:.1f} MiB；成功读取 {value['read_bytes']} 字节。未执行写入测试。"}
            self.advance()

    def close(self):
        if self.finished:
            self.parent.dismiss_inspection()
            return
        if self.closing: return
        self.closing=True
        self.core_panel.interrupt()
        self.worker.cancel.set()
        for r in self.results:
            if r["status"]=="running": r.update(status="skip",detail="用户提前结束，未判定结果")
        self.primary.configure(state="disabled")
        self.buzzer_big.configure(state="disabled")
        self.skip_btn.configure(state="disabled")
        self.fail_btn.pack_forget()
        self.confirm_btn.pack_forget()
        self.note.configure(text="正在结束检查并重启设备，请勿拔线…")
        self.worker.jobs.put("close")

    def show_report(self):
        self.diagram.pack_forget()
        self.core_panel.pack_forget()
        self.output.pack_forget()
        self.buzzer_panel.pack_forget()
        self.report_panel.pack(fill="both",expand=True,padx=4,pady=4)
        for child in self.report_panel.winfo_children(): child.destroy()
        counts={s:sum(r['status']==s for r in self.results) for s in LABELS}
        summary=ctk.CTkFrame(self.report_panel,fg_color="#202a32",corner_radius=12)
        summary.pack(fill="x",padx=8,pady=(6,12))
        ctk.CTkLabel(summary,text=f"通过 {counts['pass']}     失败 {counts['fail']}     跳过 {counts['skip']}     未完成 {counts['pending']+counts['running']}",font=ctk.CTkFont(size=17,weight="bold")).pack(anchor="w",padx=18,pady=(12,4))
        ctk.CTkLabel(summary,text=identity_text(self.device)+'  ·  '+self.port,text_color="#96a1ac",wraplength=420,justify="left").pack(anchor="w",padx=18,pady=(0,12))
        colors={'pass':'#45b97c','fail':'#e46a6a','skip':'#e9ad4a','pending':'#96a1ac','running':'#96a1ac'}
        for number,(title,result) in enumerate(zip(STEPS,self.results),1):
            color=colors[result['status']]
            card=ctk.CTkFrame(self.report_panel,fg_color="#1e252d",border_color=color,border_width=1,corner_radius=12)
            card.pack(fill="x",padx=8,pady=5)
            row=ctk.CTkFrame(card,fg_color="transparent")
            row.pack(fill="x",padx=16,pady=(12,4))
            ctk.CTkLabel(row,text=f"{number:02}   {title}",font=ctk.CTkFont(size=17,weight="bold")).pack(side="left")
            ctk.CTkLabel(row,text=LABELS[result['status']],text_color=color,font=ctk.CTkFont(size=14,weight="bold")).pack(side="right")
            detail=result.get('detail') or '本项未执行，不计为通过。'
            compact=detail.split('\n')[0]
            ctk.CTkLabel(card,text=compact,wraplength=420,justify="left",text_color="#abb7c4").pack(anchor="w",padx=16,pady=(0,6))
            if '\n' in detail:
                detail_box=ctk.CTkTextbox(card,height=140,wrap='word')
                detail_box.insert('end',detail)
                detail_box.configure(state='disabled')
                def toggle(box=detail_box):
                    if box.winfo_manager(): box.pack_forget()
                    else: box.pack(fill='x',padx=12,pady=(0,10))
                ctk.CTkButton(card,text="展开 / 收起详细日志",width=145,height=26,fg_color="#303d49",command=toggle).pack(anchor="w",padx=16,pady=(0,10))
        self.heading.configure(text="检查报告 · "+overall(self.results))
        self.hint.configure(text="跳过或未完成的项目不会计为通过。报告包含各项结果和设备恢复状态。")
        for i,label in enumerate(self.step_labels):
            label.configure(text=f"{i+1}. {STEPS[i]}\n{LABELS[self.results[i]['status']]}")
        self.primary.configure(text="保存报告…",command=self.save_report,state="normal")
        self.primary.pack(side="left",padx=(0,10))
        self.skip_btn.pack_forget()
        self.fail_btn.pack_forget()
        self.confirm_btn.pack_forget()
        self.close_btn.configure(text="返回运行日志",command=self.close,state="normal" if self.finished else "disabled")
        self.note.configure(text=self.restore)

    def save_report(self):
        path=filedialog.asksaveasfilename(parent=self,title="保存检查报告",defaultextension=".html",initialfile="LBJ-check-"+datetime.datetime.now().strftime("%Y%m%d-%H%M%S")+".html",filetypes=[("检查单报告","*.html"),("文本报告","*.txt")])
        if path:
            try:
                formatter=report_html if Path(path).suffix.lower()=='.html' else report_text
                Path(path).write_text(formatter(self.port,self.device,self.results,self.restore),encoding="utf-8")
            except OSError as exc: messagebox.showerror("保存失败",str(exc),parent=self)
