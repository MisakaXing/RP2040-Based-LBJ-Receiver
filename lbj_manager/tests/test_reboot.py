"""Windows reset/disconnect regression cases; no physical device access."""
import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lbj_manager as manager


def pico(name='COM3', serial='SN1', pid=5, location='USB1'):
    return SimpleNamespace(device=name, vid=0x2E8A, pid=pid, product='Pico',
                           serial_number=serial, location=location)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class RebootTransportTests(unittest.TestCase):
    def run_reset(self, frames=None, failure=None, close_failure=None):
        clock = Clock()
        transport = SimpleNamespace(serial=SimpleNamespace(), use_raw_paste=True,
            enter_raw_repl=mock.Mock(side_effect=failure),
            exec_raw_no_follow=mock.Mock(), close=mock.Mock(side_effect=close_failure),
            follow=mock.Mock(side_effect=AssertionError('Must not follow reset output')),
            exit_raw_repl=mock.Mock(side_effect=AssertionError('Must not touch old handle after reset')))
        frames = list(frames or [[pico()], [pico()], [], [pico()]])
        last = frames[-1]
        def enumerate_ports():
            value = frames.pop(0) if frames else last
            if isinstance(value, Exception):
                raise value
            return value
        factory = mock.Mock(return_value=transport)
        result = manager.reboot_receiver('COM3', timeout=.5, transport_factory=factory,
            enumerate_ports=enumerate_ports, clock=clock, sleep=clock.sleep)
        return result, transport, factory

    def test_reset_closes_handle_and_never_follows_or_exits_raw_repl(self):
        result, transport, _ = self.run_reset()
        self.assertTrue(result.sent)
        self.assertTrue(result.reconnected)
        self.assertEqual(result.port, 'COM3')
        transport.enter_raw_repl.assert_called_once_with(soft_reset=False, timeout_overall=5)
        self.assertIn('time.sleep_ms(150); machine.reset()', transport.exec_raw_no_follow.call_args.args[0])
        self.assertFalse(transport.use_raw_paste)
        self.assertEqual(transport.serial.timeout, 2)
        self.assertEqual(transport.serial.write_timeout, 3)
        transport.close.assert_called_once()
        transport.follow.assert_not_called()
        transport.exit_raw_repl.assert_not_called()

    def test_windows_clearcommerror_before_send_is_not_success(self):
        result, transport, _ = self.run_reset(failure=manager.serial.SerialException(
            "ClearCommError failed (PermissionError(13, '设备不识别此命令。', None, 22))"))
        self.assertFalse(result.sent)
        self.assertFalse(result.reconnected)
        self.assertIn('ClearCommError', result.message)
        transport.exec_raw_no_follow.assert_not_called()
        transport.close.assert_called_once()

    def test_send_error_closes_and_reports_uncertain(self):
        transport = SimpleNamespace(serial=SimpleNamespace(), enter_raw_repl=mock.Mock(),
            exec_raw_no_follow=mock.Mock(side_effect=OSError('lost ACK')), close=mock.Mock())
        result = manager.reboot_receiver('COM3', transport_factory=lambda port: transport,
                                        enumerate_ports=lambda: [pico()])
        self.assertFalse(result.sent)
        self.assertIn('lost ACK', result.message)
        transport.close.assert_called_once()

    def test_disconnect_without_return_warns(self):
        result, _, _ = self.run_reset([[pico()], [], []])
        self.assertTrue(result.sent)
        self.assertFalse(result.reconnected)
        self.assertIn('未在限定时间内', result.message)

    def test_port_never_disappears_is_not_confirmed(self):
        result, _, _ = self.run_reset([[pico()], [pico()]])
        self.assertTrue(result.sent)
        self.assertFalse(result.reconnected)
        self.assertIn('未观察到 USB 断开重连', result.message)

    def test_same_serial_can_return_as_new_com_port(self):
        result, _, _ = self.run_reset([[pico()], [], [pico('COM8')]])
        self.assertTrue(result.reconnected)
        self.assertEqual(result.port, 'COM8')

    def test_other_receiver_on_same_port_cannot_confirm(self):
        result, _, _ = self.run_reset([[pico()], [], [pico(serial='OTHER')]])
        self.assertFalse(result.reconnected)

    def test_anonymous_device_on_different_port_cannot_confirm(self):
        result, _, _ = self.run_reset([[pico(serial=None)], [], [pico('COM8', serial=None)]])
        self.assertFalse(result.reconnected)

    def test_anonymous_device_on_same_port_and_location_can_confirm(self):
        result, _, _ = self.run_reset([[pico(serial=None)], [], [pico(serial=None)]])
        self.assertTrue(result.reconnected)

    def test_anonymous_wrong_usb_location_cannot_confirm(self):
        result, _, _ = self.run_reset([[pico(serial=None)], [], [pico(serial=None, location='USB2')]])
        self.assertFalse(result.reconnected)

    def test_different_vid_pid_cannot_confirm(self):
        result, _, _ = self.run_reset([[pico()], [], [pico(pid=99)]])
        self.assertFalse(result.reconnected)

    def test_duplicate_serial_numbers_are_ambiguous(self):
        result, _, _ = self.run_reset([[pico()], [], [pico(), pico('COM8')]])
        self.assertFalse(result.reconnected)

    def test_missing_original_device_does_not_open_serial(self):
        result, _, factory = self.run_reset([[], []])
        self.assertFalse(result.sent)
        factory.assert_not_called()

    def test_enumeration_error_before_send_does_not_open_serial(self):
        result, _, factory = self.run_reset([OSError('enumeration failed')])
        self.assertFalse(result.sent)
        self.assertIn('enumeration failed', result.message)
        factory.assert_not_called()

    def test_enumeration_error_after_send_is_not_success(self):
        result, transport, _ = self.run_reset([[pico()], OSError('enumeration failed')])
        self.assertTrue(result.sent)
        self.assertFalse(result.reconnected)
        transport.close.assert_called_once()

    def test_serial_open_failure_is_reported(self):
        factory = mock.Mock(side_effect=PermissionError('port busy'))
        result = manager.reboot_receiver('COM3', transport_factory=factory,
                                        enumerate_ports=lambda: [pico()])
        self.assertFalse(result.sent)
        self.assertIn('port busy', result.message)

    def test_close_failure_is_not_hidden_by_reconnection(self):
        result, _, _ = self.run_reset(close_failure=OSError('close failed'))
        self.assertTrue(result.sent)
        self.assertFalse(result.reconnected)
        self.assertIn('释放旧串口失败', result.message)


