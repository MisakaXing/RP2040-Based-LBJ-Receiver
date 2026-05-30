import machine
import time
import framebuf
import micropython
import struct

# 常用颜色定义 (RGB565)
BLACK   = 0x0000
WHITE   = 0xFFFF
RED     = 0xF800
GREEN   = 0x07E0
BLUE    = 0x001F
CYAN    = 0x07FF
YELLOW  = 0xFFE0
GRAY    = 0x8410
MAGENTA = 0xF81F


# 核心优化 1：Viper 机器码加速像素计算
@micropython.viper
def _fast_draw_matrix(data: ptr8, buf: ptr8, w: int, h: int, ch: int, cl: int, bh: int, bl: int, scale: int):
    byte_idx = 0
    bit_pos = 7
    idx = 0
    
    for row in range(h):
        line_start = idx
        for col in range(w):
            is_set = data[byte_idx] & (1 << bit_pos)
            v_h = ch if is_set else bh
            v_l = cl if is_set else bl
            
            for _ in range(scale):
                buf[idx] = v_h
                buf[idx+1] = v_l
                idx += 2
                
            if bit_pos == 0:
                bit_pos = 7
                byte_idx += 1
            else:
                bit_pos -= 1
                
        line_len = idx - line_start
        for _ in range(scale - 1):
            for i in range(line_len):
                buf[idx] = buf[line_start + i]
                idx += 1

# ==========================================
# ILI9341 驱动类
# ==========================================
class ILI9341:
    def __init__(self, spi, cs, dc, rst, width=320, height=240):
        self.spi = spi
        # 引脚对象化修复
        self.cs = machine.Pin(cs, machine.Pin.OUT, value=1)
        self.dc = machine.Pin(dc, machine.Pin.OUT, value=0)
        self.rst = machine.Pin(rst, machine.Pin.OUT, value=1)
        self.width = width
        self.height = height
        
        self.char_buf = bytearray(8)
        self.char_fb = framebuf.FrameBuffer(self.char_buf, 8, 8, framebuf.MONO_HLSB)
        
        self.reset()
        self.init_display()
        self.fill(BLACK) # 初始化后清一次屏
        
        # 核心优化 2：字库文件句柄常驻内存
        try:
            self.hzk_file = open('HZK16', 'rb')
        except OSError:
            print("警告: 找不到 HZK16 字库文件，中文将无法显示。")
            self.hzk_file = None

    def write_cmd(self, cmd):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytearray([cmd]))
        self.cs.value(1)

    def write_data(self, data):
        self.dc.value(1)
        self.cs.value(0)
        if isinstance(data, int):
            self.spi.write(bytearray([data]))
        else:
            self.spi.write(data)
        self.cs.value(1)

    def reset(self):
        self.rst.value(1)
        time.sleep_ms(50)
        self.rst.value(0)
        time.sleep_ms(50)
        self.rst.value(1)
        time.sleep_ms(50)
    #屏幕初始化寄存器
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
        self.write_data(struct.pack(">HH", x, x + w - 1))
        self.write_cmd(0x2B)
        self.write_data(struct.pack(">HH", y, y + h - 1))
        self.write_cmd(0x2C)

    # 兼容性最高且速度极快的分块颜色填充，解决乘法报错
    def fill_rect(self, x, y, w, h, color):
        x, y = max(0, min(x, self.width - 1)), max(0, min(y, self.height - 1))
        w, h = max(0, min(w, self.width - x)), max(0, min(h, self.height - y))
        if w <= 0 or h <= 0: return
        self.set_window(x, y, w, h)
        self.dc.value(1)
        self.cs.value(0)
        
        chunk_h = min(8, h)
        total_bytes = w * chunk_h * 2
        
        color_buf = bytearray(total_bytes)
        ch = color >> 8
        cl = color & 0xFF
        for i in range(0, total_bytes, 2):
            color_buf[i] = ch
            color_buf[i+1] = cl
        
        for _ in range(h // chunk_h): 
            self.spi.write(color_buf)
            
        rem = h % chunk_h
        if rem: 
            self.spi.write(color_buf[:w * rem * 2])
            
        self.cs.value(1)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def _draw_matrix(self, data, w, h, x, y, color, bg_color, scale):
        block_w, block_h = w * scale, h * scale
        
        if x + block_w > self.width or y + block_h > self.height:
            return
            
        buf = bytearray(block_w * block_h * 2)
        
        ch, cl = color >> 8, color & 0xFF
        bh, bl = bg_color >> 8, bg_color & 0xFF
        
        _fast_draw_matrix(data, buf, w, h, ch, cl, bh, bl, scale)
                
        self.set_window(x, y, block_w, block_h)
        self.dc.value(1)
        self.cs.value(0)
        self.spi.write(buf)
        self.cs.value(1)

    def draw_gbk(self, gbk_bytes, x, y, color, bg_color=BLACK, scale=1):
        if type(gbk_bytes) == str:
            gbk_bytes = gbk_bytes.encode('gbk')
            
        curr_x = x
        i = 0
        
        while i < len(gbk_bytes):
            b1 = gbk_bytes[i]
            
            if b1 < 0x80: 
                self.char_fb.fill(0)
                self.char_fb.text(chr(b1), 0, 0, 1)
                self._draw_matrix(self.char_buf, 8, 8, curr_x, y, color, bg_color, scale)
                curr_x += 8 * scale
                i += 1
            else: 
                if i + 1 >= len(gbk_bytes): 
                    break
                b2 = gbk_bytes[i+1]
                font_data = bytearray(32) 
                
                if 0xA1 <= b1 <= 0xF7 and 0xA1 <= b2 <= 0xFE:
                    offset = ((b1 - 0xA1) * 94 + (b2 - 0xA1)) * 32
                    if 0 <= offset <= 267616 - 32:
                        if self.hzk_file:  
                            self.hzk_file.seek(offset)
                            read_data = self.hzk_file.read(32)
                            if len(read_data) == 32:
                                font_data = read_data
                                
                self._draw_matrix(font_data, 16, 16, curr_x, y, color, bg_color, scale)
                curr_x += 16 * scale
                i += 2
