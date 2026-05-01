import time
import json
import machine
import os
import gc
import sdcard
import _thread  
import array  
from machine import Pin, ADC, I2C
from lbj_receiver import LBJReceiver
from ili9341 import ILI9341, BLACK, WHITE, RED, GREEN, BLUE, CYAN, YELLOW, GRAY, MAGENTA
from rtc_ds3231 import DS3231
from boot_post import SystemPOST 

# 系统性能配置与时钟加固

pin_bl = Pin(6, Pin.OUT, value=0)
machine.freq(240000000) # 超频
time.sleep_ms(200) 
last_gc = 0
Program_ver = 3.2 
is_es_ver = 0 
Author_Name = "MisakaXing"
Serial_Number = "N/A"
BAT_OFFSET = 0.174 
DEBUG_MODE = False

ui_queue = [] 
ui_lock = _thread.allocate_lock() 

last_hw_update = 0  
last_rssi_str = "N/A" 
screen_is_on = True 

# 1. 硬件 IO 初始化

tft_cs = Pin(9, Pin.OUT, value=1) 
spi1 = machine.SPI(1, baudrate=20000000, sck=Pin(10), mosi=Pin(11), miso=Pin(8, Pin.IN, Pin.PULL_UP))
tft = ILI9341(spi1, cs=9, dc=12, rst=13)
spi1.init(baudrate=80000000) # 80MHz SPI 速度

sd_cs = Pin(7, Pin.OUT, value=1)
bat_en = Pin(14, Pin.OUT, value=1)
bat_adc = ADC(Pin(27)) 
buzzer = Pin(22, Pin.OUT, value=0)

i2c0 = I2C(0, sda=Pin(0), scl=Pin(1), freq=400000)
rtc = DS3231(i2c0)

btn_menu, btn_up, btn_down, btn_ok = [Pin(i, Pin.IN, Pin.PULL_UP) for i in (2, 3, 4, 5)]
sensor_temp = machine.ADC(4)

# 核心切片渲染函数
def safe_fill_rect(x, y, w, h, color, slice_h=15):
    current_y = y
    remain_h = h
    while remain_h > 0:
        step = min(slice_h, remain_h)
        tft.fill_rect(x, current_y, w, step, color)
        time.sleep_ms(1)  
        current_y += step
        remain_h -= step

safe_fill_rect(0, 0, 320, 240, BLACK)

# 2. 系统全局变量

MAX_HIST = 2000
HIST_FILE = "history.jsonl"
SD_LOG_FILE = "/sd/lbj_log.jsonl"
CONFIG_FILE = "config.json"

system_state = "DASHBOARD" 
has_received = False
menu_index = 0

cfg_scr_idx = 3 
SCR_OFF_OPTS = ["30s", "1min", "5min", "never"]
SCR_OFF_MS = [30000, 60000, 300000, -1]
cfg_buzzer = True

menu_items = ["BUZZER: ON", "SET DATE", "JUMP TO ID", "FORMAT FLASH", "FORMAT SD", "MOUNT SD", "ABOUT DEV", f"SCREEN OFF AFTER: {SCR_OFF_OPTS[cfg_scr_idx]}"]

hist_ptr = -1
total_count = 0

history_offsets = array.array('I') 
last_interaction = time.ticks_ms()

last_minute = -1 
last_sd_status_drawn = ""
last_hw_str_drawn = ""
last_mem_print = 0
last_sd_err_time = 0

sd_active = False    
sd_obj = None
current_sd_status = "SD NO INSERT" 

edit_y, edit_m, edit_d = 24, 1, 1
edit_id = [0, 0, 0, 0] 
edit_step = 0 

current_status = b'READY'
current_status_color = GREEN

last_basic, last_ext = {}, {}
last_is_full = True

need_post_train_gc = False 
last_screen_layout = None

# 3. 核心功能函数
def beep(duration=0.02):
    if cfg_buzzer: buzzer.value(1); time.sleep(duration); buzzer.value(0)

def get_max_days(y, m):
    if m == 2: return 29 if y % 4 == 0 else 28
    return 30 if m in [4, 6, 9, 11] else 31

