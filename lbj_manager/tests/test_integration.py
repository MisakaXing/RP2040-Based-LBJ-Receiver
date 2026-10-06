import ast
import importlib.util
import json
import pathlib
import sys
import threading
import time
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lbj_manager as manager
import solder_check as check


def port(name='PICO', vid=0x2E8A, product='Pico'):
    return SimpleNamespace(device=name, vid=vid, product=product)


def raw(train='139', speed='---', prefix='0D', stamp='2026-10-06 12:34'):
    return {'t': stamp, 'd': {'basic': {'train_no': train, 'speed_kmh': speed},
                            'extended': {'class_tag': prefix, 'loco_type': '轨道探伤车-04782A'}}}


class TaskTests(unittest.TestCase):
    def test_single_owner(self):
        tasks = manager.DeviceTasks()
        lease = tasks.acquire('读取', 'PICO')
        for operation in ('更新', '检查', '导出'):
            with self.assertRaises(manager.TaskBusyError):
                tasks.acquire(operation, 'PICO')
        self.assertIs(tasks.active, lease)

    def test_stale_release_cannot_unlock_new_task(self):
        tasks = manager.DeviceTasks()
        first = tasks.acquire('更新', 'PICO')
        self.assertTrue(tasks.release(first))
        second = tasks.acquire('读取', 'PICO')
        self.assertFalse(tasks.release(first))
        self.assertIs(tasks.active, second)

    def test_forged_equal_token_does_not_unlock(self):
        tasks = manager.DeviceTasks()
        lease = tasks.acquire('更新', 'PICO')
        forged = manager.DeviceLease(lease.serial, lease.owner, lease.port)
        self.assertFalse(tasks.release(forged))

    def test_empty_and_placeholder_ports_rejected(self):
        for value in ('', manager.PLACEHOLDER_PORT, '未检测到设备', '请选择端口...'):
            with self.assertRaises(ValueError):
                manager.DeviceTasks().acquire('更新', value)

    def test_none_never_releases(self):
        self.assertFalse(manager.DeviceTasks().release(None))

    def test_concurrent_claims_have_exactly_one_winner(self):
        tasks, winners = manager.DeviceTasks(), []
        barrier = threading.Barrier(40)
        def claim():
            barrier.wait()
            try:
                winners.append(tasks.acquire('并发测试', 'PICO'))
            except manager.TaskBusyError:
                pass
        threads = [threading.Thread(target=claim) for _ in range(40)]
        for worker in threads: worker.start()
        for worker in threads: worker.join(3)
        self.assertEqual(len(winners), 1)
        self.assertTrue(tasks.release(winners[0]))


class PortTests(unittest.TestCase):
    def test_initial_single_pico_selected(self):
        self.assertEqual(manager.choose_port([port()], '', True), (['PICO'], 'PICO'))

    def test_initial_many_requires_choice(self):
        self.assertEqual(manager.choose_port([port('A'), port('B')], '', True)[1], manager.PLACEHOLDER_PORT)

    def test_chosen_port_preserved_after_enumeration_reorder(self):
        self.assertEqual(manager.choose_port([port('B'), port('A')], 'A')[1], 'A')

    def test_unplug_never_falls_over_to_other_receiver(self):
        self.assertEqual(manager.choose_port([port('B')], 'A')[1], manager.PLACEHOLDER_PORT)

    def test_non_pico_and_debug_probes_not_candidates(self):
        self.assertEqual(manager.choose_port([port('Bluetooth', None), port('probe', product='Debug Probe')], '', True)[0], [])

    def test_late_attach_needs_manual_selection(self):
        self.assertEqual(manager.choose_port([port()], '', False)[1], manager.PLACEHOLDER_PORT)

    def test_selected_debug_probe_is_cleared(self):
        self.assertEqual(manager.choose_port([port('probe', product='CMSIS-DAP')], 'probe')[1], manager.PLACEHOLDER_PORT)

    def test_explicit_scan_selects_single_late_attached_pico(self):
        self.assertEqual(manager.choose_port([port()], manager.PLACEHOLDER_PORT, explicit=True), (['PICO'], 'PICO'))

    def test_explicit_scan_many_requires_choice(self):
        self.assertEqual(manager.choose_port([port('A'), port('B')], manager.PLACEHOLDER_PORT, explicit=True)[1], manager.PLACEHOLDER_PORT)

    def test_explicit_scan_preserves_valid_choice_with_multiple_devices(self):
        self.assertEqual(manager.choose_port([port('A'), port('B')], 'B', explicit=True)[1], 'B')

    def test_explicit_scan_can_select_only_remaining_device(self):
        self.assertEqual(manager.choose_port([port('B')], 'A', explicit=True)[1], 'B')

    def test_explicit_scan_empty_clears_disconnected_selection(self):
        self.assertEqual(manager.choose_port([], 'A', explicit=True), ([], manager.PLACEHOLDER_PORT))

    def test_explicit_scan_still_excludes_non_pico(self):
        self.assertEqual(manager.choose_port([port('other', vid=1234)], '', explicit=True), ([], manager.PLACEHOLDER_PORT))


