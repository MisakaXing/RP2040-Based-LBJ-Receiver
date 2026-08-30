import importlib.util
import pathlib
import sys
import types
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

fake_micropython = types.ModuleType("micropython")
fake_micropython.const = lambda value: value
sys.modules.setdefault("micropython", fake_micropython)

spec = importlib.util.spec_from_file_location(
    "sdcard_driver_under_test", PROJECT_ROOT / "sdcard.py"
)
sdcard_driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdcard_driver)


class FakeCS:
    def __init__(self):
        self.values = []

    def __call__(self, value):
        self.values.append(value)


class FakeSPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.writes = []

    def read(self, _count, _fill):
        return bytes([self.responses.pop(0) if self.responses else 0])

    def write(self, value):
        self.writes.append(bytes(value))


class FailingSPI(FakeSPI):
    def write(self, value):
        if bytes(value) == b"payload":
            raise OSError("injected SPI failure")
        super().write(value)


def card_with_responses(responses):
    card = object.__new__(sdcard_driver.SDCard)
    card.cs = FakeCS()
    card.spi = FakeSPI(responses)
    return card


class SDCardWriteTests(unittest.TestCase):
    def test_v2_csd_includes_high_capacity_byte(self):
        csd = bytearray(16)
        csd[0] = 0x40
        csd[7], csd[8], csd[9] = 0x01, 0xFF, 0xFF
        self.assertEqual(sdcard_driver._sectors_from_csd(csd), 134217728)

    def test_v1_csd_uses_read_block_length(self):
        csd = bytearray(16)
        csd[5] = 9
        c_size = 1023
        csd[6] = (c_size >> 10) & 0x03
        csd[7] = (c_size >> 2) & 0xFF
        csd[8] = (c_size & 0x03) << 6
        c_size_mult = 7
        csd[9] = (c_size_mult >> 1) & 0x03
        csd[10] = (c_size_mult & 0x01) << 7
        self.assertEqual(sdcard_driver._sectors_from_csd(csd), 524288)

    def test_ocr_distinguishes_sdhc_from_v2_sdsc(self):
        self.assertEqual(sdcard_driver._cdv_from_ocr(b"\x40\x00\x00\x00"), 1)
        self.assertEqual(sdcard_driver._cdv_from_ocr(b"\x00\x00\x00\x00"), 512)

    def test_v2_initialization_has_elapsed_time_deadline(self):
        card = object.__new__(sdcard_driver.SDCard)
        command_count = 0

        def never_ready(command, *_args, **_kwargs):
            nonlocal command_count
            if command == 41:
                command_count += 1
                return 1
            return 0

        card.cmd = never_ready
        tick = -1000

        def advancing_ticks():
            nonlocal tick
            tick += 1000
            return tick

        original_ticks = sdcard_driver._ticks_ms
        original_sleep = sdcard_driver._sleep_ms
        sdcard_driver._ticks_ms = advancing_ticks
        sdcard_driver._sleep_ms = lambda _value: None
        try:
            with self.assertRaises(OSError):
                card.init_card_v2()
        finally:
            sdcard_driver._ticks_ms = original_ticks
            sdcard_driver._sleep_ms = original_sleep
        self.assertLess(command_count, 10)

    def test_rejected_data_response_raises_io_error(self):
        # First read sends the data token; the second is the card response.
        card = card_with_responses([0xFF, 0x0B])
        with self.assertRaises(OSError):
            card.write(sdcard_driver._TOKEN_DATA, b"payload")
        self.assertEqual(card.cs.values[-1], 1)

    def test_busy_card_times_out_and_releases_chip_select(self):
        card = card_with_responses([0xFF, 0x05, 0x00, 0x00, 0x00])
        ticks = iter((0, 1000, 2000, 3000))
        original_ticks = sdcard_driver._ticks_ms
        original_sleep = sdcard_driver._sleep_ms
        sdcard_driver._ticks_ms = lambda: next(ticks)
        sdcard_driver._sleep_ms = lambda _value: None
        try:
            with self.assertRaises(OSError):
                card.write(sdcard_driver._TOKEN_DATA, b"payload")
        finally:
            sdcard_driver._ticks_ms = original_ticks
            sdcard_driver._sleep_ms = original_sleep
        self.assertEqual(card.cs.values[-1], 1)

    def test_ready_card_completes_write(self):
        card = card_with_responses([0xFF, 0x05, 0xFF])
        card.write(sdcard_driver._TOKEN_DATA, b"payload")
        self.assertEqual(card.cs.values[-1], 1)

    def test_spi_exception_still_releases_chip_select(self):
        card = card_with_responses([0xFF])
        card.spi = FailingSPI([0xFF])
        with self.assertRaises(OSError):
            card.write(sdcard_driver._TOKEN_DATA, b"payload")
        self.assertEqual(card.cs.values[-1], 1)


if __name__ == "__main__":
    unittest.main()