def get_battery_info():
    bat_en.value(0); time.sleep_ms(5)
    raw = bat_adc.read_u16()
    bat_en.value(1)
    raw_volts = (raw / 65535) * 3.3 * 2
    volts = raw_volts + BAT_OFFSET
    percent = int((volts - 3.4) / (4.2 - 3.4) * 100)
    return f"{volts:.1f}V", f"{max(0, min(100, percent))}%"

def load_config():
    global cfg_buzzer, cfg_scr_idx, menu_items
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.loads(f.read())
            cfg_buzzer = config.get("buzzer", True) 
            cfg_scr_idx = config.get("scr_idx", 3)
            menu_items[0] = f"BUZZER: {'ON' if cfg_buzzer else 'OFF'}"
            menu_items[7] = f"SCREEN OFF AFTER: {SCR_OFF_OPTS[cfg_scr_idx]}"
    except: pass 

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f: f.write(json.dumps({"buzzer": cfg_buzzer, "scr_idx": cfg_scr_idx}))
    except: pass

def init_history():
    global total_count, history_offsets
    history_offsets = array.array('I')
    try:
        with open(HIST_FILE, 'r') as f:
            while True:
                offset = f.tell()      
                line = f.readline()    
                if not line: break     
                history_offsets.append(offset)
        total_count = len(history_offsets)
    except: 
        total_count = 0; history_offsets = array.array('I')

def save_history(data):
    global total_count, history_offsets
    if total_count >= MAX_HIST: return 
    try:
        time.sleep_ms(1) 
        t_str = rtc.get_time_str(True)
        time.sleep_ms(1) 
        record = {"t": t_str, "d": data}
        json_str = json.dumps(record) 
        time.sleep_ms(1) 
        with open(HIST_FILE, 'a') as f:
            f.seek(0, 2); offset = f.tell() 
            f.write(json_str + '\n')
            history_offsets.append(offset) 
        total_count += 1
        time.sleep_ms(1) 
    except: pass

def load_history_entry(idx):
    if idx < 0 or idx >= len(history_offsets): return None
    try:
        with open(HIST_FILE, 'r') as f:
            f.seek(history_offsets[idx]); line = f.readline()
            return json.loads(line)
    except: return None

def check_sd_startup():
    global current_sd_status, sd_active, sd_obj, menu_items
    try:
        tft_cs.value(1)
        spi1.init(baudrate=1000000) 
        sd_obj = sdcard.SDCard(spi1, sd_cs)
        os.mount(os.VfsFat(sd_obj), "/sd")
        s = os.statvfs("/sd")
        total_kb = (s[0] * s[2]) / 1024
        free_kb = (s[0] * s[3]) / 1024
        used_kb = total_kb - free_kb
        if total_kb > 1048576: current_sd_status = f"SD:{used_kb/1048576:.1f}/{total_kb/1048576:.1f}G"
        else: current_sd_status = f"SD:{used_kb/1024:.1f}/{total_kb/1024:.1f}M"
        sd_active = True
    except:
        sd_active = False; sd_obj = None; current_sd_status = "SD NO INSERT"
    finally:
        menu_items[5] = "EJECT SD" if sd_active else "MOUNT SD"
        spi1.init(baudrate=80000000)

def disable_sd_forever(reason):
    global sd_active, current_sd_status, sd_obj, menu_items, last_sd_err_time
    try: os.umount("/sd")
    except: pass
    sd_active = False
    sd_obj = None
    current_sd_status = reason
    last_sd_err_time = time.ticks_ms()
    menu_items[5] = "MOUNT SD"
    if system_state == "DASHBOARD": update_top_bar()
    elif system_state == "MENU": draw_menu(full=True)

def log_to_sd(data):
    if not sd_active: return 
    try:
        time.sleep_ms(1) 
        t_str = rtc.get_time_str(True)
        time.sleep_ms(1)
        j_str = json.dumps({"t": t_str, "d": data}) + '\n'
        time.sleep_ms(1)
        tft_cs.value(1); spi1.init(baudrate=5000000)
        with open(SD_LOG_FILE, 'a') as f:
            f.write(j_str)
        time.sleep_ms(1)
    except:
        disable_sd_forever("SD WRITE ERR")
    finally:
        spi1.init(baudrate=80000000)

