import time
import json
import machine
import os
import sdcard
from machine import Pin, ADC, I2C
from lbj_receiver import LBJReceiver
from ili9341 import ILI9341, BLACK, WHITE, RED, GREEN, BLUE, CYAN, YELLOW, GRAY, MAGENTA
from rtc_ds3231 import DS3231
from boot_post import SystemPOST 

# ==========================================
# ★ 全局设置区
# ==========================================
Program_ver = 1.4
is_es_ver = 1 
Author_Name = "MisakaXing" 
BAT_OFFSET = 0.174 #根据你用万用表测出来的值修改这个
# ==========================================
# 1. 硬件初始化
# ==========================================
tft_cs = Pin(9, Pin.OUT, value=1) 
spi1 = machine.SPI(1, baudrate=40000000, sck=Pin(10), mosi=Pin(11), miso=Pin(8, Pin.IN, Pin.PULL_UP))
tft = ILI9341(spi1, cs=9, dc=12, rst=13)

sd_cs = Pin(7, Pin.OUT, value=1)
bat_en = Pin(14, Pin.OUT, value=1)
bat_adc = ADC(Pin(27)) 
buzzer = Pin(22, Pin.OUT, value=0)

i2c0 = I2C(0, sda=Pin(0), scl=Pin(1), freq=400000)
rtc = DS3231(i2c0)

btn_menu, btn_up, btn_down, btn_ok = [Pin(i, Pin.IN, Pin.PULL_UP) for i in (2, 3, 4, 5)]
sensor_temp = machine.ADC(4)

# ==========================================
# ★★★ 炫酷开机自检 (POST) ★★★
# ==========================================
post = SystemPOST(tft, tft_cs)
boot_status = post.run_all(bat_adc, bat_en, sensor_temp, rtc, spi1, sd_cs, buzzer, Program_ver, is_es_ver)

if boot_status == "HALT":
    while True: pass 

# ==========================================
# 2. 系统状态机与变量
# ==========================================
MAX_HIST = 2000
HIST_FILE = "history.jsonl"
SD_LOG_FILE = "/sd/lbj_log.jsonl"
CONFIG_FILE = "config.json"

system_state = "DASHBOARD" 
has_received = False
menu_index = 0
# ★ 菜单新增：FORMAT SD
menu_items = ["BUZZER: ON", "SET DATE", "JUMP TO ID", "FORMAT FLASH", "FORMAT SD", "SAFE EJECT SD", "ABOUT DEV", "EXIT"]
cfg_buzzer = True

hist_ptr = -1
total_count = 0
history_offsets = [] 
last_interaction = time.ticks_ms()

last_minute = -1 
last_sd_status_drawn = ""
last_hw_str_drawn = ""
sd_eject_mode = False
sd_obj = None

edit_y, edit_m, edit_d = 24, 1, 1
edit_id = [0, 0, 0, 0] 
edit_step = 0 

current_status = b'READY'
current_status_color = GREEN
current_sd_status = "SD INIT..." 

last_basic, last_ext = {}, {}
last_is_full = True

LBL_TRAIN, LBL_SPEED, LBL_ROUTE, LBL_KM, LBL_LOCO = b'\xb3\xb5:', b'\xcb\xd9:', b'\xcf\xdf:', b'\xb1\xea:', b'\xbb\xfa:'

# ==========================================
# 3. 核心工具与存储函数
# ==========================================
def beep(duration=0.02):
    if cfg_buzzer: buzzer.value(1); time.sleep(duration); buzzer.value(0)

def get_max_days(y, m):
    if m == 2: return 29 if y % 4 == 0 else 28
    return 30 if m in [4, 6, 9, 11] else 31

