import time
import machine
import sdcard

class SystemPOST:
    def __init__(self, tft, tft_cs):
        self.tft = tft
        self.tft_cs = tft_cs
        self.y = 10
        self.has_warning = False
        self.has_critical_error = False # ★ 新增：严重错误标志
        self.rtc_error = False 
        self.tft.fill(0) 
        self.tft.draw_gbk(b'Board SelfCheck', 10, self.y, 0x07FF, 0, scale=2) 
        self.y += 35
        self.tft.fill_rect(0, self.y, 320, 2, 0x7BEF) 
        self.y += 15 

    def _check_start(self, msg):
        self.tft.draw_gbk(msg.encode(), 10, self.y, 0x7BEF, 0, scale=1)
        time.sleep_ms(30) 

    def _check_end(self, status, msg):
        if status == "OK":
            self.tft.draw_gbk(b'OK  ' + msg.encode(), 130, self.y, 0x07E0, 0)
        elif status == "WARN":
            self.has_warning = True
            self.tft.draw_gbk(b'WRN ' + msg.encode(), 130, self.y, 0xFFE0, 0) 
        elif status == "ERR":
            self.has_critical_error = True # 标记为严重错误
            self.tft.draw_gbk(b'ERR ' + msg.encode(), 130, self.y, 0xF800, 0) 
        
        self.y += 22 
        time.sleep_ms(50) 

    def check_sys_ver(self, ver, is_es):
        self._check_start("Chk Sys Ver.")
        if is_es == 1: self._check_end("WARN", f"v{ver} (Eng Ver)") 
        else: self._check_end("OK", f"v{ver} (Release)")

    def check_sx1276(self, spi_id=0, sck=18, mosi=19, miso=16, cs=17, rst=15):
        self._check_start("Chk Radio...")
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

    # ★ 核心逻辑修改：电池电压三段式检查
    def check_bat(self, bat_adc, bat_en):
        self._check_start("Chk Battery.")
        bat_en.value(0); time.sleep_ms(10); raw = bat_adc.read_u16(); bat_en.value(1)
        
        # 记得在这里加上你校准后的 BAT_OFFSET，比如 0.255
        volts = (raw / 65535.0) * 3.3 * 2 + 0.255 
        
        if volts < 3.5:
            self._check_end("ERR", f"{volts:.2f}V (CRITICAL)")
        elif volts < 3.7:
            self._check_end("WARN", f"{volts:.2f}V (LOW)")
        else:
            self._check_end("OK", f"{volts:.2f}V (Good)")

    def check_temp(self, sensor_temp):
        self._check_start("Chk Temp....")
        t = 27 - (sensor_temp.read_u16()*(3.3/65535)-0.706)/0.001721
        if 10 <= t <= 45: self._check_end("OK", f"{t:.1f}C (Norm)")
        else: self._check_end("WARN", f"{t:.1f}C (Abnorm)")

    def check_rtc(self, rtc):
        self._check_start("Chk RTC.....")
        try:
            raw_d = rtc.i2c.readfrom_mem(0x68, 0x04, 3)
            # ★ 修复：给移位操作加上括号，先移位，再乘 10
            yy = (raw_d[2] >> 4) * 10 + (raw_d[2] & 0x0F)
            
            if yy < 24: 
                self._check_end("WARN", "Needs Sync!")
                self.rtc_error = True 
            else: 
                self._check_end("OK", f"Valid (20{yy})")
                self.rtc_error = False # 确保标志位被重置
        except Exception: 
            self._check_end("WARN", "Comms Failed")
            self.rtc_error = True

    def check_sd(self, spi1, sd_cs):
        self._check_start("Chk Storage.")
        self.tft_cs.value(1) 
        try:
            spi1.init(baudrate=1000000); sdcard.SDCard(spi1, sd_cs); self._check_end("OK", "Card Present")
        except: self._check_end("WARN", "Not Found")
        finally: spi1.init(baudrate=40000000)
            
    def run_all(self, bat_adc, bat_en, sensor_temp, rtc, spi1, sd_cs, buzzer, p_ver, is_es):
        self.check_sys_ver(p_ver, is_es) 
        radio_ok = self.check_sx1276()
        self.check_bat(bat_adc, bat_en) # 运行电池检查
        self.check_temp(sensor_temp)
        self.check_rtc(rtc)
        self.check_sd(spi1, sd_cs)
        
        self.y += 10
        # ★ 严重错误处理逻辑：如果是电压过低或无线电损坏，强制停机
        if not radio_ok or self.has_critical_error:
            msg = b'SYSTEM HALTED - LOW POWER' if self.has_critical_error else b'SYSTEM HALTED - RADIO DEAD'
            self.tft.fill_rect(0, self.y, 320, 30, 0xF800)
            self.tft.draw_gbk(msg, 30, self.y + 6, 0xFFFF, 0xF800, scale=1)
            # 持续鸣叫报警
            buzzer.value(1); time.sleep(1.5); buzzer.value(0)
            return "HALT"
            
        if self.has_warning:
            self.tft.fill_rect(0, self.y, 320, 30, 0x2104)
            self.tft.draw_gbk(b'BOOTING WITH WARNINGS...', 30, self.y + 6, 0xFFE0, 0x2104, scale=1)
            buzzer.value(1); time.sleep(0.05); buzzer.value(0); time.sleep(0.1); buzzer.value(1); time.sleep(0.05); buzzer.value(0)
            time.sleep(0.8) 
        else:
            self.tft.draw_gbk(b'ALL SYSTEMS GO!', 40, self.y, 0x07E0, 0, scale=2)
            buzzer.value(1); time.sleep(0.2); buzzer.value(0)
            time.sleep(0.4)
            
        if self.rtc_error: return "RTC_SYNC"
        return "OK"