# 4. UI 绘制函数 

def draw_ui_skeleton():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 0, 320, 240, BLACK) 
    tft.fill_rect(0, 190, 320, 1, GRAY)
    tft.draw_gbk(b"BAT:", 5, 218, GRAY, BLACK)
    tft.draw_gbk(b"RSSI:", 120, 218, GRAY, BLACK)
    tft.draw_gbk(b"T:", 245, 218, GRAY, BLACK)
    time.sleep_ms(1) 
    update_top_bar()

def update_top_bar():
    global last_minute, last_sd_status_drawn
    tft.fill_rect(0, 0, 320, 24, 0x01CF) 
    time.sleep_ms(1) 
    
    tft.draw_gbk(current_sd_status.encode(), 5, 4, WHITE, 0x01CF)
    last_sd_status_drawn = current_sd_status 
    time.sleep_ms(1) 
    
    t_str = rtc.get_time_str(show_seconds=False)
    tft.draw_gbk(t_str.encode(), 135, 4, YELLOW, 0x01CF)
    try: last_minute = int(t_str.split(':')[1])
    except: pass
    time.sleep_ms(1) 
    
    tft.draw_gbk(current_status, 220, 4, current_status_color, 0x01CF)
    time.sleep_ms(1) 

def draw_hardware_bar(force=False):
    global last_hw_update, last_rssi_str
    now = time.ticks_ms()
    if not force and time.ticks_diff(now, last_hw_update) < 30000: return
    
    v, p = get_battery_info()
    r = last_rssi_str
    t = f"{27 - (sensor_temp.read_u16()*(3.3/65535)-0.706)/0.001721:.1f}C"
    
    raw_p = int(p.replace('%', ''))
    bat_color = RED if raw_p < 20 else WHITE
    
    tft.fill_rect(45, 218, 70, 16, BLACK)
    tft.draw_gbk(f"{v} {p}".encode(), 45, 218, bat_color, BLACK) 
    
    tft.fill_rect(170, 218, 70, 16, BLACK)
    tft.draw_gbk(r.encode(), 170, 218, WHITE, BLACK)
    
    tft.fill_rect(265, 218, 50, 16, BLACK)
    tft.draw_gbk(t.encode(), 265, 218, WHITE, BLACK)

    last_hw_update = now
    time.sleep_ms(1)

def draw_idle_screen():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, BLACK) 
    tft.draw_gbk(b'WAITING FOR SIGNAL', 15, 95, GRAY, BLACK, scale=2)
    time.sleep_ms(1) 

