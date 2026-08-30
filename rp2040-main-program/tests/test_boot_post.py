import pathlib
import sys
import types
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

fake_machine = types.ModuleType("machine")
fake_sdcard = types.ModuleType("sdcard")
sys.modules.setdefault("machine", fake_machine)
sys.modules["sdcard"] = fake_sdcard

import boot_post
from boot_post import SystemPOST


boot_post.time.sleep_ms = lambda _: None


class FakeTFT:
    def __init__(self):
        self.draws = []
        self.rects = []

    def fill(self, color):
        self.rects.append((0, 0, 320, 240, color))

    def fill_rect(self, x, y, w, h, color):
        self.rects.append((x, y, w, h, color))

    def draw_gbk(self, text, x, y, color, background, scale=1):
        self.draws.append((text, x, y, color, background, scale))


class FakeBuzzer:
    def __init__(self):
        self.values = []

    def value(self, value):
        self.values.append(value)


class PortalResult:
    def __init__(self, ok):
        self.ok = ok

    def probe_hardware(self):
        return self.ok, "CYW43 READY" if self.ok else "CYW43 FAILED"


class NonHardwarePOST(SystemPOST):
    def check_sys_ver(self, ver, is_es):
        pass

    def check_sx1276(self):
        return True

    def check_bat(self, bat_adc, bat_en):
        pass

    def check_temp(self, sensor_temp):
        pass

    def check_rtc(self, rtc):
        self.rtc_error = False

    def check_sd(self, spi1, sd_cs):
        pass


class BootPostTests(unittest.TestCase):
    def test_title_has_w_suffix(self):
        tft = FakeTFT()
        SystemPOST(tft, object())
        self.assertTrue(any(call[0] == b"W" and call[1] == 144 for call in tft.draws))

    def test_firmware_row_shows_only_version(self):
        release_tft = FakeTFT()
        SystemPOST(release_tft, object()).check_sys_ver("5.2-W", 0)
        release_messages = [call[0] for call in release_tft.draws if call[1] == 205]
        self.assertEqual(release_messages, [b"v5.2-W"])

        engineering_tft = FakeTFT()
        SystemPOST(engineering_tft, object()).check_sys_ver("5.2-W", 1)
        engineering_messages = [call[0] for call in engineering_tft.draws if call[1] == 205]
        self.assertEqual(engineering_messages, [b"v5.2-W"])

    def test_wifi_failure_is_red_warning_not_critical(self):
        tft = FakeTFT()
        post = SystemPOST(tft, object())
        self.assertFalse(post.check_wifi(PortalResult(False)))
        self.assertFalse(post.wifi_ok)
        self.assertTrue(post.has_warning)
        self.assertFalse(post.has_critical_error)
        self.assertTrue(any(call[0] == b"WARN" and call[3] == post.RED for call in tft.draws))

    def test_wifi_failure_still_passes_post(self):
        post = NonHardwarePOST(FakeTFT(), object())
        result = post.run_all(
            object(), object(), object(), object(), object(), object(),
            FakeBuzzer(), "5.1-W", 0, PortalResult(False),
        )
        self.assertEqual(result, "OK")
        self.assertFalse(post.wifi_ok)
        self.assertFalse(post.has_critical_error)

    def test_seven_rows_do_not_overlap_footer(self):
        tft = FakeTFT()
        post = SystemPOST(tft, object())
        for _ in range(7):
            post._check_start("TEST")
            post._check_end("OK", "READY")
        row_rects = [rect for rect in tft.rects if rect[0] == 8]
        self.assertEqual(len(row_rects), 7)
        self.assertLessEqual(row_rects[-1][1] + row_rects[-1][3], 218)


if __name__ == "__main__":
    unittest.main()
