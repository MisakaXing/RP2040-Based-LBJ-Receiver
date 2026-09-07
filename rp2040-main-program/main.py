import time
import json
import machine
import os
import gc
import sdcard
import _thread  
from machine import Pin, ADC, I2C
from lbj_receiver import LBJReceiver, FixedQueue
from history_store import (
    APPEND_FULL,
    APPEND_INVALID,
    APPEND_NO_SPACE,
    APPEND_OK,
    APPEND_RETRY,
    HistoryStore,
    make_history_record,
    storage_write_due,
)
from ili9341 import ILI9341, BLACK, WHITE, RED, GREEN, BLUE, CYAN, YELLOW, GRAY, MAGENTA
from rtc_ds3231 import DS3231
from boot_post import SystemPOST
from wireless_portal import WirelessPortal, AP_SSID

# 系统性能配置

# Use the RP2350 rated clock for long-running stability testing.
# Keep the configured SPI and PIO receive settings unchanged.
CPU_FREQ_HZ = 150000000
TFT_SPI_BAUD = 60000000
UI_QUEUE_CAPACITY = 16
HISTORY_QUEUE_CAPACITY = 24
HISTORY_RADIO_QUIET_MS = 100
STORAGE_WRITE_GAP_MS = 80
STORAGE_MAX_DEFER_MS = 1500
STORAGE_HARD_DEFER_MS = 5000
RADIO_CORE_STALL_MS = 10000
RADIO_ERROR_RECOVERY_THRESHOLD = 3
RADIO_AUTO_RECOVERY_ENABLED = True
RADIO_HEALTH_LOG_MS = 60000
BUTTON_BEEP_MS = 25
HISTORY_BEEP_GAP_MS = 6
BUTTON_REPEAT_DELAY_MS = 350
BUTTON_REPEAT_INTERVAL_MS = 120
HISTORY_REPEAT_DELAY_MS = 220
HISTORY_REPEAT_INTERVAL_MS = 45
MAX_STORAGE_RETRIES = 3
SD_MIN_FREE_BYTES = 1024 * 1024
WIFI_RETRY_INITIAL_MS = 5000
WIFI_RETRY_MAX_MS = 60000
PHYSICAL_FLASH_MIB = 16

pin_bl = Pin(6, Pin.OUT, value=0)
machine.freq(CPU_FREQ_HZ)
print("BOOT_CPU_HZ", machine.freq())
try:
    print("BOOT_RESET_CAUSE", machine.reset_cause())
except Exception:
    pass
Program_ver = "5.3-W"
is_es_ver = 0 
Author_Name = "MisakaXing"
BAT_OFFSET = 0.174 

ui_queue = FixedQueue(UI_QUEUE_CAPACITY)
history_queue = FixedQueue(HISTORY_QUEUE_CAPACITY)
sd_log_queue = FixedQueue(HISTORY_QUEUE_CAPACITY)
ui_lock = _thread.allocate_lock() 
RADIO_HEARTBEAT = 0
RADIO_CONSECUTIVE_ERRORS = 1
RADIO_TOTAL_ERRORS = 2
RADIO_LAST_ERROR_LOG = 3
radio_state = [time.ticks_ms(), 0, 0, 0]
last_radio_health_log = 0
last_storage_write = 0
storage_pending_since = None
storage_turn = 0
history_write_retries = 0
history_dropped = 0
sd_dropped = 0
storage_errors = 0
storage_forced_writes = 0
wifi_service_errors = 0
wifi_retry_at = 0
wifi_retry_delay_ms = WIFI_RETRY_INITIAL_MS

last_hw_update = 0  
last_hw_draw = None
HW_SAMPLE_INTERVAL_MS = 30000
last_rssi_str = "N/A" 
hist_rssi_str = "N/A" # 用于单独储存历史记录的 RSSI
screen_is_on = True 
last_battery_v = None
last_battery_p = None
last_temp_str = None

# 1. 硬件 IO 初始化

tft_cs = Pin(9, Pin.OUT, value=1) 
spi1 = machine.SPI(1, baudrate=20000000, sck=Pin(10), mosi=Pin(11), miso=Pin(8, Pin.IN, Pin.PULL_UP))
tft = ILI9341(spi1, cs=9, dc=12, rst=13)
spi1.init(baudrate=TFT_SPI_BAUD, polarity=0, phase=0)

sd_cs = Pin(7, Pin.OUT, value=1)
bat_en = Pin(14, Pin.OUT, value=1)
bat_adc = ADC(Pin(41))
buzzer = Pin(22, Pin.OUT, value=0)
try:
    buzzer_timer = machine.Timer(-1)
except Exception:
    buzzer_timer = None

i2c0 = I2C(0, sda=Pin(0), scl=Pin(1), freq=400000)
rtc = DS3231(i2c0)

btn_menu, btn_up, btn_down, btn_ok = [Pin(i, Pin.IN, Pin.PULL_UP) for i in (2, 3, 4, 5)]
btn_wake = Pin(42, Pin.IN, Pin.PULL_UP)
sensor_temp = ADC(ADC.CORE_TEMP)


class ButtonTracker:
    def __init__(self, pin, repeat=False, debounce_ms=12,
                 repeat_delay_ms=BUTTON_REPEAT_DELAY_MS,
                 repeat_interval_ms=BUTTON_REPEAT_INTERVAL_MS):
        self.pin = pin
        self.repeat = repeat
        self.debounce_ms = debounce_ms
        self.repeat_delay_ms = repeat_delay_ms
        self.repeat_interval_ms = repeat_interval_ms
        self.raw_pressed = not pin.value()
        self.stable_pressed = self.raw_pressed
        self.changed_at = time.ticks_ms()
        self.repeat_at = time.ticks_add(self.changed_at, repeat_delay_ms)
        self.suppress_until_release = False

    def set_repeat_profile(self, delay_ms, interval_ms, now,
                           suppress_until_release=False):
        self.repeat_delay_ms = delay_ms
        self.repeat_interval_ms = interval_ms
        self.repeat_at = time.ticks_add(now, delay_ms)
        if suppress_until_release:
            self.suppress_until_release = (
                self.raw_pressed
                or self.stable_pressed
                or not self.pin.value()
            )

    def poll(self, now):
        raw_pressed = not self.pin.value()
        if raw_pressed != self.raw_pressed:
            self.raw_pressed = raw_pressed
            self.changed_at = now

        # A direction key may still be held when HISTORY exits.  Swallow that
        # hold through a debounced release so its accelerated repeat cannot
        # leak into the dashboard or menu.
        if self.suppress_until_release:
            if (
                not self.raw_pressed
                and self.stable_pressed
                and time.ticks_diff(now, self.changed_at) >= self.debounce_ms
            ):
                self.stable_pressed = False
            if not self.raw_pressed and not self.stable_pressed:
                self.suppress_until_release = False
            return False

        if (
            self.raw_pressed != self.stable_pressed
            and time.ticks_diff(now, self.changed_at) >= self.debounce_ms
        ):
            self.stable_pressed = self.raw_pressed
            if self.stable_pressed:
                self.repeat_at = time.ticks_add(now, self.repeat_delay_ms)
                return True
            return False

        if (
            self.repeat
            and self.raw_pressed
            and self.stable_pressed
            and time.ticks_diff(now, self.repeat_at) >= 0
        ):
            self.repeat_at = time.ticks_add(now, self.repeat_interval_ms)
            return True
        return False

# 显示只运行在核心 0，驱动内部已经分块发送，无需人为 sleep 或二次切片。
def safe_fill_rect(x, y, w, h, color):
    tft.fill_rect(x, y, w, h, color)