def display_train_data(basic, ext, is_full_mode=True, is_history=False, hist_time="", hist_idx=0):
    global last_screen_layout
    
    current_layout = f"{'HIST' if is_history else 'DASH'}_{'FULL' if is_full_mode else 'BASIC'}"
    bg_color = 0x1082 if is_history else BLACK 
    is_partial = (last_screen_layout == current_layout)

    if not is_partial:
        safe_fill_rect(0, 26, 320, 164, bg_color)

    if is_history:
        if is_partial: tft.fill_rect(0, 30, 320, 16, bg_color) 
        header = f"HISTORY [{hist_idx+1}/{total_count}]  {hist_time}"
        tft.draw_gbk(header.encode(), 5, 30, YELLOW, bg_color, scale=1)
        if not is_partial: time.sleep_ms(1)
        y_offset = 20
    else: y_offset = 0

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
        lbl_w = 48 if sc == 2 else 72  
        h = 16 * sc
        
        if not is_partial:
            tft.draw_gbk(b'\xb3\xb5:', 20, y_start, WHITE, bg_color, scale=sc) 
            tft.draw_gbk(b'\xcb\xd9:', 20, y_start+y_step, WHITE, bg_color, scale=sc) 
            tft.draw_gbk(b'\xb1\xea:', 20, y_start+y_step*2, WHITE, bg_color, scale=sc) 
        else:
            tft.fill_rect(20+lbl_w, y_start, 300-lbl_w, h, bg_color)
            tft.fill_rect(20+lbl_w, y_start+y_step, 300-lbl_w, h, bg_color)
            tft.fill_rect(20+lbl_w, y_start+y_step*2, 300-lbl_w, h, bg_color)

        tft.draw_gbk(full_train.encode(), 20+lbl_w, y_start, CYAN, bg_color, scale=sc)
        tft.draw_gbk(speed.encode() + b' K/H', 20+lbl_w, y_start+y_step, YELLOW, bg_color, scale=sc)
        tft.draw_gbk(km.encode() + b' K', 20+lbl_w, y_start+y_step*2, GREEN, bg_color, scale=sc)

    else:
        y1 = 35 + y_offset
        y2 = 80 + y_offset
        y3 = 125 + y_offset

        if not is_partial:
            tft.draw_gbk(b'\xb3\xb5:', 5, y1, WHITE, bg_color, scale=2)   
            tft.draw_gbk(b'\xcb\xd9:', 170, y1, WHITE, bg_color, scale=2) 
            tft.draw_gbk(b'\xcf\xdf:', 5, y2, WHITE, bg_color, scale=2)   
            tft.draw_gbk(b'\xbb\xfa:', 5, y3, WHITE, bg_color, scale=2)   
        else:
            tft.fill_rect(53, y1, 115, 32, bg_color) 
            tft.fill_rect(218, y1, 102, 32, bg_color) 
            tft.fill_rect(53, y2, 127, 32, bg_color) 
            tft.fill_rect(180, y2, 140, 32, bg_color) 
            tft.fill_rect(53, y3, 267, 32, bg_color) 

        tft.draw_gbk(full_train.encode(), 53, y1, CYAN, bg_color, scale=2)
        tft.draw_gbk(speed.encode() + b'K', 218, y1, YELLOW, bg_color, scale=2)

        route_hex = ext.get('route_hex', '')
        route_b = bytes.fromhex(route_hex)[:8] if route_hex else b'----'
        tft.draw_gbk(route_b, 53, y2, WHITE, bg_color, scale=2)

        digits = [c for c in str(train_no) if c.isdigit()]
        direction = b'\xc9\xcf' if digits and int(digits[-1]) % 2 == 0 else b'\xcf\xc2'
        tft.draw_gbk(direction, 180, y2, MAGENTA, bg_color, scale=2)

        tft.draw_gbk(km.encode() + b'K', 220, y2, GREEN, bg_color, scale=2)

        loco = ext.get('loco_type', '----')
        cab = ext.get('cab_end', '')
        if cab == '31': loco += 'A'
        elif cab == '32': loco += 'B'
        tft.draw_gbk(loco.encode(), 53, y3, WHITE, bg_color, scale=2)

    if not is_history: 
        lon = ext.get('lon', '---').replace('°', ' ')
        lat = ext.get('lat', '---').replace('°', ' ')
        tft.fill_rect(0, 192, 320, 18, BLACK) 
        if not is_partial: time.sleep_ms(1) 
        tft.draw_gbk(b'GPS: ' + lon.encode() + b' / ' + lat.encode(), 5, 195, GRAY, BLACK, scale=1)
        
    last_screen_layout = current_layout

def draw_menu(full=True, old_idx=-1):
    global last_screen_layout
    if full: 
        last_screen_layout = None 
        safe_fill_rect(0, 26, 320, 164, 0x2104) 
        tft.draw_gbk(b'--- SYSTEM MENU ---', 80, 40, CYAN, 0x2104)
        time.sleep_ms(1) 
        for i in range(len(menu_items)): 
            draw_menu_item(i, i == menu_index)
            time.sleep_ms(1) 
    else: 
        if old_idx >= 0: 
            draw_menu_item(old_idx, False) 
            time.sleep_ms(1) 
        draw_menu_item(menu_index, True)   
        time.sleep_ms(1) 

def draw_menu_item(i, is_selected):
    color = YELLOW if is_selected else WHITE
    prefix = b'> ' if is_selected else b'  '
    tft.fill_rect(40, 60 + i*16, 240, 16, 0x2104)
    tft.draw_gbk(prefix + menu_items[i].encode(), 40, 60 + i*16, color, 0x2104)

