from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class BootScreenTests(unittest.TestCase):
    def hardware(self):
        calls = []
        machine = ModuleType('machine')
        machine.freq = lambda hz: calls.append(('freq', hz))

        class Pin:
            OUT, IN, PULL_UP = 1, 0, 2
            def __init__(self, *args, **kwargs):
                pass

        class SPI:
            def __init__(self, *args, **kwargs):
                pass
            def init(self, **kwargs):
                calls.append(('spi_init', kwargs))

        class Display:
            def __init__(self, spi, **kwargs):
                self.spi = spi
                calls.append(('display_init',))
            def fill_rect(self, *args):
                pass
            def draw_gbk(self, label, *args, **kwargs):
                calls.append(('draw', label))

        machine.Pin, machine.SPI = Pin, SPI
        display = ModuleType('ili9341')
        display.ILI9341 = Display
        clock = ModuleType('time')
        clock.ticks_ms = lambda: 100
        clock.ticks_diff = lambda a, b: a - b
        return calls, machine, display, clock

    def run_boot(self, board='RP2040'):
        calls, machine, display, clock = self.hardware()
        fake_sys = ModuleType('sys')
        fake_sys.implementation = SimpleNamespace(_machine=board)
        ns = {'print': lambda *args: None}
        with mock.patch.dict(sys.modules, {'machine': machine, 'ili9341': display,
                                          'time': clock, 'sys': fake_sys}):
            exec(compile((ROOT / 'boot.py').read_text(), 'boot.py', 'exec'), ns)
        return ns, calls, machine, display

    def run_main_display_init(self, ns, machine, display):
        source = (ROOT / 'main.py').read_text()
        start = source.index('tft_cs = Pin(9')
        end = source.index('\nsd_cs =', start)
        ns.update(Pin=machine.Pin, machine=machine, ILI9341=display.ILI9341,
                  TFT_SPI_BAUD=60000000)
        exec(compile(source[start:end], 'main_display_init', 'exec'), ns)

    def test_early_frame_and_main_reuse_one_display(self):
        ns, calls, machine, display = self.run_boot()
        original_spi, original_tft = ns['_boot_display'][:2]
        self.assertEqual(calls[0], ('freq', 200000000))
        self.assertIn(('draw', b'STARTING...'), calls)
        self.run_main_display_init(ns, machine, display)
        self.assertIs(ns['tft'], original_tft)
        self.assertIs(ns['spi1'], original_spi)
        self.assertEqual(calls.count(('display_init',)), 1)
        self.assertNotIn('_boot_display', ns)

    def test_old_installation_falls_back_without_boot_file(self):
        calls, machine, display, clock = self.hardware()
        ns = {}
        self.run_main_display_init(ns, machine, display)
        self.assertEqual(calls.count(('display_init',)), 1)
        self.assertIsNone(ns['boot_started_ms'])

    def test_ordinary_bootstrap_does_not_touch_w_hardware(self):
        ns, calls, machine, display = self.run_boot('RP2350')
        self.assertIsNone(ns['_boot_display'])
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
