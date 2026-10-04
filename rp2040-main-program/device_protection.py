"""Shared, allocation-light LCD warning and battery/thermal policy."""
import time

BATTERY_EMPTY_V = 3.45
BATTERY_FULL_V = 4.2
TEMP_RED_C = 45.0
TEMP_STOP_WIFI_C = 60.0
TEMP_RESUME_WIFI_C = 55.0
WARNING_ALTERNATE_MS = 2000


def battery_percent(volts):
    if volts <= BATTERY_EMPTY_V:
        return 0
    return max(1, min(100, int((volts - BATTERY_EMPTY_V)
                             * 100 / (BATTERY_FULL_V - BATTERY_EMPTY_V))))


class DeviceProtection:
    def __init__(self):
        self.temp_c = None
        self.percent = None
        self.usb = False
        self.overheat = False
        self._both = False
        self._phase = 0
        self._phase_at = None

    def update(self, percent, temp_c, usb):
        self.percent = percent
        self.usb = bool(usb)
        self.temp_c = temp_c
        if temp_c is not None:
            if temp_c > TEMP_STOP_WIFI_C:
                self.overheat = True
            elif temp_c <= TEMP_RESUME_WIFI_C:
                self.overheat = False
        # Invalid samples must not release an existing thermal lockout.

    def low_battery(self):
        return (not self.usb and self.percent is not None
                and self.percent < 10)

    def empty_battery(self):
        return not self.usb and self.percent == 0

    def temperature_red(self):
        return self.temp_c is not None and self.temp_c > TEMP_RED_C

    def top_warning(self, now):
        hot = self.temp_c is not None and self.temp_c > TEMP_STOP_WIFI_C
        low = self.low_battery()
        both = hot and low
        if both:
            if not self._both:
                self._phase = 0
                self._phase_at = now
            elif time.ticks_diff(now, self._phase_at) >= WARNING_ALTERNATE_MS:
                self._phase ^= 1
                self._phase_at = now
        self._both = both
        if hot and (not low or self._phase == 0):
            return b'HIGH TEMP'
        return b'LOW BAT' if low else None