def draw_set_date(full=True):
    global last_screen_layout
    if full:
        last_screen_layout = None 
        safe_fill_rect(0, 26, 320, 164, 0x2104)
        tft.draw_gbk(b'--- SET DATE ---', 95, 40, CYAN, 0x2104)
        tft.draw_gbk(b'-', 134, 90, WHITE, 0x2104, scale=2)
        tft.draw_gbk(b'-', 182, 90, WHITE, 0x2104, scale=2)
        tft.draw_gbk(b'[UP/DOWN]\xb5\xf7\xd5\xfb  [OK]\xc8\xb7\xc8\xcf', 20, 155, GRAY, 0x2104, scale=1)
        time.sleep_ms(1)
        
    cols = [YELLOW if edit_step == i else WHITE for i in range(3)]
    
    tft.fill_rect(70, 90, 64, 32, 0x2104)
    tft.fill_rect(150, 90, 32, 32, 0x2104)
    tft.fill_rect(198, 90, 32, 32, 0x2104)
    
    tft.draw_gbk(f"20{edit_y:02}".encode(), 70, 90, cols[0], 0x2104, scale=2)
    tft.draw_gbk(f"{edit_m:02}".encode(), 150, 90, cols[1], 0x2104, scale=2)
    tft.draw_gbk(f"{edit_d:02}".encode(), 198, 90, cols[2], 0x2104, scale=2)
    time.sleep_ms(1)

def draw_jump_id(full=True):
    global last_screen_layout
    if full:
        last_screen_layout = None 
        safe_fill_rect(0, 26, 320, 164, 0x2104)
        tft.draw_gbk(b'--- JUMP TO ID ---', 85, 40, CYAN, 0x2104)
        tft.draw_gbk(b'RANGE: 0001 -', 60, 75, GRAY, 0x2104)
        tft.draw_gbk(str(total_count).encode(), 170, 75, GREEN, 0x2104)
        time.sleep_ms(1)
        
    for i in range(4):
        color = YELLOW if edit_step == i else WHITE
        tft.fill_rect(110 + i*25, 110, 16, 32, 0x2104)
        tft.draw_gbk(str(edit_id[i]).encode(), 110 + i*25, 110, color, 0x2104, scale=2)
        time.sleep_ms(20) 

def draw_confirm_format():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, 0x5000)
    tft.draw_gbk(b'!!! WARNING !!!', 35, 50, WHITE, 0x5000, scale=2)
    time.sleep_ms(1)
    tft.draw_gbk(b'DELETE ALL FLASH DATA?', 60, 90, YELLOW, 0x5000)
    tft.draw_gbk(b'[OK] TO CONFIRM  [MENU] TO CANCEL', 30, 140, WHITE, 0x5000)
    time.sleep_ms(1)

def draw_confirm_format_sd():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, 0x5000)
    tft.draw_gbk(b'!!! SD WARNING !!!', 15, 50, WHITE, 0x5000, scale=2)
    time.sleep_ms(1)
    tft.draw_gbk(b'ERASE ALL SD CARD DATA?', 65, 90, YELLOW, 0x5000)
    tft.draw_gbk(b'[OK] TO CONFIRM  [MENU] TO CANCEL', 15, 140, WHITE, 0x5000)
    time.sleep_ms(1)

def draw_about():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, 0x2104)
    tft.draw_gbk(b'--- ABOUT DEVICE ---', 75, 40, CYAN, 0x2104)
    time.sleep_ms(1)
    es_tag = " (ES)" if is_es_ver == 1 else " (Rel)"
    tft.draw_gbk(f"Version: v{Program_ver}{es_tag}".encode(), 40, 70, RED if is_es_ver == 1 else WHITE, 0x2104)
    time.sleep_ms(1) 
    tft.draw_gbk(f"Records: {total_count}/2000".encode(), 40, 95, WHITE, 0x2104)
    time.sleep_ms(1) 
    tft.draw_gbk(b"Author: " + Author_Name.encode(), 40, 120, YELLOW, 0x2104)
    time.sleep_ms(1) 
    tft.draw_gbk(b"Serial Number: " + Serial_Number.encode(), 40, 145, WHITE, 0x2104)
    time.sleep_ms(1) 
    tft.draw_gbk(b'Press OK to Return', 40, 175, GRAY, 0x2104)

