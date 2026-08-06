import machine
import time
import rp2
import json

# 使用 jmp_pin 进行相对映射
@rp2.asm_pio(
    in_shiftdir=rp2.PIO.SHIFT_LEFT,
    autopush=True,
    push_thresh=32,
    fifo_join=rp2.PIO.JOIN_RX,
)
def pocsag_rx():
    label("wait_low")
    jmp(pin, "wait_low")  # 如果 jmp_pin(时钟) 为高电平，就在这死循环等它变低
    
    label("wait_high")
    jmp(pin, "read_data") # 如果 jmp_pin(时钟) 变高了，跳去读数据 (上升沿触发)
    jmp("wait_high")      # 否则继续等它变高
    
    label("read_data")
    in_(pins, 1)          # 从 in_base(数据) 引脚读取 1 个 bit 存入移位寄存器


class FixedQueue:
    """Small allocation-free FIFO with a hard memory limit."""

    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._items = [None] * capacity
        self._capacity = capacity
        self._head = 0
        self._tail = 0
        self._count = 0
        self.dropped = 0

    def __len__(self):
        return self._count

    def put(self, item):
        dropped = self._count == self._capacity
        if dropped:
            self._items[self._head] = None
            self._head = (self._head + 1) % self._capacity
            self._count -= 1
            self.dropped = (self.dropped + 1) & 0x3FFFFFFF

        self._items[self._tail] = item
        self._tail = (self._tail + 1) % self._capacity
        self._count += 1
        return not dropped

    def get(self):
        if self._count == 0:
            return None
        item = self._items[self._head]
        self._items[self._head] = None
        self._head = (self._head + 1) % self._capacity
        self._count -= 1
        return item

    def clear(self):
        while self._count:
            self.get()