class ScanTests(unittest.TestCase):
    def root(self, selected=manager.PLACEHOLDER_PORT):
        value = mock.Mock()
        value.get.return_value = selected
        value.set.side_effect = lambda new: setattr(value.get, 'return_value', new)
        root = SimpleNamespace(is_working=False, _scan_initialized=True, port_var=value,
                               pico_candidate_ports=set(), port_menu=mock.Mock(),
                               connection_value=mock.Mock(), device_status=mock.Mock(),
                               notice=mock.Mock(), sync_controls=mock.Mock(),
                               _reset_selected_device_state=mock.Mock())
        root.refresh_ports = manager.LBJManager.refresh_ports.__get__(root)
        return root

    def test_scan_action_selects_and_reports_without_opening_serial(self):
        root = self.root()
        with (mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[port()]),
              mock.patch.object(manager.serial, 'Serial') as transport,
              mock.patch.object(manager, 'run_command') as command):
            self.assertTrue(manager.LBJManager.scan_devices(root))
        self.assertEqual(root.port_var.get(), 'PICO')
        self.assertEqual(root.pico_candidate_ports, {'PICO'})
        self.assertIn('已自动选中 Pico：PICO', root.notice.call_args.args[0])
        self.assertIn('已自动选中 Pico', root.device_status.configure.call_args.kwargs['text'])
        transport.assert_not_called(); command.assert_not_called()

    def test_background_refresh_does_not_select_late_device_or_overwrite_notice(self):
        root = self.root()
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[port()]):
            root.refresh_ports()
        self.assertEqual(root.port_var.get(), manager.PLACEHOLDER_PORT)
        self.assertIn('发现 1 台 Pico', root.device_status.configure.call_args.kwargs['text'])
        root.notice.assert_not_called()

    def test_initial_refresh_can_select_single_device(self):
        root = self.root(); root._scan_initialized = False
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[port()]):
            root.refresh_ports()
        self.assertEqual(root.port_var.get(), 'PICO')
        self.assertIn('已自动选中 Pico', root.notice.call_args.args[0])
        self.assertIn('已自动选中 Pico', root.device_status.configure.call_args.kwargs['text'])

    def test_auto_selection_hint_survives_background_scan_without_footer_spam(self):
        root = self.root(); root._scan_initialized = False
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[port()]):
            root.refresh_ports()
            root.notice.reset_mock()
            root.refresh_ports(silent=True)
        self.assertIn('已自动选中 Pico', root.device_status.configure.call_args.kwargs['text'])
        root.notice.assert_not_called()

    def test_explicit_scan_of_already_selected_device_still_confirms(self):
        root = self.root('PICO')
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[port()]):
            manager.LBJManager.scan_devices(root)
        self.assertIn('已自动选中 Pico', root.device_status.configure.call_args.kwargs['text'])
        self.assertIn('已自动选中 Pico', root.notice.call_args.args[0])

    def test_manually_selected_device_is_not_called_automatic(self):
        root = self.root('PICO')
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[port()]):
            root.refresh_ports(silent=True)
        self.assertIn('已选中 Pico', root.device_status.configure.call_args.kwargs['text'])
        self.assertNotIn('自动', root.device_status.configure.call_args.kwargs['text'])

    def test_unplug_removes_green_selection_confirmation(self):
        root = self.root('PICO'); root._auto_selected_port = 'PICO'
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[]):
            root.refresh_ports(silent=True)
        self.assertIsNone(root._auto_selected_port)
        self.assertIn('未发现 Pico', root.device_status.configure.call_args.kwargs['text'])
        self.assertEqual(root.device_status.configure.call_args.kwargs['fg_color'], manager.COLORS['surface_alt'])

    def test_many_device_scan_reports_selection_required(self):
        root = self.root()
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[port('A'), port('B')]):
            manager.LBJManager.scan_devices(root)
        self.assertEqual(root.port_var.get(), manager.PLACEHOLDER_PORT)
        self.assertIn('发现 2 台 Pico', root.notice.call_args.args[0])

    def test_empty_scan_clears_choice_and_reports(self):
        root = self.root('PICO')
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[]):
            manager.LBJManager.scan_devices(root)
        self.assertEqual(root.port_var.get(), manager.PLACEHOLDER_PORT)
        self.assertIn('未发现 Pico', root.notice.call_args.args[0])
        root._reset_selected_device_state.assert_called_once()

    def test_enumeration_failure_invalidates_candidates_and_reports(self):
        root = self.root('PICO'); root.pico_candidate_ports = {'PICO'}
        with mock.patch.object(manager.serial.tools.list_ports, 'comports', side_effect=OSError('USB failed')):
            self.assertFalse(manager.LBJManager.scan_devices(root))
        self.assertEqual(root.pico_candidate_ports, set())
        root.sync_controls.assert_called_once()
        self.assertIn('USB failed', root.notice.call_args.args[0])

    def test_busy_scan_does_not_enumerate_or_change_selection(self):
        root = self.root('PICO'); root.is_working = True
        with mock.patch.object(manager.serial.tools.list_ports, 'comports') as scan:
            self.assertFalse(manager.LBJManager.scan_devices(root))
        scan.assert_not_called()
        self.assertEqual(root.port_var.get(), 'PICO')
        self.assertIn('任务尚未结束', root.notice.call_args.args[0])