LOCO_NAME_GBK = {
    "解放": b"\xbd\xe2\xb7\xc5",
    "前进": b"\xc7\xb0\xbd\xf8",
    "建设": b"\xbd\xa8\xc9\xe8",
    "蓝箭控车": b"\xc0\xb6\xbc\xfd\xbf\xd8\xb3\xb5",
    "北京": b"\xb1\xb1\xbe\xa9",
    "北京宽": b"\xb1\xb1\xbe\xa9\xbf\xed",
    "轻油": b"\xc7\xe1\xd3\xcd",
    "天安": b"\xcc\xec\xb0\xb2",
    "新曙光": b"\xd0\xc2\xca\xef\xb9\xe2",
    "神州": b"\xc9\xf1\xd6\xdd",
    "DJ熊猫": b"DJ\xd0\xdc\xc3\xa8",
    "蓝箭动车": b"\xc0\xb6\xbc\xfd\xb6\xaf\xb3\xb5",
    "先锋号": b"\xcf\xc8\xb7\xe6\xba\xc5",
    "天梭": b"\xcc\xec\xcb\xf3",
    "DJ4和谐": b"DJ4\xba\xcd\xd0\xb3",
}
UNKNOWN_LOCO_GBK = b"\xce\xb4\xd6\xaa"

def encode_loco_gbk(loco):
    """Encode the mixed Chinese/ASCII locomotive label for HZK16."""
    loco = str(loco)

    if loco.startswith("UNK("):
        return UNKNOWN_LOCO_GBK + loco[3:].encode()
    if loco.startswith("未知("):
        return UNKNOWN_LOCO_GBK + loco[2:].encode()

    separator = loco.find("-")
    name = loco if separator < 0 else loco[:separator]
    suffix = b"" if separator < 0 else loco[separator:].encode()
    name_gbk = LOCO_NAME_GBK.get(name)
    if name_gbk is not None:
        return name_gbk + suffix

    return loco.encode()

def get_serial_number():
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    try:
        n = int.from_bytes(machine.unique_id(), 'big')#读取芯片唯一ID

        # Base36
        s = ""
        while n:
            n, r = divmod(n, 36)
            s = chars[r] + s

        s = s or "0"

        # 固定12位，前补0
        if len(s) < 12:
            s = ("0" * (12 - len(s))) + s

        # 超过12位截取后12位
        return s[-12:]

    except Exception as e:
        print(e)
        return "S/N INVALID"



# 2. 系统全局变量
Serial_Number = get_serial_number()
wifi_portal = WirelessPortal(Serial_Number)
HIST_FILE = "history.jsonl"
SD_LOG_FILE = "/sd/lbj_log.jsonl"
CONFIG_FILE = "config.json"
PPM_CALIBRATION_VERSION = 7
history_store = HistoryStore(HIST_FILE)
MAX_HIST = history_store.max_records
FLASH_FS_BYTES = history_store.total_bytes

system_state = "DASHBOARD" 
has_received = False
menu_index = 0

cfg_scr_idx = 3
SCR_OFF_OPTS = ["30s", "1min", "5min", "never", "on demand"]
SCR_OFF_MS = [30000, 60000, 300000, -1, -1]
SCR_OFF_ON_DEMAND_INDEX = 4
cfg_buzzer = True
cfg_ppm_offset = 6.0
cfg_ppm_calibrated = False
cfg_wifi_enabled = False
wifi_module_ok = True

menu_items = ["BUZZER: ON", "SET DATE", "JUMP TO ID", "FORMAT FLASH", "FORMAT SD", "MOUNT SD", "ABOUT DEV", f"SCREEN OFF AFTER: {SCR_OFF_OPTS[cfg_scr_idx]}", "WIRELESS SETTING"]
MENU_WIFI_INDEX = 8
MENU_VISIBLE_ROWS = 8
menu_view_start = 0

hist_ptr = -1
total_count = 0

last_interaction = time.ticks_ms()
history_last_input = last_interaction

last_minute = -1 
last_sd_err_time = 0

sd_active = False    
sd_obj = None
current_sd_status = "NO SD CARD" 

edit_y, edit_m, edit_d = 24, 1, 1
edit_id = [0, 0, 0, 0] 
edit_step = 0 

current_status = b'READY'
current_status_color = GREEN

last_basic, last_ext = {}, {}
last_is_full = True
latest_train_record = None

need_post_train_gc = False 
last_screen_layout = None
buzzer_off_at = None

# 3. 核心功能函数

    
def _buzzer_timer_off(_timer):
    # Pin writes are IRQ-safe on RP2; keep the timer callback allocation-free.
    buzzer.value(0)

def _cancel_buzzer_timer():
    if buzzer_timer is not None:
        try:
            buzzer_timer.deinit()
        except Exception:
            pass

def beep():
    global buzzer_off_at
    if not cfg_buzzer:
        return
    _cancel_buzzer_timer()
    buzzer_off_at = None
    buzzer.value(1)
    time.sleep_ms(BUTTON_BEEP_MS)
    buzzer.value(0)
    buzzer_off_at = None

def service_buzzer(now):
    global buzzer_off_at
    if buzzer_off_at is not None and time.ticks_diff(now, buzzer_off_at) >= 0:
        buzzer.value(0)
        buzzer_off_at = None
        _cancel_buzzer_timer()

def stop_buzzer():
    global buzzer_off_at
    _cancel_buzzer_timer()
    buzzer.value(0)
    buzzer_off_at = None

def set_screen_power(enabled):
    """Set TFT backlight power and keep the software state in sync."""
    global screen_is_on
    screen_is_on = bool(enabled)
    pin_bl.value(0 if screen_is_on else 1)

def handle_screen_button_event(wake_event):
    """Handle screen-off input and return True when this key is consumed."""
    if cfg_scr_idx == SCR_OFF_ON_DEMAND_INDEX:
        if wake_event:
            set_screen_power(not screen_is_on)
            print("SCREEN_POWER", "ON" if screen_is_on else "OFF")
            beep()
            return True
        # While manually off, navigation keys must not wake the display or
        # operate an invisible menu.  The power key is the sole wake source.
        return not screen_is_on

    if not screen_is_on:
        set_screen_power(True)
        beep()
        return True
    return False

def wake_screen_for_train():
    """Keep manual-off mode dark while preserving legacy auto-wake modes."""
    if (
        not screen_is_on
        and cfg_scr_idx != SCR_OFF_ON_DEMAND_INDEX
    ):
        set_screen_power(True)

def navigation_beep():
    beep()

def history_navigation_beep():
    """Emit one distinct pulse for every displayed HISTORY page."""
    global buzzer_off_at
    if not cfg_buzzer:
        return

    # Normally the one-shot timer has already made the pin low while the next
    # page is drawing.  This fallback gap also guarantees separation if timer
    # delivery was delayed by a driver or the timer is unavailable.
    if buzzer.value():
        buzzer.value(0)
        time.sleep_ms(HISTORY_BEEP_GAP_MS)
    _cancel_buzzer_timer()
    buzzer.value(1)
    buzzer_off_at = time.ticks_add(time.ticks_ms(), BUTTON_BEEP_MS)
    if buzzer_timer is not None:
        try:
            buzzer_timer.init(
                period=BUTTON_BEEP_MS,
                mode=machine.Timer.ONE_SHOT,
                callback=_buzzer_timer_off,
            )
            return
        except Exception:
            pass

    # Compatibility fallback for a firmware without virtual timers.
    time.sleep_ms(BUTTON_BEEP_MS)
    buzzer.value(0)
    buzzer_off_at = None

def set_history_navigation_mode(enabled, suppress_until_release=False, now=None):
    """Change only the HISTORY repeat profile; menu timing stays untouched."""
    if now is None:
        now = time.ticks_ms()
    delay_ms = HISTORY_REPEAT_DELAY_MS if enabled else BUTTON_REPEAT_DELAY_MS
    interval_ms = (
        HISTORY_REPEAT_INTERVAL_MS if enabled else BUTTON_REPEAT_INTERVAL_MS
    )
    up_button.set_repeat_profile(
        delay_ms, interval_ms, now, suppress_until_release
    )
    down_button.set_repeat_profile(
        delay_ms, interval_ms, now, suppress_until_release
    )

def get_max_days(y, m):
    if m == 2: return 29 if y % 4 == 0 else 28
    return 30 if m in [4, 6, 9, 11] else 31

