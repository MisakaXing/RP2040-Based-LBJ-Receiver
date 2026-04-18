import machine

class DS3231:
    def __init__(self, i2c, addr=0x68):
        self.i2c = i2c
        self.addr = addr

    def _dec2bcd(self, dec):
        return ((dec // 10) << 4) + (dec % 10)

    def _bcd2dec(self, bcd):
        return (bcd >> 4) * 10 + (bcd & 0x0F)

    def get_time(self):
        """返回 (hh, mm, ss) 数组"""
        try:
            data = self.i2c.readfrom_mem(self.addr, 0x00, 3)
            return (self._bcd2dec(data[2]), self._bcd2dec(data[1]), self._bcd2dec(data[0]))
        except:
            return (0, 0, 0)

    def get_time_str(self, show_seconds=False):
        """返回 HH:MM 或 HH:MM:SS 字符串"""
        h, m, s = self.get_time()
        if show_seconds:
            return f"{h:02}:{m:02}:{s:02}"
        return f"{h:02}:{m:02}"

    def sync_time(self, hh, mm):
        """同步小时和分钟，秒归零"""
        try:
            self.i2c.writeto_mem(self.addr, 0x00, bytes([0, self._dec2bcd(mm), self._dec2bcd(hh)]))
            return True
        except:
            return False

    def set_date(self, yy, mm, dd):
        """设置年月日 (yy为 20xx 后两位)"""
        try:
            self.i2c.writeto_mem(self.addr, 0x04, bytes([self._dec2bcd(dd), self._dec2bcd(mm), self._dec2bcd(yy)]))
        except:
            pass