def get_battery_info():
    # 1. 瞬间拉低，接通分压电路
    bat_en.value(0)
    time.sleep_ms(5) # 等待电压稳定
    
    # 2. 读取安全的减半电压
    raw = bat_adc.read_u16()
    
    # 3. 测完立刻拉高，切断电路，保护 ADC 引脚
    bat_en.value(1)
    
    # 4. 计算真实电压并加上校准值
    raw_volts = (raw / 65535) * 3.3 * 2
    volts = raw_volts + BAT_OFFSET
    
    percent = int((volts - 3.4) / (4.2 - 3.4) * 100)
    return f"{volts:.1f}V", f"{max(0, min(100, percent))}%"

def load_config():
    global cfg_buzzer, menu_items
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.loads(f.read())
            cfg_buzzer = config.get("buzzer", True) 
            menu_items[0] = f"BUZZER: {'ON' if cfg_buzzer else 'OFF'}"
    except: pass 

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            f.write(json.dumps({"buzzer": cfg_buzzer}))
    except: pass

def init_history():
    global total_count, history_offsets
    history_offsets = []
    try:
        with open(HIST_FILE, 'r') as f:
            while True:
                offset = f.tell()      
                line = f.readline()    
                if not line: break     
                history_offsets.append(offset)
        total_count = len(history_offsets)
    except: 
        total_count = 0
        history_offsets = []

def save_history(data):
    global total_count, history_offsets
    if total_count >= MAX_HIST: return 
    try:
        t_str = rtc.get_time_str(True)
        record = {"t": t_str, "d": data}
        with open(HIST_FILE, 'a') as f:
            offset = f.tell() 
            f.write(json.dumps(record) + '\n')
            history_offsets.append(offset) 
        total_count += 1
    except: pass

def load_history_entry(idx):
    if idx < 0 or idx >= len(history_offsets): return None
    try:
        with open(HIST_FILE, 'r') as f:
            f.seek(history_offsets[idx]) 
            line = f.readline()
            return json.loads(line)
    except: return None


def check_sd():
    global current_sd_status, sd_eject_mode, last_sd_status_drawn, sd_obj
    try:
        tft_cs.value(1) 
        if sd_eject_mode:
            current_sd_status = "SAFE TO REMOVE"
        elif 'sd' in os.listdir('/'):
            try:
                # ★ 极速物理探测：直接向底层发送读取第0扇区指令（仅耗时1~2毫秒）
                if sd_obj:
                    spi1.init(baudrate=5000000)
                    sd_obj.readblocks(0, bytearray(512))
                
                s = os.statvfs("/sd")
                total_kb = (s[0] * s[2]) / 1024
                free_kb = (s[0] * s[3]) / 1024
                used_kb = total_kb - free_kb
                if free_kb < 1024: current_sd_status = "SD FULL"
                elif total_kb > 1048576: current_sd_status = f"SD:{used_kb/1048576:.1f}/{total_kb/1048576:.1f}G"
                else: current_sd_status = f"SD:{used_kb/1024:.1f}/{total_kb/1024:.1f}M"
            except:
                # 只要底层报错，说明卡被物理拔出，立刻清理残余挂载
                try: os.umount("/sd")
                except: pass
                sd_obj = None # 清空对象
                current_sd_status = "SD NO INSERT"
        else:
            spi1.init(baudrate=1000000) 
            try:
                sd_obj = sdcard.SDCard(spi1, sd_cs)
                os.mount(os.VfsFat(sd_obj), "/sd")
                current_sd_status = "SD INIT..." 
            except Exception as e:
                sd_obj = None
                err = str(e).lower()
                if '19' in err: current_sd_status = "SD NEED FORMAT"
                else: current_sd_status = "SD NO INSERT"
    except Exception:
        current_sd_status = "SD NO INSERT"
    finally:
        spi1.init(baudrate=40000000) 
        if system_state == "DASHBOARD" and current_sd_status != last_sd_status_drawn:
            tft.fill_rect(0, 0, 130, 24, 0x01CF) 
            tft.draw_gbk(current_sd_status.encode(), 5, 4, WHITE, 0x01CF)
            last_sd_status_drawn = current_sd_status