def get_rtc_date_for_edit():
    try:
        year, month, day = rtc.get_date()
        if (
            24 <= year <= 99
            and 1 <= month <= 12
            and 1 <= day <= get_max_days(year, month)
        ):
            return year, month, day
    except:
        pass
    return 26, 1, 1

def get_battery_info():
    bat_en.value(0)
    try:
        time.sleep_ms(5)
        raw = bat_adc.read_u16()
    finally:
        bat_en.value(1)
    raw_volts = (raw / 65535) * 3.3 * 2
    volts = raw_volts + BAT_OFFSET
    percent = int((volts - 3.4) / (4.2 - 3.4) * 100)
    return f"{volts:.1f}V", f"{max(0, min(100, percent))}%"

def sample_device_status(now, force=False):
    """Share one bounded ADC sample between the LCD and wireless snapshot."""
    global last_hw_update, last_battery_v, last_battery_p, last_temp_str
    if (not force and last_battery_v is not None
            and time.ticks_diff(now, last_hw_update) < HW_SAMPLE_INTERVAL_MS):
        return
    battery_percent = None
    temp_c = None
    try:
        last_battery_v, last_battery_p = get_battery_info()
        battery_percent = int(last_battery_p.rstrip('%'))
    except Exception:
        last_battery_v, last_battery_p = "---", "---"
    try:
        reading = sensor_temp.read_u16() * (3.3 / 65535.0)
        temp_c = round(27 - (reading - 0.706) / 0.001721, 1)
        if not -100 <= temp_c <= 200:
            temp_c = None
    except Exception:
        pass
    last_temp_str = "ERR" if temp_c is None else f"{temp_c:.1f}C"
    last_hw_update = now
    wifi_portal.set_device_status(battery_percent, temp_c)

def _read_config_dict():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.loads(f.read())
    except:
        return {}

def load_config():
    global cfg_buzzer, cfg_scr_idx, cfg_ppm_offset, cfg_ppm_calibrated
    global cfg_wifi_enabled, menu_items
    try:
        config = _read_config_dict()
        cfg_buzzer = bool(config.get("buzzer", True))
        try:
            loaded_scr_idx = int(config.get("scr_idx", 3))
        except (TypeError, ValueError):
            loaded_scr_idx = 3
        cfg_scr_idx = loaded_scr_idx if 0 <= loaded_scr_idx < len(SCR_OFF_OPTS) else 3
        ppm_value = float(config.get("ppm_offset", 6.0))
        ppm_valid = -25.0 <= ppm_value <= 25.0
        ppm_version_ok = config.get("ppm_calibration_version", 0) == PPM_CALIBRATION_VERSION
        cfg_ppm_calibrated = (
            bool(config.get("ppm_calibrated", False))
            and ppm_valid
            and ppm_version_ok
        )
        cfg_ppm_offset = ppm_value if cfg_ppm_calibrated else 6.0
        cfg_wifi_enabled = bool(config.get("wifi_enabled", False))
        menu_items[0] = f"BUZZER: {'ON' if cfg_buzzer else 'OFF'}"
        menu_items[7] = f"SCREEN OFF AFTER: {SCR_OFF_OPTS[cfg_scr_idx]}"
    except Exception as exc:
        print("CONFIG_LOAD_ERR", repr(exc))
        cfg_buzzer = True
        cfg_scr_idx = 3
        cfg_ppm_offset = 6.0
        cfg_ppm_calibrated = False
        cfg_wifi_enabled = False
        menu_items[0] = "BUZZER: ON"
        menu_items[7] = f"SCREEN OFF AFTER: {SCR_OFF_OPTS[cfg_scr_idx]}"

def save_config():
    temp_name = CONFIG_FILE + ".tmp"
    try:
        config = _read_config_dict()
        config["buzzer"] = cfg_buzzer
        config["scr_idx"] = cfg_scr_idx
        config["ppm_offset"] = cfg_ppm_offset
        config["ppm_calibrated"] = cfg_ppm_calibrated
        config["ppm_calibration_version"] = PPM_CALIBRATION_VERSION
        config["wifi_enabled"] = cfg_wifi_enabled
        with open(temp_name, 'w') as f:
            f.write(json.dumps(config))
            f.flush()
        os.rename(temp_name, CONFIG_FILE)
        return True
    except Exception as exc:
        print("CONFIG_SAVE_ERR", repr(exc))
        try:
            os.remove(temp_name)
        except Exception:
            pass
        return False

def _schedule_wifi_retry(now):
    global wifi_retry_at, wifi_retry_delay_ms
    wifi_retry_at = time.ticks_add(now, wifi_retry_delay_ms)
    wifi_retry_delay_ms = min(WIFI_RETRY_MAX_MS, wifi_retry_delay_ms * 2)

def service_wifi(now):
    global wifi_service_errors, wifi_retry_delay_ms, wifi_retry_at
    if not cfg_wifi_enabled or not wifi_module_ok:
        return
    if not wifi_portal.is_enabled():
        if time.ticks_diff(now, wifi_retry_at) < 0:
            return
        if wifi_portal.set_enabled(True):
            wifi_retry_delay_ms = WIFI_RETRY_INITIAL_MS
            wifi_retry_at = 0
        else:
            wifi_service_errors = (wifi_service_errors + 1) & 0x3FFFFFFF
            print("WIFI_RETRY_ERR", wifi_portal.get_status()["error"])
            _schedule_wifi_retry(now)
        return
    try:
        wifi_portal.service(now)
    except Exception as exc:
        wifi_service_errors = (wifi_service_errors + 1) & 0x3FFFFFFF
        print("WIFI_SERVICE_ERR", repr(exc), "count=", wifi_service_errors)
        wifi_portal.stop_for_error(exc)
        gc.collect()
        _schedule_wifi_retry(now)

def init_history():
    global total_count, current_status, current_status_color
    global MAX_HIST, FLASH_FS_BYTES
    total_count = history_store.scan()
    MAX_HIST = history_store.max_records
    FLASH_FS_BYTES = history_store.total_bytes
    print(
        "HISTORY_INDEX",
        "valid=", total_count,
        "invalid=", history_store.invalid_lines,
        "checkpoints=", len(history_store.offsets),
        "limit=", MAX_HIST,
        "fs_bytes=", history_store.total_bytes,
    )
    if not history_store.index_complete:
        current_status, current_status_color = b'HIST ERR', RED
        print("HISTORY_INDEX_ERR", history_store.last_error)

def save_history(record):
    global total_count
    result = history_store.append(record)
    total_count = history_store.count
    return result

def history_alert_status():
    if history_store.read_only or not history_store.index_complete:
        return b'HIST ERR'
    if (
        history_store.full
        or total_count + len(history_queue) >= MAX_HIST
    ):
        return b'MEM FULL'
    return None

def queue_history(record):
    global history_dropped
    # Flash writes can stall both RP2040 cores. Keep the real-time receiver
    # independent and persist only during a quiet gap between transmissions.
    if record is None:
        return False
    if (
        history_store.read_only
        or history_store.full
        or total_count + len(history_queue) >= MAX_HIST
    ):
        history_dropped = (history_dropped + 1) & 0x3FFFFFFF
        return False
    if len(history_queue) >= HISTORY_QUEUE_CAPACITY:
        history_dropped = (history_dropped + 1) & 0x3FFFFFFF
        return False
    queued = history_queue.put(record)
    if not queued:
        history_dropped = (history_dropped + 1) & 0x3FFFFFFF
    return queued

