import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "pico_updater.py"
SPEC = importlib.util.spec_from_file_location("pico_updater_module", MODULE_PATH)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)

STANDARD_RUNTIME_FILES = (
    "HZK16",
    "boot_post.py",
    "ili9341.py",
    "lbj_receiver.py",
    "locos.json",
    "rtc_ds3231.py",
    "sdcard.py",
    "main.py",
)
WIRELESS_RUNTIME_FILES = (
    "HZK16",
    "boot_post.py",
    "ili9341.py",
    "lbj_receiver.py",
    "locos.json",
    "rtc_ds3231.py",
    "sdcard.py",
    "history_store.py",
    "wireless_portal.py",
    "main.py",
)


class DummyWidget:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


def profile_for(label):
    profile = dict(updater.get_firmware_profile(label))
    profile["runtime_files"] = tuple(profile["runtime_files"])
    return profile


def write_firmware_zip(path, profile, version_line, missing=()):
    root = f"repo-{profile['branch']}/rp2040-main-program"
    with zipfile.ZipFile(path, "w") as archive:
        for name in profile["runtime_files"]:
            if name in missing:
                continue
            data = version_line if name == "main.py" else f"content:{name}"
            archive.writestr(f"{root}/{name}", data)
        archive.writestr(f"{root}/README.md", "not firmware")
        archive.writestr(f"{root}/extra.py", "not firmware")
        archive.writestr(f"{root}/tests/test_dummy.py", "not firmware")
        archive.writestr(f"{root}/.DS_Store", "not firmware")