class DispatchTests(unittest.TestCase):
    def test_threads_only_enqueue_and_main_drains(self):
        dispatch, observations = manager.UiDispatch(), []
        main_thread = threading.get_ident()
        def producer():
            for i in range(1000):
                dispatch.post(0, lambda i=i: observations.append((i, threading.get_ident())))
        worker = threading.Thread(target=producer)
        worker.start(); worker.join()
        self.assertFalse(observations)
        while dispatch.drain(lambda delay, fn, args: fn(*args)):
            pass
        self.assertEqual([i for i, tid in observations], list(range(1000)))
        self.assertTrue(all(tid == main_thread for i, tid in observations))

    def test_drain_limit_protects_event_loop(self):
        dispatch = manager.UiDispatch()
        for i in range(100): dispatch.post(0, mock.Mock())
        self.assertEqual(dispatch.drain(lambda *args: None, limit=7), 7)

    def test_close_rejects_late_updates(self):
        dispatch = manager.UiDispatch()
        callback = mock.Mock()
        dispatch.post(0, callback)
        dispatch.close()
        self.assertFalse(dispatch.post(0, callback))
        dispatch.drain(lambda delay, fn, args: fn(*args))
        callback.assert_not_called()

    def test_worker_after_does_not_touch_tk(self):
        root = SimpleNamespace(_ui_thread=-1, _task_context=threading.local(),
                               dispatch=manager.UiDispatch(), tasks=manager.DeviceTasks(),
                               set_ui_state=mock.Mock(), _finish_updater=mock.Mock())
        callback = mock.Mock()
        manager.LBJManager.after(root, 0, callback, 'value')
        callback.assert_not_called()
        root.dispatch.drain(lambda delay, fn, args: fn(*args))
        callback.assert_called_once_with('value')

    def test_stale_worker_ui_updates_ignored(self):
        tasks = manager.DeviceTasks()
        first = tasks.acquire('更新', 'PICO')
        root = SimpleNamespace(_ui_thread=-1, _task_context=SimpleNamespace(lease=first),
                               dispatch=manager.UiDispatch(), tasks=tasks,
                               set_ui_state=mock.Mock(), _finish_updater=mock.Mock())
        callback = mock.Mock()
        manager.LBJManager.after(root, 0, callback)
        tasks.release(first); tasks.acquire('历史', 'PICO')
        root.dispatch.drain(lambda delay, fn, args: fn(*args))
        callback.assert_not_called()

    def test_worker_finish_retains_lease_identity(self):
        tasks = manager.DeviceTasks()
        first = tasks.acquire('更新', 'PICO')
        root = SimpleNamespace(_ui_thread=-1, _task_context=SimpleNamespace(lease=first),
                               dispatch=manager.UiDispatch(), tasks=tasks,
                               set_ui_state=mock.Mock(), _finish_updater=mock.Mock())
        manager.LBJManager.after(root, 0, root.set_ui_state, False)
        root.dispatch.drain(lambda delay, fn, args: fn(*args))
        root._finish_updater.assert_called_once_with(first)
        root.set_ui_state.assert_not_called()


class RecordTests(unittest.TestCase):
    def test_numeric_prefix_and_chinese_loco_preserved(self):
        result = manager.normalise_record(raw())
        self.assertEqual(result['train_no'], '0D139')
        self.assertEqual(result['loco_type'], '轨道探伤车-04782A')

    def test_dash_speed_is_unknown_not_zero(self):
        self.assertEqual(manager.normalise_record(raw())['speed'], '---')
        self.assertEqual(manager.normalise_record(raw(speed=0))['speed'], '0')

    def test_placeholder_basic_kept(self):
        result = manager.normalise_record(raw('---'))
        self.assertEqual(result['train_no'], '未知')

    def test_numeric_train_and_null_fields_tolerated(self):
        result = manager.normalise_record(raw(139, None, None))
        self.assertEqual(result['train_no'], '139')
        self.assertEqual(result['speed'], '---')

    def test_extended_only_keeps_vehicle_and_coordinates(self):
        record = raw(); record['d']['basic'] = None
        record['d']['extended'].update(lat="39°30.0000'N", lon="116°15.0000'E")
        result = manager.normalise_record(record)
        self.assertEqual((result['lat'], result['lon']), (39.5, 116.25))
        self.assertEqual(result['gps_status'], '有坐标')

    def test_bad_json_and_schema_skipped_without_losing_valid_rows(self):
        lines = [json.dumps(raw()), 'oops', '[]', '{"d":3}', '{"d":{"basic":3}}', '', json.dumps(raw('7'))]
        rows, invalid = manager.parse_history_lines(lines)
        self.assertEqual(invalid, 4)
        self.assertEqual([r['_index'] for r in rows], [0, 1])

    def test_9999_records_all_retained(self):
        rows, invalid = manager.parse_history_lines([json.dumps(raw())] * 9999)
        self.assertEqual((len(rows), invalid), (9999, 0))
        self.assertEqual(rows[-1]['_index'], 9998)

    def test_five_digit_number_and_ab_are_not_truncated(self):
        self.assertIn('04782A', manager.normalise_record(raw())['loco_type'])

    def test_record_time_date_and_minutes_preserved(self):
        self.assertEqual(manager.normalise_record(raw())['time'], '2026-10-06 12:34')

    def test_zero_coordinate_is_valid(self):
        self.assertEqual(manager.decimal_coordinate(0, 'lat'), 0)

    def test_southern_and_western_coordinates(self):
        self.assertEqual(manager.decimal_coordinate("10°30.0'S", 'lat'), -10.5)
        self.assertEqual(manager.decimal_coordinate("70°30.0'W", 'lon'), -70.5)

    def test_bad_coordinate_cases(self):
        for value in (None, '', True, float('nan'), float('inf'), "90°01'N", "39°60'N", "39°00'E", "abc39°00'N"):
            self.assertIsNone(manager.decimal_coordinate(value, 'lat'))

    def test_log_import_does_not_mutate_input(self):
        record = raw(); original = json.dumps(record)
        manager.normalise_record(record)
        self.assertEqual(json.dumps(record), original)