def service_history_storage(now):
    global last_storage_write, storage_pending_since, storage_turn
    global history_write_retries, storage_forced_writes
    global history_dropped, sd_dropped, storage_errors
    global current_status, current_status_color
    if len(history_queue) == 0 and len(sd_log_queue) == 0:
        storage_pending_since = None
        return
    if storage_pending_since is None:
        storage_pending_since = now
    raw_pending = len(receiver.raw_queue)
    if not storage_write_due(
        now,
        receiver.last_word_time,
        storage_pending_since,
        last_storage_write,
        raw_pending,
        time.ticks_diff,
        HISTORY_RADIO_QUIET_MS,
        STORAGE_MAX_DEFER_MS,
        STORAGE_HARD_DEFER_MS,
        STORAGE_WRITE_GAP_MS,
    ):
        return
    if (
        raw_pending
        or time.ticks_diff(now, receiver.last_word_time) < HISTORY_RADIO_QUIET_MS
    ):
        storage_forced_writes = (storage_forced_writes + 1) & 0x3FFFFFFF

    # Alternate destinations so a busy internal log cannot starve the SD log.
    # Peek first and remove only after success or a terminal failure.
    for _ in range(2):
        target = storage_turn
        storage_turn ^= 1
        if target == 0:
            record = history_queue.peek()
            if record is None:
                continue
            result = save_history(record)
            if result == APPEND_OK:
                history_queue.get()
                history_write_retries = 0
            elif result in (APPEND_FULL, APPEND_NO_SPACE, APPEND_INVALID):
                history_queue.get()
                history_dropped = (history_dropped + 1) & 0x3FFFFFFF
                history_write_retries = 0
                current_status = b'MEM FULL' if result != APPEND_INVALID else b'HIST BAD'
                current_status_color = RED
                print("HISTORY_REJECT", result, history_store.last_error)
                if system_state == "DASHBOARD":
                    update_top_bar()
            else:
                history_write_retries += 1
                storage_errors = (storage_errors + 1) & 0x3FFFFFFF
                print("HISTORY_WRITE_ERR", history_store.last_error,
                      "retry=", history_write_retries)
                if history_write_retries >= MAX_STORAGE_RETRIES:
                    history_queue.get()
                    history_dropped = (history_dropped + 1) & 0x3FFFFFFF
                    history_write_retries = 0
                    current_status, current_status_color = b'HIST ERR', RED
                elif history_store.read_only or not history_store.index_complete:
                    current_status, current_status_color = b'HIST ERR', RED
                if current_status == b'HIST ERR' and system_state == "DASHBOARD":
                    update_top_bar()
            last_storage_write = now
            storage_pending_since = (
                now if len(history_queue) or len(sd_log_queue) else None
            )
            return

        record = sd_log_queue.peek()
        if record is None:
            continue
        if log_to_sd(record):
            sd_log_queue.get()
        else:
            # log_to_sd disables the failed card and may clear the queue.
            if sd_log_queue.peek() is record:
                sd_log_queue.get()
                sd_dropped = (sd_dropped + 1) & 0x3FFFFFFF
            storage_errors = (storage_errors + 1) & 0x3FFFFFFF
        last_storage_write = now
        storage_pending_since = (
            now if len(history_queue) or len(sd_log_queue) else None
        )
        return

    storage_pending_since = None

def queue_sd_log(record):
    global sd_dropped
    if not sd_active or len(sd_log_queue) >= HISTORY_QUEUE_CAPACITY:
        if sd_active:
            sd_dropped = (sd_dropped + 1) & 0x3FFFFFFF
        return False
    queued = sd_log_queue.put(record)
    if not queued:
        sd_dropped = (sd_dropped + 1) & 0x3FFFFFFF
    return queued

def load_history_entry(idx):
    return history_store.load(idx)

def load_latest_valid_history():
    return history_store.latest()

def check_sd_startup(preinitialized=None):
    global current_sd_status, sd_active, sd_obj, menu_items
    try:
        tft_cs.value(1)
        if preinitialized is None:
            spi1.init(baudrate=1000000)
            sd_obj = sdcard.SDCard(spi1, sd_cs)
        else:
            sd_obj = preinitialized
        spi1.init(baudrate=5000000)
        os.mount(os.VfsFat(sd_obj), "/sd")
        s = os.statvfs("/sd")
        total_kb = (s[0] * s[2]) / 1024
        free_kb = (s[0] * s[3]) / 1024
        used_kb = total_kb - free_kb
        if total_kb > 1048576: current_sd_status = f"SD:{used_kb/1048576:.1f}/{total_kb/1048576:.1f}G"
        else: current_sd_status = f"SD:{used_kb/1024:.1f}/{total_kb/1024:.1f}M"
        sd_active = True
    except Exception as exc:
        print("SD_MOUNT_ERR", repr(exc))
        sd_active = False; sd_obj = None; current_sd_status = "NO SD CARD"
    finally:
        menu_items[5] = "EJECT SD" if sd_active else "MOUNT SD"
        spi1.init(baudrate=TFT_SPI_BAUD, polarity=0, phase=0)

def disable_sd_forever(reason, redraw=True):
    global sd_active, current_sd_status, sd_obj, menu_items, last_sd_err_time
    global sd_dropped
    try:
        tft_cs.value(1)
        spi1.init(baudrate=5000000)
        os.umount("/sd")
    except:
        pass
    finally:
        spi1.init(baudrate=TFT_SPI_BAUD, polarity=0, phase=0)
    sd_active = False
    sd_obj = None
    sd_dropped = (sd_dropped + len(sd_log_queue)) & 0x3FFFFFFF
    sd_log_queue.clear()
    current_sd_status = reason
    last_sd_err_time = time.ticks_ms()
    menu_items[5] = "MOUNT SD"
    if redraw:
        if system_state == "DASHBOARD": update_top_bar()
        elif system_state == "MENU": draw_menu(full=True)

def log_to_sd(record):
    if not sd_active:
        return False
    redraw_after_error = False
    try:
        tft_cs.value(1)
        spi1.init(baudrate=5000000)
        values = os.statvfs("/sd")
        free_bytes = values[0] * values[3]
        if free_bytes < SD_MIN_FREE_BYTES:
            raise OSError("SD reserve reached")
        j_data = (json.dumps(record) + '\n').encode('utf-8')
        with open(SD_LOG_FILE, 'ab') as f:
            written = f.write(j_data)
            if written is not None and written != len(j_data):
                raise OSError("short SD write")
            try: f.flush()
            except AttributeError: pass
        return True
    except Exception as exc:
        print("SD_LOG_ERR", repr(exc))
        disable_sd_forever("SD WRITE ERR", redraw=False)
        redraw_after_error = True
    finally:
        spi1.init(baudrate=TFT_SPI_BAUD, polarity=0, phase=0)
    if redraw_after_error:
        if system_state == "DASHBOARD": update_top_bar()
        elif system_state == "MENU": draw_menu(full=True)
    return False

# 4. UI 绘制函数 

def draw_ui_skeleton():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 0, 320, 240, BLACK) 
    tft.fill_rect(0, 190, 320, 1, GRAY)
    tft.draw_gbk(b"BAT:", 5, 218, GRAY, BLACK)
    tft.draw_gbk(b"RSSI:", 120, 218, GRAY, BLACK)
    tft.draw_gbk(b"T:", 245, 218, GRAY, BLACK)
    update_top_bar()

def update_top_bar():
    global last_minute
    tft.fill_rect(0, 0, 320, 24, 0x01CF) 
    tft.draw_gbk(current_sd_status.encode(), 10, 4, WHITE, 0x01CF)
    t_str = rtc.get_time_str(show_seconds=False)
    tft.draw_gbk(t_str.encode(), 145, 4, YELLOW, 0x01CF)
    try: last_minute = int(t_str.split(':')[1])
    except: pass
    tft.draw_gbk(current_status, 230, 4, current_status_color, 0x01CF)

def draw_hardware_bar(force=False):
    global last_hw_draw
    now = time.ticks_ms()
    sample_device_status(now)
    if not force and last_hw_draw == last_hw_update:
        return
    last_hw_draw = last_hw_update

    v, p, t = last_battery_v, last_battery_p, last_temp_str
    
    # 处于 HISTORY 模式时，底部状态栏使用历史 RSSI
    r = hist_rssi_str if system_state == "HISTORY" else last_rssi_str
    
    try:
        bat_color = RED if int(p.rstrip('%')) < 20 else WHITE
    except (TypeError, ValueError):
        bat_color = WHITE
    
    tft.fill_rect(45, 218, 70, 16, BLACK)
    tft.draw_gbk(f"{v} {p}".encode(), 45, 218, bat_color, BLACK) 
    
    tft.fill_rect(170, 218, 70, 16, BLACK)
    tft.draw_gbk(r.encode(), 170, 218, WHITE, BLACK)
    
    tft.fill_rect(265, 218, 50, 16, BLACK)
    tft.draw_gbk(t.encode(), 265, 218, WHITE, BLACK)

