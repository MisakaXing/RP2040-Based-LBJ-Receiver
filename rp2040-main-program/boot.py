"""Show startup feedback before compiling the receiver application."""
import machine
import sys
import time


def _show_startup_screen():
    started = time.ticks_ms()
    machine.freq(200000000)
    from ili9341 import ILI9341

    machine.Pin(6, machine.Pin.OUT, value=0)
    machine.Pin(7, machine.Pin.OUT, value=1)
    machine.Pin(9, machine.Pin.OUT, value=1)
    spi = machine.SPI(1, baudrate=20000000, sck=machine.Pin(10),
                      mosi=machine.Pin(11),
                      miso=machine.Pin(8, machine.Pin.IN, machine.Pin.PULL_UP))
    tft = ILI9341(spi, cs=9, dc=12, rst=13)
    spi.init(baudrate=60000000, polarity=0, phase=0)
    panel = 0x1082
    tft.fill_rect(0, 0, 320, 42, panel)
    tft.fill_rect(0, 40, 320, 2, 0x07FF)
    tft.draw_gbk(b'LBJ', 14, 8, 0xFFFF, panel, scale=2)
    tft.draw_gbk(b'RECEIVER', 72, 14, 0x07FF, panel)
    tft.draw_gbk(b'POWER-ON CHECK', 198, 14, 0x8410, panel)
    tft.draw_gbk(b'STARTING...', 72, 96, 0x07FF, 0, scale=2)
    tft.draw_gbk(b'LOADING FIRMWARE', 100, 135, 0x8410, 0)
    first_frame_ms = time.ticks_diff(time.ticks_ms(), started)
    print('BOOT_FIRST_FRAME_MS', first_frame_ms)
    return spi, tft, started, first_frame_ms


_boot_display = None
# Leave this ordinary-board bootstrap inactive on RP2350/W hardware.
if 'RP2040' in getattr(sys.implementation, '_machine', ''):
    try:
        _boot_display = _show_startup_screen()
    except Exception as exc:
        print('BOOT_EARLY_SCREEN_ERR', repr(exc))
del _show_startup_screen