class FilterTests(unittest.TestCase):
    def rows(self):
        return [manager.normalise_record(raw(stamp='2026-10-06 ' + t)) for t in ('00:30', '12:34', '23:30')]

    def test_full_date_minute_records_compare_correctly(self):
        self.assertEqual(len(manager.filter_records(self.rows(), start='12:34', end='12:34')), 1)

    def test_cross_midnight_range(self):
        self.assertEqual(len(manager.filter_records(self.rows(), start='23:00', end='01:00')), 2)

    def test_case_insensitive_train_and_loco(self):
        self.assertEqual(len(manager.filter_records(self.rows(), train='0d', loco='探伤')), 3)

    def test_one_sided_ranges(self):
        self.assertEqual(len(manager.filter_records(self.rows(), start='12:00')), 2)
        self.assertEqual(len(manager.filter_records(self.rows(), end='12:00')), 1)

    def test_invalid_range_fails_before_rows_change(self):
        rows = self.rows()
        for invalid in ('24:00', '12:60', '12:00:60', 'nonsense', '12', '-1:00'):
            with self.assertRaises(ValueError): manager.filter_records(rows, start=invalid)
        self.assertEqual(len(rows), 3)

    def test_minute_end_is_inclusive(self):
        self.assertEqual(manager.time_filter_value('12:34', True), 12 * 3600 + 34 * 60 + 59)

    def test_malformed_record_time_not_matched(self):
        rows = [manager.normalise_record(raw(stamp='未知'))]
        self.assertEqual(manager.filter_records(rows, start='00:00'), [])
        self.assertEqual(len(manager.filter_records(rows)), 1)


class CommandTests(unittest.TestCase):
    def test_output_and_error_collected(self):
        ok, output = manager.run_command([sys.executable, '-c', "import sys; print('ok'); print('warn',file=sys.stderr)"], 3)
        self.assertTrue(ok); self.assertIn('ok', output); self.assertIn('warn', output)

    def test_exit_failure(self):
        ok, output = manager.run_command([sys.executable, '-c', "raise SystemExit(4)"], 3)
        self.assertFalse(ok)

    def test_idle_output_cannot_block_timeout(self):
        started = time.monotonic()
        ok, output = manager.run_command([sys.executable, '-c', 'import time; time.sleep(5)'], .2)
        self.assertFalse(ok); self.assertIn('超时', output)
        self.assertLess(time.monotonic() - started, 2)

    def test_unterminated_line_cannot_block_timeout(self):
        started = time.monotonic()
        ok, output = manager.run_command([sys.executable, '-c', "import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(5)"], .2)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - started, 2)

    def test_progress_observer_failure_does_not_stop_command(self):
        observer = mock.Mock(side_effect=RuntimeError('widget closed'))
        ok, output = manager.run_command([sys.executable, '-c', "print('complete')"], 3, observer)
        self.assertTrue(ok); self.assertIn('complete', output)

    def test_arguments_not_interpreted_as_shell(self):
        ok, output = manager.run_command([sys.executable, '-c', 'import sys;print(sys.argv[1])', '$(touch unexpected)'], 3)
        self.assertTrue(ok); self.assertIn('$(touch unexpected)', output)


class LifecycleTests(unittest.TestCase):
    def root(self):
        root = SimpleNamespace(tasks=manager.DeviceTasks(), _updater_lease=None,
            _finishing=False, _device_touched=False, _reset_done=False, _flash_partial=False,
            _task_context=threading.local(), dispatch=manager.UiDispatch(), notice=mock.Mock(),
            sync_controls=mock.Mock(), refresh_ports=mock.Mock(), run_mpremote=mock.Mock(return_value=(True, 'reset')))
        root._release_updater = manager.LBJManager._release_updater.__get__(root)
        root._finish_updater = manager.LBJManager._finish_updater.__get__(root)
        root._updater_lease = root.tasks.acquire('更新', 'PICO')
        return root

    def test_untouched_task_releases_without_reboot(self):
        root = self.root()
        root._finish_updater(root._updater_lease)
        self.assertIsNone(root.tasks.active)
        root.run_mpremote.assert_not_called()

    def test_cancelled_preflight_recovers_before_unlock(self):
        root = self.root(); root._device_touched = True
        lease = root._updater_lease
        root._finish_updater(lease)
        deadline = time.monotonic() + 2
        while not root.run_mpremote.called and time.monotonic() < deadline: time.sleep(.01)
        self.assertIs(root.tasks.active, lease)
        root.dispatch.drain(lambda delay, fn, args: fn(*args))
        self.assertIsNone(root.tasks.active)
        self.assertIn('machine.reset()', str(root.run_mpremote.call_args))

    def test_already_reset_not_reset_twice(self):
        root = self.root(); root._device_touched = root._reset_done = True
        root._finish_updater(root._updater_lease)
        root.run_mpremote.assert_not_called()

    def test_partial_flash_not_booted_as_healthy(self):
        root = self.root(); root._device_touched = root._flash_partial = True
        with mock.patch.object(manager.messagebox, 'showwarning') as warning:
            root._finish_updater(root._updater_lease)
        root.run_mpremote.assert_not_called()
        warning.assert_called_once()

    def test_stale_finish_cannot_release_history(self):
        root = self.root(); old = root._updater_lease
        root.tasks.release(old); current = root.tasks.acquire('历史', 'PICO')
        root._finish_updater(old)
        self.assertIs(root.tasks.active, current)

    def test_recovery_failure_visible_and_unlocks(self):
        root = self.root(); root._device_touched = True
        root.run_mpremote.return_value = (False, 'USB missing')
        root._finish_updater(root._updater_lease)
        time.sleep(.03)
        with mock.patch.object(manager.messagebox, 'showwarning') as warning:
            root.dispatch.drain(lambda delay, fn, args: fn(*args))
        warning.assert_called_once()
        self.assertIsNone(root.tasks.active)

    def test_close_blocked_while_task_active(self):
        root = self.root(); root.destroy = mock.Mock()
        with mock.patch.object(manager.messagebox, 'showwarning'):
            manager.LBJManager._on_close(root)
        root.destroy.assert_not_called()

    def test_close_blocked_while_file_loading(self):
        root = self.root(); root.tasks.release(root._updater_lease)
        root.history = SimpleNamespace(_loading_data=True); root.destroy = mock.Mock()
        with mock.patch.object(manager.messagebox, 'showwarning'):
            manager.LBJManager._on_close(root)
        root.destroy.assert_not_called()

    def test_safe_close_shuts_down_dispatch(self):
        root = self.root(); root.tasks.release(root._updater_lease)
        root._zip_loading = False
        root.history = SimpleNamespace(_loading_data=False); root.destroy = mock.Mock()
        manager.LBJManager._on_close(root)
        root.destroy.assert_called_once()
        self.assertTrue(root.dispatch.closed)

    def test_wrong_lease_never_runs_mpremote(self):
        root = self.root()
        result = manager.LBJManager.run_mpremote(root, 'OTHER', ['exec', 'print(1)'])
        self.assertEqual(result[0], False)


