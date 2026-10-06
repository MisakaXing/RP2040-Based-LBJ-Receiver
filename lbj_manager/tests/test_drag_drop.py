"""Local-file drag routing, ZIP staging and task safety; no device access."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lbj_manager as manager


class DropPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tcl = manager.tk.Tcl()

    def file(self, name):
        path = pathlib.Path(self.directory.name, name)
        path.write_text('fixture', encoding='utf-8')
        return str(path)

    def data(self, *paths):
        return self.tcl.call('list', *paths)

    def test_chinese_spaces_braces_and_case_preserved(self):
        path = self.file('接收器 {新版} 测试.ZIP')
        self.assertEqual(manager.dropped_file(self.tcl, self.data(path), {'.zip'}), path)

    def test_multiple_files_rejected(self):
        with self.assertRaisesRegex(ValueError, '一个文件'):
            manager.dropped_file(self.tcl, self.data(self.file('a.zip'), self.file('b.zip')), {'.zip'})

    def test_empty_list_rejected(self):
        with self.assertRaises(ValueError):
            manager.dropped_file(self.tcl, '', {'.zip'})

    def test_malformed_tcl_list_rejected(self):
        with self.assertRaisesRegex(ValueError, '无法识别'):
            manager.dropped_file(self.tcl, '{not closed', {'.zip'})

    def test_folder_rejected(self):
        with self.assertRaisesRegex(ValueError, '文件夹'):
            manager.dropped_file(self.tcl, self.data(self.directory.name), {'.zip'})

    def test_missing_file_rejected(self):
        with self.assertRaisesRegex(ValueError, '已移除'):
            manager.dropped_file(self.tcl, self.data(str(pathlib.Path(self.directory.name, 'gone.zip'))), {'.zip'})

    def test_wrong_extension_rejected(self):
        with self.assertRaisesRegex(ValueError, '支持'):
            manager.dropped_file(self.tcl, self.data(self.file('history.jsonl')), {'.zip'})

    def test_all_log_extensions_accepted(self):
        for suffix in ('.json', '.jsonl', '.txt', '.log'):
            path = self.file('中文 记录' + suffix)
            self.assertEqual(manager.dropped_file(self.tcl, self.data(path), {'.json', '.jsonl', '.txt', '.log'}), path)


class DropFlowTests(unittest.TestCase):
    def root(self):
        root = SimpleNamespace(is_working=False, _zip_loading=False, _closing=False,
            history=SimpleNamespace(_loading_data=False, _pico_busy=False, load_json_file=mock.Mock(),
                                    log_drop_zone=mock.Mock()),
            zip_drop_zone=mock.Mock(), zip_drop_label=mock.Mock(), offline_zip_btn=mock.Mock(),
            notice=mock.Mock(), log=mock.Mock(), sync_controls=mock.Mock(), after=mock.Mock(),
            _pending_zip_path=None, _stage_zip=mock.Mock(), show_page=mock.Mock(return_value=True),
            tk=manager.tk.Tcl(), _selected_profile=mock.Mock(return_value=manager.get_firmware_profile('main')))
        for name in ('_drop_busy', '_drop_hover', '_accept_dropped_file', '_finish_zip_stage'):
            setattr(root, name, getattr(manager.LBJManager, name).__get__(root))
        return root

    def test_drop_deferred_until_native_callback_returns(self):
        root = self.root()
        with tempfile.NamedTemporaryFile(suffix='.zip') as fixture:
            event = SimpleNamespace(data=root.tk.call('list', fixture.name))
            self.assertEqual(manager.LBJManager._on_file_drop(root, 'zip', event), 'copy')
            root.after.assert_called_once_with(0, root._accept_dropped_file, 'zip', fixture.name)
        root._stage_zip.assert_not_called()

    def test_invalid_drop_has_visible_prompt_and_no_callback(self):
        root = self.root()
        self.assertEqual(manager.LBJManager._on_file_drop(root, 'zip', SimpleNamespace(data='')), 'refuse_drop')
        root.notice.assert_called_once()
        root.after.assert_not_called()

    def test_busy_flags_reject_drop_and_deferred_callback(self):
        for owner, attribute in (('root', 'is_working'), ('root', '_zip_loading'), ('root', '_closing'),
                                 ('history', '_loading_data'), ('history', '_pico_busy')):
            with self.subTest(attribute=attribute):
                root = self.root()
                setattr(root if owner == 'root' else root.history, attribute, True)
                self.assertEqual(manager.LBJManager._on_file_drop(root, 'log', SimpleNamespace(data='unused')), 'refuse_drop')
                root._accept_dropped_file('zip', 'file.zip')
                root._stage_zip.assert_not_called()
                root.history.load_json_file.assert_not_called()

    def test_log_drop_routes_to_import_without_dialog_or_serial(self):
        root = self.root()
        root._accept_dropped_file('log', 'history.jsonl')
        root.show_page.assert_called_once_with('历史记录')
        root.history.load_json_file.assert_called_once_with('history.jsonl')
        root._stage_zip.assert_not_called()

    def test_blocked_navigation_does_not_import(self):
        root = self.root(); root.show_page.return_value = False
        root._accept_dropped_file('log', 'history.jsonl')
        root.history.load_json_file.assert_not_called()

    def test_zip_drop_only_stages_not_flashes(self):
        root = self.root()
        root._accept_dropped_file('zip', 'firmware.zip')
        root._stage_zip.assert_called_once_with('firmware.zip')
        root.history.load_json_file.assert_not_called()

    def test_successful_stage_sets_file_and_confirmation_hint(self):
        root = self.root(); root._zip_loading = True
        info = manager.parse_program_version('Program_ver="5.12"', ['main.py'])
        root._finish_zip_stage('/tmp/新版.zip', info, '')
        self.assertEqual(root._pending_zip_path, '/tmp/新版.zip')
        self.assertFalse(root._zip_loading)
        self.assertIn('仍需确认', root.notice.call_args.args[0])
        self.assertIn('刷入已载入 ZIP', root.offline_zip_btn.configure.call_args.kwargs['text'])

    def test_failed_stage_clears_stale_selection_and_unlocks(self):
        root = self.root(); root._pending_zip_path = 'old.zip'; root._zip_loading = True
        root._finish_zip_stage('bad.zip', None, '缺少 DMA')
        self.assertIsNone(root._pending_zip_path)
        self.assertFalse(root._zip_loading)
        self.assertIn('缺少 DMA', root.notice.call_args.args[0])
        root.sync_controls.assert_called_once()

    def test_stage_thread_failure_unlocks(self):
        root = self.root(); root._stage_zip_worker = mock.Mock()
        with mock.patch.object(manager.threading.Thread, 'start', side_effect=RuntimeError('failed')):
            manager.LBJManager._stage_zip(root, 'firmware.zip')
        self.assertFalse(root._zip_loading)
        self.assertIsNone(root._pending_zip_path)
        self.assertIn('failed', root.notice.call_args.args[0])

    def test_zip_validation_blocks_device_task(self):
        root = self.root(); root._zip_loading = True; root.tasks = manager.DeviceTasks()
        with self.assertRaises(manager.TaskBusyError):
            manager.LBJManager.begin_task(root, '更新', 'PICO')
        self.assertIsNone(root.tasks.active)

    def test_close_waits_for_zip_validation(self):
        root = self.root(); root._zip_loading = True; root.tasks = manager.DeviceTasks(); root.destroy = mock.Mock()
        with mock.patch.object(manager.messagebox, 'showwarning'):
            manager.LBJManager._on_close(root)
        root.destroy.assert_not_called()

    def test_preloaded_zip_skips_picker_but_keeps_port_guard(self):
        root = manager.LBJManager.__new__(manager.LBJManager)
        root.is_working = False; root._zip_loading = False; root._pending_zip_path = 'selected.zip'
        root._confirm_selected_port = mock.Mock(return_value=False)
        with mock.patch.object(manager.filedialog, 'askopenfilename') as dialog:
            root.start_offline_zip_update()
        root._confirm_selected_port.assert_called_once_with('离线刷入')
        dialog.assert_not_called()

    def test_preloaded_zip_enters_existing_preflight_not_direct_flash(self):
        root = manager.LBJManager.__new__(manager.LBJManager)
        root.is_working = False; root._zip_loading = False; root._pending_zip_path = 'selected.zip'
        root._confirm_selected_port = mock.Mock(return_value=True)
        root.port_var = mock.Mock(); root.port_var.get.return_value = 'PICO'
        root._selected_profile = mock.Mock(return_value=manager.get_firmware_profile('main'))
        root.set_ui_state = mock.Mock(); root.clear_log = mock.Mock(); root.set_progress = mock.Mock(); root.log = mock.Mock()
        with (mock.patch.object(manager.filedialog, 'askopenfilename') as dialog,
              mock.patch.object(manager.threading, 'Thread') as thread):
            root.start_offline_zip_update()
        dialog.assert_not_called()
        self.assertEqual(thread.call_args.kwargs['target'], root._offline_zip_prepare_worker)
        self.assertEqual(thread.call_args.kwargs['args'][1], 'selected.zip')
        root.set_ui_state.assert_called_once_with(True)

    def test_zip_worker_cleans_temp_directory_without_device_commands(self):
        root = self.root(); destinations = []
        def extract(path, directory, profile):
            destinations.append(directory)
            return [], {'label': 'fixture'}
        root._extract_zip_firmware = extract
        with mock.patch.object(manager, 'run_command') as transport:
            manager.LBJManager._stage_zip_worker(root, 'file.zip', manager.get_firmware_profile('main'))
        transport.assert_not_called()
        self.assertFalse(pathlib.Path(destinations[0]).exists())
        self.assertEqual(root.after.call_args.args[2:], ('file.zip', {'label': 'fixture'}, ''))

    def test_zip_worker_reports_validation_error(self):
        root = self.root(); root._extract_zip_firmware = mock.Mock(side_effect=ValueError('missing main.py'))
        manager.LBJManager._stage_zip_worker(root, 'bad.zip', manager.get_firmware_profile('main'))
        self.assertEqual(root.after.call_args.args[2:], ('bad.zip', None, 'missing main.py'))

    def test_log_path_uses_background_import_without_file_picker(self):
        panel = manager.HistoryPanel.__new__(manager.HistoryPanel)
        panel._loading_data = panel._pico_busy = False
        panel.owner = SimpleNamespace(is_working=False, _zip_loading=False)
        panel.sync_controls = mock.Mock(); panel.source_status = mock.Mock()
        with (mock.patch.object(manager.filedialog, 'askopenfilename') as dialog,
              mock.patch.object(manager.threading, 'Thread') as thread):
            panel.load_json_file('中文 日志.jsonl')
        dialog.assert_not_called()
        self.assertTrue(panel._loading_data)
        self.assertEqual(thread.call_args.kwargs['args'], ('中文 日志.jsonl',))


if __name__ == '__main__':
    unittest.main()