def draw_popup(msg, color=RED):
    tft.fill_rect(60, 80, 200, 60, color)
    time.sleep_ms(1) 
    tft.draw_gbk(msg, 75, 100, WHITE, color)
    time.sleep_ms(1)

# 5. 核心 1 子线程
def light_callback(data):
    with ui_lock:
        ui_queue.append(data)

def radio_core_task():
    import time
    while True:
        try:
            receiver.tick()
        except Exception:
            pass
        time.sleep_ms(1)

def process_ui_data(data):
    global last_basic, last_ext, last_is_full, has_received, current_status, current_status_color, last_rssi_str
    global screen_is_on, last_interaction, need_post_train_gc
    
    if not DEBUG_MODE:
        time.sleep_ms(60)

    try:
        msg_type = data.get("type")
        time.sleep_ms(1) 
        if msg_type == "time_sync":
            hh, mm = map(int, data.get('time').split(':'))
            if 0 <= hh < 24 and 0 <= mm < 60:
                rtc.sync_time(hh, mm)
                if system_state == "DASHBOARD": 
                    current_status, current_status_color = b'TIME SYNC', YELLOW
                    time.sleep_ms(1)
                    update_top_bar()
        elif "train_data" in msg_type or "only" in msg_type:
            
            # 正常接收的提示音
            beep(0.05) 
                
            has_received = True
            
            if not screen_is_on:
                pin_bl.value(0)
                screen_is_on = True
            last_interaction = time.ticks_ms()
            
            if "rssi" in data:
                last_rssi_str = str(data["rssi"])
                
            save_history(data)
            log_to_sd(data)
            time.sleep_ms(1) 
            
            last_basic, last_ext = data.get("basic", {}), data.get("extended", {})
            last_is_full = (msg_type != "basic_only")
            
            if system_state == "DASHBOARD":
                # 内存满了强制在右上角显示红色的 MEM FULL
                if total_count >= MAX_HIST:
                    current_status, current_status_color = b'MEM FULL', RED
                else:
                    current_status, current_status_color = (b'FULL DATA', GREEN) if last_is_full else (b'BASIC', YELLOW)
                    
                current_status = current_status[:8]
                time.sleep_ms(1)
                
                update_top_bar()
                display_train_data(last_basic, last_ext, last_is_full)
                draw_hardware_bar(force=True) 
                
            need_post_train_gc = True
    except: pass

# 6. 启动初始化

load_config() 
init_history()
check_sd_startup() 

post = SystemPOST(tft, tft_cs)
boot_status = post.run_all(bat_adc, bat_en, sensor_temp, rtc, spi1, sd_cs, buzzer, Program_ver, is_es_ver)
if boot_status == "HALT":
    while True: pass 

receiver = LBJReceiver()
receiver.set_callback(light_callback) 
_thread.start_new_thread(radio_core_task, ()) 

if boot_status == "RTC_SYNC":
    system_state = "SET_DATE"; edit_step = 0
    try:
        raw_d = i2c0.readfrom_mem(0x68, 0x04, 3)
        edit_d, edit_m, edit_y = [(r >> 4) * 10 + (r & 0x0F) for r in raw_d]
    except: pass
    draw_ui_skeleton(); draw_set_date(full=True)    
else:
    draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)

last_sec = time.ticks_ms()
heartbeat = False

# 7. 核心 0 主循环