def log_to_sd(data):
    if "SD:" not in current_sd_status: return 
    try:
        tft_cs.value(1)
        spi1.init(baudrate=5000000)
        with open(SD_LOG_FILE, 'a') as f:
            f.write(json.dumps({"t": rtc.get_time_str(True), "d": data}) + '\n')
    except Exception: pass
    finally:
        spi1.init(baudrate=40000000)

# ==========================================
# 4. UI 绘制逻辑 
# ==========================================
def draw_ui_skeleton():
    tft.fill(BLACK)
    tft.fill_rect(0, 190, 320, 1, GRAY)
    update_top_bar()

def update_top_bar():
    global last_minute, last_sd_status_drawn
    tft.fill_rect(0, 0, 320, 24, 0x01CF) 
    
    tft.draw_gbk(current_sd_status.encode(), 5, 4, WHITE, 0x01CF)
    last_sd_status_drawn = current_sd_status 
    
    t_str = rtc.get_time_str(show_seconds=False)
    tft.draw_gbk(t_str.encode(), 135, 4, YELLOW, 0x01CF)
    try: last_minute = int(t_str.split(':')[1])
    except: pass
    
    tft.draw_gbk(current_status, 220, 4, current_status_color, 0x01CF)

def draw_hardware_bar(force=False):
    global last_hw_str_drawn
    v, p = get_battery_info()
    r = receiver.get_rssi()
    t = f"{27 - (sensor_temp.read_u16()*(3.3/65535)-0.706)/0.001721:.1f}C"
    new_str = f"BAT:{v} {p} | RSSI:{r} | T:{t}"

    if force or new_str != last_hw_str_drawn:
        tft.fill_rect(0, 215, 320, 25, BLACK)
        tft.draw_gbk(new_str.encode(), 5, 218, GRAY, BLACK)
        last_hw_str_drawn = new_str

def draw_idle_screen():
    tft.fill_rect(0, 26, 320, 164, BLACK)
    tft.draw_gbk(b'WAITING FOR SIGNAL', 15, 95, GRAY, BLACK, scale=2)

def display_train_data(basic, ext, is_full_mode=True, is_history=False, hist_time="", hist_idx=0):
    bg_color = 0x1082 if is_history else BLACK 
    tft.fill_rect(0, 26, 320, 164, bg_color)
    
    if is_history:
        header = f"HISTORY [{hist_idx+1}/{total_count}]  {hist_time}"
        tft.draw_gbk(header.encode(), 5, 30, YELLOW, bg_color, scale=1)
        y_offset = 20
    else:
        y_offset = 0

    train_no = basic.get('train_no', '---')
    speed = str(basic.get('speed_kmh', '---'))
    km = str(basic.get('km_post', '---'))
    cls = ext.get('class_tag', '') if ext.get('class_tag') != '?' else ''
    full_train = f"{cls}{train_no}"

    if len(full_train) > 8: full_train = full_train[:8]

    if not is_full_mode:
        sc = 2 if is_history else 3
        y_start = 55 if is_history else 35
        y_step = 40 if is_history else 50
        tft.draw_gbk(LBL_TRAIN + full_train.encode(), 20, y_start, CYAN, bg_color, scale=sc)
        tft.draw_gbk(LBL_SPEED + speed.encode() + b' K/H', 20, y_start+y_step, YELLOW, bg_color, scale=sc)
        tft.draw_gbk(LBL_KM + km.encode() + b' K', 20, y_start+y_step*2, GREEN, bg_color, scale=sc)
    else:
        tft.draw_gbk(LBL_TRAIN + full_train.encode(), 5, 35+y_offset, CYAN, bg_color, scale=2)
        tft.draw_gbk(LBL_SPEED + speed.encode() + b'K', 170, 35+y_offset, YELLOW, bg_color, scale=2)
        route_hex = ext.get('route_hex', '')
        route_b = bytes.fromhex(route_hex)[:8] if route_hex else b'----'
        tft.draw_gbk(LBL_ROUTE + route_b, 5, 80+y_offset, WHITE, bg_color, scale=2)
        
        digits = [c for c in str(train_no) if c.isdigit()]
        direction = b'\xc9\xcf' if digits and int(digits[-1]) % 2 == 0 else b'\xcf\xc2'
        tft.draw_gbk(direction, 180, 80+y_offset, MAGENTA, bg_color, scale=2)
        tft.draw_gbk(km.encode() + b'K', 220, 80+y_offset, GREEN, bg_color, scale=2)

        loco = ext.get('loco_type', '----')
        cab = ext.get('cab_end', '')
        if cab == '31': loco += 'A'
        elif cab == '32': loco += 'B'
        tft.draw_gbk(LBL_LOCO + loco.encode(), 5, 125+y_offset, WHITE, bg_color, scale=2)

    if not is_history: 
        lon = ext.get('lon', '---').replace('°', ' ')
        lat = ext.get('lat', '---').replace('°', ' ')
        tft.fill_rect(0, 192, 320, 18, BLACK)
        tft.draw_gbk(b'GPS: ' + lon.encode() + b' / ' + lat.encode(), 5, 195, GRAY, BLACK, scale=1)