class FlashCompletionTests(unittest.TestCase):
    def root(self, result):
        root = SimpleNamespace(run_mpremote=mock.Mock(return_value=result), after=mock.Mock(),
            log=mock.Mock(), set_progress=mock.Mock(), set_ui_state=mock.Mock(),
            _wipe_device_files=mock.Mock(return_value=True),
            _copy_firmware_files=mock.Mock(return_value=True), _cleanup_temp_dir=mock.Mock())
        root._complete_flash = manager.PicoUpdaterApp._complete_flash.__get__(root)
        return root

    def test_online_and_zip_show_warning_not_false_success(self):
        for method, args in (('_online_flash_worker', ('COM3', False, None, [], {})),
                             ('_offline_zip_flash_worker', ('COM3', None, [], {}))):
            root = self.root((False, 'USB did not return'))
            getattr(manager.PicoUpdaterApp, method)(root, *args)
            callbacks = [call.args[1] for call in root.after.call_args_list]
            self.assertIn(manager.messagebox.showwarning, callbacks)
            self.assertNotIn(manager.messagebox.showinfo, callbacks)
            self.assertIn('重启待确认', str(root.after.call_args_list))
            self.assertIn('USB did not return', root._reset_warning)
            self.assertTrue(root._reset_notice_posted)
            root.run_mpremote.assert_called_once()

    def test_online_and_zip_success_only_claims_usb_reconnected(self):
        for method, args in (('_online_flash_worker', ('COM3', False, None, [], {})),
                             ('_offline_zip_flash_worker', ('COM3', None, [], {}))):
            root = self.root((True, 'same device COM3'))
            getattr(manager.PicoUpdaterApp, method)(root, *args)
            callbacks = [call.args[1] for call in root.after.call_args_list]
            self.assertIn(manager.messagebox.showinfo, callbacks)
            self.assertNotIn(manager.messagebox.showwarning, callbacks)
            self.assertEqual(root._reset_warning, '')
            messages = str(root.log.call_args_list)
            self.assertIn('请确认接收器屏幕', messages)
            self.assertNotIn('已加载所选分支程序', messages)

    def test_write_failure_does_not_reset_or_claim_completed(self):
        root = self.root((True, 'unused'))
        root._copy_firmware_files.return_value = False
        manager.PicoUpdaterApp._offline_zip_flash_worker(root, 'COM3', None, [], {})
        root.run_mpremote.assert_not_called()