def draw_history_rssi():
    """Refresh the only hardware-bar value that belongs to a history entry."""
    try:
        rssi = str(hist_rssi_str)[:8].encode()
    except Exception:
        rssi = b'N/A'
    tft.fill_rect(170, 218, 70, 16, BLACK)
    tft.draw_gbk(rssi, 170, 218, WHITE, BLACK)


def draw_idle_screen():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, BLACK) 
    tft.draw_gbk(b'WAITING FOR SIGNAL', 15, 95, GRAY, BLACK, scale=2)

def display_train_data(basic, ext, is_full_mode=True, is_history=False,
                       hist_time="", hist_idx=0, record_type=""):
    global last_screen_layout
    
    current_layout = f"{'HIST' if is_history else 'DASH'}_{'FULL' if is_full_mode else 'BASIC'}"
    bg_color = 0x1082 if is_history else BLACK 
    is_partial = (last_screen_layout == current_layout)

    if not is_partial:
        safe_fill_rect(0, 26, 320, 164, bg_color)

    if is_history:
        if is_partial: tft.fill_rect(0, 30, 320, 16, bg_color) 
        page_name = "EXT ONLY" if record_type == "extended_only" else "HISTORY"
        header = f"{page_name} [{hist_idx+1}/{total_count}]  {hist_time}"
        tft.draw_gbk(header.encode(), 5, 30, YELLOW, bg_color, scale=1)
        y_offset = 20
    else: y_offset = 0

    train_no = str(basic.get('train_no', '---'))
    has_train_no = train_no.isdigit()
    speed = str(basic.get('speed_kmh', '---'))
    km = str(basic.get('km_post', '---'))
    cls = ext.get('class_tag', '') if has_train_no and ext.get('class_tag') != '?' else ''
    full_train = f"{cls}{train_no}" if has_train_no else "---"
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

        route_hex = str(ext.get('route_hex', '') or '')
        try:
            route_b = bytes.fromhex(route_hex)[:8] if route_hex else b'----'
        except (TypeError, ValueError):
            route_b = b'----'
        tft.draw_gbk(route_b, 53, y2, WHITE, bg_color, scale=2)

        digits = [c for c in train_no if c.isdigit()] if has_train_no else []
        if digits:
            direction = b'\xc9\xcf' if int(digits[-1]) % 2 == 0 else b'\xcf\xc2'
        else:
            direction = b'--'
        tft.draw_gbk(direction, 180, y2, MAGENTA, bg_color, scale=2)

        tft.draw_gbk(km.encode() + b'K', 220, y2, GREEN, bg_color, scale=2)

        loco = str(ext.get('loco_type', '----'))
        cab = str(ext.get('cab_end', ''))
        if cab == '31': loco += 'A'
        elif cab == '32': loco += 'B'
        tft.draw_gbk(encode_loco_gbk(loco), 53, y3, WHITE, bg_color, scale=2)

    if not is_history: 
        lon = str(ext.get('lon', '---')).replace('°', ' ')
        lat = str(ext.get('lat', '---')).replace('°', ' ')
        tft.fill_rect(0, 192, 320, 18, BLACK) 
        tft.draw_gbk(b'GPS: ' + lon.encode() + b' / ' + lat.encode(), 5, 195, GRAY, BLACK, scale=1)
        
    last_screen_layout = current_layout

def show_history_index(index, announce=False):
    global system_state, hist_ptr, hist_rssi_str, history_last_input
    global current_status, current_status_color
    entry = load_history_entry(index)
    if not isinstance(entry, dict) or not isinstance(entry.get('d'), dict):
        current_status, current_status_color = b'HIST ERR', RED
        print("HISTORY_READ_ERR", "index=", index, history_store.last_error)
        if system_state == "DASHBOARD":
            update_top_bar()
        return False
    data = entry['d']
    basic = data.get('basic', {})
    extended = data.get('extended', {})
    if not isinstance(basic, dict) or not isinstance(extended, dict):
        current_status, current_status_color = b'HIST ERR', RED
        return False
    # The Flash record is valid, so this key event will display exactly one
    # page. Start its non-blocking pulse before the comparatively slow TFT draw.
    if announce:
        history_navigation_beep()
    entering_history = system_state != "HISTORY"
    system_state = "HISTORY"
    if entering_history:
        set_history_navigation_mode(True)
    history_last_input = time.ticks_ms()
    hist_ptr = index
    hist_rssi_str = str(data.get('rssi', 'N/A'))
    display_train_data(
        basic,
        extended,
        data.get('type') != "basic_only",
        True,
        entry.get('t', '---'),
        index,
        data.get('type', ''),
    )
    draw_history_rssi()
    return True

def _ensure_menu_visible():
    global menu_view_start
    menu_view_start = (menu_index // MENU_VISIBLE_ROWS) * MENU_VISIBLE_ROWS

def move_menu_selection(delta):
    global menu_index
    old_idx = menu_index
    old_view = menu_view_start
    menu_index = (menu_index + delta) % len(menu_items)
    _ensure_menu_visible()
    draw_menu(full=(old_view != menu_view_start), old_idx=old_idx)

def draw_menu(full=True, old_idx=-1):
    global last_screen_layout
    _ensure_menu_visible()
    if full: 
        last_screen_layout = None 
        safe_fill_rect(0, 26, 320, 164, 0x2104) 
        page_count = (len(menu_items) + MENU_VISIBLE_ROWS - 1) // MENU_VISIBLE_ROWS
        page_number = menu_index // MENU_VISIBLE_ROWS + 1
        title = f"--- SYSTEM MENU {page_number}/{page_count} ---"
        tft.draw_gbk(title.encode(), 68, 40, CYAN, 0x2104)
        end = min(len(menu_items), menu_view_start + MENU_VISIBLE_ROWS)
        for i in range(menu_view_start, end):
            draw_menu_item(i, i == menu_index)
    else: 
        if old_idx >= 0: 
            draw_menu_item(old_idx, False) 
        draw_menu_item(menu_index, True)   

def draw_menu_item(i, is_selected):
    slot = i - menu_view_start
    if slot < 0 or slot >= MENU_VISIBLE_ROWS:
        return
    unavailable_wifi = i == MENU_WIFI_INDEX and not wifi_module_ok
    color = RED if unavailable_wifi and is_selected else GRAY if unavailable_wifi else YELLOW if is_selected else WHITE
    prefix = b'! ' if unavailable_wifi and is_selected else b'> ' if is_selected else b'  '
    y = 60 + slot * 16
    tft.fill_rect(40, y, 240, 16, 0x2104)
    tft.draw_gbk(prefix + menu_items[i].encode(), 40, y, color, 0x2104)

def draw_wifi_settings():
    global last_screen_layout
    last_screen_layout = None
    safe_fill_rect(0, 26, 320, 164, 0x2104)
    if not wifi_module_ok:
        tft.draw_gbk(b'--- WIRELESS SETTING ---', 55, 40, CYAN, 0x2104)
        tft.draw_gbk(b'WIFI MODULE FAILED', 75, 96, RED, 0x2104)
        tft.draw_gbk(b'[MENU] BACK', 108, 150, GRAY, 0x2104)
        return
    status = wifi_portal.get_status()
    enabled = status["enabled"]
    retrying = cfg_wifi_enabled and not enabled
    state_color = GREEN if enabled else YELLOW if retrying else GRAY
    tft.draw_gbk(b'--- WIRELESS SETTING ---', 55, 40, CYAN, 0x2104)
    tft.draw_gbk(b'WIFI:', 32, 68, WHITE, 0x2104)
    state_text = b'ON' if enabled else b'RETRY' if retrying else b'OFF'
    tft.draw_gbk(state_text, 88, 68, state_color, 0x2104)
    if enabled:
        tft.draw_gbk(("IP:" + status["ip"]).encode(), 145, 68, GREEN, 0x2104)
    elif status["error"]:
        tft.draw_gbk(status["error"][:24].encode(), 120, 68, RED, 0x2104)
    tft.draw_gbk(b'HOTSPOT NAME:', 32, 94, GRAY, 0x2104)
    tft.draw_gbk(AP_SSID.encode(), 32, 111, WHITE, 0x2104)
    tft.draw_gbk(b'PASSWORD (MACHINE SN):', 32, 136, GRAY, 0x2104)
    tft.draw_gbk(Serial_Number.encode(), 32, 153, YELLOW, 0x2104)
    tft.draw_gbk(b'[OK] TOGGLE  [MENU] BACK', 32, 174, GRAY, 0x2104)

def draw_set_date(full=True):
    global last_screen_layout
    if full:
        last_screen_layout = None 
        safe_fill_rect(0, 26, 320, 164, 0x2104)
        tft.draw_gbk(b'--- SET DATE ---', 95, 40, CYAN, 0x2104)
        tft.draw_gbk(b'-', 134, 90, WHITE, 0x2104, scale=2)
        tft.draw_gbk(b'-', 182, 90, WHITE, 0x2104, scale=2)
        tft.draw_gbk(b'[UP/DOWN]\xb5\xf7\xd5\xfb  [OK]\xc8\xb7\xc8\xcf', 20, 155, GRAY, 0x2104, scale=1)
        
    cols = [YELLOW if edit_step == i else WHITE for i in range(3)]
    
    tft.fill_rect(70, 90, 64, 32, 0x2104)
    tft.fill_rect(150, 90, 32, 32, 0x2104)
    tft.fill_rect(198, 90, 32, 32, 0x2104)
    
    tft.draw_gbk(f"20{edit_y:02}".encode(), 70, 90, cols[0], 0x2104, scale=2)
    tft.draw_gbk(f"{edit_m:02}".encode(), 150, 90, cols[1], 0x2104, scale=2)
    tft.draw_gbk(f"{edit_d:02}".encode(), 198, 90, cols[2], 0x2104, scale=2)

def draw_jump_id(full=True):
    global last_screen_layout
    if full:
        last_screen_layout = None 
        safe_fill_rect(0, 26, 320, 164, 0x2104)
        tft.draw_gbk(b'--- JUMP TO ID ---', 85, 40, CYAN, 0x2104)
        tft.draw_gbk(b'RANGE: 0001 -', 60, 75, GRAY, 0x2104)
        tft.draw_gbk(str(total_count).encode(), 170, 75, GREEN, 0x2104)
        
    for i in range(4):
        color = YELLOW if edit_step == i else WHITE
        tft.fill_rect(110 + i*25, 110, 16, 32, 0x2104)
        tft.draw_gbk(str(edit_id[i]).encode(), 110 + i*25, 110, color, 0x2104, scale=2)

def draw_confirm_format():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, 0x5000)
    tft.draw_gbk(b'!!! WARNING !!!', 35, 50, WHITE, 0x5000, scale=2)
    tft.draw_gbk(b'DELETE ALL FLASH DATA?', 60, 90, YELLOW, 0x5000)
    tft.draw_gbk(b'[OK] TO CONFIRM  [MENU] TO CANCEL', 30, 140, WHITE, 0x5000)