class TransferTests(unittest.TestCase):
    def panel(self):
        panel = manager.HistoryPanel.__new__(manager.HistoryPanel)
        tasks = manager.DeviceTasks()
        lease = tasks.acquire('读取历史', 'PICO')
        panel.owner = SimpleNamespace(tasks=tasks, is_working=True, notice=mock.Mock(),
                                     sync_controls=mock.Mock(), refresh_ports=mock.Mock(), task_progress=mock.Mock())
        panel._pico_busy = True
        panel.after = lambda delay, fn, *args: fn(*args)
        panel._accept_records = mock.Mock()
        return panel, lease

    def test_read_releases_shared_lock_after_verified_download(self):
        panel, lease = self.panel()
        result = manager.HistoryDownload(json.dumps(raw()).encode(), 'hash')
        with mock.patch.object(manager, 'require_pico_port'), mock.patch.object(manager, 'download_history', return_value=result):
            panel._transfer_worker(lease, None)
        self.assertEqual(panel._accept_records.call_args.args[0][0]['train_no'], '0D139')
        self.assertIsNone(panel.owner.tasks.active)

    def test_failure_never_loads_or_saves_partial_history(self):
        for path in (None, 'chosen.jsonl'):
            panel, lease = self.panel()
            with mock.patch.object(manager, 'require_pico_port'), mock.patch.object(manager, 'download_history', side_effect=manager.HistoryTransferError('corrupt')), mock.patch.object(manager, 'save_history_atomic') as save, mock.patch.object(manager.messagebox, 'showerror'):
                panel._transfer_worker(lease, path)
            save.assert_not_called(); panel._accept_records.assert_not_called()
            self.assertIsNone(panel.owner.tasks.active)

    def test_export_uses_exact_verified_bytes(self):
        panel, lease = self.panel(); result = manager.HistoryDownload(b'bytes', 'hash')
        with mock.patch.object(manager, 'require_pico_port'), mock.patch.object(manager, 'download_history', return_value=result), mock.patch.object(manager, 'save_history_atomic') as save:
            panel._transfer_worker(lease, 'chosen.jsonl')
        save.assert_called_once_with('chosen.jsonl', b'bytes')

    def test_strict_utf8_failure_keeps_previous_records(self):
        panel, lease = self.panel()
        with mock.patch.object(manager, 'require_pico_port'), mock.patch.object(manager, 'download_history', return_value=manager.HistoryDownload(b'\xff', 'hash')), mock.patch.object(manager.messagebox, 'showerror'):
            panel._transfer_worker(lease, None)
        panel._accept_records.assert_not_called()

    def test_late_history_finish_does_not_unlock_firmware(self):
        panel, lease = self.panel()
        panel.owner.tasks.release(lease); current = panel.owner.tasks.acquire('固件', 'PICO')
        panel._finish_device_transfer(lease)
        self.assertIs(panel.owner.tasks.active, current)

    def test_busy_history_start_does_not_open_dialog(self):
        panel, lease = self.panel(); panel._loading_data = False
        with mock.patch.object(manager.filedialog, 'asksaveasfilename') as dialog:
            panel.start_pico_export()
        dialog.assert_not_called()


