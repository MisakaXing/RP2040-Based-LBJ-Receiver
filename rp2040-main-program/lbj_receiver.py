import machine
import time
import rp2
import json

@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT, autopush=True, push_thresh=8)
def pocsag_rx():
    wait(0, gpio, 20)
    wait(1, gpio, 20)
    in_(pins, 1)

class LBJReceiver:
    def __init__(self, spi_id=0, sck=18, mosi=19, miso=16, cs=17, rst=15, data_pin=21, loco_file='locos.json'):
        self.loco_types = {}
        self.callback = None
        self.loco_file = loco_file
        
        self.current_address = ""
        self.numeric_output = ""
        self.error_strike = 0
        self.BCD_MAP = ['0','1','2','3','4','5','6','7','8','9','*','U',' ','-',']','[']
        self.pending_msg = None
        self.pending_time = 0
        self.last_timeout_check = 0
        
        self.sync_window = 0
        self.synced = False
        self.bit_count = 0
        self.current_cw = 0
        
        # ★ 新增：解耦架构核心！原始数据缓冲队列
        self.raw_queue = [] 
        self.last_sync_time = time.ticks_ms()
        
        self.POCSAG_SYNC  = 0x7CD215D8
        self.POCSAG_IDLE  = 0x7A89C197
        self.BCH_POLY     = 0x769
        
        self._load_loco_types()
        self._init_radio(spi_id, sck, mosi, miso, cs, rst)
        self._init_pio(data_pin)
        time.sleep_ms(100)  
        self._w(0x01, 0x01) # 切回 Standby，清空开机瞬间锁定到的环境杂音
        time.sleep_ms(5)
        self._w(0x01, 0x05) # 重新切入 RX 监听模式
        while self.sm.rx_fifo() > 0: 
            self.sm.get()   # 抽干 PIO 里开机电涌产生的乱码
            
        self.last_sync_time = time.ticks_ms() # 重置看门狗
    def set_callback(self, callback_func):
        self.callback = callback_func

    def _emit(self, data_dict):
        if self.callback: self.callback(data_dict)

    def _load_loco_types(self):
        try:
            with open(self.loco_file, 'r') as f: self.loco_types = json.load(f)
        except Exception: pass

    def _init_radio(self, spi_id, sck, mosi, miso, cs, rst):
        self.spi = machine.SPI(spi_id, baudrate=5000000,
                               sck=machine.Pin(sck), mosi=machine.Pin(mosi), miso=machine.Pin(miso))
        self.cs_pin = machine.Pin(cs, machine.Pin.OUT, value=1)
        self.rst_pin = machine.Pin(rst, machine.Pin.OUT, value=1)
        
        self.rst_pin.value(0); time.sleep_ms(10); self.rst_pin.value(1); time.sleep_ms(10)
        self._setup_pocsag(821237500, ppm_offset=6.0, bps=1200)

    def _r(self, r):
        self.cs_pin.value(0)
        self.spi.write(bytearray([r & 0x7F]))
        res = self.spi.read(1)[0]
        self.cs_pin.value(1)
        return res

    def get_rssi(self):
        try:
            val = self._r(0x11)
            return f"-{val // 2}dBm"
        except: return "N/A"

    def _w(self, r, v):
        self.cs_pin.value(0)
        self.spi.write(bytearray([r | 0x80, v]))
        self.cs_pin.value(1)

    def _setup_pocsag(self, base_freq_hz, ppm_offset, bps):
        actual_freq_hz = base_freq_hz * (1 + (ppm_offset / 1000000.0))
        self._w(0x01, 0x00); time.sleep_ms(10)
        self._w(0x01, 0x01); time.sleep_ms(10)

        bitrate = int(32000000 / bps)
        self._w(0x02, (bitrate >> 8) & 0xFF)
        self._w(0x03, bitrate & 0xFF)
        self._w(0x04, 0x00); self._w(0x05, 74) 

        frf = int((actual_freq_hz * 524288) / 32000000)
        self._w(0x06, (frf >> 16) & 0xFF)
        self._w(0x07, (frf >> 8) & 0xFF)
        self._w(0x08, frf & 0xFF)

        self._w(0x12, 0x15) 
        self._w(0x0C, 0x23) 
        self._w(0x0D, 0x50) 
        self._w(0x31, 0x00); self._w(0x40, 0x00); self._w(0x01, 0x05)

    def _init_pio(self, data_pin):
        self.sm = rp2.StateMachine(0, pocsag_rx, freq=2000000, in_base=machine.Pin(data_pin))
        self.sm.active(1)
        while self.sm.rx_fifo() > 0: self.sm.get()

    def _calc_syndrome(self, cw):
        reg = (cw >> 1) & 0x7FFFFFFF
        for i in range(30, 9, -1):
            if (reg >> i) & 1: reg ^= (self.BCH_POLY << (i - 10))
        return reg

    def _correct_bch(self, cw):
        synd = self._calc_syndrome(cw)
        parity_ok = bin(cw).count('1') % 2 == 0
        if synd == 0 and parity_ok: return cw, 0
        if synd == 0 and not parity_ok: return cw ^ 1, 1 
        if synd != 0 and not parity_ok:
            for i in range(1, 32):
                test_cw = cw ^ (1 << i)
                if self._calc_syndrome(test_cw) == 0:
                    test_cw = (test_cw & 0xFFFFFFFE) | (bin(test_cw >> 1).count('1') % 2)
                    return test_cw, 1
        if synd != 0 and parity_ok:
            for i in range(1, 32):
                for j in range(i + 1, 32):
                    test_cw = cw ^ (1 << i) ^ (1 << j)
                    if self._calc_syndrome(test_cw) == 0: return test_cw, 2
        return cw, -1

    def _bcd_to_hex(self, s):
        m = {'*':'A', 'U':'B', ' ':'C', '-': 'D', ']':'E', ')':'E', '[':'F', '(':'F'}
        return "".join([m.get(c, c if c.isdigit() else '0') for c in s])

    def _parse_basic(self, s):
        parts = [p for p in s.split(' ') if p]
        cleaned_parts = []
        i = 0
        while i < len(parts):
            if parts[i] in ['-', '+'] and i + 1 < len(parts):
                cleaned_parts.append(parts[i] + parts[i+1]); i += 2
            else:
                cleaned_parts.append(parts[i]); i += 1
        if len(cleaned_parts) >= 3:
            try: km = round(float(cleaned_parts[2]) / 10.0, 1)
            except: km = cleaned_parts[2]
            return {"train_no": cleaned_parts[0], "speed_kmh": cleaned_parts[1], "km_post": km}
        return {}

    def _parse_ext(self, s):
        if len(s) < 47: s = s + 'X' * (47 - len(s))
        try:
            cls_hex = self._bcd_to_hex(s[0:4])
            cls_tag = bytes.fromhex(cls_hex).decode('ascii').replace('\x00', '').strip() if cls_hex else "?"
            loco_raw = s[4:12]
            try:
                type_str = str(int(loco_raw[0:3]))
                loco_display = f"{self.loco_types[type_str]}-{loco_raw[4:8]}" if type_str in self.loco_types else f"未知({type_str})-{loco_raw[4:8]}"
            except: loco_display = loco_raw 
            route_hex = self._bcd_to_hex(s[14:30]) 
            return {
                "loco_type": loco_display, "loco_raw": loco_raw, "cab_end": s[12:14],
                "route_hex": route_hex, "class_tag": cls_tag,
                "lon": f"{s[30:33]}°{s[33:35]}.{s[35:39]}' E", "lat": f"{s[39:41]}°{s[41:43]}.{s[43:47]}' N"
            }
        except: return {}

    def _find_lbj_block(self, msg):
        best_score, best_idx = -1, -1
        if len(msg) < 12: return -1
        for i in range(len(msg) - 11):
            block = msg[i:i+47]
            loco_part = block[4:12]
            if ' ' in loco_part: continue
            coord_part = block[30:47] if len(block) >= 47 else block[30:]
            loco_digits = sum(1 for c in loco_part if c.isdigit())
            coord_digits = sum(1 for c in coord_part if c.isdigit())
            score = (loco_digits * 3) + coord_digits
            if loco_digits >= 6 or (loco_digits >= 4 and coord_digits >= 4):
                if score > best_score: best_score, best_idx = score, i
        return best_idx

    def _parse_train_data(self, msg):
        msg_clean = msg.strip(' \r\n\t\x00')
        if not msg_clean: return {"type": "empty", "raw": msg}
        if msg_clean.startswith(('*', '-')) and len(msg_clean) >= 5 and msg_clean[1:5].isdigit():
            return {"type": "time_sync", "time": f"{msg_clean[1:3]}:{msg_clean[3:5]}", "raw": msg_clean}

        lbj_start_idx = self._find_lbj_block(msg_clean)
        if lbj_start_idx != -1:
            lbj_block = msg_clean[lbj_start_idx:lbj_start_idx+47]
            basic_str = msg_clean[:lbj_start_idx].strip()
            ext_dict = self._parse_ext(lbj_block)
            if basic_str:
                basic_dict = self._parse_basic(basic_str)
                if basic_dict and "train_no" in basic_dict:
                    return {"type": "train_data_full", "raw": msg_clean, "basic": basic_dict, "extended": ext_dict}
                return {"type": "extended_only", "raw": msg_clean, "extended": ext_dict, "garbage_prefix": basic_str}
            return {"type": "extended_only", "raw": msg_clean, "extended": ext_dict}
        else:
            basic_dict = self._parse_basic(msg_clean)
            if basic_dict and "train_no" in basic_dict: return {"type": "basic_only", "raw": msg_clean, "basic": basic_dict}
            return {"type": "unknown", "raw": msg_clean}

    def _handle_parsed_msg(self, msg):
        now = time.ticks_ms()
        if self.pending_msg and msg.get("ric") == self.pending_msg.get("ric"):
            p_type, c_type = self.pending_msg.get("type"), msg.get("type")
            if (p_type == "basic_only" and c_type == "extended_only") or (p_type == "extended_only" and c_type == "basic_only"):
                merged = {
                    "type": "train_data_merged", "ric": msg["ric"],
                    "raw": self.pending_msg["raw"] + " | " + msg["raw"],
                    "basic": self.pending_msg["basic"] if p_type == "basic_only" else msg["basic"],
                    "extended": self.pending_msg["extended"] if p_type == "extended_only" else msg["extended"]
                }
                self._emit(merged)
                self.pending_msg = None
                return
        if self.pending_msg:
            self._emit(self.pending_msg)
            self.pending_msg = None
        if msg.get("type") in ["basic_only", "extended_only"]:
            self.pending_msg = msg
            self.pending_time = now
        else:
            self._emit(msg)

    # ★ 核心修复 3：不再原地解析，只扔进缓冲队列！
    def _flush_message(self):
        if self.numeric_output:
            self.raw_queue.append((self.current_address, self.numeric_output))
        self.numeric_output = ""
        self.current_address = ""
        self.error_strike = 0

    def tick(self):
        now = time.ticks_ms()
        
        # 1. 射频自愈看门狗
        if time.ticks_diff(now, self.last_sync_time) > 30000:
            self._w(0x01, 0x01) 
            time.sleep_ms(2)
            self._w(0x01, 0x05) 
            while self.sm.rx_fifo() > 0: self.sm.get()
            self.sync_window = 0
            self.synced = False
            self.last_sync_time = now

        # ========================================================
        # ★ 核心修复 1：把 if 换成了 while，瞬间榨干 PIO 缓存，绝不溢出丢包！
        # ========================================================
        while self.sm.rx_fifo() > 0:
            byte = (self.sm.get() ^ 0xFF) & 0xFF 
            for i in range(7, -1, -1):
                bit = (byte >> i) & 1
                if not self.synced:
                    self.sync_window = ((self.sync_window << 1) | bit) & 0xFFFFFFFF
                    if self.sync_window == self.POCSAG_SYNC:
                        self.synced = True
                        self.bit_count = self.current_cw = 0
                        self.last_sync_time = time.ticks_ms() 
                else:
                    self.current_cw = ((self.current_cw << 1) | bit) & 0xFFFFFFFF
                    self.bit_count += 1
                    if self.bit_count == 32:
                        if self.current_cw != self.POCSAG_SYNC:
                            cw_fixed, err_status = self._correct_bch(self.current_cw)
                            if err_status == -1:
                                if self.current_address:
                                    self.numeric_output += "XXXXX"
                                    self.error_strike += 1
                                if self.error_strike >= 3:
                                    self._flush_message()
                                    self.synced = False
                            else:
                                if cw_fixed == self.POCSAG_IDLE:
                                    self._flush_message()
                                else:
                                    is_message = (cw_fixed >> 31) & 1
                                    payload = (cw_fixed >> 11) & 0xFFFFF
                                    if is_message == 0:
                                        ric = (payload >> 2) & 0x3FFFF
                                        func = payload & 0x03
                                        if ric not in [0, 2097151]:
                                            self._flush_message()
                                            self.current_address = f"{ric}-F{func}"
                                    else:
                                        if self.current_address:
                                            self.error_strike = 0 
                                            for j in range(4, -1, -1):
                                                nibble = (payload >> (j * 4)) & 0x0F
                                                nibble_rev = ((nibble & 1) << 3) | ((nibble & 2) << 1) | ((nibble & 4) >> 1) | ((nibble & 8) >> 3)
                                                self.numeric_output += self.BCD_MAP[nibble_rev]
                        self.bit_count = self.current_cw = 0

        # ========================================================
        # ★ 核心修复 2：异步处理队列里的数据，只有等底层射频缓存全空了，才允许 CPU 解析大段 JSON！
        # ========================================================
        if len(self.raw_queue) > 0 and self.sm.rx_fifo() == 0:
            addr, raw = self.raw_queue.pop(0)
            try:
                parsed = self._parse_train_data(raw)
                if parsed.get("type") != "empty":
                    parsed["ric"] = addr
                    self._handle_parsed_msg(parsed)
            except Exception as e:
                self._emit({"type": "error", "ric": addr, "raw": raw, "error": str(e)})

        # 2. 尾巴报文合并超时触发
        if time.ticks_diff(now, self.last_timeout_check) > 100:
            if self.pending_msg and time.ticks_diff(now, self.pending_time) > 2000:
                self._emit(self.pending_msg)
                self.pending_msg = None
            self.last_timeout_check = now
