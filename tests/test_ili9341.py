import importlib.util
import pathlib
import sys
import types
import unittest


fake_micropython = types.ModuleType("micropython")
fake_micropython.viper = lambda func: func
sys.modules.setdefault("micropython", fake_micropython)

fake_framebuf = types.ModuleType("framebuf")
fake_framebuf.MONO_HLSB = 0
fake_framebuf.FrameBuffer = object
sys.modules.setdefault("framebuf", fake_framebuf)

fake_machine = sys.modules.setdefault("machine", types.ModuleType("machine"))

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "rp2040-main-program"
    / "ili9341.py"
)
spec = importlib.util.spec_from_file_location("ili9341_under_test", MODULE_PATH)
ili = importlib.util.module_from_spec(spec)
ili.ptr8 = object
spec.loader.exec_module(ili)


class FakePin:
    def __init__(self):
        self.state = 1

    def value(self, new_value=None):
        if new_value is not None:
            self.state = new_value
        return self.state


class FakeSPI:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))


def make_driver():
    driver = ili.ILI9341.__new__(ili.ILI9341)
    driver.width = 320
    driver.height = 240
    driver.spi = FakeSPI()
    driver.cs = FakePin()
    driver.dc = FakePin()
    driver._one = bytearray(1)
    driver._window_buf = bytearray(4)
    driver._fill_buf = bytearray(4096)
    driver._fill_mv = memoryview(driver._fill_buf)
    driver._fill_color = -1
    driver._glyph_buf = bytearray(16 * 16 * 3 * 3 * 2)
    driver._glyph_mv = memoryview(driver._glyph_buf)
    return driver


class ILI9341Tests(unittest.TestCase):
    def test_fill_color_buffer_uses_rgb565_big_endian_bytes(self):
        buffer = bytearray(8)
        ili._fill_color_buffer(buffer, 4, 0x12, 0x34)
        self.assertEqual(buffer, b"\x12\x34" * 4)

    def test_fill_rect_reuses_buffer_and_writes_exact_pixel_count(self):
        driver = make_driver()
        buffer_id = id(driver._fill_buf)
        driver.fill_rect(0, 0, 2, 2, 0x1234)
        self.assertEqual(driver.spi.writes[-1], b"\x12\x34" * 4)
        driver.fill_rect(5, 5, 1, 3, 0x1234)
        self.assertEqual(driver.spi.writes[-1], b"\x12\x34" * 3)
        self.assertEqual(id(driver._fill_buf), buffer_id)
        self.assertEqual(driver.cs.state, 1)

    def test_scaled_glyph_renderer_reuses_preallocated_buffer(self):
        driver = make_driver()
        glyph_id = id(driver._glyph_buf)
        driver.set_window = lambda *_args: None
        driver._draw_matrix(bytearray([0x80]), 1, 1, 0, 0, 0xFFFF, 0x0000, 2)
        self.assertEqual(driver.spi.writes[-1], b"\xff\xff" * 4)
        self.assertEqual(id(driver._glyph_buf), glyph_id)
        self.assertEqual(driver.cs.state, 1)


if __name__ == "__main__":
    unittest.main()