def draw_confirm_format_sd():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, 0x5000)
    tft.draw_gbk(b'!!! SD WARNING !!!', 15, 50, WHITE, 0x5000, scale=2)
    tft.draw_gbk(b'ERASE ALL SD CARD DATA?', 65, 90, YELLOW, 0x5000)
    tft.draw_gbk(b'[OK] TO CONFIRM  [MENU] TO CANCEL', 15, 140, WHITE, 0x5000)

def draw_about():
    global last_screen_layout
    last_screen_layout = None 
    safe_fill_rect(0, 26, 320, 164, 0x2104)
    tft.draw_gbk(b'--- ABOUT DEVICE ---', 75, 40, CYAN, 0x2104)
    es_tag = " (ES)" if is_es_ver == 1 else " (Rel)"
    tft.draw_gbk(f"Version: v{Program_ver}{es_tag}".encode(), 40, 65, RED if is_es_ver == 1 else WHITE, 0x2104)
    fs_mib = FLASH_FS_BYTES / (1024 * 1024)
    tft.draw_gbk(f"Flash: {PHYSICAL_FLASH_MIB}M (FS {fs_mib:.1f}M)".encode(), 40, 87, CYAN, 0x2104)
    tft.draw_gbk(f"Records: {total_count}/{MAX_HIST}".encode(), 40, 109, WHITE, 0x2104)
    tft.draw_gbk(b"Author: " + Author_Name.encode(), 40, 131, YELLOW, 0x2104)
    tft.draw_gbk(b"Serial Number: " + Serial_Number.encode(), 40, 153, WHITE, 0x2104)
    tft.draw_gbk(b'Press OK to Return', 40, 175, GRAY, 0x2104)

def draw_popup(msg, color=RED):
    tft.fill_rect(60, 80, 200, 60, color)
    tft.draw_gbk(msg, 75, 100, WHITE, color)

# 5. 核心 1 子线程
def light_callback(data, queue=ui_queue, lock=ui_lock):
    msg_type = data.get("type", "")
    if msg_type == "error":
        print("RADIO_EVENT_ERR", data.get("error", "unknown"))
        return
    if msg_type != "time_sync" and "train_data" not in msg_type and "only" not in msg_type:
        return
    with lock:
        queue.put(data)

def radio_core_task(receiver_obj, state=radio_state,
                    auto_recovery=RADIO_AUTO_RECOVERY_ENABLED,
                    recovery_threshold=RADIO_ERROR_RECOVERY_THRESHOLD):
    # MicroPython starts this on core1 with its own global-name context.
    # Keep the clock module local, just as the known-stable receiver loop did.
    import time
    while True:
        try:
            receiver_obj.tick()
            state[1] = 0
        except Exception as exc:
            state[2] = (state[2] + 1) & 0x3FFFFFFF
            state[1] += 1
            now = time.ticks_ms()
            if time.ticks_diff(now, state[3]) >= 2000:
                try:
                    print("RADIO_TICK_ERR", "count=", state[2], repr(exc))
                except Exception:
                    pass
                state[3] = now
            if auto_recovery and state[1] >= recovery_threshold:
                try:
                    if receiver_obj.recover(
                        "tick_exception",
                        hard=(state[1] >= recovery_threshold * 2),
                    ):
                        state[1] = 0
                except Exception as recovery_exc:
                    try:
                        print("RADIO_RECOVERY_ERR", repr(recovery_exc))
                    except Exception:
                        pass
        state[0] = time.ticks_ms()
        time.sleep_ms(1)

def process_ui_data(data):
    global last_basic, last_ext, last_is_full, has_received, current_status, current_status_color, last_rssi_str
    global screen_is_on, last_interaction, need_post_train_gc, latest_train_record
    
    try:
        msg_type = data.get("type", "")
        if msg_type == "time_sync":
            hh, mm = map(int, data.get('time').split(':'))
            if 0 <= hh < 24 and 0 <= mm < 60:
                rtc.sync_time(hh, mm)
                if system_state == "DASHBOARD": 
                    current_status, current_status_color = b'TIME SYNC', YELLOW
                    update_top_bar()
        elif "train_data" in msg_type or "only" in msg_type:
            
            # 正常接收的提示音
            beep()
                
            has_received = True
            
            wake_screen_for_train()
            last_interaction = time.ticks_ms()
            
            if "rssi" in data:
                last_rssi_str = str(data["rssi"])
                
            received_at = rtc.get_time_str(True)
            received_record = {"t": received_at, "d": data}
            compact_record = make_history_record(received_at, data)
            if compact_record is not None:
                queue_history(compact_record)
            queue_sd_log(received_record)
            service_buzzer(time.ticks_ms())
            
            last_basic, last_ext = data.get("basic", {}), data.get("extended", {})
            last_is_full = (msg_type != "basic_only")
            # Web/SSE is a live snapshot, not an internal-history reader.
            # Publish every parsed fragment, including extended_only.
            latest_train_record = received_record
            sample_device_status(time.ticks_ms(), force=True)
            wifi_portal.set_latest(latest_train_record)
            
            if system_state == "DASHBOARD":
                # 内存满了强制在右上角显示红色的 MEM FULL
                history_alert = history_alert_status()
                if history_alert is not None:
                    current_status, current_status_color = history_alert, RED
                else:
                    if msg_type == "extended_only":
                        current_status, current_status_color = b'EXT ONLY', YELLOW
                    else:
                        current_status, current_status_color = (b'FULL DATA', GREEN) if last_is_full else (b'BASIC', YELLOW)
                    
                current_status = current_status[:8]
                update_top_bar()
                display_train_data(last_basic, last_ext, last_is_full)
                draw_hardware_bar(force=True) 
                
            need_post_train_gc = True
    except Exception as exc:
        print("UI_EVENT_ERR", msg_type, repr(exc))