def draw_menu(full=True, old_idx=-1):
    if full: 
        tft.fill_rect(0, 26, 320, 164, 0x2104)
        tft.draw_gbk(b'--- SYSTEM MENU ---', 80, 40, CYAN, 0x2104)
        for i in range(len(menu_items)):
            draw_menu_item(i, i == menu_index)
    else: 
        if old_idx >= 0: 
            draw_menu_item(old_idx, False) 
        draw_menu_item(menu_index, True)   

def draw_menu_item(i, is_selected):
    color = YELLOW if is_selected else WHITE
    prefix = b'> ' if is_selected else b'  '
    tft.fill_rect(40, 60 + i*16, 240, 16, 0x2104) # 把间距从20压缩到16，防止菜单项太多超出屏幕
    tft.draw_gbk(prefix + menu_items[i].encode(), 40, 60 + i*16, color, 0x2104)

def draw_set_date():
    tft.fill_rect(0, 26, 320, 164, 0x2104)
    tft.draw_gbk(b'--- SET DATE ---', 95, 40, CYAN, 0x2104)
    cols = [YELLOW if edit_step == i else WHITE for i in range(3)]
    tft.draw_gbk(f"20{edit_y:02}".encode(), 70, 90, cols[0], 0x2104, scale=2)
    tft.draw_gbk(b'-', 134, 90, WHITE, 0x2104, scale=2)
    tft.draw_gbk(f"{edit_m:02}".encode(), 150, 90, cols[1], 0x2104, scale=2)
    tft.draw_gbk(b'-', 182, 90, WHITE, 0x2104, scale=2)
    tft.draw_gbk(f"{edit_d:02}".encode(), 198, 90, cols[2], 0x2104, scale=2)
    tft.draw_gbk(b'[UP/DOWN]\xb5\xf7\xd5\xfb  [OK]\xc8\xb7\xc8\xcf', 20, 155, GRAY, 0x2104, scale=1)

def draw_jump_id():
    tft.fill_rect(0, 26, 320, 164, 0x2104)
    tft.draw_gbk(b'--- JUMP TO ID ---', 85, 40, CYAN, 0x2104)
    tft.draw_gbk(b'RANGE: 0001 -', 60, 75, GRAY, 0x2104)
    tft.draw_gbk(str(total_count).encode(), 170, 75, GREEN, 0x2104)
    for i in range(4):
        color = YELLOW if edit_step == i else WHITE
        tft.draw_gbk(str(edit_id[i]).encode(), 110 + i*25, 110, color, 0x2104, scale=2)

def draw_confirm_format():
    tft.fill_rect(0, 26, 320, 164, 0x5000)
    tft.draw_gbk(b'!!! WARNING !!!', 85, 50, WHITE, 0x5000, scale=2)
    tft.draw_gbk(b'DELETE ALL FLASH DATA?', 40, 90, YELLOW, 0x5000)
    tft.draw_gbk(b'[OK] TO CONFIRM  [MENU] TO CANCEL', 15, 140, WHITE, 0x5000)