class IntegrationResetTests(unittest.TestCase):
    def root(self):
        root = SimpleNamespace(tasks=manager.DeviceTasks(), _task_context=threading.local(),
            _device_touched=False, _reset_done=False, _reset_attempted=False,
            _reset_notice_posted=False, _reset_warning='', _finishing=False, _flash_partial=False,
            notice=mock.Mock(), dispatch=manager.UiDispatch(), sync_controls=mock.Mock(), refresh_ports=mock.Mock())
        root._updater_lease = root.tasks.acquire('更新', 'COM3')
        root.run_mpremote = manager.LBJManager.run_mpremote.__get__(root)
        root._release_updater = manager.LBJManager._release_updater.__get__(root)
        return root

    def test_reset_does_not_spawn_mpremote_child_on_either_platform(self):
        for platform in ('win32', 'darwin'):
            root = self.root()
            with (mock.patch.object(manager.sys, 'platform', platform),
                  mock.patch.object(manager, 'reboot_receiver', return_value=manager.RebootResult(True, True, 'COM3', 'reconnected')) as reboot,
                  mock.patch.object(manager, 'run_command') as child):
                success, _ = root.run_mpremote('COM3', ['exec', 'import machine; machine.reset()'], 10)
            self.assertTrue(success)
            self.assertTrue(root._reset_done)
            self.assertTrue(root._reset_attempted)
            reboot.assert_called_once_with('COM3', timeout=10)
            child.assert_not_called()

    def test_sent_but_unconfirmed_is_not_marked_reset_done(self):
        root = self.root()
        with mock.patch.object(manager, 'reboot_receiver', return_value=manager.RebootResult(True, False, message='timeout')):
            success, output = root.run_mpremote('COM3', ['exec', 'machine.reset()'])
        self.assertFalse(success)
        self.assertFalse(root._reset_done)
        self.assertTrue(root._reset_attempted)
        self.assertEqual(output, 'timeout')

    def test_unexpected_reboot_error_is_visible_not_success(self):
        root = self.root()
        with mock.patch.object(manager, 'reboot_receiver', side_effect=RuntimeError('unexpected')):
            success, output = root.run_mpremote('COM3', ['exec', 'machine.reset()'])
        self.assertFalse(success)
        self.assertIn('unexpected', output)

    def test_ordinary_serial_errors_still_fail(self):
        root = self.root()
        with mock.patch.object(manager, 'run_command', return_value=(False, 'ClearCommError')):
            success, output = root.run_mpremote('COM3', ['fs', 'cp', 'file.py', ':file.py'])
        self.assertFalse(success)
        self.assertEqual(output, 'ClearCommError')
        self.assertFalse(root._reset_attempted)

    def test_wrong_lease_cannot_reboot(self):
        root = self.root()
        with mock.patch.object(manager, 'reboot_receiver') as reboot:
            success, _ = root.run_mpremote('COM8', ['exec', 'machine.reset()'])
        self.assertFalse(success)
        reboot.assert_not_called()

    def test_unconfirmed_reset_is_not_retried_on_finish(self):
        root = self.root(); root._device_touched = root._reset_attempted = True
        root._reset_warning = 'reset uncertain'
        with (mock.patch.object(manager, 'reboot_receiver') as reboot,
              mock.patch.object(manager.messagebox, 'showwarning') as warning):
            manager.LBJManager._finish_updater(root, root._updater_lease)
        reboot.assert_not_called()
        self.assertIsNone(root.tasks.active)
        warning.assert_called_once()
        self.assertEqual(root.notice.call_args.args[0], 'reset uncertain')

    def test_completion_warning_is_not_duplicated_on_release(self):
        root = self.root(); root._device_touched = root._reset_attempted = root._reset_notice_posted = True
        root._reset_warning = 'reset uncertain'
        with mock.patch.object(manager.messagebox, 'showwarning') as warning:
            manager.LBJManager._finish_updater(root, root._updater_lease)
        warning.assert_not_called()
        self.assertEqual(root.notice.call_args.args[0], 'reset uncertain')


if __name__ == '__main__':
    unittest.main()
