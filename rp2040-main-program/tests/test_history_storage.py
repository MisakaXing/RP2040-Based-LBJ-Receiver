import array
import ast
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ('history_is_full', 'block_history_saving', 'check_history_space',
             'init_history', 'save_history', 'queue_history', 'service_history_storage',
             'queue_sd_log', 'load_history_entry', 'draw_battery_top_status', 'process_ui_data')


class Queue:
    def __init__(self):
        self.items = []

    def __len__(self):
        return len(self.items)

    def put(self, value):
        self.items.append(value)

    def get(self):
        return self.items.pop(0) if self.items else None

    def clear(self):
        self.items.clear()


class HistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'history.jsonl'
        self.free_blocks = 128
        self.statvfs = Mock(side_effect=lambda _: (4096, 4096, 352,
                                                  self.free_blocks, self.free_blocks))
        self.now = 1000
        self.screen = SimpleNamespace(fill_rect=Mock(), draw_gbk=Mock())
        self.ns = dict(
            os=SimpleNamespace(statvfs=self.statvfs), json=json, array=array,
            MAX_HIST=2000, HISTORY_RESERVE_BYTES=16*1024,
            total_count=0, history_offsets=array.array('I'),
            history_block_reason=None, history_free_bytes=None,
            history_queue=Queue(), sd_log_queue=Queue(), HISTORY_QUEUE_CAPACITY=24,
            HIST_FILE=str(self.path), rtc=SimpleNamespace(
                get_history_time_str=lambda: '2026-10-05 00:30', sync_time=Mock()),
            time=SimpleNamespace(ticks_ms=lambda: self.now, ticks_diff=lambda a,b:a-b),
            receiver=SimpleNamespace(input_is_buffered=lambda:True, input_pending=lambda:0,
                                     raw_queue=[], last_word_time=0),
            HISTORY_RADIO_QUIET_MS=100, STORAGE_WRITE_GAP_MS=80, last_storage_write=0,
            sd_active=True, log_to_sd=Mock(), print=Mock(),
            tft=self.screen, RED=1, GREEN=2, YELLOW=3,
            protection=SimpleNamespace(top_warning=lambda now:None),
            current_status=b'READY', current_status_color=2, last_top_status=None,
            system_state='DASHBOARD', last_basic={}, last_ext={}, last_is_full=True,
            has_received=False, last_rssi_str='N/A', screen_is_on=True,
            beep=Mock(), wake_screen_for_train=Mock(), service_buzzer=Mock(),
            update_top_bar=Mock(), display_train_data=Mock(), draw_hardware_bar=Mock(),
            last_interaction=0, need_post_train_gc=False,
        )
        tree = ast.parse((ROOT / 'main.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS]
        self.assertEqual(len(nodes), len(FUNCTIONS))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'main.py', 'exec'), self.ns)
        self.data = {'type': 'basic_only', 'basic': {'train_no': '57082'}, 'rssi': '-89.0dBm'}

    def call(self, name, *args):
        return self.ns[name](*args)

    def test_constants_and_about_use_same_cap(self):
        source = (ROOT / 'main.py').read_text()
        self.assertIn('MAX_HIST = 2000', source)
        self.assertIn('HISTORY_RESERVE_BYTES = 16 * 1024', source)
        self.assertIn('Records: {total_count}/{MAX_HIST}', source)
        self.assertNotIn('/2500', source)

    def test_normal_write_and_unicode_byte_offsets(self):
        data = dict(self.data, extended={'loco_type':'轨道探伤车-04782'})
        self.assertTrue(self.call('save_history', data))
        first_size = self.path.stat().st_size
        self.assertTrue(self.call('save_history', self.data))
        self.assertEqual(list(self.ns['history_offsets']), [0, first_size])
        self.assertEqual(self.call('load_history_entry', 0)['d'], data)
        self.assertEqual(self.ns['total_count'], 2)
        self.assertEqual(self.statvfs.call_count, 2)

    def test_limit_stops_writes_and_stat_calls(self):
        self.ns['total_count'] = 2000
        self.assertFalse(self.call('save_history', self.data))
        self.assertFalse(self.call('queue_history', self.data))
        self.assertEqual(self.ns['history_block_reason'], 'limit')
        self.statvfs.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_final_record_writes_and_latches_limit(self):
        self.ns['total_count'] = 1999
        self.assertTrue(self.call('save_history', self.data))
        self.assertEqual(self.ns['total_count'], 2000)
        self.assertEqual(self.ns['history_block_reason'], 'limit')
        contents = self.path.read_bytes()
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.path.read_bytes(), contents)

    def test_queued_slots_count_toward_limit_without_discarding_valid_queue(self):
        self.ns['total_count'] = 1999
        self.assertTrue(self.call('queue_history', self.data))
        self.assertTrue(self.call('history_is_full'))
        self.assertFalse(self.call('queue_history', self.data))
        self.assertEqual(len(self.ns['history_queue']), 1)
        self.call('service_history_storage', 1000)
        self.assertEqual(self.ns['total_count'], 2000)

    def test_queue_never_calls_statvfs(self):
        for _ in range(24):
            self.assertTrue(self.call('queue_history', self.data))
        self.assertFalse(self.call('queue_history', self.data))
        self.statvfs.assert_not_called()

    def test_low_space_blocks_before_creating_file(self):
        self.free_blocks = 4
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.ns['history_block_reason'], 'space')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.ns['total_count'], 0)
        self.assertEqual(len(self.ns['history_offsets']), 0)

    def test_exact_allocation_margin_boundary(self):
        self.free_blocks = 7  # 4 reserve + 1 payload + 2 COW scratch blocks.
        self.assertTrue(self.call('save_history', self.data))
        self.free_blocks = 6
        before = self.path.read_bytes()
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.path.read_bytes(), before)

    def test_large_payload_requires_more_blocks(self):
        self.free_blocks = 7
        self.assertFalse(self.call('save_history', dict(self.data, raw='7'*5000)))
        self.assertFalse(self.path.exists())

    def test_space_guard_uses_available_blocks_and_fragment_size(self):
        self.statvfs.side_effect = None
        self.statvfs.return_value = (1024, 4096, 352, 100, 3)
        self.assertFalse(self.call('check_history_space'))
        self.assertEqual(self.ns['history_free_bytes'], 12288)

    def test_zero_fragment_size_falls_back_to_block_size(self):
        self.statvfs.side_effect = None
        self.statvfs.return_value = (4096, 0, 352, 100, 100)
        self.assertTrue(self.call('check_history_space'))
        self.assertEqual(self.ns['history_free_bytes'], 409600)

    def test_stat_error_latches_without_repeated_checks(self):
        self.statvfs.side_effect = OSError(5, 'I/O error')
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.ns['history_block_reason'], 'space_check')
        for _ in range(10):
            self.assertFalse(self.call('queue_history', self.data))
            self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.statvfs.call_count, 1)

    def test_no_space_rejection_clears_pending_once_and_does_not_retry(self):
        for _ in range(4):
            self.call('queue_history', self.data)
        self.free_blocks = 1
        self.call('service_history_storage', 1000)
        self.assertEqual(len(self.ns['history_queue']), 0)
        self.assertEqual(self.ns['history_block_reason'], 'space')
        for now in range(1100, 1600, 100):
            self.call('service_history_storage', now)
        self.assertEqual(self.statvfs.call_count, 1)

    def test_enospc_latches_and_does_not_publish_index(self):
        opener = Mock(side_effect=OSError(28, 'No space left on device'))
        self.ns['open'] = opener
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.ns['history_block_reason'], 'write_error')
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(self.ns['total_count'], 0)
        self.assertEqual(len(self.ns['history_offsets']), 0)

    def test_short_write_never_publishes_index(self):
        stream = SimpleNamespace(seek=Mock(), tell=lambda:0, write=lambda payload:len(payload)-1)
        class File:
            def __enter__(self): return stream
            def __exit__(self, *args): return False
        self.ns['open'] = lambda *args: File()
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.ns['history_block_reason'], 'write_error')
        self.assertEqual(self.ns['total_count'], 0)
        self.assertEqual(len(self.ns['history_offsets']), 0)

    def test_close_flush_error_never_publishes_index(self):
        stream = SimpleNamespace(seek=Mock(), tell=lambda:0, write=lambda payload:len(payload))
        class File:
            def __enter__(self): return stream
            def __exit__(self, *args): raise OSError(28)
        self.ns['open'] = lambda *args: File()
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.ns['total_count'], 0)
        self.assertEqual(len(self.ns['history_offsets']), 0)

    def test_startup_detects_low_space_below_count_limit(self):
        self.path.write_text(json.dumps({'t':'2026-10-05 00:30','d':self.data})+'\n')
        self.free_blocks = 1
        self.call('init_history')
        self.assertEqual(self.ns['total_count'], 1)
        self.assertEqual(self.ns['history_block_reason'], 'space')

    def test_startup_preserves_legacy_records_above_new_cap(self):
        line = json.dumps({'t':'2026-10-04 20:01','d':self.data})+'\n'
        self.path.write_text(line*2006)
        before = self.path.read_bytes()
        self.call('init_history')
        self.assertEqual(self.ns['total_count'], 2006)
        self.assertEqual(self.ns['history_block_reason'], 'limit')
        self.assertIsNotNone(self.call('load_history_entry', 2005))
        self.assertEqual(self.path.read_bytes(), before)

    def test_incomplete_tail_blocks_new_appends_without_deleting_records(self):
        good = json.dumps({'t':'2026-10-05 00:30','d':self.data})+'\n'
        self.path.write_text(good+'{"t":')
        before = self.path.read_bytes()
        self.call('init_history')
        self.assertEqual(self.ns['total_count'], 1)
        self.assertEqual(self.ns['history_block_reason'], 'incomplete_tail')
        self.assertFalse(self.call('save_history', self.data))
        self.assertEqual(self.path.read_bytes(), before)

    def test_format_reset_rechecks_space_and_reenables_saving(self):
        self.call('block_history_saving', 'space')
        self.path.write_bytes(b'')
        self.call('init_history')
        self.assertIsNone(self.ns['history_block_reason'])
        self.assertTrue(self.call('save_history', self.data))
        source = (ROOT/'main.py').read_text()
        self.assertIn("open(HIST_FILE, 'w').close()\n            init_history()", source)

    def test_missing_history_is_normal_but_read_error_blocks_saving(self):
        self.call('init_history')
        self.assertEqual(self.ns['total_count'], 0)
        self.assertIsNone(self.ns['history_block_reason'])
        self.ns['open'] = Mock(side_effect=OSError(5))
        self.call('init_history')
        self.assertEqual(self.ns['history_block_reason'], 'read_error')

    def test_storage_waits_for_radio_backlog_and_quiet_gap(self):
        self.call('queue_history', self.data)
        self.ns['receiver'].input_pending = lambda:2
        self.call('service_history_storage', 1000)
        self.statvfs.assert_not_called()
        self.ns['receiver'].input_pending = lambda:0
        self.ns['receiver'].raw_queue = [1]
        self.call('service_history_storage', 1000)
        self.statvfs.assert_not_called()
        self.ns['receiver'].raw_queue = []
        self.ns['receiver'].input_is_buffered = lambda:False
        self.ns['receiver'].last_word_time = 950
        self.call('service_history_storage', 1000)
        self.statvfs.assert_not_called()
        self.call('service_history_storage', 1100)
        self.assertEqual(self.ns['total_count'], 1)

    def test_storage_write_rate_limit_remains_active(self):
        self.call('queue_history', self.data)
        self.ns['last_storage_write'] = 950
        self.call('service_history_storage', 1000)
        self.statvfs.assert_not_called()
        self.call('service_history_storage', 1030)
        self.assertEqual(self.ns['total_count'], 1)

    def test_sd_logging_still_runs_when_flash_saving_disabled(self):
        self.call('block_history_saving', 'space')
        self.call('queue_sd_log', self.data)
        self.call('service_history_storage', 1000)
        self.ns['log_to_sd'].assert_called_once_with(self.data)

    def test_full_status_is_persistent_and_does_not_redraw_repeatedly(self):
        self.call('block_history_saving', 'space')
        self.call('draw_battery_top_status')
        self.call('draw_battery_top_status')
        self.assertEqual(self.screen.draw_gbk.call_count, 1)
        self.assertEqual(self.screen.draw_gbk.call_args.args[:4], (b'MEM FULL',230,4,1))
        self.ns['current_status'] = b'TIME SYNC'
        self.call('draw_battery_top_status')
        self.assertEqual(self.screen.draw_gbk.call_count, 1)

    def test_battery_and_temperature_warnings_keep_priority(self):
        self.call('block_history_saving', 'space')
        self.ns['protection'].top_warning = lambda now:b'LOW BAT'
        self.call('draw_battery_top_status')
        self.assertEqual(self.screen.draw_gbk.call_args.args[0], b'LOW BAT')

    def test_io_failure_status_is_not_misreported_as_full(self):
        self.call('block_history_saving', 'write_error')
        self.call('draw_battery_top_status')
        self.assertEqual(self.screen.draw_gbk.call_args.args[0], b'SAVE ERR')

    def test_realtime_ui_still_updates_when_history_is_blocked(self):
        self.call('block_history_saving', 'space')
        for msg_type in ('basic_only', 'extended_only', 'train_data_merged'):
            data = dict(self.data, type=msg_type,
                        extended={'loco_type':'轨道探伤车-04782','lon':'116 E','lat':'39 N'})
            self.call('process_ui_data', data)
            self.assertTrue(self.ns['has_received'])
            self.assertEqual(self.ns['last_basic'], self.data['basic'])
            self.assertEqual(self.ns['last_ext'], data['extended'])
            self.assertEqual(self.ns['last_rssi_str'], '-89.0dBm')
            self.assertEqual(len(self.ns['history_queue']), 0)
            self.assertEqual(self.ns['current_status'], b'MEM FULL')
        self.assertEqual(self.ns['display_train_data'].call_count, 3)
        self.assertEqual(self.ns['beep'].call_count, 3)
        self.assertEqual(len(self.ns['sd_log_queue']), 3)
        self.statvfs.assert_not_called()


if __name__ == '__main__':
    unittest.main()