# ★ 新增：SD 卡格式化警告页面
def draw_confirm_format_sd():
    tft.fill_rect(0, 26, 320, 164, 0x5000)
    tft.draw_gbk(b'!!! SD WARNING !!!', 15, 50, WHITE, 0x5000, scale=2)
    tft.draw_gbk(b'ERASE ALL SD CARD DATA?', 65, 90, YELLOW, 0x5000)
    tft.draw_gbk(b'[OK] TO CONFIRM  [MENU] TO CANCEL', 15, 140, WHITE, 0x5000)

def draw_about():
    tft.fill_rect(0, 26, 320, 164, 0x2104)
    tft.draw_gbk(b'--- ABOUT DEVICE ---', 75, 40, CYAN, 0x2104)
    
    es_tag = " (ES)" if is_es_ver == 1 else " (Rel)"
    ver_str = "Version: v" + str(Program_ver) + es_tag
    ver_color = RED if is_es_ver == 1 else WHITE
    tft.draw_gbk(ver_str.encode(), 40, 70, ver_color, 0x2104)
    
    rec_str = "Records: {}/2000".format(total_count)
    tft.draw_gbk(rec_str.encode(), 40, 95, WHITE, 0x2104)
    
    auth_str = "Author: " + Author_Name
    tft.draw_gbk(auth_str.encode(), 40, 120, YELLOW, 0x2104)
    
    tft.draw_gbk(b'HW Rev: Pico-W-SPI-v1.1', 40, 145, WHITE, 0x2104)
    tft.draw_gbk(b'Press OK to Return', 40, 175, GRAY, 0x2104)

def draw_popup(msg, color=RED):
    tft.fill_rect(60, 80, 200, 60, color)
    tft.draw_gbk(msg, 75, 100, WHITE, color)

# ==========================================
# 5. 状态机跳转与回调
# ==========================================
def on_lbj_data_received(data):
    global last_basic, last_ext, last_is_full, has_received, current_status, current_status_color
    try:
        msg_type = data.get("type")
        if msg_type == "time_sync":
            hh, mm = map(int, data.get('time').split(':'))
            if 0 <= hh < 24 and 0 <= mm < 60:
                rtc.sync_time(hh, mm)
                if system_state == "DASHBOARD": 
                    current_status, current_status_color = b'TIME SYNC', YELLOW; update_top_bar()
        elif "train_data" in msg_type or "only" in msg_type:
            beep(0.05); has_received = True
            save_history(data); log_to_sd(data)
            last_basic, last_ext = data.get("basic", {}), data.get("extended", {})
            last_is_full = (msg_type != "basic_only")
            if system_state == "DASHBOARD":
                current_status, current_status_color = (b'FULL DATA', GREEN) if last_is_full else (b'BASIC', YELLOW)
                current_status = current_status[:8]
                update_top_bar(); display_train_data(last_basic, last_ext, last_is_full)
                draw_hardware_bar(force=True) 
    except: pass

# ==========================================
# 6. 主循环与界面分发
# ==========================================
load_config() 
init_history()
check_sd() 

receiver = LBJReceiver(); receiver.set_callback(on_lbj_data_received)

if boot_status == "RTC_SYNC":
    system_state = "SET_DATE"
    edit_step = 0
    try:
        raw_d = i2c0.readfrom_mem(0x68, 0x04, 3)
        edit_d, edit_m, edit_y = [(r >> 4) * 10 + (r & 0x0F) for r in raw_d]
    except: pass
    draw_ui_skeleton() 
    draw_set_date()    
else:
    draw_ui_skeleton()
    draw_idle_screen()
    draw_hardware_bar(force=True)

last_sec = time.ticks_ms()
heartbeat = False

