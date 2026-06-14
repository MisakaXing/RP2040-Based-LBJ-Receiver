import machine
import time
import rp2
import json

# 使用 jmp_pin 进行相对映射
@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT, autopush=True, push_thresh=32)
def pocsag_rx():
    label("wait_low")
    jmp(pin, "wait_low")  # 如果 jmp_pin(时钟) 为高电平，就在这死循环等它变低
    
    label("wait_high")
    jmp(pin, "read_data") # 如果 jmp_pin(时钟) 变高了，跳去读数据 (上升沿触发)
    jmp("wait_high")      # 否则继续等它变高
    
    label("read_data")
    in_(pins, 1)          # 从 in_base(数据) 引脚读取 1 个 bit 存入移位寄存器

class LBJReceiver:
    FXOSC = 32000000
    FRF_SCALE = 524288
    FSTEP_HZ = FXOSC / FRF_SCALE
    BASE_FREQ_HZ = 821237500

    # Stable Direct-mode profile: fixed +6 ppm tuning with per-burst hardware
    # AFC. FEI is diagnostic only and must never accumulate into ppm_offset.
    FEI_MAX_HZ = 20000
    PPM_LIMIT = 25.0
    PRINT_FEI_CORRECTION = True

    RXBW = 0x0D  # 12.5 kHz: mantissa 20, exponent 5.
    AFCBW = 0x0B  # 50 kHz acquisition bandwidth.
    LNA_FIXED_GAIN_BOOST = 0x23  # Gain 001 + LnaBoostHf, AGC disabled.
    PREAMBLE_DETECT = 0xAA  # Enabled, 2-byte detector, tolerance 10.
    RXCONFIG_AFC_PREAMBLE = 0x16  # AFC auto, AGC off, preamble trigger.
    PPM_SCAN_INTERVAL_MS = 20000
    PPM_SCAN_STEPS = (0, 1, -1, 2, -2, 3, -3)
    CALIBRATION_SAMPLES = 3
    CALIBRATION_SPREAD_PPM = 0.75
    NUMERIC_CHARS_PER_WORD = 5
    LBJ_BLOCK_LEN = 47
    MIN_LBJ_BLOCK_SCORE = 120
    VALID_CAB_ENDS = ("30", "31", "32")

    REG_FRF_MSB = 0x06
    REG_FRF_MID = 0x07
    REG_FRF_LSB = 0x08
    REG_RXBW = 0x12
    REG_AFCBW = 0x13
    REG_RXCONFIG = 0x0D
    REG_AFCFEI = 0x1A
    REG_AFCMSB = 0x1B
    REG_AFCLSB = 0x1C
    REG_FEIMSB = 0x1D
    REG_FEILSB = 0x1E
    REG_PREAMBLEDETECT = 0x1F

    def __init__(self, spi_id=0, sck=18, mosi=19, miso=16, cs=17, rst=15,
                 data_pin=21, clk_pin=20, loco_file='locos.json',
                 ppm_offset=6.0, enable_ppm_scan=True, enable_calibration=True):
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
        
        self.raw_queue = [] 
        self.last_sync_time = time.ticks_ms()
        
        self.current_rssi = "N/A"
        self.rssi_val = "N/A" 

        self.base_freq_hz = self.BASE_FREQ_HZ
        self.ppm_offset = ppm_offset
        self.saved_ppm_offset = ppm_offset
        self.pending_fei_hz = None
        self.pending_afc_hz = None
        self.last_fei_hz = 0.0
        self.last_fei_ppm = 0.0
        self.last_afc_hz = 0.0
        self.calibrated_ppm_offset = None
        self.calibration_samples = []
        self.calibration_enabled = enable_calibration
        self.ppm_scan_index = 0
        self.ppm_scan_enabled = enable_ppm_scan
        self.last_ppm_scan_time = time.ticks_ms()
        print("BOOT_PPM", self.ppm_offset,
              "scan=", self.ppm_scan_enabled,
              "calibration=", self.calibration_enabled)
        
        self.POCSAG_SYNC  = 0x7CD215D8
        self.POCSAG_IDLE  = 0x7A89C197
        self.BCH_POLY     = 0x769
        
        # 在初始化时预计算 BCH 纠错表，只需执行一次
        self._init_syndrome_table()
        
        self._load_loco_types()
        self._init_radio(spi_id, sck, mosi, miso, cs, rst)
        self._init_pio(data_pin, clk_pin)
        time.sleep_ms(100)  
        self._w(0x01, 0x01) 
        time.sleep_ms(5)
        self._w(0x01, 0x05) 
        while self.sm.rx_fifo() > 0: 
            self.sm.get()   
            
        self.last_sync_time = time.ticks_ms()

    #预计算 1-bit 和 2-bit 错误的校验子查表
    def _init_syndrome_table(self):
        self.syndrome_table = {}
        # 预计算 1-bit 错误 (数据位在 bits 1~31)
        for i in range(1, 32):
            mask = 1 << i
            synd = self._calc_syndrome(mask)
            self.syndrome_table[synd] = (mask, 1) # 记录 (错误掩码, 错误位数)
            
        # 预计算 2-bit 错误 (数据位在 bits 1~31)
        for i in range(1, 32):
            for j in range(i + 1, 32):
                mask = (1 << i) | (1 << j)
                synd = self._calc_syndrome(mask)
                self.syndrome_table[synd] = (mask, 2)

    def set_callback(self, callback_func):
        self.callback = callback_func

    def _emit(self, data_dict):
        if self.callback: self.callback(data_dict)

    def _load_loco_types(self):
        try:
            with open(self.loco_file, 'r') as f: self.loco_types = json.load(f)
        except Exception: pass

    def _init_radio(self, spi_id, sck, mosi, miso, cs, rst):
        self.spi = machine.SPI(spi_id, baudrate=2000000, polarity=0, phase=0,
                               sck=machine.Pin(sck), mosi=machine.Pin(mosi), miso=machine.Pin(miso))
        self.cs_pin = machine.Pin(cs, machine.Pin.OUT, value=1)
        self.rst_pin = machine.Pin(rst, machine.Pin.OUT, value=1)
        
        self.rst_pin.value(0); time.sleep_ms(10); self.rst_pin.value(1); time.sleep_ms(10)
        
        chip_ver = self._r(0x42)
        if chip_ver in [0x00, 0xFF]:
            print(f"⚠️ 严重警告: 射频芯片通信失败! 读到版本号: {hex(chip_ver)}。请检查 SPI 接线！")
            
        self._setup_pocsag(self.base_freq_hz, ppm_offset=self.ppm_offset, bps=1200)

    def _r(self, r):
        self.cs_pin.value(0)
        self.spi.write(bytearray([r & 0x7F]))
        res = self.spi.read(1)[0]
        self.cs_pin.value(1)
        return res

    def get_rssi(self):
        try:
            val = self._r(0x11)
            corrected_rssi = -(val // 2)
            return f"{corrected_rssi:.1f}dBm"
        except: return "N/A"

    def _w(self, r, v):
        self.cs_pin.value(0)
        self.spi.write(bytearray([r | 0x80, v]))
        self.cs_pin.value(1)

    def _read_s16(self, msb_reg, lsb_reg):
        raw = (self._r(msb_reg) << 8) | self._r(lsb_reg)
        if raw & 0x8000:
            raw -= 0x10000
        return raw

    def _read_fei_hz(self):
        try:
            fei_raw = self._read_s16(self.REG_FEIMSB, self.REG_FEILSB)
            return fei_raw * self.FSTEP_HZ
        except:
            return None

    def _read_afc_hz(self):
        try:
            afc_raw = self._read_s16(self.REG_AFCMSB, self.REG_AFCLSB)
            return afc_raw * self.FSTEP_HZ
        except:
            return None

    def _set_frequency_from_ppm(self, ppm_offset):
        if ppm_offset > self.PPM_LIMIT:
            ppm_offset = self.PPM_LIMIT
        elif ppm_offset < -self.PPM_LIMIT:
            ppm_offset = -self.PPM_LIMIT

        self.ppm_offset = ppm_offset
        actual_freq_hz = self.base_freq_hz * (1 + (ppm_offset / 1000000.0))
        frf = int((actual_freq_hz * self.FRF_SCALE) / self.FXOSC)

        self._w(self.REG_FRF_MSB, (frf >> 16) & 0xFF)
        self._w(self.REG_FRF_MID, (frf >> 8) & 0xFF)
        self._w(self.REG_FRF_LSB, frf & 0xFF)

    def _clamp_ppm(self, ppm_offset):
        if ppm_offset > self.PPM_LIMIT:
            return self.PPM_LIMIT
        if ppm_offset < -self.PPM_LIMIT:
            return -self.PPM_LIMIT
        return ppm_offset

    def _update_calibration(self, corrected_ppm):
        if not self.calibration_enabled:
            return

        self.calibration_samples.append(corrected_ppm)
        if len(self.calibration_samples) > self.CALIBRATION_SAMPLES:
            self.calibration_samples.pop(0)

        print("PPM_CAL_SAMPLE", corrected_ppm,
              "count=", len(self.calibration_samples))

        if len(self.calibration_samples) < self.CALIBRATION_SAMPLES:
            return

        low = min(self.calibration_samples)
        high = max(self.calibration_samples)
        if high - low > self.CALIBRATION_SPREAD_PPM:
            return

        calibrated_ppm = sum(self.calibration_samples) / len(self.calibration_samples)
        self.calibrated_ppm_offset = self._clamp_ppm(calibrated_ppm)
        self.calibration_enabled = False
        self.ppm_scan_enabled = False
        self._set_frequency_from_ppm(self.calibrated_ppm_offset)
        print("PPM_CALIBRATED", self.calibrated_ppm_offset)

    def _report_pending_frequency_error(self):
        if self.pending_fei_hz is None:
            return

        fei_hz = self.pending_fei_hz
        afc_hz = self.pending_afc_hz
        self.pending_fei_hz = None
        self.pending_afc_hz = None

        if abs(fei_hz) > self.FEI_MAX_HZ:
            print("FEI_REJECT", "fei_hz=", fei_hz,
                  "fixed_ppm=", self.ppm_offset)
            return

        fei_ppm = (fei_hz / self.base_freq_hz) * 1000000.0
        self.last_fei_hz = fei_hz
        self.last_fei_ppm = fei_ppm
        if afc_hz is not None:
            self.last_afc_hz = afc_hz

        if self.PRINT_FEI_CORRECTION:
            print("AFC_LOCK",
                  "fei_hz=", fei_hz,
                  "fei_ppm=", fei_ppm,
                  "afc_hz=", afc_hz,
                  "fixed_ppm=", self.ppm_offset)

    def _setup_pocsag(self, base_freq_hz, ppm_offset, bps):
        self.base_freq_hz = base_freq_hz
        self.ppm_offset = ppm_offset
        self.saved_ppm_offset = ppm_offset
        self._w(0x01, 0x00); time.sleep_ms(10)
        self._w(0x01, 0x01); time.sleep_ms(10)

        bitrate = int(32000000 / bps)
        self._w(0x02, (bitrate >> 8) & 0xFF)
        self._w(0x03, bitrate & 0xFF)
        self._w(0x04, 0x00); self._w(0x05, 74) 

        self._set_frequency_from_ppm(ppm_offset)

        self._w(self.REG_RXBW, self.RXBW)
        self._w(self.REG_AFCBW, self.AFCBW)
        self._w(0x0C, self.LNA_FIXED_GAIN_BOOST)
        self._w(0x31, 0x00)  # Continuous Direct mode.
        self._w(0x40, 0x00)  # DIO1=DCLK, DIO2=DATA.
        self._w(self.REG_PREAMBLEDETECT, self.PREAMBLE_DETECT)
        self._w(self.REG_AFCFEI, 0x02)  # Clear stale AFC before entering RX.
        self._w(self.REG_AFCFEI, 0x01)  # Auto-clear AFC for each trigger.
        self._w(self.REG_RXCONFIG, self.RXCONFIG_AFC_PREAMBLE)
        self._w(0x01, 0x05)
        print("RADIO_CFG",
              "freq_hz=", int(self.base_freq_hz * (1 + self.ppm_offset / 1000000.0)),
              "ppm=", self.ppm_offset,
              "rxbw_hz=12500",
              "afc=hardware",
              "agc=off",
              "lna=0x23")

    def _init_pio(self, data_pin, clk_pin=20):
        self.hardware_clk = machine.Pin(clk_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self.hardware_data = machine.Pin(data_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        
        self.sm = rp2.StateMachine(0, pocsag_rx, freq=2000000, 
                                   in_base=self.hardware_data, 
                                   jmp_pin=self.hardware_clk)
        self.sm.active(1)
        while self.sm.rx_fifo() > 0: 
            self.sm.get()

    def _calc_syndrome(self, cw):
        reg = (cw >> 1) & 0x7FFFFFFF
        for i in range(30, 9, -1):
            if (reg >> i) & 1: reg ^= (self.BCH_POLY << (i - 10))
        return reg

    # 纯位运算校验偶校验，摒弃巨慢的字符串转换 bin(cw).count('1')
    def _parity_check(self, cw):
        cw ^= cw >> 16
        cw ^= cw >> 8
        cw ^= cw >> 4
        cw ^= cw >> 2
        cw ^= cw >> 1
        return (cw & 1) == 0

    #重写 BCH 纠错，O(N^2) 穷举变为 O(1) 查表
    def _correct_bch(self, cw):
        synd = self._calc_syndrome(cw)
        parity_ok = self._parity_check(cw)

        # 0 bit 错误 (或仅仅是偶校验位错了)
        if synd == 0:
            if parity_ok:
                return cw, 0       # 完美无错
            else:
                return cw ^ 1, 1   # 数据段正确，仅第 0 位(校验位)错误

        # 查表匹配 1-bit 和 2-bit 错误图谱
        match = self.syndrome_table.get(synd)
        if match is not None:
            err_mask, err_count = match
            
            if not parity_ok:
                # 校验失败(奇数个错): 查到 1-bit 错说明是 1个数据位错误
                if err_count == 1:
                    return cw ^ err_mask, 1
            else:
                # 校验成功(偶数个错)
                if err_count == 1:
                    # 查到 1-bit 数据位错误，但校验和却成功了，说明校验位(bit 0)也跟着错了一个，共错 2 位
                    return cw ^ err_mask ^ 1, 2
                elif err_count == 2:
                    # 查到 2-bit 数据位错误
                    return cw ^ err_mask, 2

        # 查表无果，说明大于 2-bit 错误，无法纠正
        return cw, -1

    def _bcd_to_hex(self, s):
        m = {'*':'A', 'U':'B', ' ':'C', '-': 'D', ']':'E', ')':'E', '[':'F', '(':'F'}
        return "".join([m.get(c, c if c.isdigit() else '0') for c in s])

    def _decode_class_tag(self, raw):
        if len(raw) != 4 or 'X' in raw:
            return "?", False
        try:
            class_bytes = bytes.fromhex(self._bcd_to_hex(raw))
            if any(value != 0 and not 32 <= value <= 126 for value in class_bytes):
                return "?", False
            return class_bytes.decode('ascii').replace('\x00', '').strip(), True
        except:
            return "?", False

    def _resolve_loco_code(self, raw_code):
        if len(raw_code) != 3:
            return raw_code, None
        if raw_code.isdigit():
            code = str(int(raw_code))
            return code, self.loco_types.get(code)
        if any(char != 'X' and not char.isdigit() for char in raw_code):
            return raw_code, None

        matched_code = None
        matched_name = None
        for code, name in self.loco_types.items():
            padded = str(code).zfill(3)
            if len(padded) != 3:
                continue
            if all(a == 'X' or a == b for a, b in zip(raw_code, padded)):
                if matched_code is not None:
                    return raw_code, None
                matched_code = str(code)
                matched_name = name
        return matched_code if matched_code is not None else raw_code, matched_name

    def _score_lbj_candidate(self, block):
        # 失败码字已经由 XXXXX 占满 5 个字符；不足 47 字符表示消息被截断，
        # 不能再补位后猜测，否则报文尾部数字很容易被误认成车型。
        if len(block) < self.LBJ_BLOCK_LEN:
            return None

        class_raw = block[0:4]
        loco_raw = block[4:12]
        cab_raw = block[12:14]
        if len(loco_raw) != 8:
            return None
        if any(char != 'X' and not char.isdigit() for char in loco_raw):
            return None

        type_raw = loco_raw[0:3]
        number_raw = loco_raw[4:8]
        type_digits = sum(char.isdigit() for char in type_raw)
        number_digits = sum(char.isdigit() for char in number_raw)
        if type_digits < 2 or number_digits < 2:
            return None

        type_code, type_name = self._resolve_loco_code(type_raw)
        class_tag, class_valid = self._decode_class_tag(class_raw)

        score = 0
        score += 100 if type_name is not None else 20
        score += type_digits * 10
        score += number_digits * 12
        score += 45 if class_valid else -15

        if loco_raw[3].isdigit():
            score += 8
            if loco_raw[3] == '0':
                score += 4

        if cab_raw in self.VALID_CAB_ENDS:
            score += 35
        elif cab_raw.isdigit():
            score += 8
        elif all(char == 'X' or char.isdigit() for char in cab_raw):
            score -= 5
        else:
            score -= 20

        coord_raw = block[30:self.LBJ_BLOCK_LEN]
        score += sum(char.isdigit() for char in coord_raw)
        if len(block) >= self.LBJ_BLOCK_LEN:
            score += 5

        return score, type_code, type_name, class_tag

    def _parse_basic(self, s):
        parts = [p for p in s.split(' ') if p]
        cleaned_parts = []
        i = 0
        while i < len(parts):
            if parts[i] in ['-', '+'] and i + 1 < len(parts):
                cleaned_parts.append(parts[i] + parts[i+1]); i += 2
            else:
                cleaned_parts.append(parts[i]); i += 1

        # 基础报文固定为“车次 速度 公里标”。不可纠正码字产生的 XXXXX
        # 也可能恰好被空格分成三段，不能仅凭字段数量就认定为有效报文。
        if len(cleaned_parts) != 3:
            return {}

        train_no, speed_raw, km_raw = cleaned_parts
        if not train_no.isdigit() or not 1 <= len(train_no) <= 8:
            return {}
        if 'X' in speed_raw or 'X' in km_raw:
            return {}

        try:
            speed = float(speed_raw)
            km_value = float(km_raw)
        except:
            return {}

        if abs(speed) > 500 or abs(km_value) > 1000000:
            return {}

        return {
            "train_no": train_no,
            "speed_kmh": speed_raw,
            "km_post": round(km_value / 10.0, 1)
        }

    def _parse_ext(self, s):
        if len(s) < self.LBJ_BLOCK_LEN:
            s = s + 'X' * (self.LBJ_BLOCK_LEN - len(s))
        try:
            cls_tag, class_valid = self._decode_class_tag(s[0:4])
            loco_raw = s[4:12]
            type_str, type_name = self._resolve_loco_code(loco_raw[0:3])
            if type_name is not None:
                loco_display = f"{type_name}-{loco_raw[4:8]}"
            elif loco_raw[0:3].isdigit():
                loco_display = f"UNK({type_str})-{loco_raw[4:8]}"
            else:
                loco_display = loco_raw

            route_raw = s[14:30]
            route_hex = "" if 'X' in route_raw else self._bcd_to_hex(route_raw)
            return {
                "loco_type": loco_display, "loco_raw": loco_raw, "cab_end": s[12:14],
                "route_hex": route_hex, "class_tag": cls_tag,
                "class_valid": class_valid,
                "lon": f"{s[30:33]}°{s[33:35]}.{s[35:39]}' E", "lat": f"{s[39:41]}°{s[41:43]}.{s[43:47]}' N"
            }
        except: return {}

    def _find_lbj_block(self, msg):
        best_score, best_idx = -1, -1
        best_info = None
        if len(msg) < 12: return -1

        # 每个 POCSAG 数字消息码字固定产生 5 个字符。不可纠正码字也以
        # "XXXXX" 占位，因此扩展块只能从 5 字符边界开始，不能逐字符滑动。
        for i in range(0, len(msg) - 11, self.NUMERIC_CHARS_PER_WORD):
            block = msg[i:i+self.LBJ_BLOCK_LEN]
            candidate = self._score_lbj_candidate(block)
            if candidate is None:
                continue
            score, type_code, type_name, class_tag = candidate
            if score > best_score:
                best_score = score
                best_idx = i
                best_info = (type_code, type_name, class_tag, block[4:12])

        if best_score < self.MIN_LBJ_BLOCK_SCORE:
            return -1

        if best_idx != -1 and 'X' in msg:
            print("LBJ_ALIGN",
                  "start=", best_idx,
                  "score=", best_score,
                  "loco=", best_info[3],
                  "type=", best_info[1] if best_info[1] is not None else best_info[0])
        return best_idx

    def _parse_train_data(self, msg):
        # 保留开头空格，它们也是 5 字符码字相位的一部分。
        msg_clean = msg.rstrip('\r\n\t\x00')
        if not msg_clean or not msg_clean.strip(' '):
            return {"type": "empty", "raw": msg}

        time_msg = msg_clean.lstrip(' ')
        if time_msg.startswith(('*', '-')) and len(time_msg) >= 5 and time_msg[1:5].isdigit():
            return {"type": "time_sync", "time": f"{time_msg[1:3]}:{time_msg[3:5]}", "raw": msg_clean}

        lbj_start_idx = self._find_lbj_block(msg_clean)
        if lbj_start_idx != -1:
            lbj_block = msg_clean[lbj_start_idx:lbj_start_idx+self.LBJ_BLOCK_LEN]
            basic_str = msg_clean[:lbj_start_idx].strip()
            ext_dict = self._parse_ext(lbj_block)
            ext_dict["block_start"] = lbj_start_idx
            if basic_str:
                basic_dict = self._parse_basic(basic_str)
                if basic_dict and "train_no" in basic_dict:
                    return {"type": "train_data_full", "raw": msg_clean, "basic": basic_dict, "extended": ext_dict}
                return {"type": "extended_only", "raw": msg_clean, "extended": ext_dict, "garbage_prefix": basic_str}
            return {"type": "extended_only", "raw": msg_clean, "extended": ext_dict}
        else:
            basic_dict = self._parse_basic(msg_clean)
            if basic_dict and "train_no" in basic_dict: return {"type": "basic_only", "raw": msg_clean, "basic": basic_dict}
            x_count = msg_clean.count('X')
            if x_count:
                return {
                    "type": "corrupt",
                    "raw": msg_clean,
                    "error": "uncorrectable_codewords",
                    "x_count": x_count
                }
            return {"type": "unknown", "raw": msg_clean}

    def _handle_parsed_msg(self, msg):
        if msg.get("type") == "corrupt":
            print("LBJ_DROP_CORRUPT",
                  "ric=", msg.get("ric", ""),
                  "x=", msg.get("x_count", 0),
                  "len=", len(msg.get("raw", "")))
            return

        now = time.ticks_ms()
        if self.pending_msg and msg.get("ric") == self.pending_msg.get("ric"):
            p_type, c_type = self.pending_msg.get("type"), msg.get("type")
            if (p_type == "basic_only" and c_type == "extended_only") or (p_type == "extended_only" and c_type == "basic_only"):
                merged = {
                    "type": "train_data_merged", 
                    "ric": msg["ric"],
                    "rssi": msg.get("rssi", "N/A"), 
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

    def _flush_message(self):
        self._report_pending_frequency_error()
        if self.numeric_output:
            self.raw_queue.append((self.current_address, self.numeric_output, self.current_rssi))
        self.numeric_output = ""
        self.current_address = ""
        self.error_strike = 0

    def _cold_start_ppm_scan(self, now):
        if not self.ppm_scan_enabled or self.synced:
            return
        if time.ticks_diff(now, self.last_ppm_scan_time) < self.PPM_SCAN_INTERVAL_MS:
            return

        self.ppm_scan_index = (self.ppm_scan_index + 1) % len(self.PPM_SCAN_STEPS)
        scan_ppm = self.saved_ppm_offset + self.PPM_SCAN_STEPS[self.ppm_scan_index]
        self._set_frequency_from_ppm(scan_ppm)
        self.last_ppm_scan_time = now
        print("PPM_SCAN", scan_ppm)

    def tick(self):
        now = time.ticks_ms()
        self._cold_start_ppm_scan(now)
        
        if time.ticks_diff(now, self.last_sync_time) > 30000:
            self._w(0x01, 0x01) 
            time.sleep_ms(2)
            self._w(0x01, 0x05) 
            while self.sm.rx_fifo() > 0: self.sm.get()
            self.sync_window = 0
            self.synced = False
            self.last_sync_time = now

        while self.sm.rx_fifo() > 0:
            word = (self.sm.get() ^ 0xFFFFFFFF) & 0xFFFFFFFF 
            for i in range(31, -1, -1):
                bit = (word >> i) & 1
                if not self.synced:
                    self.sync_window = ((self.sync_window << 1) | bit) & 0xFFFFFFFF
                    if self.sync_window == self.POCSAG_SYNC:
                        self.synced = True
                        self.saved_ppm_offset = self.ppm_offset
                        self.ppm_scan_enabled = False
                        self.bit_count = self.current_cw = 0
                        self.last_sync_time = time.ticks_ms() 
                        self.current_rssi = self.get_rssi()
                        self.rssi_val = self.current_rssi 
                        fei_hz = self._read_fei_hz()
                        if fei_hz is not None:
                            self.pending_fei_hz = fei_hz
                            self.pending_afc_hz = self._read_afc_hz()
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

        if len(self.raw_queue) > 0:
            addr, raw, pkt_rssi = self.raw_queue.pop(0)
            try:
                parsed = self._parse_train_data(raw)
                if parsed.get("type") != "empty":
                    parsed["ric"] = addr
                    parsed["rssi"] = pkt_rssi 
                    self._handle_parsed_msg(parsed)
            except Exception as e:
                self._emit({"type": "error", "ric": addr, "raw": raw, "error": str(e)})

        if time.ticks_diff(now, self.last_timeout_check) > 100:
            if self.pending_msg and time.ticks_diff(now, self.pending_time) > 2000:
                self._emit(self.pending_msg)
                self.pending_msg = None
            self.last_timeout_check = now