while True:
    now = time.ticks_ms()
    
    if screen_is_on and cfg_scr_idx != 3: 
        if time.ticks_diff(now, last_interaction) > SCR_OFF_MS[cfg_scr_idx]:
            pin_bl.value(1) 
            screen_is_on = False
            
    if need_post_train_gc and time.ticks_diff(now, last_interaction) > 1000:
        gc.collect()
        need_post_train_gc = False

    ui_data_to_process = None
    with ui_lock:
        if len(ui_queue) > 0:
            ui_data_to_process = ui_queue.pop(0)

    if ui_data_to_process:
        try:
            process_ui_data(ui_data_to_process)
        except:
            pass 
            
    if gc.mem_free() < 20000:
        time.sleep_ms(1)
        gc.collect()
        time.sleep_ms(1)

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
            
            if not sd_active and current_sd_status != "SD NO INSERT" and time.ticks_diff(now, last_sd_err_time) > 3000:
                current_sd_status = "SD NO INSERT"
                update_top_bar() 
                
            if not has_received: 
                draw_hardware_bar(force=False) 
        last_sec = now

    if system_state == "HISTORY" and time.ticks_diff(now, last_interaction) > 20000:
        system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True); last_interaction = now
        if has_received: display_train_data(last_basic, last_ext, last_is_full)
        else: draw_idle_screen()

    # 按钮逻辑
    any_btn = not btn_menu.value() or not btn_up.value() or not btn_down.value() or not btn_ok.value()
    if any_btn:
        last_interaction = now
        if not screen_is_on:
            pin_bl.value(0) 
            screen_is_on = True
            time.sleep_ms(300) 
            continue 
            
    if not btn_menu.value():
        last_interaction = now; beep()
        if system_state in ["DASHBOARD", "HISTORY", "ABOUT", "CONFIRM_FORMAT", "CONFIRM_FORMAT_SD", "SET_DATE", "JUMP_ID"]:
            system_state = "MENU"; draw_menu(full=True) 
        else: 
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
            if has_received: display_train_data(last_basic, last_ext, last_is_full)
            else: draw_idle_screen()
        time.sleep_ms(60)
            
    if not btn_down.value():
        last_interaction = now; beep()
        if system_state == "DASHBOARD" and total_count > 0:
            system_state = "HISTORY"; hist_ptr = total_count - 1; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "HISTORY":
            hist_ptr = (hist_ptr - 1) % total_count; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "MENU": 
            old_idx = menu_index; menu_index = (menu_index - 1) % len(menu_items); draw_menu(full=False, old_idx=old_idx)
        elif system_state == "SET_DATE":
            if edit_step == 0: edit_y = (edit_y+1)%100; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            elif edit_step == 1: edit_m = edit_m%12+1; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            else: edit_d = (edit_d%get_max_days(edit_y, edit_m))+1
            draw_set_date(full=False)
        elif system_state == "JUMP_ID":
            edit_id[edit_step] = (edit_id[edit_step]+1)%10; draw_jump_id(full=False)
        time.sleep_ms(40)

    if not btn_up.value():
        last_interaction = now; beep()
        if system_state == "DASHBOARD" and total_count > 0:
            system_state = "HISTORY"; hist_ptr = total_count - 1; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "HISTORY":
            hist_ptr = (hist_ptr + 1) % total_count; entry = load_history_entry(hist_ptr)
            if entry: display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
        elif system_state == "MENU": 
            old_idx = menu_index; menu_index = (menu_index + 1) % len(menu_items); draw_menu(full=False, old_idx=old_idx)
        elif system_state == "SET_DATE":
            if edit_step == 0: edit_y = (edit_y-1)%100; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            elif edit_step == 1: edit_m = edit_m-1 if edit_m>1 else 12; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
            else: edit_d = edit_d-1 if edit_d>1 else get_max_days(edit_y, edit_m)
            draw_set_date(full=False)
        elif system_state == "JUMP_ID":
            edit_id[edit_step] = (edit_id[edit_step]-1)%10; draw_jump_id(full=False)
        time.sleep_ms(40)

    if not btn_ok.value():
        last_interaction = now; beep(0.04)
        if system_state == "HISTORY" or system_state == "ABOUT":
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
            if has_received: display_train_data(last_basic, last_ext, last_is_full)
            else: draw_idle_screen()
            
        elif system_state == "MENU":
            if menu_index == 0: 
                cfg_buzzer = not cfg_buzzer; menu_items[0] = f"BUZZER: {'ON' if cfg_buzzer else 'OFF'}"
                save_config(); draw_menu_item(0, True)
                time.sleep_ms(100)
            elif menu_index == 1: 
                try:
                    raw_d = i2c0.readfrom_mem(0x68, 0x04, 3)
                    edit_d, edit_m, edit_y = [(r >> 4) * 10 + (r & 0x0F) for r in raw_d]
                except: pass
                edit_step = 0; system_state = "SET_DATE"; draw_set_date(full=True)
            elif menu_index == 2: 
                edit_step = 0; edit_id = [0,0,0,0]; system_state = "JUMP_ID"; draw_jump_id(full=True)
            elif menu_index == 3: 
                system_state = "CONFIRM_FORMAT"; draw_confirm_format()
            elif menu_index == 4: 
                if not sd_active:
                    draw_popup(b'NO SD CARD!', color=RED); time.sleep(1); draw_menu(full=True)
                else: system_state = "CONFIRM_FORMAT_SD"; draw_confirm_format_sd()
            elif menu_index == 5: 
                if sd_active:
                    draw_popup(b'UNMOUNTING...', color=YELLOW); disable_sd_forever("SD REMOVED") 
                    draw_popup(b'SAFE TO REMOVE', color=CYAN); time.sleep(2)
                else:
                    draw_popup(b'MOUNTING SD...', color=YELLOW); check_sd_startup() 
                    if sd_active: draw_popup(b'MOUNT OK!', color=GREEN)
                    else: draw_popup(b'MOUNT FAIL!', color=RED)
                    time.sleep_ms(1)
                system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
                if has_received: display_train_data(last_basic, last_ext, last_is_full)
                else: draw_idle_screen()
            elif menu_index == 6: system_state = "ABOUT"; draw_about()
            elif menu_index == 7: 
                cfg_scr_idx = (cfg_scr_idx + 1) % 4
                menu_items[7] = f"SCREEN OFF AFTER: {SCR_OFF_OPTS[cfg_scr_idx]}"
                save_config(); draw_menu_item(7, True)
                time.sleep_ms(100)
                
        elif system_state == "SET_DATE":
            edit_step += 1
            if edit_step > 2: 
                rtc.set_date(edit_y, edit_m, edit_d); system_state = "MENU"; draw_menu(full=True) 
            else: draw_set_date(full=False)
                
        elif system_state == "JUMP_ID":
            edit_step += 1
            if edit_step > 3:
                target_id = edit_id[0]*1000 + edit_id[1]*100 + edit_id[2]*10 + edit_id[3] - 1
                if 0 <= target_id < total_count:
                    system_state = "HISTORY"; hist_ptr = target_id; entry = load_history_entry(hist_ptr)
                    display_train_data(entry['d'].get('basic',{}), entry['d'].get('extended',{}), entry['d'].get('type')!="basic_only", True, entry['t'], hist_ptr)
                else: draw_popup(b'INDEX ERROR!'); time.sleep(1); draw_jump_id(full=True); edit_step = 0
            else: draw_jump_id(full=False)
            
        elif system_state == "CONFIRM_FORMAT":
            draw_popup(b'FORMATTING...', color=GREEN); open(HIST_FILE, 'w').close()
            total_count = 0; hist_ptr = -1; history_offsets = array.array('I'); time.sleep(1)
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)

        elif system_state == "CONFIRM_FORMAT_SD":
            draw_popup(b'FORMATTING SD...', color=YELLOW)
            try:
                if 'sd' in os.listdir('/'): 
                    try: os.umount("/sd")
                    except: pass
                tft_cs.value(1); spi1.init(baudrate=1000000); sd_obj = sdcard.SDCard(spi1, sd_cs)
                spi1.init(baudrate=10000000); os.VfsFat.mkfs(sd_obj); os.mount(os.VfsFat(sd_obj), "/sd")
                draw_popup(b'SD FORMAT OK!', color=GREEN)
            except:
                sd_obj = None; draw_popup(b'FORMAT FAIL!', color=RED); disable_sd_forever("FORMAT FAIL") 
            spi1.init(baudrate=80000000) 
            time.sleep(1)
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)

    time.sleep_ms(1)
