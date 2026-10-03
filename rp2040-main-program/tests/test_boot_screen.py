from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class BootScreenTests(unittest.TestCase):
    def test_w_boot_draws_early_and_main_reuses_display(self):
        pin = Mock(OUT=1, IN=0, PULL_UP=2)
        spi = Mock()
        display = Mock()
        machine = SimpleNamespace(Pin=pin, SPI=Mock(return_value=spi), freq=Mock())
        modules = {
            'machine': machine,
            'time': SimpleNamespace(ticks_ms=lambda: 100, ticks_diff=lambda a, b: a-b),
            'sys': SimpleNamespace(implementation=SimpleNamespace(_machine='RP2350')),
            'ili9341': SimpleNamespace(ILI9341=Mock(return_value=display)),
        }
        ns = {'print': Mock()}
        with patch.dict(sys.modules, modules):
            exec(compile((ROOT/'boot.py').read_text(), 'boot.py', 'exec'), ns)
        machine.freq.assert_called_once_with(150000000)
        self.assertIn(b'STARTING...', [c.args[0] for c in display.draw_gbk.call_args_list])
        source = (ROOT/'main.py').read_text()
        start = source.index('tft_cs = Pin(9')
        end = source.index('\nsd_cs =', start)
        ns.update(Pin=pin, machine=machine, ILI9341=modules['ili9341'].ILI9341,
                  TFT_SPI_BAUD=60000000)
        exec(compile(source[start:end], 'main_display_init', 'exec'), ns)
        self.assertIs(ns['tft'], display)
        self.assertIs(ns['spi1'], spi)
        modules['ili9341'].ILI9341.assert_called_once()
        self.assertNotIn('_boot_display', ns)


if __name__ == '__main__':
    unittest.main()
