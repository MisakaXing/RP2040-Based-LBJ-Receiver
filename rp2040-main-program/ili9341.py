import machine
import time
import framebuf

BLACK, WHITE, RED, GREEN, BLUE, CYAN, YELLOW, GRAY, MAGENTA = 0x0000, 0xFFFF, 0xF800, 0x07E0, 0x001F, 0x07FF, 0xFFE0, 0x8410, 0xF81F

class ILI9341:
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
        time.sleep_ms(100)
        self.rst.value(1)
        time.sleep_ms(200)

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
        self.write_cmd(0x01)
        time.sleep_ms(150)

        self.write_cmd(0x11)
        time.sleep_ms(150)

        self.write_cmd(0x3A)
        self.write_data(b'\x55')

        self.write_cmd(0x36)
        self.write_data(b'\x28')

        self.write_cmd(0x29)
        time.sleep_ms(20)

    def set_window(self, x, y, w, h):
        self.write_cmd(0x2A)
        self.write_data(bytearray([x >> 8, x & 0xFF, (x+w-1) >> 8, (x+w-1) & 0xFF]))
        self.write_cmd(0x2B)
        self.write_data(bytearray([y >> 8, y & 0xFF, (y+h-1) >> 8, (y+h-1) & 0xFF]))
        self.write_cmd(0x2C)

    def fill_rect(self, x, y, w, h, color):
        x, y = max(0, min(x, self.width - 1)), max(0, min(y, self.height - 1))
        w, h = max(0, min(w, self.width - x)), max(0, min(h, self.height - y))
        if w <= 0 or h <= 0: return
        self.set_window(x, y, w, h)
        self.dc.value(1)
        self.cs.value(0)
        
        # 优化：每次将 8 行组合成一个数据块，大幅减少 Python 循环和 SPI 开销
        # w*8*2 字节 = 最大只需占用约 5KB 内存，RP2040 完全扛得住
        chunk_h = min(8, h)
        color_buf = bytes([color >> 8, color & 0xFF]) * (w * chunk_h)
        
        for _ in range(h // chunk_h): 
            self.spi.write(color_buf)
            
        rem = h % chunk_h
        if rem: # 写入剩余不足一块的行
            self.spi.write(bytes([color >> 8, color & 0xFF]) * (w * rem))
            
        self.cs.value(1)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def _draw_matrix(self, data, w, h, x, y, color, bg_color, scale):
        block_w, block_h = w * scale, h * scale
        buf = bytearray(block_w * block_h * 2)
        
        # 优化：提前分离高低字节，避免在内层循环做上千次位移计算
        ch, cl = color >> 8, color & 0xFF
        bh, bl = bg_color >> 8, bg_color & 0xFF
        
        idx = 0
        byte_idx = 0
        bit_pos = 7
        
        for row in range(h):
            # 以行为单位渲染，减少内循环复杂度
            line_buf = bytearray(block_w * 2)
            l_idx = 0
            for col in range(w):
                is_set = data[byte_idx] & (1 << bit_pos)
                high, low = (ch, cl) if is_set else (bh, bl)
                
                # 水平放大
                for _ in range(scale):
                    line_buf[l_idx] = high
                    line_buf[l_idx+1] = low
                    l_idx += 2
                    
                if bit_pos == 0:
                    bit_pos = 7
                    byte_idx += 1
                else:
                    bit_pos -= 1
                    
            # 垂直放大：直接整行进行 bytearray 级拷贝，比单个像素描绘快极多
            for _ in range(scale):
                buf[idx:idx+len(line_buf)] = line_buf
                idx += len(line_buf)
                
        self.set_window(x, y, block_w, block_h)
        self.dc.value(1); self.cs.value(0); self.spi.write(buf); self.cs.value(1)

    def draw_gbk(self, gbk_bytes, x, y, color, bg_color=BLACK, scale=1):
        curr_x = x
        i = 0
        hzk_file = None # 优化：把文件句柄提到循环外
        
        try:
            while i < len(gbk_bytes):
                b1 = gbk_bytes[i]
                if b1 < 0x80: 
                    self.char_fb.fill(0); self.char_fb.text(chr(b1), 0, 0, 1)
                    self._draw_matrix(self.char_buf, 8, 8, curr_x, y, color, bg_color, scale)
                    curr_x += 8 * scale; i += 1
                else: 
                    if i + 1 >= len(gbk_bytes): break
                    b2 = gbk_bytes[i+1]
                    font_data = bytearray(32) 
                    
                    if 0xA1 <= b1 <= 0xF7 and 0xA1 <= b2 <= 0xFE:
                        offset = ((b1 - 0xA1) * 94 + (b2 - 0xA1)) * 32
                        if 0 <= offset <= 267616 - 32:
                            # 优化：画一整串字，只做一次文件 Open！
                            if hzk_file is None:
                                try: hzk_file = open('HZK16', 'rb')
                                except: pass
                                
                            if hzk_file:
                                hzk_file.seek(offset)
                                read_data = hzk_file.read(32)
                                if len(read_data) == 32:
                                    font_data = read_data
                                    
                    self._draw_matrix(font_data, 16, 16, curr_x, y, color, bg_color, scale)
                    curr_x += 16 * scale; i += 2
        finally:
            # 无论出不出错，退出前统一关闭一次文件即可
            if hzk_file: 
                hzk_file.close()