# 6. 启动初始化

load_config()
post = SystemPOST(tft, tft_cs)
boot_status = post.run_all(bat_adc, bat_en, sensor_temp, rtc, spi1, sd_cs, buzzer, Program_ver, is_es_ver, wifi_portal)
wifi_module_ok = post.wifi_ok
if not wifi_module_ok:
    menu_items[MENU_WIFI_INDEX] = "WIRELESS: UNAVAILABLE"
spi1.init(baudrate=TFT_SPI_BAUD, polarity=0, phase=0)
if boot_status == "HALT":
    while True: pass 

# Potentially slow storage work runs only after the POST is already visible.
init_history()
sample_device_status(time.ticks_ms(), force=True)
if total_count > 0:
    latest_train_record = load_latest_valid_history()
    wifi_portal.set_latest(latest_train_record)
if post.sd_obj is not None:
    # POST already initialized the card; reuse it instead of making startup
    # wait through the same card handshake twice. A missing card can still be
    # probed later with the MOUNT SD menu item.
    check_sd_startup(post.sd_obj)

if not wifi_module_ok:
    # Keep the user's saved preference.  A one-boot CYW43 probe failure must
    # disable this boot's menu/AP, but a later healthy POST should restore the
    # hotspot automatically instead of leaving Wi-Fi permanently switched off.
    if cfg_wifi_enabled:
        print("WIFI_DEFERRED_POST_FAILURE")
elif cfg_wifi_enabled:
    wifi_retry_at = time.ticks_add(time.ticks_ms(), -1)
    service_wifi(time.ticks_ms())

receiver = LBJReceiver(ppm_offset=cfg_ppm_offset)
receiver.set_callback(light_callback) 
radio_state[RADIO_HEARTBEAT] = time.ticks_ms()
last_radio_health_log = radio_state[RADIO_HEARTBEAT]
_thread.start_new_thread(radio_core_task, (receiver,))

if boot_status == "RTC_SYNC":
    system_state = "SET_DATE"; edit_step = 0
    edit_y, edit_m, edit_d = get_rtc_date_for_edit()
    draw_ui_skeleton(); draw_set_date(full=True)    
else:
    draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)

menu_button = ButtonTracker(btn_menu)
up_button = ButtonTracker(btn_up, repeat=True)
down_button = ButtonTracker(btn_down, repeat=True)
ok_button = ButtonTracker(btn_ok)
wake_button = ButtonTracker(btn_wake)
last_sec = time.ticks_ms()
heartbeat = False

# 7. 核心 0 主循环