class LBJReceiver:
    COUNTER_MASK = 0x3FFFFFFF
    FXOSC = 32000000
    FRF_SCALE = 524288
    FSTEP_HZ = FXOSC / FRF_SCALE
    BASE_FREQ_HZ = 821237500

    # The LBJ service occupies this POCSAG RIC block.  The reference
    # implementation listens with address 1234000 / mask 0xFFFF0, covering
    # basic (1234000), extended (1234002), and time sync (1234008) messages.
    LBJ_RIC_BASE = 1234000
    LBJ_RIC_MASK = 0xFFFF0
    LBJ_BASIC_RIC = 1234000
    LBJ_EXTENDED_RIC = 1234002
    LBJ_TIME_RIC = 1234008

    # Stable Direct-mode profile: fixed +6 ppm tuning with per-burst hardware
    # AFC. FEI is diagnostic only and must never accumulate into ppm_offset.
    FEI_MAX_HZ = 20000
    PPM_LIMIT = 25.0
    PRINT_FEI_CORRECTION = False
    READ_FEI_ON_SYNC = True
    DIAGNOSTIC_LOG_INTERVAL_MS = 5000

    RXBW = 0x0D  # 12.5 kHz: mantissa 20, exponent 5.
    AFCBW = 0x0B  # 50 kHz acquisition bandwidth.
    LNA_FIXED_GAIN_BOOST = 0x23  # Gain 001 + LnaBoostHf, AGC disabled.
    PREAMBLE_DETECT = 0xAA  # Enabled, 2-byte detector, tolerance 10.
    RXCONFIG_AFC_PREAMBLE = 0x16  # AFC auto, AGC off, preamble trigger.
    NUMERIC_CHARS_PER_WORD = 5
    LBJ_BLOCK_LEN = 47
    MIN_LBJ_BLOCK_SCORE = 120
    VALID_CAB_ENDS = ("30", "31", "32")

    RAW_QUEUE_CAPACITY = 12
    RAW_PARSE_BUDGET = 3
    MAX_PENDING_FRAGMENTS = 6
    # The reference receiver reads basic/extended together from one short
    # POCSAG burst. Keep a small window so adjacent trains cannot cross-pair.
    MERGE_TIMEOUT_MS = 1500
    MAX_BAD_CODEWORDS = 3
    HEALTH_CHECK_INTERVAL_MS = 10000
    RECOVERY_COOLDOWN_MS = 2000
    RADIO_PROFILE_MONITOR_ENABLED = True

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
                 ppm_offset=6.0):
        self.loco_types = {}
        self.callback = None
        self.loco_file = loco_file
        
        self.current_address = ""
        self.numeric_output = ""
        self.bad_codeword_streak = 0
        self.BCD_MAP = ['0','1','2','3','4','5','6','7','8','9','*','U',' ','-',']','[']
        self.pending_fragments = []
        self.last_timeout_check = 0
        
        self.sync_window = 0
        self.synced = False
        self.bit_count = 0
        self.current_cw = 0
        self.batch_position = 0
        # Mirrors the reference receiver's readDataMSA transaction: one group
        # runs from an LBJ address until an idle word or foreign address.
        self.rx_group_id = 0
        
        self.raw_queue = FixedQueue(self.RAW_QUEUE_CAPACITY)
        self.last_sync_time = time.ticks_ms()
        self.last_word_time = self.last_sync_time
        self.last_health_check = self.last_sync_time
        self.last_recovery_time = time.ticks_add(
            self.last_sync_time, -self.RECOVERY_COOLDOWN_MS
        )
        self.last_corrupt_log = time.ticks_add(
            self.last_sync_time, -self.DIAGNOSTIC_LOG_INTERVAL_MS
        )
        self.last_resync_log = self.last_corrupt_log
        self.last_align_log = self.last_corrupt_log
        self.last_queue_drop_log = self.last_corrupt_log
        
        self.current_rssi = "N/A"
        self.rssi_val = "N/A" 

        self.base_freq_hz = self.BASE_FREQ_HZ
        self.ppm_offset = ppm_offset
        self.pending_fei_hz = None
        self.pending_afc_hz = None
        self.last_fei_hz = 0.0
        self.last_fei_ppm = 0.0
        self.last_afc_hz = 0.0
        self.words_seen = 0
        self.sync_count = 0
        self.corrected_sync_words = 0
        self.soft_sync_locks = 0
        self.lbj_address_words = 0
        self.foreign_address_words = 0
        self.codewords_seen = 0
        self.corrected_codewords = 0
        self.uncorrectable_codewords = 0
        self.radio_recoveries = 0
        self.resync_count = 0
        self.corrupt_messages = 0
        self.corrupt_since_log = 0
        self.spi_errors = 0
        self.callback_errors = 0
        print("BOOT_PPM", self.ppm_offset, "profile=stable_hardware_afc")
        
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
        # Receive-group metadata is only for the decoder.  It must not leak
        # into UI events, JSON logs, or persisted history records.
        data_dict.pop("_rx_group", None)
        if not self.callback:
            return
        try:
            self.callback(data_dict)
        except Exception as exc:
            self.callback_errors = (self.callback_errors + 1) & self.COUNTER_MASK
            print("LBJ_CALLBACK_ERR", repr(exc))

    def _load_loco_types(self):
        try:
            with open(self.loco_file, 'r') as f: self.loco_types = json.load(f)
        except Exception as exc:
            print("LOCO_DB_ERR", repr(exc))

    def _init_radio(self, spi_id, sck, mosi, miso, cs, rst):
        self.spi = machine.SPI(spi_id, baudrate=2000000, polarity=0, phase=0,
                               sck=machine.Pin(sck), mosi=machine.Pin(mosi), miso=machine.Pin(miso))
        self.cs_pin = machine.Pin(cs, machine.Pin.OUT, value=1)
        self.rst_pin = machine.Pin(rst, machine.Pin.OUT, value=1)
        self._spi_tx = bytearray(2)
        self._spi_rx = bytearray(2)

        chip_ver = 0x00
        for _ in range(3):
            self._hard_reset_radio()
            chip_ver = self._r(0x42)
            if chip_ver not in (0x00, 0xFF):
                break
        if chip_ver in (0x00, 0xFF):
            raise OSError("SX1276 SPI failed: version=" + hex(chip_ver))

        self._setup_pocsag(self.base_freq_hz, ppm_offset=self.ppm_offset, bps=1200)

    def _hard_reset_radio(self):
        self.cs_pin.value(1)
        self.rst_pin.value(0)
        time.sleep_ms(10)
        self.rst_pin.value(1)
        time.sleep_ms(10)

    def _r(self, r):
        self._spi_tx[0] = r & 0x7F
        self._spi_tx[1] = 0
        self.cs_pin.value(0)
        try:
            self.spi.write_readinto(self._spi_tx, self._spi_rx)
            return self._spi_rx[1]
        except Exception:
            self.spi_errors = (self.spi_errors + 1) & self.COUNTER_MASK
            raise
        finally:
            self.cs_pin.value(1)

    def get_rssi(self):
        try:
            val = self._r(0x11)
            corrected_rssi = -(val // 2)
            return f"{corrected_rssi:.1f}dBm"
        except: return "N/A"

    def _w(self, r, v):
        self._spi_tx[0] = r | 0x80
        self._spi_tx[1] = v & 0xFF
        self.cs_pin.value(0)
        try:
            self.spi.write(self._spi_tx)
        except Exception:
            self.spi_errors = (self.spi_errors + 1) & self.COUNTER_MASK
            raise
        finally:
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
        self._expected_frf = frf

        self._w(self.REG_FRF_MSB, (frf >> 16) & 0xFF)
        self._w(self.REG_FRF_MID, (frf >> 8) & 0xFF)
        self._w(self.REG_FRF_LSB, frf & 0xFF)

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

    def _reset_decoder(self, discard_message=True):
        self.sync_window = 0
        self.synced = False
        self.bit_count = 0
        self.current_cw = 0
        self.batch_position = 0
        self.bad_codeword_streak = 0
        if discard_message:
            self.numeric_output = ""
            self.current_address = ""
            self.pending_fei_hz = None
            self.pending_afc_hz = None

    def _profile_fault(self):
        version = self._r(0x42)
        if version in (0x00, 0xFF):
            return "spi_version"

        op_mode = self._r(0x01)
        if (op_mode & 0x87) != 0x05:
            return "op_mode"

        expected = (
            (self.REG_FRF_MSB, (self._expected_frf >> 16) & 0xFF, "frf_msb"),
            (self.REG_FRF_MID, (self._expected_frf >> 8) & 0xFF, "frf_mid"),
            (self.REG_FRF_LSB, self._expected_frf & 0xFF, "frf_lsb"),
            (self.REG_RXBW, self.RXBW, "rxbw"),
            (self.REG_AFCBW, self.AFCBW, "afcbw"),
            (0x0C, self.LNA_FIXED_GAIN_BOOST, "lna"),
            (0x31, 0x00, "packet_config2"),
            (0x40, 0x00, "dio_mapping"),
            (self.REG_PREAMBLEDETECT, self.PREAMBLE_DETECT, "preamble"),
            (self.REG_RXCONFIG, self.RXCONFIG_AFC_PREAMBLE, "rx_config"),
        )
        for register, wanted, name in expected:
            if self._r(register) != wanted:
                return name
        return None

    def _service_radio_health(self, now):
        # Register reads/reconfiguration while direct data is clocking can
        # interrupt marginal receivers. Keep this diagnostic monitor off in
        # the production receive path; recover() remains available for an
        # explicit radio-thread exception.
        if not self.RADIO_PROFILE_MONITOR_ENABLED:
            return
        if time.ticks_diff(now, self.last_health_check) < self.HEALTH_CHECK_INTERVAL_MS:
            return
        self.last_health_check = now

        if not self.sm.active():
            self.recover("pio_inactive")
            return

        fault = self._profile_fault()
        if fault is None:
            return

        # Read twice before touching RX; a single bad SPI sample must not interrupt a burst.
        confirmed_fault = self._profile_fault()
        if confirmed_fault is not None:
            self.recover(
                "profile_" + confirmed_fault,
                hard=(confirmed_fault == "spi_version"),
            )

    def recover(self, reason="manual", hard=False):
        now = time.ticks_ms()
        if time.ticks_diff(now, self.last_recovery_time) < self.RECOVERY_COOLDOWN_MS:
            return False
        self.last_recovery_time = now
        self.radio_recoveries = (self.radio_recoveries + 1) & self.COUNTER_MASK
        print("RADIO_RECOVER", "reason=", reason, "hard=", hard,
              "count=", self.radio_recoveries)

        self.sm.active(0)
        try:
            self.cs_pin.value(1)
            if hard:
                self._hard_reset_radio()
                version = self._r(0x42)
                if version in (0x00, 0xFF):
                    raise OSError("SX1276 unavailable during recovery")
            self._setup_pocsag(self.base_freq_hz, self.ppm_offset, 1200)
            try:
                self.sm.restart()
            except AttributeError:
                pass
            while self.sm.rx_fifo() > 0:
                self.sm.get()
            self._reset_decoder(discard_message=True)
            self._advance_rx_group()
            self.last_health_check = now
            self.last_word_time = now
        finally:
            self.sm.active(1)
        return True

    def get_health_snapshot(self):
        now = time.ticks_ms()
        return {
            "synced": self.synced,
            "ppm": self.ppm_offset,
            "words": self.words_seen,
            "syncs": self.sync_count,
            "corrected_syncs": self.corrected_sync_words,
            "soft_sync_locks": self.soft_sync_locks,
            "lbj_addresses": self.lbj_address_words,
            "foreign_addresses": self.foreign_address_words,
            "codewords": self.codewords_seen,
            "corrected": self.corrected_codewords,
            "uncorrectable": self.uncorrectable_codewords,
            "raw_pending": len(self.raw_queue),
            "raw_dropped": self.raw_queue.dropped,
            "fragments": len(self.pending_fragments),
            "recoveries": self.radio_recoveries,
            "resyncs": self.resync_count,
            "corrupt": self.corrupt_messages,
            "spi_errors": self.spi_errors,
            "callback_errors": self.callback_errors,
            "sync_age_ms": time.ticks_diff(now, self.last_sync_time),
            "word_age_ms": time.ticks_diff(now, self.last_word_time),
        }

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
            # MicroPython's str does not provide zfill().  Locomotive codes
            # are always three digits, so manual left padding is sufficient.
            padded = str(code)
            if len(padded) < 3:
                padded = ("000" + padded)[-3:]
            if len(padded) != 3:
                continue
            if all(a == 'X' or a == b for a, b in zip(raw_code, padded)):
                if matched_code is not None:
                    return raw_code, None
                matched_code = str(code)
                matched_name = name
        return matched_code if matched_code is not None else raw_code, matched_name

    def _is_emu_loco_code(self, type_code):
        try:
            return int(type_code) >= 301
        except:
            return False

    def _loco_number_raw(self, loco_raw, type_code):
        if self._is_emu_loco_code(type_code):
            return loco_raw[3:7]
        return loco_raw[4:8]

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
        type_digits = sum(char.isdigit() for char in type_raw)
        type_code, type_name = self._resolve_loco_code(type_raw)
        legacy_number_digits = sum(char.isdigit() for char in loco_raw[4:8])
        emu_number_digits = sum(char.isdigit() for char in loco_raw[3:7])
        number_digits = max(legacy_number_digits, emu_number_digits)
        if type_digits < 2 or number_digits < 2:
            return None

        class_tag, class_valid = self._decode_class_tag(class_raw)

        score = 0
        score += 100 if type_name is not None else 20
        score += type_digits * 10
        score += number_digits * 12
        score += 45 if class_valid else -15

        placeholder_score = 0
        for placeholder_idx in (3, 7):
            if loco_raw[placeholder_idx].isdigit():
                candidate_score = 8
                if loco_raw[placeholder_idx] == '0':
                    candidate_score += 4
                if candidate_score > placeholder_score:
                    placeholder_score = candidate_score
        score += placeholder_score

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
        if train_no == '---' and speed_raw == '---' and km_raw == '---':
            return {
                "train_no": "---",
                "speed_kmh": "---",
                "km_post": "---",
                "placeholder": True
            }

        if not train_no.isdigit() or not 1 <= len(train_no) <= 8:
            return {}

        speed_out = "---"
        km_out = "---"
        try:
            if 'X' not in speed_raw:
                speed = float(speed_raw)
                if abs(speed) <= 500:
                    speed_out = speed_raw
        except:
            pass

        try:
            if 'X' not in km_raw:
                km_value = float(km_raw)
                if abs(km_value) <= 1000000:
                    km_out = round(km_value / 10.0, 1)
        except:
            pass

        if speed_out == "---" and km_out == "---":
            return {}

        result = {
            "train_no": train_no,
            "speed_kmh": speed_out,
            "km_post": km_out
        }
        if speed_out == "---" or km_out == "---":
            result["partial"] = True
        return result

    def _parse_ext(self, s):
        if len(s) < self.LBJ_BLOCK_LEN:
            s = s + 'X' * (self.LBJ_BLOCK_LEN - len(s))
        try:
            cls_tag, class_valid = self._decode_class_tag(s[0:4])
            loco_raw = s[4:12]
            type_str, type_name = self._resolve_loco_code(loco_raw[0:3])
            loco_number = self._loco_number_raw(loco_raw, type_str)
            if type_name is not None:
                loco_display = f"{type_name}-{loco_number}"
            elif loco_raw[0:3].isdigit():
                loco_display = f"UNK({type_str})-{loco_number}"
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
            now = time.ticks_ms()
            if time.ticks_diff(now, self.last_align_log) >= self.DIAGNOSTIC_LOG_INTERVAL_MS:
                self.last_align_log = now
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

    def _ric_addr(self, ric):
        if not ric:
            return ""
        return str(ric).split("-F", 1)[0]

    def _ric_number(self, ric):
        try:
            return int(self._ric_addr(ric))
        except (TypeError, ValueError):
            return -1

    def _lbj_fragment_role(self, msg):
        msg_type = msg.get("type")
        ric = self._ric_number(msg.get("ric"))
        if msg_type == "basic_only" and ric == self.LBJ_BASIC_RIC:
            basic = msg.get("basic", {})
            if str(basic.get("train_no", "")).isdigit():
                return "basic"
        elif msg_type == "extended_only" and ric == self.LBJ_EXTENDED_RIC:
            if msg.get("extended"):
                return "extended"
        return ""

    def _flush_pending_outside_group(self, group_id):
        if group_id is None:
            return
        idx = len(self.pending_fragments) - 1
        while idx >= 0:
            pending = self.pending_fragments[idx][1]
            if pending.get("_rx_group") != group_id:
                self.pending_fragments.pop(idx)
                self._emit(pending)
            idx -= 1

    def _flush_pending_in_group(self, group_id):
        if group_id is None:
            return
        idx = len(self.pending_fragments) - 1
        while idx >= 0:
            pending = self.pending_fragments[idx][1]
            if pending.get("_rx_group") == group_id:
                self.pending_fragments.pop(idx)
                self._emit(pending)
            idx -= 1

    def _merge_lbj_fragments(self, first, second):
        if self._lbj_fragment_role(first) == "basic":
            basic_msg, extended_msg = first, second
        else:
            basic_msg, extended_msg = second, first
        return {
            "type": "train_data_merged",
            # Keep the RIC that completed the pair for compatibility with
            # existing log consumers, and preserve both source addresses.
            "ric": second.get("ric", first.get("ric", "")),
            "basic_ric": basic_msg.get("ric", ""),
            "extended_ric": extended_msg.get("ric", ""),
            "rssi": second.get("rssi", first.get("rssi", "N/A")),
            "raw": first.get("raw", "") + " | " + second.get("raw", ""),
            "basic": basic_msg["basic"],
            "extended": extended_msg["extended"],
        }

    def _handle_parsed_msg(self, msg):
        group_id = msg.get("_rx_group")
        # The reference code builds one lbj_data object only from consecutive
        # POCSAG data collected before the next idle/non-LBJ address.  A time
        # window alone spans multiple trains and is therefore not a valid
        # merge key.
        self._flush_pending_outside_group(group_id)

        if msg.get("type") == "corrupt":
            self.corrupt_messages = (
                self.corrupt_messages + 1
            ) & self.COUNTER_MASK
            self.corrupt_since_log = (
                self.corrupt_since_log + 1
            ) & self.COUNTER_MASK
            now = time.ticks_ms()
            if time.ticks_diff(now, self.last_corrupt_log) >= self.DIAGNOSTIC_LOG_INTERVAL_MS:
                print("LBJ_DROP_CORRUPT",
                      "events=", self.corrupt_since_log,
                      "ric=", msg.get("ric", ""),
                      "x=", msg.get("x_count", 0),
                      "len=", len(msg.get("raw", "")))
                self.corrupt_since_log = 0
                self.last_corrupt_log = now
            return

        now = time.ticks_ms()
        self._flush_pending_fragments(now)
        msg_type = msg.get("type")
        if msg_type not in ("basic_only", "extended_only"):
            if msg_type in ("train_data_full", "train_data_merged"):
                # A complete message already contains both halves.  Emit any
                # earlier incomplete fragment rather than silently dropping
                # it or letting a later extension attach to it.
                self._flush_pending_in_group(group_id)
            self._emit(msg)
            return

        role = self._lbj_fragment_role(msg)
        if not role:
            self._emit(msg)
            return

        opposite_role = "extended" if role == "basic" else "basic"

        # The reference receiver fills one lbj_data structure in wire order:
        # a 1234000 basic is followed by its 1234002 extension (or vice
        # versa), before the group terminator.  Within that group use the
        # nearest opposite fragment; fragments from another group never mix.
        for idx in range(len(self.pending_fragments) - 1, -1, -1):
            pending = self.pending_fragments[idx][1]
            if pending.get("_rx_group") != group_id:
                continue
            pending_role = self._lbj_fragment_role(pending)
            if pending_role == opposite_role:
                self.pending_fragments.pop(idx)
                self._emit(self._merge_lbj_fragments(pending, msg))
                return

        # LBJ basic data precedes its extension on air. If an extension has no
        # basic fragment waiting in the same receive group, that basic was
        # missed or corrupt; delaying the extension cannot create a valid
        # pair and only makes it appear together with the next train.
        if role == "extended":
            self._emit(msg)
            return

        # A basic fragment can still wait briefly for its following extension.
        if len(self.pending_fragments) >= self.MAX_PENDING_FRAGMENTS:
            self._emit(self.pending_fragments.pop(0)[1])

        self.pending_fragments.append((now, msg))

    def _flush_pending_fragments(self, now, force=False):
        idx = 0
        while idx < len(self.pending_fragments):
            created_at, msg = self.pending_fragments[idx]
            if force or time.ticks_diff(now, created_at) >= self.MERGE_TIMEOUT_MS:
                self.pending_fragments.pop(idx)
                self._emit(msg)
            else:
                idx += 1

    def _flush_message(self):
        self._report_pending_frequency_error()
        if self.numeric_output:
            queued = self.raw_queue.put(
                (self.current_address, self.numeric_output, self.current_rssi,
                 self.rx_group_id)
            )
            if not queued:
                now = time.ticks_ms()
                if time.ticks_diff(now, self.last_queue_drop_log) >= self.DIAGNOSTIC_LOG_INTERVAL_MS:
                    print("LBJ_RAW_QUEUE_DROP", "count=", self.raw_queue.dropped)
                    self.last_queue_drop_log = now
        self.numeric_output = ""
        self.current_address = ""
        self.bad_codeword_streak = 0

    def _advance_rx_group(self):
        self.rx_group_id = (
            getattr(self, "rx_group_id", 0) + 1
        ) & self.COUNTER_MASK

    def _record_sync(self, now):
        self.synced = True
        self.bit_count = 0
        self.current_cw = 0
        self.batch_position = 0
        self.bad_codeword_streak = 0
        self.last_sync_time = now
        self.sync_count = (self.sync_count + 1) & self.COUNTER_MASK
        self.current_rssi = self.get_rssi()
        self.rssi_val = self.current_rssi
        if self.READ_FEI_ON_SYNC:
            fei_hz = self._read_fei_hz()
            if fei_hz is not None:
                self.pending_fei_hz = fei_hz
                self.pending_afc_hz = self._read_afc_hz()

    def _try_acquire_sync(self, now):
        if self.sync_window == self.POCSAG_SYNC:
            self._record_sync(now)
            return True

        # The direct receiver has no packet engine to correct its sync word.
        # A POCSAG BCH word safely tolerates up to two bit errors.  Check the
        # Hamming distance directly so this hot path stays allocation-free.
        difference = self.sync_window ^ self.POCSAG_SYNC
        difference &= difference - 1
        if difference:
            difference &= difference - 1
        if difference == 0:
            self.corrected_sync_words = (
                self.corrected_sync_words + 1
            ) & self.COUNTER_MASK
            self.soft_sync_locks = (
                self.soft_sync_locks + 1
            ) & self.COUNTER_MASK
            self._record_sync(now)
            return True
        return False

    def _lose_sync(self, reason):
        if self.numeric_output:
            self._flush_message()
        else:
            self.numeric_output = ""
            self.current_address = ""
            self.pending_fei_hz = None
            self.pending_afc_hz = None
        self._advance_rx_group()
        self.sync_window = 0
        self.synced = False
        self.bit_count = 0
        self.current_cw = 0
        self.batch_position = 0
        self.bad_codeword_streak = 0
        self.resync_count = (self.resync_count + 1) & self.COUNTER_MASK
        now = time.ticks_ms()
        if time.ticks_diff(now, self.last_resync_log) >= self.DIAGNOSTIC_LOG_INTERVAL_MS:
            print("LBJ_RESYNC", "reason=", reason,
                  "events=", self.resync_count,
                  "uncorrectable=", self.uncorrectable_codewords)
            self.last_resync_log = now

    def _decode_codeword(self, codeword, now):
        self.codewords_seen = (self.codewords_seen + 1) & self.COUNTER_MASK
        cw_fixed, err_status = self._correct_bch(codeword)
        if err_status == -1:
            # Bad codewords still consume one of the 16 batch positions.
            self.batch_position = (self.batch_position + 1) & 0x0F
            self.uncorrectable_codewords = (
                self.uncorrectable_codewords + 1
            ) & self.COUNTER_MASK
            self.bad_codeword_streak += 1
            if self.current_address:
                self.numeric_output += "XXXXX"
            if self.bad_codeword_streak >= self.MAX_BAD_CODEWORDS:
                self._lose_sync("bch_streak")
            return

        self.bad_codeword_streak = 0
        if err_status > 0:
            self.corrected_codewords = (
                self.corrected_codewords + 1
            ) & self.COUNTER_MASK

        # Validate/correct first, then recognize the frame marker. A weak
        # but BCH-correctable sync must reset the batch position.
        if cw_fixed == self.POCSAG_SYNC:
            if err_status > 0:
                self.corrected_sync_words = (
                    self.corrected_sync_words + 1
                ) & self.COUNTER_MASK
            self._record_sync(now)
            return

        codeword_position = self.batch_position
        self.batch_position = (self.batch_position + 1) & 0x0F

        if cw_fixed == self.POCSAG_IDLE:
            self._flush_message()
            self._advance_rx_group()
            return

        is_message = (cw_fixed >> 31) & 1
        payload = (cw_fixed >> 11) & 0xFFFFF
        if is_message == 0:
            address_field = (payload >> 2) & 0x3FFFF
            ric = (address_field << 3) | (codeword_position >> 1)
            func = payload & 0x03
            if (ric & self.LBJ_RIC_MASK) == (self.LBJ_RIC_BASE & self.LBJ_RIC_MASK):
                self.lbj_address_words = (
                    self.lbj_address_words + 1
                ) & self.COUNTER_MASK
                self._flush_message()
                self.current_address = f"{ric}-F{func}"
            else:
                self.foreign_address_words = (
                    self.foreign_address_words + 1
                ) & self.COUNTER_MASK
                self._flush_message()
                self._advance_rx_group()
            return

        if not self.current_address:
            return
        for j in range(4, -1, -1):
            nibble = (payload >> (j * 4)) & 0x0F
            nibble_rev = (
                ((nibble & 1) << 3)
                | ((nibble & 2) << 1)
                | ((nibble & 4) >> 1)
                | ((nibble & 8) >> 3)
            )
            self.numeric_output += self.BCD_MAP[nibble_rev]

    def _process_raw_queue(self):
        processed = 0
        while processed < self.RAW_PARSE_BUDGET:
            # A new direct word arrived while parsing a previous message.
            # Return to the PIO drain loop first; display/JSON parsing can
            # safely wait, a four-word hardware FIFO cannot.
            if self.sm.rx_fifo() > 0:
                break
            item = self.raw_queue.get()
            if item is None:
                break
            addr, raw, pkt_rssi, group_id = item
            try:
                parsed = self._parse_train_data(raw)
                if parsed.get("type") != "empty":
                    parsed["ric"] = addr
                    parsed["rssi"] = pkt_rssi
                    parsed["_rx_group"] = group_id
                    self._handle_parsed_msg(parsed)
            except Exception as exc:
                print("LBJ_PARSE_ERR", "ric=", addr, repr(exc))
                self._emit({"type": "error", "ric": addr, "raw": raw,
                            "error": str(exc)})
            processed += 1

    def tick(self):
        now = time.ticks_ms()

        while self.sm.rx_fifo() > 0:
            word = (self.sm.get() ^ 0xFFFFFFFF) & 0xFFFFFFFF
            self.words_seen = (self.words_seen + 1) & self.COUNTER_MASK
            self.last_word_time = now
            for i in range(31, -1, -1):
                bit = (word >> i) & 1
                if not self.synced:
                    self.sync_window = ((self.sync_window << 1) | bit) & 0xFFFFFFFF
                    self._try_acquire_sync(now)
                else:
                    self.current_cw = ((self.current_cw << 1) | bit) & 0xFFFFFFFF
                    self.bit_count += 1
                    if self.bit_count == 32:
                        codeword = self.current_cw
                        self.bit_count = 0
                        self.current_cw = 0
                        self._decode_codeword(codeword, now)

        self._process_raw_queue()
        if time.ticks_diff(now, self.last_timeout_check) > 100:
            self._flush_pending_fragments(now)
            self.last_timeout_check = now
        self._service_radio_health(now)