while True:
    receiver.tick(); now = time.ticks_ms()
    
    if time.ticks_diff(now, last_sec) > 1000:
        if system_state == "DASHBOARD": 
            heartbeat = not heartbeat
            tft.fill_rect(310, 8, 6, 6, GREEN if heartbeat else 0x01CF)
            
            t_str = rtc.get_time_str(show_seconds=False)
            try:
                now_min = int(t_str.split(':')[1])
                if now_min != last_minute:
                    tft.fill_rect(135, 4, 60, 16, 0x01CF) 
                    tft.draw_gbk(t_str.encode(), 135, 4, YELLOW, 0x01CF)
                    last_minute = now_min
            except: pass
            
        if int(now / 1000) % 2 == 0: 
            check_sd()
            if system_state == "DASHBOARD" and not has_received: 
                draw_hardware_bar(force=False) 
        last_sec = now

    if system_state == "HISTORY" and time.ticks_diff(now, last_interaction) > 20000:
        system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True); last_interaction = now
        if has_received: display_train_data(last_basic, last_ext, last_is_full)
        else: draw_idle_screen()

    if not btn_menu.value():
        last_interaction = now; beep()
        # ★ 记得要把 CONFIRM_FORMAT_SD 也加入按 MENU 可以取消的列表
        if system_state in ["DASHBOARD", "HISTORY", "ABOUT", "CONFIRM_FORMAT", "CONFIRM_FORMAT_SD", "SET_DATE", "JUMP_ID"]:
            system_state = "MENU"; draw_menu(full=True) 
        else: 
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
            if has_received: display_train_data(last_basic, last_ext, last_is_full)
            else: draw_idle_screen()
        time.sleep_ms(60)
            
    if not btn_up.value():
        last_interaction = now; beep()
        if system_state == "DASHBOARD" and total_count > 0:
            system_state = "HISTORY"; hist_ptr = total_count - 1; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "HISTORY":
            hist_ptr = (hist_ptr - 1) % total_count; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "MENU": 
            old_idx = menu_index
            menu_index = (menu_index - 1) % len(menu_items)
            draw_menu(full=False, old_idx=old_idx)
        elif system_state == "SET_DATE":
            if edit_step == 0: edit_y = (edit_y+1)%100; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            elif edit_step == 1: edit_m = edit_m%12+1; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            else: edit_d = (edit_d%get_max_days(edit_y, edit_m))+1
            draw_set_date()
        elif system_state == "JUMP_ID":
            edit_id[edit_step] = (edit_id[edit_step]+1)%10; draw_jump_id()
        time.sleep_ms(40)

    if not btn_down.value():
        last_interaction = now; beep()
        if system_state == "DASHBOARD" and total_count > 0:
            system_state = "HISTORY"; hist_ptr = total_count - 1; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "HISTORY":
            hist_ptr = (hist_ptr + 1) % total_count; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "MENU": 
            old_idx = menu_index
            menu_index = (menu_index + 1) % len(menu_items)
            draw_menu(full=False, old_idx=old_idx)
        elif system_state == "SET_DATE":
            if edit_step == 0: edit_y = (edit_y-1)%100; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            elif edit_step == 1: edit_m = edit_m-1 if edit_m>1 else 12; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            else: edit_d = edit_d-1 if edit_d>1 else get_max_days(edit_y, edit_m)
            draw_set_date()
        elif system_state == "JUMP_ID":
            edit_id[edit_step] = (edit_id[edit_step]-1)%10; draw_jump_id()
        time.sleep_ms(40)

    if not btn_ok.value():
        last_interaction = now; beep(0.04)
        if system_state == "HISTORY" or system_state == "ABOUT":
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
            if has_received: display_train_data(last_basic, last_ext, last_is_full)
            else: draw_idle_screen()
            
        elif system_state == "MENU":
            # 0. BUZZER
            if menu_index == 0: 
                cfg_buzzer = not cfg_buzzer; menu_items[0] = f"BUZZER: {'ON' if cfg_buzzer else 'OFF'}"
                save_config(); draw_menu(full=True)
            # 1. SET DATE
            elif menu_index == 1: 
                try:
                    raw_d = i2c0.readfrom_mem(0x68, 0x04, 3)
                    edit_d, edit_m, edit_y = [(r >> 4) * 10 + (r & 0x0F) for r in raw_d]
                except: pass
                edit_step = 0; system_state = "SET_DATE"; draw_set_date()
            # 2. JUMP TO ID
            elif menu_index == 2: 
                edit_step = 0; edit_id = [0,0,0,0]; system_state = "JUMP_ID"; draw_jump_id()
            # 3. FORMAT FLASH
            elif menu_index == 3: 
                system_state = "CONFIRM_FORMAT"; draw_confirm_format()
            # 4. ★ 新增：FORMAT SD
            elif menu_index == 4: 
                if current_sd_status == "SD NO INSERT":
                    draw_popup(b'NO SD CARD!', color=RED)
                    time.sleep(1)
                    draw_menu(full=True)
                else:
                    system_state = "CONFIRM_FORMAT_SD"; draw_confirm_format_sd()
            # 5. SAFE EJECT SD
            elif menu_index == 5: 
                draw_popup(b'UNMOUNTING...', color=YELLOW)
                try: os.umount("/sd")
                except: pass
                sd_obj = None
                sd_eject_mode = True
                current_sd_status = "SAFE TO REMOVE"
                time.sleep(1)
                system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
                if has_received: display_train_data(last_basic, last_ext, last_is_full)
                else: draw_idle_screen()
            # 6. ABOUT
            elif menu_index == 6: 
                system_state = "ABOUT"; draw_about()
            # 7. EXIT
            elif menu_index == 7: 
                system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
                if has_received: display_train_data(last_basic, last_ext, last_is_full)
                else: draw_idle_screen()
                
        elif system_state == "SET_DATE":
            edit_step += 1
            if edit_step > 2: 
                rtc.set_date(edit_y, edit_m, edit_d)
                system_state = "MENU"; draw_menu(full=True) 
            else: 
                draw_set_date()
                
        elif system_state == "JUMP_ID":
            edit_step += 1
            if edit_step > 3:
                target_id = edit_id[0]*1000 + edit_id[1]*100 + edit_id[2]*10 + edit_id[3] - 1
                if 0 <= target_id < total_count:
                    system_state = "HISTORY"; hist_ptr = target_id; entry = load_history_entry(hist_ptr)
                    display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
                else: draw_popup(b'INDEX ERROR!'); time.sleep(1); draw_jump_id(); edit_step = 0
            else: draw_jump_id()
            
        elif system_state == "CONFIRM_FORMAT":
            draw_popup(b'FORMATTING...', color=GREEN); open(HIST_FILE, 'w').close()
            total_count = 0; hist_ptr = -1; history_offsets.clear(); time.sleep(1)
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)

       # ★ 新增：执行 SD 卡原生极速格式化
        elif system_state == "CONFIRM_FORMAT_SD":
            draw_popup(b'FORMATTING SD...', color=YELLOW)
            try:
                # 1. 强行卸载
                if 'sd' in os.listdir('/'): 
                    try: os.umount("/sd")
                    except: pass
                tft_cs.value(1)
                
                # 2. 用 1MHz 的标准速度进行初始安全握手
                spi1.init(baudrate=1000000) 
                sd_obj = sdcard.SDCard(spi1, sd_cs)
                
                # 3. ★ 握手成功后，狂飙到 10MHz 进行极速擦写！
                spi1.init(baudrate=10000000) 
                os.VfsFat.mkfs(sd_obj) 
                
                # 4. 重新挂载
                os.mount(os.VfsFat(sd_obj), "/sd")
                draw_popup(b'SD FORMAT OK!', color=GREEN)
            except Exception as e:
                sd_obj = None # 如果格式化失败，销毁对象
                draw_popup(b'FORMAT FAIL!', color=RED)
            
            # 切回屏幕专属的 40MHz 极限速度
            spi1.init(baudrate=40000000) 
            time.sleep(1)
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)