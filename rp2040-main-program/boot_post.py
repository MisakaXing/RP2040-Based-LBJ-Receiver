import time
import machine
import sdcard

class SystemPOST:
    BLACK = 0x0000
    PANEL = 0x1082
    PANEL_ALT = 0x18E3
    WHITE = 0xFFFF
    MUTED = 0x8410
    CYAN = 0x07FF
    GREEN = 0x07E0
    YELLOW = 0xFFE0
    RED = 0xF800

    def __init__(self, tft, tft_cs):
        self.tft = tft
        self.tft_cs = tft_cs
        self.y = 54
        self.row_index = 0
        self.has_warning = False
        self.has_critical_error = False
        self.rtc_error = False 
        self.current_label = ""
        self.tft.fill(self.BLACK)
        self.tft.fill_rect(0, 0, 320, 42, self.PANEL)
        self.tft.fill_rect(0, 40, 320, 2, self.CYAN)
        self.tft.draw_gbk(b'LBJ', 14, 8, self.WHITE, self.PANEL, scale=2)
        self.tft.draw_gbk(b'RECEIVER', 72, 14, self.CYAN, self.PANEL)
        self.tft.draw_gbk(b'POWER-ON CHECK', 198, 14, self.MUTED, self.PANEL)

    def _check_start(self, msg):
        self.current_label = msg

    def _check_end(self, status, msg):
        row_bg = self.PANEL_ALT if self.row_index & 1 else self.BLACK
        self.tft.fill_rect(8, self.y - 4, 304, 24, row_bg)
        self.tft.draw_gbk(self.current_label.encode(), 18, self.y, self.WHITE, row_bg)
        if status == "OK":
            tag, color = b'OK', self.GREEN
        elif status == "WARN":
            self.has_warning = True
            tag, color = b'CHECK', self.YELLOW
        elif status == "ERR":
            self.has_critical_error = True
            tag, color = b'FAIL', self.RED
        else:
            tag, color = b'OPTIONAL', self.MUTED
        self.tft.draw_gbk(tag, 132, self.y, color, row_bg)
        self.tft.draw_gbk(msg.encode(), 205, self.y, color, row_bg)
        self.y += 27
        self.row_index += 1

    def check_sys_ver(self, ver, is_es):
        self._check_start("FIRMWARE")
        if is_es == 1: self._check_end("WARN", f"v{ver} (Eng Ver)") 
        else: self._check_end("OK", f"v{ver} (Release)")

    def check_sx1276(self, spi_id=0, sck=18, mosi=19, miso=16, cs=17, rst=15):
        self._check_start("RADIO")
        try:
            spi = machine.SPI(spi_id, baudrate=1000000, sck=machine.Pin(sck), mosi=machine.Pin(mosi), miso=machine.Pin(miso))
            cs_pin = machine.Pin(cs, machine.Pin.OUT, value=1)
            rst_pin = machine.Pin(rst, machine.Pin.OUT, value=1)
            rst_pin.value(0); time.sleep_ms(10); rst_pin.value(1); time.sleep_ms(10)
            cs_pin.value(0)
            buf = bytearray([0x42 & 0x7F, 0x00]); spi.write_readinto(buf, buf); cs_pin.value(1)
            ver = buf[1]
            if ver in [0x12, 0x22]: self._check_end("OK", f"SX1276 (v{ver:02X})"); return True
            else: self._check_end("ERR", "NOT FOUND/DEAD"); return False
        except Exception: self._check_end("ERR", "SPI BUS ERROR"); return False

    # 电池电压三段式检查
    def check_bat(self, bat_adc, bat_en):
        self._check_start("BATTERY")
        bat_en.value(0); time.sleep_ms(10); raw = bat_adc.read_u16(); bat_en.value(1)
        volts = (raw / 65535.0) * 3.3 * 2 + 0.174
        if volts < 3.5:
            self._check_end("ERR", f"{volts:.2f}V (CRITICAL)")
        elif volts < 3.7:
            self._check_end("WARN", f"{volts:.2f}V (LOW)")
        else:
            self._check_end("OK", f"{volts:.2f}V (Good)")

    def check_temp(self, sensor_temp):
        self._check_start("TEMPERATURE")
        t = 27 - (sensor_temp.read_u16()*(3.3/65535)-0.706)/0.001721
        if 10 <= t <= 45: self._check_end("OK", f"{t:.1f}C (Norm)")
        else: self._check_end("WARN", f"{t:.1f}C (Abnorm)")

    def check_rtc(self, rtc):
        self._check_start("RTC")
        try:
            model = rtc.detect()
            if model == "UNKNOWN":
                self._check_end("WARN", "Not Found")
                self.rtc_error = True
            elif rtc.needs_sync():
                self._check_end("WARN", f"{model} Sync!")
                self.rtc_error = True
            else:
                self._check_end("OK", model)
                self.rtc_error = False
        except Exception:
            self._check_end("WARN", "Comms Failed")
            self.rtc_error = True

    def check_sd(self, spi1, sd_cs):
        self._check_start("SD CARD")
        self.tft_cs.value(1) 
        try:
            spi1.init(baudrate=1000000); sdcard.SDCard(spi1, sd_cs); self._check_end("OK", "READY")
        except: self._check_end("OPTIONAL", "NOT INSERTED")
        finally: spi1.init(baudrate=40000000)
            
    def run_all(self, bat_adc, bat_en, sensor_temp, rtc, spi1, sd_cs, buzzer, p_ver, is_es):
        self.check_sys_ver(p_ver, is_es) 
        radio_ok = self.check_sx1276()
        self.check_bat(bat_adc, bat_en) # 运行电池检查
        self.check_temp(sensor_temp)
        self.check_rtc(rtc)
        self.check_sd(spi1, sd_cs)
        
        footer_y = 218
        # 如果是电压过低或无线电损坏，强制停机
        if not radio_ok or self.has_critical_error:
            msg = b'SYSTEM HALTED - LOW POWER' if self.has_critical_error else b'SYSTEM HALTED - RADIO DEAD'
            self.tft.fill_rect(0, footer_y, 320, 22, self.RED)
            self.tft.draw_gbk(msg, 30, footer_y + 3, self.WHITE, self.RED)
            # 持续鸣叫报警
            buzzer.value(1); time.sleep(1.5); buzzer.value(0)
            return "HALT"
            
        if self.has_warning:
            self.tft.fill_rect(0, footer_y, 320, 22, self.PANEL)
            self.tft.draw_gbk(b'CHECK REQUIRED - STARTING', 52, footer_y + 3, self.YELLOW, self.PANEL)
            buzzer.value(1); time.sleep_ms(25); buzzer.value(0)
            time.sleep_ms(80)
        else:
            self.tft.fill_rect(0, footer_y, 320, 22, self.PANEL)
            self.tft.draw_gbk(b'READY - STARTING RECEIVER', 48, footer_y + 3, self.GREEN, self.PANEL)
            buzzer.value(1); time.sleep_ms(25); buzzer.value(0)
            time.sleep_ms(80)
            
        if self.rtc_error: return "RTC_SYNC"
        return "OK"