class NavigationShortcutTests(unittest.TestCase):
    def bindings(self, platform):
        root = SimpleNamespace(bind=mock.Mock(), show_page=mock.Mock())
        with mock.patch.object(manager.sys, 'platform', platform):
            manager.LBJManager._bind_navigation_shortcuts(root)
        return root, root.bind.call_args_list

    def test_mac_uses_explicit_command_keypress(self):
        _, calls = self.bindings('darwin')
        self.assertEqual([call.args[0] for call in calls],
                         ['<Command-KeyPress-1>', '<Command-KeyPress-2>'])

    def test_windows_uses_explicit_control_keypress(self):
        _, calls = self.bindings('win32')
        self.assertEqual([call.args[0] for call in calls],
                         ['<Control-KeyPress-1>', '<Control-KeyPress-2>'])

    def test_linux_uses_control_keypress(self):
        _, calls = self.bindings('linux')
        self.assertEqual([call.args[0] for call in calls],
                         ['<Control-KeyPress-1>', '<Control-KeyPress-2>'])

    def test_callbacks_select_expected_pages(self):
        for platform in ('darwin', 'win32'):
            root, calls = self.bindings(platform)
            for call in calls:
                call.args[1](None)
            self.assertEqual(root.show_page.call_args_list,
                             [mock.call('设备管理'), mock.call('历史记录')])

    def test_no_ambiguous_numeric_shortcuts_in_source(self):
        tree = ast.parse((ROOT / 'lbj_manager.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'bind':
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    self.assertNotIn(node.args[0].value, ('<Command-1>', '<Command-2>', '<Control-1>', '<Control-2>'))


class SourceTests(unittest.TestCase):
    def test_standalone_entry_does_not_import_old_gui_modules(self):
        tree = ast.parse((ROOT / 'lbj_manager.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ('pico_updater', 'jsondecode', 'pico_history'))

    def test_retired_tool_directories_are_not_required(self):
        for name in ('pico_updater', 'log-viewer'):
            self.assertFalse((ROOT.parent / name).exists())

    def test_regression_suites_import_only_the_integrated_modules(self):
        for path in (ROOT / 'tests').glob('test_*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, ('pico_updater', 'jsondecode', 'pico_history'))
                elif isinstance(node, ast.Import):
                    self.assertFalse({n.name for n in node.names} & {'pico_updater', 'jsondecode', 'pico_history'})

    def test_rtc_helper_is_preserved_and_compiles(self):
        ast.parse((ROOT / 'rtc_sync_gui.py').read_text(encoding='utf-8'))

    def test_worker_dispatch_does_not_call_tk(self):
        import inspect
        source = inspect.getsource(manager.LBJManager.after)
        worker_source = source[source.index('lease = '):]
        self.assertNotIn('super().after', worker_source)
        self.assertIn('dispatch.post', worker_source)

    def test_both_profiles_include_dma_and_protection(self):
        for label in manager.FIRMWARE_BRANCHES:
            profile = manager.get_firmware_profile(label)
            for name in ('pio_dma_rx.py', 'device_protection.py'):
                self.assertIn(name, profile['optional_runtime_files'])

    def test_w_core_probes_gp46_vsys_and_ordinary_keeps_gp27(self):
        wireless = check.core_script(manager.HARDWARE_TEST_SCRIPT, True)
        standard = check.core_script(manager.HARDWARE_TEST_SCRIPT, False)
        self.assertIn('machine.ADC(machine.Pin(46))', wireless)
        self.assertNotIn('machine.ADC(machine.Pin(41))', wireless)
        self.assertIn('machine.ADC(machine.Pin(27))', standard)
        self.assertIn("_core_step('Wireless','running')", wireless)
        ast.parse(wireless); ast.parse(standard)

    def test_vsys_uses_median_and_no_gate_or_gain(self):
        adc = mock.Mock()
        adc.read_u16.side_effect = [50000, 30535, 20000]
        machine = SimpleNamespace(Pin=mock.Mock(return_value='pin'), ADC=mock.Mock(return_value=adc))
        ns = {'machine': machine, 'test_results': {}}
        exec(check.VSYS_SCRIPT, ns)
        self.assertEqual(machine.Pin.call_args.args, (46,))
        self.assertAlmostEqual(ns['test_results']['Battery_V'], 30535 / 65535 * 9.9, places=3)
        self.assertEqual(ns['test_results']['Voltage_Source'], 'VSYS')

    def test_train_zero_is_not_missing(self):
        self.assertEqual(manager.normalise_record(raw(train=0, prefix=''))['train_no'], '0')


class BundleTests(unittest.TestCase):
    def bundle(self, directory, profile, modern=True):
        files = []
        for name in manager.runtime_file_order(profile):
            if name == 'boot.py':
                continue
            path = pathlib.Path(directory) / name
            data = 'pass\n'
            if name == 'main.py':
                data = ('Program_ver = "5.14-W"\n' if profile['branch'] != 'main'
                        else 'Program_ver = "5.12"\n')
                if modern:
                    data += 'from device_protection import DeviceProtection\n'
            if name == 'lbj_receiver.py' and modern:
                data = 'try:\n    from pio_dma_rx import PioDmaRx\nexcept ImportError:\n    pass\n'
            path.write_text(data)
            files.append({'name': name, 'path': str(path)})
        return files

    def test_modern_bundles_complete_for_both_branches(self):
        for profile in manager.FIRMWARE_BRANCHES.values():
            with self.subTest(branch=profile['branch']), tempfile.TemporaryDirectory() as tmp:
                files = self.bundle(tmp, profile)
                names = manager.validate_firmware_bundle(profile, files)
                self.assertLess(names.index('pio_dma_rx.py'), names.index('main.py'))
                self.assertLess(names.index('device_protection.py'), names.index('main.py'))

    def test_modern_missing_dma_and_protection_rejected(self):
        for profile in manager.FIRMWARE_BRANCHES.values():
            for missing in ('pio_dma_rx.py', 'device_protection.py'):
                with self.subTest(branch=profile['branch'], missing=missing), tempfile.TemporaryDirectory() as tmp:
                    files = [item for item in self.bundle(tmp, profile) if item['name'] != missing]
                    with self.assertRaisesRegex(ValueError, missing):
                        manager.validate_firmware_bundle(profile, files)

    def test_old_firmware_without_new_imports_remains_compatible(self):
        profile = manager.get_firmware_profile('main')
        with tempfile.TemporaryDirectory() as tmp:
            files = [item for item in self.bundle(tmp, profile, modern=False)
                     if item['name'] in profile['runtime_files']]
            manager.validate_firmware_bundle(profile, files)

    def test_comments_do_not_create_false_dependencies(self):
        profile = manager.get_firmware_profile('main')
        with tempfile.TemporaryDirectory() as tmp:
            files = [item for item in self.bundle(tmp, profile, modern=False)
                     if item['name'] in profile['runtime_files']]
            pathlib.Path(tmp, 'main.py').write_text('# from pio_dma_rx import PioDmaRx\npass\n')
            manager.validate_firmware_bundle(profile, files)

    def test_import_alias_dependency_is_checked(self):
        profile = manager.get_firmware_profile('main')
        with tempfile.TemporaryDirectory() as tmp:
            files = [item for item in self.bundle(tmp, profile, modern=False)
                     if item['name'] in profile['runtime_files']]
            pathlib.Path(tmp, 'main.py').write_text('import device_protection as protection\n')
            with self.assertRaisesRegex(ValueError, 'device_protection.py'):
                manager.validate_firmware_bundle(profile, files)

    def test_duplicate_and_foreign_files_rejected(self):
        profile = manager.get_firmware_profile('main')
        with tempfile.TemporaryDirectory() as tmp:
            files = self.bundle(tmp, profile)
            with self.assertRaisesRegex(ValueError, '重复'):
                manager.validate_firmware_bundle(profile, files + [files[0]])
            with self.assertRaisesRegex(ValueError, '不允许'):
                manager.validate_firmware_bundle(profile, files + [{'name': 'extra.py', 'path': 'missing'}])

    def test_corrupt_syntax_utf8_and_unreadable_sources_rejected(self):
        profile = manager.get_firmware_profile('main')
        with tempfile.TemporaryDirectory() as tmp:
            files = self.bundle(tmp, profile)
            path = pathlib.Path(tmp, 'pio_dma_rx.py')
            for data in (b'if!\n', b'\xff'):
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, 'pio_dma_rx.py'):
                    manager.validate_firmware_bundle(profile, files)
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'pio_dma_rx.py'):
                manager.validate_firmware_bundle(profile, files)

    def test_offline_zip_flat_github_and_windows_paths_include_dma(self):
        for profile in manager.FIRMWARE_BRANCHES.values():
            for prefix in ('', 'repo/rp2040-main-program/', 'repo\\rp2040-main-program\\'):
                with self.subTest(branch=profile['branch'], prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                    files = self.bundle(tmp, profile)
                    archive_path = pathlib.Path(tmp, 'firmware.zip')
                    with zipfile.ZipFile(archive_path, 'w') as archive:
                        for item in files:
                            archive.write(item['path'], prefix + item['name'])
                        archive.writestr(prefix + 'README.md', 'not runtime')
                    dest = pathlib.Path(tmp, 'out'); dest.mkdir()
                    app = manager.LBJManager.__new__(manager.LBJManager)
                    app.target_dir = manager.TARGET_DIR; app.log = mock.Mock()
                    unpacked, info = app._extract_zip_firmware(archive_path, dest, profile)
                    self.assertEqual(info['branch'], profile['branch'])
                    self.assertEqual(unpacked[-1]['name'], 'main.py')
                    for item in files:
                        self.assertEqual(pathlib.Path(item['path']).read_bytes(), (dest / item['name']).read_bytes())

    def test_current_real_firmware_sources_pass_both_branch_zip_validation(self):
        sources = {'main': ROOT.parent / 'rp2040-main-program',
                   'Wireless-Enabled': pathlib.Path('/Users/xinghening/Documents/Codex/rp2040-lbj-wireless-worktree/rp2040-main-program')}
        for profile in manager.FIRMWARE_BRANCHES.values():
            source = sources[profile['branch']]
            if not source.exists():
                continue  # W checkout is optional on other contributors' Macs.
            with self.subTest(branch=profile['branch']), tempfile.TemporaryDirectory() as tmp:
                archive_path = pathlib.Path(tmp, 'actual-firmware.zip')
                expected = []
                with zipfile.ZipFile(archive_path, 'w') as archive:
                    for name in manager.runtime_file_order(profile):
                        if (source / name).is_file():
                            archive.write(source / name, 'repo/rp2040-main-program/' + name)
                            expected.append(name)
                dest = pathlib.Path(tmp, 'out'); dest.mkdir()
                app = manager.LBJManager.__new__(manager.LBJManager)
                app.target_dir = manager.TARGET_DIR; app.log = mock.Mock()
                files, info = app._extract_zip_firmware(archive_path, dest, profile)
                self.assertEqual([item['name'] for item in files], expected)
                for name in expected:
                    self.assertEqual((source / name).read_bytes(), (dest / name).read_bytes())

    def test_online_incomplete_bundle_never_opens_flash_confirmation(self):
        profile = manager.get_firmware_profile('main')
        app = manager.LBJManager.__new__(manager.LBJManager)
        app.log = mock.Mock(); app._cleanup_temp_dir = mock.Mock()
        app.set_ui_state = mock.Mock(); app.show_confirm_dialog = mock.Mock()
        with mock.patch.object(manager.messagebox, 'showerror'):
            app._confirm_online_update('PICO', False, 'temp', [], {}, {}, profile)
        app.show_confirm_dialog.assert_not_called()
        app._cleanup_temp_dir.assert_called_once_with('temp')
        app.set_ui_state.assert_called_once_with(False)

    def test_zip_missing_new_module_is_rejected_by_actual_extractor(self):
        for profile in manager.FIRMWARE_BRANCHES.values():
            for missing in ('pio_dma_rx.py', 'device_protection.py'):
                with self.subTest(branch=profile['branch'], missing=missing), tempfile.TemporaryDirectory() as tmp:
                    files = self.bundle(tmp, profile)
                    archive_path = pathlib.Path(tmp, 'missing-module.zip')
                    with zipfile.ZipFile(archive_path, 'w') as archive:
                        for item in files:
                            if item['name'] != missing:
                                archive.write(item['path'], 'repo/rp2040-main-program/' + item['name'])
                    dest = pathlib.Path(tmp, 'out'); dest.mkdir()
                    app = manager.LBJManager.__new__(manager.LBJManager)
                    app.target_dir = manager.TARGET_DIR; app.log = mock.Mock()
                    with self.assertRaisesRegex(ValueError, missing):
                        app._extract_zip_firmware(archive_path, dest, profile)

    def test_online_missing_dma_is_rejected_before_confirmation(self):
        profile = manager.get_firmware_profile('main')
        with tempfile.TemporaryDirectory() as tmp:
            files = [item for item in self.bundle(tmp, profile) if item['name'] != 'pio_dma_rx.py']
            app = manager.LBJManager.__new__(manager.LBJManager)
            app.log = mock.Mock(); app._cleanup_temp_dir = mock.Mock()
            app.set_ui_state = mock.Mock(); app.show_confirm_dialog = mock.Mock()
            with mock.patch.object(manager.messagebox, 'showerror') as error:
                app._confirm_online_update('PICO', False, tmp, files, {}, {}, profile)
            self.assertIn('pio_dma_rx.py', error.call_args.args[1])
            app.show_confirm_dialog.assert_not_called()
            app.set_ui_state.assert_called_once_with(False)

    def test_copy_calls_write_all_runtime_files_main_last(self):
        profile = manager.get_firmware_profile('main')
        with tempfile.TemporaryDirectory() as tmp:
            files = self.bundle(tmp, profile)
            app = manager.LBJManager.__new__(manager.LBJManager)
            app.log = mock.Mock(); app.after = mock.Mock()
            app.run_mpremote = mock.Mock(return_value=(True, 'copied'))
            app._flash_partial = True
            self.assertTrue(app._copy_firmware_files('PICO', files))
            targets = [call.args[1][-1] for call in app.run_mpremote.call_args_list]
            self.assertIn(':pio_dma_rx.py', targets)
            self.assertIn(':device_protection.py', targets)
            self.assertEqual(targets[-1], ':main.py')
            self.assertFalse(app._flash_partial)

    def test_copy_failure_keeps_partial_flash_warning_flag(self):
        app = manager.LBJManager.__new__(manager.LBJManager)
        app.log = mock.Mock(); app.after = mock.Mock()
        app.run_mpremote = mock.Mock(return_value=(False, 'unplugged'))
        app._flash_partial = True
        self.assertFalse(app._copy_firmware_files('PICO', [{'name': 'main.py', 'path': 'fake'}]))
        self.assertTrue(app._flash_partial)


class ExtraLifecycleTests(unittest.TestCase):
    def test_thread_start_failure_releases_online_and_offline_tasks(self):
        for method in ('start_update_process', 'start_offline_zip_update'):
            app = manager.LBJManager.__new__(manager.LBJManager)
            app.is_working = False; app._confirm_selected_port = mock.Mock(return_value=True)
            app.port_var = mock.Mock(); app.port_var.get.return_value = 'PICO'
            app._selected_profile = mock.Mock(return_value=manager.get_firmware_profile('main'))
            app.set_ui_state = mock.Mock(); app.clear_log = mock.Mock()
            app.set_progress = mock.Mock(); app.log = mock.Mock()
            with (mock.patch.object(manager.threading.Thread, 'start', side_effect=RuntimeError('no threads')),
                 mock.patch.object(manager.filedialog, 'askopenfilename', return_value='fake.zip'),
                 mock.patch.object(manager.messagebox, 'showerror') as error):
                getattr(app, method)()
            self.assertEqual(app.set_ui_state.call_args_list, [mock.call(True), mock.call(False)])
            error.assert_called_once()

    def test_incompatible_hardware_does_not_mark_partial_or_wipe(self):
        app = manager.LBJManager.__new__(manager.LBJManager)
        app._flash_partial = False
        app._check_hardware_compatibility = mock.Mock(return_value=False)
        app.run_mpremote = mock.Mock()
        self.assertFalse(app._wipe_device_files('PICO', {}))
        self.assertFalse(app._flash_partial)
        app.run_mpremote.assert_not_called()

    def test_recovery_thread_cannot_start_unlocks_with_visible_warning(self):
        root = LifecycleTests().root(); root._device_touched = True
        with (mock.patch.object(manager.threading.Thread, 'start', side_effect=RuntimeError('no threads')),
              mock.patch.object(manager.messagebox, 'showwarning') as warning):
            root._finish_updater(root._updater_lease)
        self.assertIsNone(root.tasks.active)
        warning.assert_called_once()

    def test_marker_delete_reentrant_selection_is_suppressed(self):
        panel = manager.HistoryPanel.__new__(manager.HistoryPanel)
        panel._loading_data = False; panel._selecting = False
        panel._selection_key = None; panel._render_generation = 1
        panel.tree = mock.Mock(); panel.tree.selection.return_value = ('1',)
        def nested(*args):
            panel.on_tree_select(None)
        with mock.patch.object(manager._HistoryViewBase, 'on_tree_select', side_effect=nested) as base:
            panel.on_tree_select(None)
            panel.on_tree_select(None)
        base.assert_called_once()
        self.assertFalse(panel._selecting)

    def test_marker_clear_detaches_before_reentrant_event(self):
        panel = manager.HistoryPanel.__new__(manager.HistoryPanel)
        panel._loading_data = False; panel._selecting = False
        panel._selection_key = (1, '1'); panel._render_generation = 1
        panel.tree = mock.Mock(); panel.tree.selection.return_value = ('1',)
        def deleted():
            self.assertIsNone(panel.current_marker)
            self.assertTrue(panel._selecting)
            panel.on_tree_select(None)
        marker = mock.Mock(); marker.delete.side_effect = deleted
        panel.current_marker = marker
        with (mock.patch.object(manager._HistoryViewBase, '_clear_selection_detail'),
              mock.patch.object(manager._HistoryViewBase, 'on_tree_select') as selection):
            panel._clear_selection_detail()
        marker.delete.assert_called_once(); selection.assert_not_called()
        self.assertFalse(panel._selecting)


if __name__ == '__main__':
    unittest.main()
