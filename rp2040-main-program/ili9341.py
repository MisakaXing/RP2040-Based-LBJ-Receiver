import machine
import time
import framebuf

BLACK, WHITE, RED, GREEN, BLUE, CYAN, YELLOW, GRAY, MAGENTA = 0x0000, 0xFFFF, 0xF800, 0x07E0, 0x001F, 0x07FF, 0xFFE0, 0x8410, 0xF81F

class ILI9341:
    # 宽度和高度修改为横屏比例 320x240
    def __init__(self, spi, cs, dc, rst, width=320, height=240): 
        self.spi = spi
        self.cs = machine.Pin(cs, machine.Pin.OUT, value=1)
        self.dc = machine.Pin(dc, machine.Pin.OUT, value=0)
        self.rst = machine.Pin(rst, machine.Pin.OUT, value=1)
        self.width, self.height = width, height
        
        self.char_buf = bytearray(8)
        self.char_fb = framebuf.FrameBuffer(self.char_buf, 8, 8, framebuf.MONO_HLSB)
        
        self.reset()
        self.init_display()

    def reset(self):
        self.rst.value(0)
        time.sleep_ms(50)
        self.rst.value(1)
        time.sleep_ms(50)

    def write_cmd(self, cmd):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytearray([cmd]))
        self.cs.value(1)

    def write_data(self, data):
        self.dc.value(1)
        self.cs.value(0)
        if isinstance(data, int): self.spi.write(bytearray([data]))
        else: self.spi.write(data)
        self.cs.value(1)

    def init_display(self):
        for cmd, data in [
            (0x01, None), (0x11, None), (0x3A, b'\x55'), 
            (0x36, b'\x28'), # MADCTL: 0x28 为标准的横屏模式 (Landscape)
            (0x29, None)
        ]:
            self.write_cmd(cmd)
            if data: self.write_data(data)
            time.sleep_ms(10)

    def set_window(self, x, y, w, h):
        self.write_cmd(0x2A)
        self.write_data(bytearray([x >> 8, x & 0xFF, (x+w-1) >> 8, (x+w-1) & 0xFF]))
        self.write_cmd(0x2B)
        self.write_data(bytearray([y >> 8, y & 0xFF, (y+h-1) >> 8, (y+h-1) & 0xFF]))
        self.write_cmd(0x2C)

    def fill_rect(self, x, y, w, h, color):
        x, y = max(0, min(x, self.width - 1)), max(0, min(y, self.height - 1))
        w, h = max(0, min(w, self.width - x)), max(0, min(h, self.height - y))
        if w == 0 or h == 0: return
        self.set_window(x, y, w, h)
        self.dc.value(1)
        self.cs.value(0)
        color_buf = bytes([color >> 8, color & 0xFF]) * w
        for _ in range(h): self.spi.write(color_buf)
        self.cs.value(1)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def _draw_matrix(self, data, w, h, x, y, color, bg_color, scale):
        block_w, block_h = w * scale, h * scale
        buf = bytearray(block_w * block_h * 2)
        idx = 0
        for row in range(h):
            for _ in range(scale):
                for col in range(w):
                    bit_idx = row * w + col
                    byte_idx, bit_pos = bit_idx // 8, 7 - (bit_idx % 8)
                    pixel_color = color if (data[byte_idx] & (1 << bit_pos)) else bg_color
                    high, low = pixel_color >> 8, pixel_color & 0xFF
                    for _ in range(scale):
                        buf[idx], buf[idx+1] = high, low
                        idx += 2
        self.set_window(x, y, block_w, block_h)
        self.dc.value(1); self.cs.value(0); self.spi.write(buf); self.cs.value(1)

    def draw_gbk(self, gbk_bytes, x, y, color, bg_color=BLACK, scale=1):
        curr_x = x
        i = 0
        while i < len(gbk_bytes):
            b1 = gbk_bytes[i]
            if b1 < 0x80: 
                self.char_fb.fill(0); self.char_fb.text(chr(b1), 0, 0, 1)
                self._draw_matrix(self.char_buf, 8, 8, curr_x, y, color, bg_color, scale)
                curr_x += 8 * scale; i += 1
            else: 
                if i + 1 >= len(gbk_bytes): break
                b2 = gbk_bytes[i+1]
                
                # 默认设为空白像素块 (全 0)
                font_data = bytearray(32) 
                
                # 【核心拦截器】：严格校验 GB2312 汉字编码范围
                # 只有区码和位码都在合法范围内，才去读字库
                if 0xA1 <= b1 <= 0xF7 and 0xA1 <= b2 <= 0xFE:
                    offset = ((b1 - 0xA1) * 94 + (b2 - 0xA1)) * 32
                    if 0 <= offset <= 267616 - 32:
                        try:
                            with open('HZK16', 'rb') as f:
                                f.seek(offset)
                                read_data = f.read(32)
                                if len(read_data) == 32:
                                    font_data = read_data
                        except: pass 
                
                self._draw_matrix(font_data, 16, 16, curr_x, y, color, bg_color, scale)
                curr_x += 16 * scale; i += 2