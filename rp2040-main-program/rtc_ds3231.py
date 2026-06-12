class DS3231:
    """Unified DS3231/PCF8563 RTC driver.

    The class name is kept for compatibility with the existing application.
    """

    DS3231_ADDR = 0x68
    PCF8563_ADDR = 0x51

    def __init__(self, i2c, addr=None):
        self.i2c = i2c
        self.addr = addr
        self.model = "UNKNOWN"
        self.detect()

    def _dec2bcd(self, dec):
        return ((dec // 10) << 4) | (dec % 10)

    def _bcd2dec(self, bcd):
        return ((bcd >> 4) * 10) + (bcd & 0x0F)

    def _probe(self, addr, reg):
        try:
            self.i2c.readfrom_mem(addr, reg, 1)
            return True
        except:
            return False

    def detect(self):
        if self.addr == self.DS3231_ADDR:
            candidates = ((self.DS3231_ADDR, "DS3231", 0x00),)
        elif self.addr == self.PCF8563_ADDR:
            candidates = ((self.PCF8563_ADDR, "PCF8563", 0x02),)
        else:
            candidates = (
                (self.DS3231_ADDR, "DS3231", 0x00),
                (self.PCF8563_ADDR, "PCF8563", 0x02),
            )

        try:
            devices = self.i2c.scan()
        except:
            devices = None

        for addr, model, reg in candidates:
            if devices is not None and addr not in devices:
                continue
            if self._probe(addr, reg):
                self.addr = addr
                self.model = model
                return model

        self.addr = None
        self.model = "UNKNOWN"
        return self.model

    def get_model(self):
        return self.model

    def is_present(self):
        return self.addr is not None and self.model != "UNKNOWN"

    def get_time(self):
        """Return (hour, minute, second)."""
        try:
            if self.model == "DS3231":
                data = self.i2c.readfrom_mem(self.addr, 0x00, 3)
                second = self._bcd2dec(data[0] & 0x7F)
                minute = self._bcd2dec(data[1] & 0x7F)
                hour_reg = data[2]
                if hour_reg & 0x40:
                    hour = self._bcd2dec(hour_reg & 0x1F)
                    if hour_reg & 0x20:
                        hour = (hour % 12) + 12
                    elif hour == 12:
                        hour = 0
                else:
                    hour = self._bcd2dec(hour_reg & 0x3F)
                return (hour, minute, second)

            if self.model == "PCF8563":
                data = self.i2c.readfrom_mem(self.addr, 0x02, 3)
                second = self._bcd2dec(data[0] & 0x7F)
                minute = self._bcd2dec(data[1] & 0x7F)
                hour = self._bcd2dec(data[2] & 0x3F)
                return (hour, minute, second)
        except:
            pass
        return (0, 0, 0)

    def get_time_str(self, show_seconds=False):
        """Return HH:MM or HH:MM:SS."""
        hour, minute, second = self.get_time()
        if show_seconds:
            return f"{hour:02}:{minute:02}:{second:02}"
        return f"{hour:02}:{minute:02}"

    def get_date(self):
        """Return (year, month, day), where year is the two-digit 20xx year."""
        try:
            if self.model == "DS3231":
                data = self.i2c.readfrom_mem(self.addr, 0x04, 3)
                day = self._bcd2dec(data[0] & 0x3F)
                month = self._bcd2dec(data[1] & 0x1F)
                year = self._bcd2dec(data[2])
                return (year, month, day)

            if self.model == "PCF8563":
                day = self._bcd2dec(
                    self.i2c.readfrom_mem(self.addr, 0x05, 1)[0] & 0x3F
                )
                data = self.i2c.readfrom_mem(self.addr, 0x07, 2)
                month = self._bcd2dec(data[0] & 0x1F)
                year = self._bcd2dec(data[1])
                return (year, month, day)
        except:
            pass
        return (0, 0, 0)

    def needs_sync(self):
        if not self.is_present():
            return True

        try:
            year, month, day = self.get_date()
            date_invalid = (
                year < 24
                or not 1 <= month <= 12
                or not 1 <= day <= 31
            )

            if self.model == "DS3231":
                if date_invalid:
                    return True

                # OSF is sticky and is also set on first power-up. A valid
                # stored date is sufficient here; clear the historical flag
                # so it does not force date setup on every subsequent boot.
                self._clear_ds3231_osf()
                return False

            if self.model == "PCF8563":
                voltage_low = bool(
                    self.i2c.readfrom_mem(self.addr, 0x02, 1)[0] & 0x80
                )
                return voltage_low or date_invalid
        except:
            return True

        return True

    def _clear_ds3231_osf(self):
        if self.model != "DS3231":
            return
        try:
            status = self.i2c.readfrom_mem(self.addr, 0x0F, 1)[0]
            self.i2c.writeto_mem(self.addr, 0x0F, bytes([status & 0x7F]))
        except:
            pass

    def _clear_pcf8563_vl(self):
        if self.model != "PCF8563":
            return
        try:
            second = self.i2c.readfrom_mem(self.addr, 0x02, 1)[0] & 0x7F
            self.i2c.writeto_mem(self.addr, 0x02, bytes([second]))
        except:
            pass

    def sync_time(self, hh, mm):
        """Set hour/minute and reset seconds to zero."""
        try:
            data = bytes([0, self._dec2bcd(mm), self._dec2bcd(hh)])
            if self.model == "DS3231":
                self.i2c.writeto_mem(self.addr, 0x00, data)
                self._clear_ds3231_osf()
                return True

            if self.model == "PCF8563":
                self.i2c.writeto_mem(self.addr, 0x02, data)
                return True
        except:
            pass
        return False

    def set_date(self, yy, mm, dd):
        """Set date, where yy is the last two digits of a 20xx year."""
        try:
            if self.model == "DS3231":
                data = bytes([
                    self._dec2bcd(dd),
                    self._dec2bcd(mm),
                    self._dec2bcd(yy),
                ])
                self.i2c.writeto_mem(self.addr, 0x04, data)
                self._clear_ds3231_osf()
                return True

            if self.model == "PCF8563":
                self.i2c.writeto_mem(
                    self.addr, 0x05, bytes([self._dec2bcd(dd)])
                )
                self.i2c.writeto_mem(
                    self.addr,
                    0x07,
                    bytes([self._dec2bcd(mm), self._dec2bcd(yy)]),
                )
                self._clear_pcf8563_vl()
                return True
        except:
            pass
        return False