class FirmwareSelectionTests(unittest.TestCase):
    def test_branch_selection_updates_raw_and_api_urls(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        app.github_repo = updater.GITHUB_REPO
        app.target_dir = updater.TARGET_DIR
        app.remote_firmware_info = {
            "label": "old", "version": (1,), "branch": "main"
        }
        app.remote_version = 1.0
        app.remote_ver_label = DummyWidget()
        app.branch_hint = DummyWidget()
        messages = []
        app.log = messages.append

        app._on_branch_selected(updater.WIRELESS_CHANNEL_LABEL)

        self.assertEqual(app.active_profile["branch"], "Wireless-Enabled")
        self.assertIn(
            "/Wireless-Enabled/rp2040-main-program/main.py",
            app.main_py_url,
        )
        self.assertEqual(
            app.api_url,
            "https://api.github.com/repos/"
            "MisakaXing/RP2040-Based-LBJ-Receiver/contents/"
            "rp2040-main-program?ref=Wireless-Enabled",
        )
        self.assertEqual(app.remote_firmware_info["version"], ())
        self.assertEqual(app.remote_ver_label.options["text"], "未知")
        self.assertTrue(any("Wireless-Enabled" in item for item in messages))

        main_raw, main_api = updater.build_github_urls(
            updater.GITHUB_REPO, updater.TARGET_DIR, "main"
        )
        self.assertTrue(main_raw.endswith("/main/rp2040-main-program/main.py"))
        self.assertTrue(main_api.endswith("rp2040-main-program?ref=main"))

    def test_parse_versions_and_tuple_order(self):
        standard = updater.parse_program_version("Program_ver = 5.2")
        wireless = updater.parse_program_version('Program_ver = "5.2-W"')
        newer = updater.parse_program_version("Program_ver = 5.10")

        self.assertEqual(standard, {
            "label": "5.2", "version": (5, 2), "branch": "main"
        })
        self.assertEqual(wireless, {
            "label": "5.2-W",
            "version": (5, 2),
            "branch": "Wireless-Enabled",
        })
        self.assertTrue(updater.version_is_at_least(newer, standard))

    def test_select_runtime_files_is_exact_and_ordered(self):
        expected_by_label = {
            updater.STANDARD_CHANNEL_LABEL: STANDARD_RUNTIME_FILES,
            updater.WIRELESS_CHANNEL_LABEL: WIRELESS_RUNTIME_FILES,
        }
        for label, expected_files in expected_by_label.items():
            with self.subTest(label=label):
                profile = profile_for(label)
                self.assertEqual(profile["runtime_files"], expected_files)
                api_items = [
                    {"name": "README.md", "type": "file"},
                    {"name": "tests", "type": "dir"},
                    {"name": ".DS_Store", "type": "file"},
                ] + [
                    {"name": name, "type": "file", "download_url": name}
                    for name in reversed(profile["runtime_files"])
                ]

                selected, missing = updater.select_runtime_files(
                    profile, api_items
                )
                self.assertFalse(missing)
                self.assertEqual(
                    [item["name"] for item in selected],
                    list(expected_files),
                )
                self.assertEqual(selected[-1]["name"], "main.py")

                without_first = [
                    item for item in api_items
                    if item.get("name") != profile["runtime_files"][0]
                ]
                _, missing = updater.select_runtime_files(
                    profile, without_first
                )
                self.assertEqual(missing, [profile["runtime_files"][0]])

    def test_online_w_prepare_uses_frozen_branch_and_exact_allowlist(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        app.github_repo = updater.GITHUB_REPO
        app.target_dir = updater.TARGET_DIR
        app.log = mock.Mock()
        app._check_hardware_compatibility = mock.Mock(return_value=True)
        app._read_device_firmware_info = mock.Mock(return_value={
            "label": "5.2", "version": (5, 2), "branch": "main"
        })
        app.run_mpremote = mock.Mock(return_value=(True, "PICO_OK"))
        app.set_ui_state = mock.Mock()
        scheduled_confirmation = {}

        def fake_after(delay, callback, *args):
            if callback == app._confirm_online_update:
                scheduled_confirmation["args"] = args

        app.after = fake_after
        profile = profile_for(updater.WIRELESS_CHANNEL_LABEL)
        api_items = [
            {"name": "README_W.md", "type": "file", "download_url": "junk"},
            {"name": ".DS_Store", "type": "file", "download_url": "junk2"},
            {"name": "tests", "type": "dir"},
        ] + [
            {
                "name": name,
                "type": "file",
                "download_url": f"https://download.invalid/{name}",
            }
            for name in reversed(WIRELESS_RUNTIME_FILES)
        ]
        requested_urls = []

        class FakeResponse:
            def __init__(self, status_code=200, text="", content=b"", data=None):
                self.status_code = status_code
                self.text = text
                self.content = content
                self._data = data

            def json(self):
                return self._data

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise updater.requests.HTTPError(str(self.status_code))

        def fake_get(url, timeout):
            requested_urls.append(url)
            if "raw.githubusercontent.com" in url:
                return FakeResponse(text='Program_ver = "5.2-W"\n')
            if "api.github.com" in url:
                return FakeResponse(data=api_items)
            name = url.rsplit("/", 1)[-1]
            content = (
                b'Program_ver = "5.2-W"\n'
                if name == "main.py" else name.encode("utf-8")
            )
            return FakeResponse(content=content)

        with mock.patch.object(updater.requests, "get", side_effect=fake_get):
            app._update_worker("TEST_PORT", False, profile)

        self.assertIn("args", scheduled_confirmation)
        confirm_args = scheduled_confirmation["args"]
        temp_dir = confirm_args[2]
        try:
            firmware_files = confirm_args[3]
            self.assertEqual(
                [item["name"] for item in firmware_files],
                list(WIRELESS_RUNTIME_FILES),
            )
            self.assertEqual(confirm_args[4]["branch"], "Wireless-Enabled")
            self.assertEqual(confirm_args[5]["branch"], "main")
            self.assertEqual(confirm_args[6]["branch"], "Wireless-Enabled")
            self.assertIn(
                "/Wireless-Enabled/rp2040-main-program/main.py",
                requested_urls[0],
            )
            self.assertTrue(requested_urls[1].endswith(
                "rp2040-main-program?ref=Wireless-Enabled"
            ))
            self.assertEqual(
                [url.rsplit("/", 1)[-1] for url in requested_urls[2:]],
                list(WIRELESS_RUNTIME_FILES),
            )
            self.assertNotIn("junk", requested_urls)
            self.assertNotIn("junk2", requested_urls)
        finally:
            app._cleanup_temp_dir(temp_dir)

    def test_online_remote_branch_mismatch_stops_before_directory_request(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        app.github_repo = updater.GITHUB_REPO
        app.target_dir = updater.TARGET_DIR
        app.log = mock.Mock()
        app.after = mock.Mock()
        app.set_ui_state = mock.Mock()
        app._check_hardware_compatibility = mock.Mock(return_value=True)
        app.run_mpremote = mock.Mock(return_value=(True, "PICO_OK"))
        profile = profile_for(updater.WIRELESS_CHANNEL_LABEL)
        standard_response = mock.Mock(
            status_code=200, text="Program_ver = 5.2\n"
        )

        with mock.patch.object(
            updater.requests, "get", return_value=standard_response
        ) as get_request:
            app._update_worker("TEST_PORT", False, profile)

        self.assertEqual(get_request.call_count, 1)
        self.assertTrue(any(
            "与所选 Wireless-Enabled 不一致" in str(call)
            for call in app.log.call_args_list
        ))


class HardwareCompatibilityTests(unittest.TestCase):
    def test_parse_probe_and_compatibility_matrix(self):
        standard = profile_for(updater.STANDARD_CHANNEL_LABEL)
        wireless = profile_for(updater.WIRELESS_CHANNEL_LABEL)
        rp2040 = {
            "build": "RPI_PICO",
            "impl_machine": "Raspberry Pi Pico with RP2040",
            "uname_machine": "Raspberry Pi Pico with RP2040",
            "platform": "rp2",
            "network": 0,
            "pins_41_42": 0,
            "fs_bytes": 1_400_000,
        }
        waveshare = {
            "build": "WAVESHARE_RP2350B_PLUS_W",
            "impl_machine": "Waveshare RP2350B PLUS W with RP2350",
            "uname_machine": "Waveshare RP2350B PLUS W with RP2350",
            "platform": "rp2",
            "network": 1,
            "pins_41_42": 1,
            "fs_bytes": 14 * 1024 * 1024,
        }

        noisy_output = (
            "mpremote noise\n"
            "LBJ_HW_PROBE_V1|build=WAVESHARE_RP2350B_PLUS_W|"
            "impl_machine=Waveshare RP2350B PLUS W with RP2350|"
            "uname_machine=Waveshare RP2350B PLUS W with RP2350|"
            "platform=rp2|network=1|pins_41_42=1|fs_bytes=14680064\n"
            ">>>"
        )
        parsed = updater.parse_hardware_probe(noisy_output)
        self.assertEqual(parsed["build"], "WAVESHARE_RP2350B_PLUS_W")
        self.assertEqual(parsed["fs_bytes"], 14 * 1024 * 1024)
        self.assertIn('getattr(network, "STA_IF"', updater.HARDWARE_PROBE_SCRIPT)

        cases = [
            (standard, rp2040, True),
            (wireless, rp2040, False),
            (wireless, waveshare, True),
            (standard, waveshare, False),
            (wireless, {**waveshare, "build": "GENERIC_RP2350", "impl_machine": "RP2350", "uname_machine": "RP2350"}, False),
            (wireless, {**waveshare, "network": 0}, False),
            (wireless, {**waveshare, "pins_41_42": 0}, False),
            (wireless, {**waveshare, "fs_bytes": 11 * 1024 * 1024}, False),
            (wireless, None, False),
        ]
        for profile, info, expected in cases:
            with self.subTest(
                branch=profile["branch"], info=info, expected=expected
            ):
                compatible, _ = updater.evaluate_hardware_compatibility(
                    profile, info
                )
                self.assertEqual(compatible, expected)

    def test_online_force_update_incompatible_board_has_zero_side_effects(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        app.github_repo = updater.GITHUB_REPO
        app.target_dir = updater.TARGET_DIR
        scheduled = []
        app.after = lambda *args: scheduled.append(args)
        app.set_ui_state = mock.Mock()
        messages = []
        app.log = messages.append
        calls = []

        def fake_run(port, args, timeout_sec=60, live_stream=False):
            calls.append(tuple(args))
            if args == ["exec", "print('PICO_OK')"]:
                return True, "PICO_OK"
            if args == ["exec", updater.HARDWARE_PROBE_SCRIPT]:
                return True, (
                    "LBJ_HW_PROBE_V1|build=RPI_PICO|"
                    "impl_machine=Raspberry Pi Pico with RP2040|"
                    "uname_machine=Raspberry Pi Pico with RP2040|platform=rp2|"
                    "network=0|pins_41_42=0|fs_bytes=1400000"
                )
            raise AssertionError(f"unexpected device command: {args!r}")

        app.run_mpremote = fake_run
        profile = profile_for(updater.WIRELESS_CHANNEL_LABEL)

        with mock.patch.object(updater.requests, "get") as get_request:
            app._update_worker("TEST_PORT", True, profile)

        self.assertIn(("exec", updater.HARDWARE_PROBE_SCRIPT), calls)
        get_request.assert_not_called()
        command_text = repr(calls)
        self.assertNotIn("os.remove", command_text)
        self.assertNotIn("'fs', 'cp'", command_text)
        self.assertNotIn("machine.reset", command_text)
        self.assertFalse(
            any("处理过程中发生错误" in item for item in messages),
            messages,
        )
        self.assertTrue(any("阻止刷入" in item for item in messages))
        self.assertTrue(any(
            call[1] == app.set_ui_state and call[2:] == (False,)
            for call in scheduled
        ))

    def test_wipe_rechecks_compatibility_before_delete(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        app.log = mock.Mock()
        app.after = mock.Mock()
        app._check_hardware_compatibility = mock.Mock(return_value=False)
        app.run_mpremote = mock.Mock(
            side_effect=AssertionError("delete must not run")
        )

        result = app._wipe_device_files(
            "TEST_PORT", profile_for(updater.STANDARD_CHANNEL_LABEL)
        )

        self.assertFalse(result)
        app._check_hardware_compatibility.assert_called_once()
        app.run_mpremote.assert_not_called()


class OfflineZipTests(unittest.TestCase):
    def make_app(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        app.target_dir = updater.TARGET_DIR
        app.log = mock.Mock()
        return app

    def test_zip_extracts_only_selected_runtime_files_for_both_branches(self):
        for label, version_line in (
            (updater.STANDARD_CHANNEL_LABEL, "Program_ver = 5.2\n"),
            (updater.WIRELESS_CHANNEL_LABEL, 'Program_ver = "5.2-W"\n'),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                profile = profile_for(label)
                zip_path = Path(temp) / "firmware.zip"
                output_dir = Path(temp) / "out"
                output_dir.mkdir()
                write_firmware_zip(zip_path, profile, version_line)

                files, info = self.make_app()._extract_zip_firmware(
                    zip_path, output_dir, profile
                )

                self.assertEqual(
                    [item["name"] for item in files],
                    list(profile["runtime_files"]),
                )
                self.assertEqual(files[-1]["name"], "main.py")
                self.assertEqual(info["branch"], profile["branch"])
                self.assertEqual(
                    sorted(path.name for path in output_dir.iterdir()),
                    sorted(profile["runtime_files"]),
                )

    def test_zip_rejects_branch_mismatch_and_missing_required_file(self):
        standard = profile_for(updater.STANDARD_CHANNEL_LABEL)
        wireless = profile_for(updater.WIRELESS_CHANNEL_LABEL)

        with tempfile.TemporaryDirectory() as temp:
            zip_path = Path(temp) / "wireless.zip"
            output_dir = Path(temp) / "out-mismatch"
            output_dir.mkdir()
            write_firmware_zip(
                zip_path, wireless, 'Program_ver = "5.2-W"\n'
            )
            with self.assertRaisesRegex(ValueError, "ZIP 属于"):
                self.make_app()._extract_zip_firmware(
                    zip_path, output_dir, standard
                )

        with tempfile.TemporaryDirectory() as temp:
            zip_path = Path(temp) / "missing.zip"
            output_dir = Path(temp) / "out-missing"
            output_dir.mkdir()
            missing_name = standard["runtime_files"][0]
            write_firmware_zip(
                zip_path,
                standard,
                "Program_ver = 5.2\n",
                missing=(missing_name,),
            )
            with self.assertRaisesRegex(ValueError, missing_name):
                self.make_app()._extract_zip_firmware(
                    zip_path, output_dir, standard
                )


class MpremoteResultTests(unittest.TestCase):
    def test_run_mpremote_respects_process_return_code(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        failed_result = mock.Mock(returncode=1, stdout="failed")
        successful_result = mock.Mock(returncode=0, stdout="ok")

        with mock.patch.object(
            updater.subprocess, "run", return_value=failed_result
        ):
            success, output = app.run_mpremote("PORT", ["exec", "pass"])
            self.assertFalse(success)
            self.assertEqual(output, "failed")

        with mock.patch.object(
            updater.subprocess, "run", return_value=successful_result
        ):
            success, output = app.run_mpremote("PORT", ["exec", "pass"])
            self.assertTrue(success)
            self.assertEqual(output, "ok")


class FlashGuardTests(unittest.TestCase):
    def test_window_cannot_close_while_an_operation_is_running(self):
        app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
        app.destroy = mock.Mock()

        app.is_working = True
        with mock.patch.object(updater.messagebox, "showwarning") as warning:
            app._on_close()
        warning.assert_called_once()
        app.destroy.assert_not_called()

        app.is_working = False
        app._on_close()
        app.destroy.assert_called_once()

    def test_both_flash_workers_stop_when_final_hardware_recheck_fails(self):
        cases = (
            ("_online_flash_worker", ("PORT", False, None, [], {})),
            ("_offline_zip_flash_worker", ("PORT", None, [], {})),
        )
        for method_name, args in cases:
            with self.subTest(worker=method_name):
                app = updater.PicoUpdaterApp.__new__(updater.PicoUpdaterApp)
                app.after = mock.Mock()
                app.log = mock.Mock()
                app._cleanup_temp_dir = mock.Mock()
                app._wipe_device_files = mock.Mock(return_value=False)
                app._copy_firmware_files = mock.Mock()
                app.run_mpremote = mock.Mock()

                getattr(app, method_name)(*args)

                app._wipe_device_files.assert_called_once()
                app._copy_firmware_files.assert_not_called()
                app.run_mpremote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
