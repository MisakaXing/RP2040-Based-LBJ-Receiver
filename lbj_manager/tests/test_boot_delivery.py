"""Startup-file delivery regressions; no USB access or real downloads."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
import tempfile
import threading
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lbj_manager as manager


class BootDeliveryTests(unittest.TestCase):
    def bundle(self, directory, profile, boot=True, early=True):
        files = []
        for name in manager.runtime_file_order(profile):
            if name == 'boot.py' and not boot:
                continue
            path = Path(directory) / name
            data = 'pass\n'
            if name == 'main.py':
                version = '5.15-W' if profile['branch'] != 'main' else '5.13'
                data = f'Program_ver = "{version}"\n'
                if early:
                    data += "_boot_display = globals().pop('_boot_display', None)\n"
                    data += "startup_display = _boot_display\n"
            elif name == 'boot.py':
                data = "_boot_display = 'LOADING FIRMWARE'\n"
            path.write_text(data, encoding='utf-8')
            files.append({'name': name, 'path': str(path)})
        return files

    def app(self):
        app = manager.LBJManager.__new__(manager.LBJManager)
        app.target_dir = manager.TARGET_DIR
        app.github_repo = manager.GITHUB_REPO
        app._updater_lease = manager.DeviceLease(1, 'test', 'SIMULATED')
        app.tasks = SimpleNamespace(active=app._updater_lease)
        app._task_context = threading.local()
        for name in ('log', 'after', 'set_progress', 'set_ui_state',
                     '_cleanup_temp_dir', '_complete_flash', 'show_confirm_dialog'):
            setattr(app, name, mock.Mock())
        app._check_hardware_compatibility = mock.Mock(return_value=True)
        app._read_device_firmware_info = mock.Mock(return_value={
            'label': '', 'version': (), 'branch': ''})
        return app

    def test_online_download_preserves_w_boot_bytes_and_order(self):
        profile = manager.get_firmware_profile('Wireless-Enabled')
        with tempfile.TemporaryDirectory() as tmp:
            files = self.bundle(tmp, profile)
            payloads = {item['name']: Path(item['path']).read_bytes() for item in files}
            app = self.app()
            app.run_mpremote = mock.Mock(return_value=(True, 'PICO_OK'))
            captured = []

            def dispatch(delay, callback, *args):
                if callback == app._confirm_online_update:
                    captured.append(args)

            app.after.side_effect = dispatch
            items = [{'name': name, 'type': 'file', 'download_url': 'https://test.invalid/' + name}
                     for name in reversed(payloads)]
            items.append({'name': 'tests', 'type': 'dir'})

            def response(url, timeout):
                if 'api.github.com' in url:
                    return SimpleNamespace(status_code=200, json=lambda: items)
                if 'raw.githubusercontent.com' in url:
                    return SimpleNamespace(status_code=200, text=payloads['main.py'].decode())
                return SimpleNamespace(content=payloads[url.rsplit('/', 1)[1]],
                                       raise_for_status=lambda: None)

            with mock.patch.object(manager.requests, 'get', side_effect=response):
                app._update_worker('SIMULATED', False, profile)
            self.assertEqual(len(captured), 1)
            downloaded = captured[0][3]
            directory = captured[0][2]
            try:
                names = manager.validate_firmware_bundle(profile, downloaded)
                self.assertEqual(names.count('boot.py'), 1)
                self.assertLess(names.index('boot.py'), names.index('main.py'))
                self.assertEqual(names[-1], 'main.py')
                for item in downloaded:
                    self.assertEqual(Path(item['path']).read_bytes(), payloads[item['name']])
            finally:
                # Invoke the real cleanup; the worker's cleanup is mocked.
                manager.PicoUpdaterApp._cleanup_temp_dir(app, directory)

    def test_zip_to_wipe_copy_preserves_boot_for_both_branches_and_workers(self):
        for profile in manager.FIRMWARE_BRANCHES.values():
            for prefix in ('', 'repo/rp2040-main-program/', 'repo\\rp2040-main-program\\'):
                for online in (False, True):
                    with self.subTest(branch=profile['branch'], prefix=prefix, online=online), tempfile.TemporaryDirectory() as tmp:
                        files = self.bundle(tmp, profile)
                        archive_path = Path(tmp) / 'firmware.zip'
                        with zipfile.ZipFile(archive_path, 'w') as archive:
                            for item in files:
                                archive.write(item['path'], prefix + item['name'])
                        out = Path(tmp) / 'out'; out.mkdir()
                        app = self.app()
                        unpacked, _ = app._extract_zip_firmware(archive_path, out, profile)
                        device = {'boot.py': b'old bootstrap', 'main.py': b'old firmware'}
                        writes = []

                        def transport(port, args, **kwargs):
                            if args[0] == 'exec':
                                self.assertIn('os.remove', args[1])
                                device.clear()
                            else:
                                self.assertEqual(args[:2], ['fs', 'cp'])
                                writes.append(args[-1][1:])
                                device[writes[-1]] = Path(args[2]).read_bytes()
                            return True, 'simulated'

                        app.run_mpremote = mock.Mock(side_effect=transport)
                        if online:
                            app._online_flash_worker('SIMULATED', False, str(out), unpacked, profile)
                        else:
                            app._offline_zip_flash_worker('SIMULATED', str(out), unpacked, profile)
                        self.assertEqual(writes[-1], 'main.py')
                        self.assertEqual(writes.count('boot.py'), 1)
                        for item in files:
                            self.assertEqual(device[item['name']], Path(item['path']).read_bytes())
                        namespace = {}
                        exec(device['boot.py'], namespace)
                        exec(device['main.py'], namespace)
                        self.assertEqual(namespace['startup_display'], 'LOADING FIRMWARE')
                        self.assertFalse(app._flash_partial)
                        app._complete_flash.assert_called_once()

    def test_missing_boot_rejected_before_online_confirmation_and_zip_flash(self):
        for profile in manager.FIRMWARE_BRANCHES.values():
            with self.subTest(branch=profile['branch']), tempfile.TemporaryDirectory() as tmp:
                files = self.bundle(tmp, profile, boot=False)
                app = self.app()
                app.run_mpremote = mock.Mock(side_effect=AssertionError('No device access'))
                with mock.patch.object(manager.messagebox, 'showerror') as error:
                    app._confirm_online_update('SIMULATED', False, tmp, files, {}, {}, profile)
                self.assertIn('boot.py', error.call_args.args[1])
                app.show_confirm_dialog.assert_not_called()
                app.run_mpremote.assert_not_called()
                archive_path = Path(tmp) / 'missing-boot.zip'
                with zipfile.ZipFile(archive_path, 'w') as archive:
                    for item in files:
                        archive.write(item['path'], item['name'])
                out = Path(tmp) / 'out'; out.mkdir()
                with self.assertRaisesRegex(ValueError, 'boot.py'):
                    app._extract_zip_firmware(archive_path, out, profile)
                app.run_mpremote.assert_not_called()

    def test_boot_copy_failure_never_reboots_or_writes_main(self):
        profile = manager.get_firmware_profile('Wireless-Enabled')
        with tempfile.TemporaryDirectory() as tmp:
            files = self.bundle(tmp, profile)
            app = self.app()
            app.run_mpremote = mock.Mock(side_effect=lambda port, args, **kw:
                (False, 'injected boot write failure') if args[-1] == ':boot.py'
                else (True, 'simulated'))
            app._offline_zip_flash_worker('SIMULATED', tmp, files, profile)
            self.assertTrue(app._flash_partial)
            app._complete_flash.assert_not_called()
            targets = [c.args[1][-1] for c in app.run_mpremote.call_args_list]
            self.assertIn(':boot.py', targets)
            self.assertNotIn(':main.py', targets)

    def test_old_firmware_without_boot_hand_off_remains_compatible(self):
        for profile in manager.FIRMWARE_BRANCHES.values():
            with self.subTest(branch=profile['branch']), tempfile.TemporaryDirectory() as tmp:
                manager.validate_firmware_bundle(profile, self.bundle(tmp, profile, boot=False, early=False))

    def test_comment_or_unrelated_string_does_not_require_boot(self):
        profile = manager.get_firmware_profile('Wireless-Enabled')
        with tempfile.TemporaryDirectory() as tmp:
            files = self.bundle(tmp, profile, boot=False, early=False)
            with (Path(tmp) / 'main.py').open('a') as output:
                output.write("# _boot_display\ntext = '_boot_display'\n")
            manager.validate_firmware_bundle(profile, files)

    def test_invalid_boot_code_is_rejected_not_silently_dropped(self):
        profile = manager.get_firmware_profile('Wireless-Enabled')
        with tempfile.TemporaryDirectory() as tmp:
            files = self.bundle(tmp, profile)
            for data in (b'if!\n', b'\xff'):
                (Path(tmp) / 'boot.py').write_bytes(data)
                with self.assertRaisesRegex(ValueError, 'boot.py'):
                    manager.validate_firmware_bundle(profile, files)


if __name__ == '__main__':
    unittest.main()