while True:
    now = time.ticks_ms()
    service_buzzer(now)
    service_wifi(now)

    if time.ticks_diff(now, radio_state[RADIO_HEARTBEAT]) > RADIO_CORE_STALL_MS:
        if time.ticks_diff(now, radio_state[RADIO_LAST_ERROR_LOG]) >= 5000:
            print("RADIO_CORE_STALL", "age_ms=", time.ticks_diff(now, radio_state[RADIO_HEARTBEAT]))
            radio_state[RADIO_LAST_ERROR_LOG] = now

    if time.ticks_diff(now, last_radio_health_log) >= RADIO_HEALTH_LOG_MS:
        with ui_lock:
            ui_pending = len(ui_queue)
            ui_dropped = ui_queue.dropped
        print("RADIO_HEALTH", receiver.get_health_snapshot(),
              "thread_errors=", radio_state[RADIO_TOTAL_ERRORS],
              "ui_pending=", ui_pending,
              "ui_dropped=", ui_dropped,
              "history_pending=", len(history_queue),
              "history_dropped=", history_dropped,
              "sd_pending=", len(sd_log_queue),
              "sd_dropped=", sd_dropped,
              "storage_errors=", storage_errors,
              "storage_forced=", storage_forced_writes,
              "wifi_errors=", wifi_service_errors,
              "mem_free=", gc.mem_free())
        last_radio_health_log = now

    screen_timeout_ms = SCR_OFF_MS[cfg_scr_idx]
    if screen_is_on and screen_timeout_ms >= 0:
        if time.ticks_diff(now, last_interaction) > screen_timeout_ms:
            set_screen_power(False)
            
    if need_post_train_gc and time.ticks_diff(now, last_interaction) > 1000:
        gc.collect()
        need_post_train_gc = False

    ui_data_to_process = None
    with ui_lock:
        ui_data_to_process = ui_queue.get()

    if ui_data_to_process:
        try:
            process_ui_data(ui_data_to_process)
        except Exception as exc:
            print("UI_QUEUE_ERR", repr(exc))

    try:
        service_history_storage(now)
    except Exception as exc:
        # Never allow non-essential storage work to exit the main loop.
        print("STORAGE_SERVICE_ERR", repr(exc))
        storage_errors = (storage_errors + 1) & 0x3FFFFFFF
        history_dropped = (history_dropped + len(history_queue)) & 0x3FFFFFFF
        sd_dropped = (sd_dropped + len(sd_log_queue)) & 0x3FFFFFFF
        history_queue.clear()
        sd_log_queue.clear()
        storage_pending_since = None
        current_status, current_status_color = b'STOR ERR', RED
        if system_state == "DASHBOARD":
            update_top_bar()
            
    if gc.mem_free() < 20000:
        gc.collect()

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
            
            if not sd_active and current_sd_status != "NO SD CARD" and time.ticks_diff(now, last_sd_err_time) > 3000:
                current_sd_status = "NO SD CARD"
                update_top_bar() 
                
            if not has_received: 
                draw_hardware_bar(force=False) 
        last_sec = now

    wake_event = wake_button.poll(now)
    menu_event = menu_button.poll(now)
    down_event = down_button.poll(now)
    up_event = up_button.poll(now)
    ok_event = ok_button.poll(now)
    any_button_event = wake_event or menu_event or down_event or up_event or ok_event

    if any_button_event:
        last_interaction = now
        if system_state == "HISTORY":
            history_last_input = now
        if handle_screen_button_event(wake_event):
            continue

    # MENU/OK are HISTORY exit commands.  Restore the normal repeat profile
    # before handling them and consume any direction repeat from this same
    # loop, otherwise one long hold can also move the menu or flip one more
    # history page.
    history_exit_requested = (
        system_state == "HISTORY" and (menu_event or ok_event)
    )
    if history_exit_requested:
        set_history_navigation_mode(
            False, suppress_until_release=True, now=now
        )

    if wake_event:
        beep()

    if menu_event:
        last_interaction = now; beep()
        if system_state in ["DASHBOARD", "HISTORY", "ABOUT", "CONFIRM_FORMAT", "CONFIRM_FORMAT_SD", "SET_DATE", "JUMP_ID", "WIFI_SETTINGS"]:
            system_state = "MENU"; draw_menu(full=True) 
        else: 
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
            if has_received: display_train_data(last_basic, last_ext, last_is_full)
            else: draw_idle_screen()
            
    if down_event and not history_exit_requested:
        last_interaction = now
        if system_state == "DASHBOARD" and total_count > 0:
            show_history_index(total_count - 1, announce=True)
        elif system_state == "HISTORY":
            show_history_index((hist_ptr - 1) % total_count, announce=True)
        else:
            navigation_beep()
            if system_state == "MENU":
                move_menu_selection(-1)
            elif system_state == "SET_DATE":
                if edit_step == 0: edit_y = (edit_y+1)%100; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
                elif edit_step == 1: edit_m = edit_m%12+1; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
                else: edit_d = (edit_d%get_max_days(edit_y, edit_m))+1
                draw_set_date(full=False)
            elif system_state == "JUMP_ID":
                edit_id[edit_step] = (edit_id[edit_step]+1)%10; draw_jump_id(full=False)

    if up_event and not history_exit_requested:
        last_interaction = now
        if system_state == "DASHBOARD" and total_count > 0:
            show_history_index(total_count - 1, announce=True)
        elif system_state == "HISTORY":
            show_history_index((hist_ptr + 1) % total_count, announce=True)
        else:
            navigation_beep()
            if system_state == "MENU":
                move_menu_selection(1)
            elif system_state == "SET_DATE":
                if edit_step == 0: edit_y = (edit_y-1)%100; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
                elif edit_step == 1: edit_m = edit_m-1 if edit_m>1 else 12; edit_d = min(edit_d, get_max_days(edit_y, edit_m))
                else: edit_d = edit_d-1 if edit_d>1 else get_max_days(edit_y, edit_m)
                draw_set_date(full=False)
            elif system_state == "JUMP_ID":
                edit_id[edit_step] = (edit_id[edit_step]-1)%10; draw_jump_id(full=False)

    if ok_event and not (history_exit_requested and menu_event):
        last_interaction = now; beep()
        if system_state == "HISTORY" or system_state == "ABOUT":
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
            if has_received: display_train_data(last_basic, last_ext, last_is_full)
            else: draw_idle_screen()
            
        elif system_state == "MENU":
            if menu_index == 0: 
                cfg_buzzer = not cfg_buzzer; menu_items[0] = f"BUZZER: {'ON' if cfg_buzzer else 'OFF'}"
                save_config(); draw_menu_item(0, True)
            elif menu_index == 1: 
                edit_y, edit_m, edit_d = get_rtc_date_for_edit()
                edit_step = 0; system_state = "SET_DATE"; draw_set_date(full=True)
            elif menu_index == 2: 
                edit_step = 0; edit_id = [0,0,0,0]; system_state = "JUMP_ID"; draw_jump_id(full=True)
            elif menu_index == 3: 
                system_state = "CONFIRM_FORMAT"; draw_confirm_format()
            elif menu_index == 4: 
                if not sd_active:
                    draw_popup(b'NO SD CARD!', color=RED); stop_buzzer(); time.sleep(1); draw_menu(full=True)
                else: system_state = "CONFIRM_FORMAT_SD"; draw_confirm_format_sd()
            elif menu_index == 5: 
                if sd_active:
                    draw_popup(b'UNMOUNTING...', color=YELLOW); disable_sd_forever("SD REMOVED") 
                    draw_popup(b'SAFE TO REMOVE', color=CYAN); stop_buzzer(); time.sleep(2)
                else:
                    draw_popup(b'MOUNTING SD...', color=YELLOW); check_sd_startup() 
                    service_buzzer(time.ticks_ms())
                    if sd_active: draw_popup(b'MOUNT OK!', color=GREEN)
                    else: draw_popup(b'MOUNT FAIL!', color=RED)
                system_state = "DASHBOARD"; draw_ui_skeleton(); draw_hardware_bar(force=True)
                if has_received: display_train_data(last_basic, last_ext, last_is_full)
                else: draw_idle_screen()
            elif menu_index == 6: system_state = "ABOUT"; draw_about()
            elif menu_index == 7: 
                cfg_scr_idx = (cfg_scr_idx + 1) % len(SCR_OFF_OPTS)
                menu_items[7] = f"SCREEN OFF AFTER: {SCR_OFF_OPTS[cfg_scr_idx]}"
                save_config(); draw_menu_item(7, True)
            elif menu_index == MENU_WIFI_INDEX:
                if not wifi_module_ok:
                    draw_popup(b'WIFI MODULE FAILED', color=RED)
                    stop_buzzer(); time.sleep(1); draw_menu(full=True)
                else:
                    system_state = "WIFI_SETTINGS"
                    draw_wifi_settings()

        elif system_state == "WIFI_SETTINGS":
            if not wifi_module_ok:
                system_state = "MENU"
                draw_menu(full=True)
            else:
                target_enabled = not cfg_wifi_enabled
                draw_popup(b'WIFI STARTING...' if target_enabled else b'WIFI STOPPING...', color=YELLOW)
                cfg_wifi_enabled = target_enabled
                if target_enabled:
                    wifi_retry_at = time.ticks_add(time.ticks_ms(), -1)
                    wifi_retry_delay_ms = WIFI_RETRY_INITIAL_MS
                    service_wifi(time.ticks_ms())
                else:
                    wifi_portal.set_enabled(False)
                    wifi_retry_delay_ms = WIFI_RETRY_INITIAL_MS
                    wifi_retry_at = 0
                save_config()
                draw_wifi_settings()
                
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
                    if not show_history_index(target_id):
                        draw_popup(b'HISTORY READ ERR', color=RED)
                        stop_buzzer(); time.sleep(1); draw_jump_id(full=True); edit_step = 0
                else: draw_popup(b'INDEX ERROR!'); stop_buzzer(); time.sleep(1); draw_jump_id(full=True); edit_step = 0
            else: draw_jump_id(full=False)
            
        elif system_state == "CONFIRM_FORMAT":
            draw_popup(b'FORMATTING...', color=GREEN)
            if history_store.clear():
                history_queue.clear()
                with ui_lock:
                    ui_queue.clear()
                total_count = 0; hist_ptr = -1; history_write_retries = 0
                has_received = False; last_basic = {}; last_ext = {}; last_is_full = True
                latest_train_record = None; wifi_portal.set_latest(None)
                current_status = b'READY'; current_status_color = GREEN
                need_post_train_gc = False
                stop_buzzer(); time.sleep(1)
                system_state = "DASHBOARD"; draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)
            else:
                print("HISTORY_CLEAR_ERR", history_store.last_error)
                draw_popup(b'FORMAT FAIL!', color=RED)
                stop_buzzer(); time.sleep(1)
                system_state = "MENU"; draw_menu(full=True)

        elif system_state == "CONFIRM_FORMAT_SD":
            draw_popup(b'FORMATTING SD...', color=YELLOW)
            stop_buzzer()
            sd_log_queue.clear()
            try:
                if 'sd' in os.listdir('/'): 
                    try: os.umount("/sd")
                    except: pass
                tft_cs.value(1); spi1.init(baudrate=1000000); sd_obj = sdcard.SDCard(spi1, sd_cs)
                spi1.init(baudrate=10000000); os.VfsFat.mkfs(sd_obj); os.mount(os.VfsFat(sd_obj), "/sd")
                draw_popup(b'SD FORMAT OK!', color=GREEN)
            except Exception as exc:
                print("SD_FORMAT_ERR", repr(exc))
                sd_obj = None; draw_popup(b'FORMAT FAIL!', color=RED); disable_sd_forever("FORMAT FAIL") 
            spi1.init(baudrate=TFT_SPI_BAUD, polarity=0, phase=0)
            time.sleep(1)
            system_state = "DASHBOARD"; draw_ui_skeleton(); draw_idle_screen(); draw_hardware_bar(force=True)

    # HISTORY uses a user-input clock of its own. Incoming train messages may
    # wake the screen and refresh live data, but must not keep a viewer trapped
    # in history forever when nobody is touching the controls.
    if (
        system_state == "HISTORY"
        and time.ticks_diff(now, history_last_input) > 20000
    ):
        set_history_navigation_mode(
            False, suppress_until_release=True, now=now
        )
        system_state = "DASHBOARD"
        draw_ui_skeleton()
        draw_hardware_bar(force=True)
        last_interaction = now
        if has_received:
            display_train_data(last_basic, last_ext, last_is_full)
        else:
            draw_idle_screen()

    time.sleep_ms(1)
