import ast
import hashlib
import json
import pathlib
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from history_store import (
    APPEND_OK, CHECKPOINT_HEADER_BYTES, HistoryStore,
)
from test_history_store import fake_statvfs, record, extended_record


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "history.jsonl"
        self.index = pathlib.Path(str(self.path) + ".idx")

    def tearDown(self):
        self.temp.cleanup()

    def store(self, **kwargs):
        return HistoryStore(str(self.path), root=self.temp.name,
                            statvfs_fn=fake_statvfs(), **kwargs)

    def seed(self, count=130, **kwargs):
        with self.path.open("wb") as f:
            for number in range(count):
                f.write((json.dumps(record(number)) + "\n").encode())
        store = self.store(**kwargs)
        self.assertEqual(store.scan(), count)
        self.assertEqual(store.scan_mode, "full")
        self.assertTrue(store.save_checkpoint())
        return store

    def test_fast_boot_parses_no_archived_json(self):
        first = self.seed()
        original = self.path.read_bytes()
        restarted = self.store()
        with mock.patch("history_store.json.loads", side_effect=AssertionError("full parse")):
            self.assertEqual(restarted.scan(), first.count)
        self.assertEqual(restarted.scan_mode, "tail")
        self.assertEqual(restarted.scan_lines, 0)
        self.assertEqual(list(restarted.offsets), list(first.offsets))
        self.assertEqual(self.path.read_bytes(), original)
        for index in (0, 15, 16, 63, 129):
            self.assertEqual(restarted.load(index), first.load(index))

    def test_tail_replays_only_new_mixed_records(self):
        first = self.seed()
        for item in (record(130), extended_record(), record(132)):
            self.assertEqual(first.append(item), APPEND_OK)
        restarted = self.store()
        self.assertEqual(restarted.scan(), 133)
        self.assertEqual(restarted.scan_mode, "tail")
        self.assertEqual(restarted.scan_lines, 3)
        self.assertEqual(restarted.load(131)["d"]["type"], "extended_only")
        self.assertEqual(restarted.latest()["d"]["basic"]["train_no"], "132")
        self.assertEqual(list(first.offsets), list(restarted.offsets))

    def test_checkpoint_batches_at_64_not_every_train(self):
        first = self.seed(128)
        for number in range(63):
            self.assertEqual(first.append(record(number)), APPEND_OK)
            self.assertFalse(first.checkpoint_due())
            self.assertFalse(first.save_checkpoint())
        self.assertEqual(first.append(record(63)), APPEND_OK)
        self.assertTrue(first.checkpoint_due())
        self.assertTrue(first.save_checkpoint())
        self.assertEqual(first.checkpoint_writes, 2)
        restarted = self.store()
        self.assertEqual(restarted.scan(), 192)
        self.assertEqual(restarted.scan_lines, 0)

    def test_torn_index_leaves_history_and_falls_back(self):
        self.seed()
        original = self.path.read_bytes()
        self.index.write_bytes(self.index.read_bytes()[:80])
        restarted = self.store()
        self.assertEqual(restarted.scan(), 130)
        self.assertEqual(restarted.scan_mode, "full")
        self.assertEqual(restarted.scan_lines, 130)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(restarted.save_checkpoint())
        self.assertEqual(self.store().scan(), 130)

    def test_checksum_mismatch_and_stale_temp_are_not_trusted(self):
        self.seed()
        saved = self.index.read_bytes()
        pathlib.Path(str(self.index) + ".tmp").write_bytes(saved)
        damaged = bytearray(saved)
        damaged[CHECKPOINT_HEADER_BYTES + 65] ^= 1
        self.index.write_bytes(damaged)
        restarted = self.store()
        self.assertEqual(restarted.scan(), 130)
        self.assertEqual(restarted.scan_mode, "full")

    def test_truncated_history_rejects_old_checkpoint(self):
        self.seed()
        self.path.write_bytes(b"".join(self.path.read_bytes().splitlines(True)[:5]))
        restarted = self.store()
        self.assertEqual(restarted.scan(), 5)
        self.assertEqual(restarted.scan_mode, "full")
        self.assertEqual(restarted.latest()["d"]["basic"]["train_no"], "4")

    def test_same_length_replacement_rejects_anchor(self):
        self.seed()
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b'"train_no": "0"', b'"train_no": "9"', 1))
        self.assertEqual(len(self.path.read_bytes()), len(original))
        restarted = self.store()
        self.assertEqual(restarted.scan(), 130)
        self.assertEqual(restarted.scan_mode, "full")
        self.assertEqual(restarted.load(0)["d"]["basic"]["train_no"], "9")

    def test_policy_change_invalidates_checkpoint(self):
        self.seed()
        for options in ({"index_stride": 8}, {"max_records": 60}):
            restarted = self.store(**options)
            self.assertEqual(restarted.scan(), options.get("max_records", 130))
            self.assertEqual(restarted.scan_mode, "full")
        with mock.patch("history_store.CHECKPOINT_SCHEMA", 999):
            restarted = self.store()
            self.assertEqual(restarted.scan(), 130)
            self.assertEqual(restarted.scan_mode, "full")

    def test_bad_offset_bounds_rejected_even_with_matching_checksum(self):
        self.seed()
        data = bytearray(self.index.read_bytes())
        struct.pack_into("<I", data, CHECKPOINT_HEADER_BYTES + 64, 0xFFFFFFFF)
        data[-32:] = hashlib.sha256(data[:-32]).digest()
        self.index.write_bytes(data)
        restarted = self.store()
        self.assertEqual(restarted.scan(), 130)
        self.assertEqual(restarted.scan_mode, "full")

    def test_torn_tail_is_repaired_by_existing_separator_logic(self):
        self.seed()
        with self.path.open("ab") as f:
            f.write((json.dumps(extended_record()) + "\n").encode())
            f.write(b'{"t":"cut", "d": {"loco":"\xe5\x89')
        restarted = self.store()
        self.assertEqual(restarted.scan(), 131)
        self.assertEqual(restarted.scan_mode, "tail")
        self.assertEqual(restarted.scan_lines, 2)
        self.assertEqual(restarted.invalid_lines, 1)
        self.assertTrue(restarted.tail_needs_separator)
        self.assertFalse(restarted.save_checkpoint(force=True))
        self.assertEqual(restarted.append(record(132)), APPEND_OK)
        self.assertTrue(restarted.save_checkpoint(force=True))
        recovered = self.store()
        self.assertEqual(recovered.scan(), 132)
        self.assertEqual(recovered.invalid_lines, 1)
        self.assertEqual(recovered.latest()["d"]["basic"]["train_no"], "132")

    def test_valid_unterminated_tail_is_not_checkpointed(self):
        self.seed()
        with self.path.open("ab") as f:
            f.write(json.dumps(record(130)).encode())
        restarted = self.store()
        self.assertEqual(restarted.scan(), 131)
        self.assertTrue(restarted.tail_needs_separator)
        self.assertFalse(restarted.save_checkpoint(force=True))
        self.assertEqual(restarted.append(record(131)), APPEND_OK)
        self.assertEqual(self.store().scan(), 132)

    def test_oversize_and_invalid_tail_keep_valid_record_numbers(self):
        self.seed()
        with self.path.open("ab") as f:
            f.write(b"x" * 5000 + b"\n\n{}\n")
            f.write((json.dumps(record(130)) + "\n").encode())
        restarted = self.store()
        self.assertEqual(restarted.scan(), 131)
        self.assertEqual(restarted.scan_lines, 4)
        self.assertEqual(restarted.invalid_lines, 3)
        self.assertEqual(restarted.latest()["d"]["basic"]["train_no"], "130")

    def test_failed_cache_commit_retains_old_cache_and_recording(self):
        first = self.seed()
        old_cache = self.index.read_bytes()
        first.append(record(130))
        with mock.patch("history_store.os.rename", side_effect=OSError("power loss")):
            self.assertFalse(first.save_checkpoint(force=True))
        self.assertEqual(self.index.read_bytes(), old_cache)
        self.assertFalse(first.read_only)
        self.assertTrue(first.index_complete)
        self.assertEqual(first.append(record(131)), APPEND_OK)
        restarted = self.store()
        self.assertEqual(restarted.scan(), 132)
        self.assertEqual(restarted.scan_lines, 2)

    def test_low_space_skips_optional_index_without_disabling_log(self):
        first = self.seed()
        first._statvfs = fake_statvfs(free_bytes=first.reserve_bytes)
        self.assertFalse(first.save_checkpoint(force=True))
        self.assertEqual(first.checkpoint_error, "INDEX SPACE")
        self.assertFalse(first.read_only)

    def test_index_memory_error_falls_back_to_full_scan(self):
        self.seed()
        real_unpack = struct.unpack
        calls = 0
        def fail_once(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise MemoryError("injected")
            return real_unpack(*args)
        restarted = self.store()
        with mock.patch("history_store.struct.unpack", side_effect=fail_once):
            self.assertEqual(restarted.scan(), 130)
        self.assertEqual(restarted.scan_mode, "full")
        self.assertTrue(restarted.index_complete)

    def test_tail_memory_error_never_silently_skips_record(self):
        first = self.seed()
        first.append(record(130))
        restarted = self.store()
        with mock.patch("history_store.json.loads", side_effect=MemoryError("injected")):
            self.assertEqual(restarted.scan(), 130)
        self.assertFalse(restarted.index_complete)
        self.assertTrue(restarted.read_only)
        self.assertFalse(restarted.save_checkpoint(force=True))

    def test_clear_invalidates_old_checkpoint_before_new_history(self):
        first = self.seed()
        self.assertTrue(first.clear())
        self.assertFalse(self.index.exists())
        first.append(record(999))
        restarted = self.store()
        self.assertEqual(restarted.scan(), 1)
        self.assertEqual(restarted.latest()["d"]["basic"]["train_no"], "999")

    def test_failed_clear_never_leaves_valid_stale_index(self):
        first = self.seed()
        with mock.patch("history_store.os.rename", side_effect=OSError("injected")):
            self.assertFalse(first.clear())
        self.assertFalse(self.index.exists())
        self.assertEqual(self.store().scan(), 130)

    def test_empty_full_and_force_rebuild(self):
        first = self.seed(0, max_records=3)
        restarted = self.store(max_records=3)
        self.assertEqual(restarted.scan(), 0)
        self.assertEqual(restarted.scan_mode, "tail")
        for i in range(3):
            restarted.append(record(i))
        self.assertTrue(restarted.save_checkpoint(force=True))
        full = self.store(max_records=3)
        self.assertEqual(full.scan(), 3)
        self.assertTrue(full.full)
        self.assertEqual(full.scan_lines, 0)
        self.assertEqual(full.scan(use_checkpoint=False), 3)
        self.assertEqual(full.scan_mode, "full")
        self.assertEqual(full.scan_lines, 3)

    def test_9999_checkpoint_size_and_correct_random_reads(self):
        first = self.seed(9999)
        self.assertEqual(self.index.stat().st_size, 40 + 64 + 625 * 4 + 32)
        restarted = self.store()
        self.assertEqual(restarted.scan(), 9999)
        self.assertEqual(restarted.scan_lines, 0)
        for index in (0, 16, 127, 2048, 7999, 9998):
            self.assertEqual(restarted.load(index), first.load(index))


class CheckpointSchedulerTests(unittest.TestCase):
    def namespace(self):
        tree = ast.parse((pathlib.Path(__file__).parents[1] / "main.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "service_history_checkpoint")
        ns = dict(time=SimpleNamespace(ticks_ms=lambda: 1000, ticks_diff=lambda a, b: a-b),
                  system_state="DASHBOARD", ui_queue=[], history_queue=[], sd_log_queue=[],
                  receiver=SimpleNamespace(raw_queue=[], last_word_time=0),
                  history_store=SimpleNamespace(checkpoint_due=lambda: True,
                                               save_checkpoint=mock.Mock(return_value=True)),
                  last_history_checkpoint_attempt=None, last_storage_write=0,
                  HISTORY_RADIO_QUIET_MS=100, STORAGE_WRITE_GAP_MS=80)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), ns)
        return ns

    def test_cache_write_waits_for_quiet_and_empty_queues(self):
        for change in (dict(system_state="HISTORY"), dict(system_state="MENU"),
                       dict(ui_queue=[1]), dict(history_queue=[1]), dict(sd_log_queue=[1]),
                       dict(receiver=SimpleNamespace(raw_queue=[1], last_word_time=0)),
                       dict(receiver=SimpleNamespace(raw_queue=[], last_word_time=999))):
            ns = self.namespace()
            ns.update(change)
            ns["service_history_checkpoint"](1000)
            ns["history_store"].save_checkpoint.assert_not_called()

    def test_retry_rate_is_bounded(self):
        ns = self.namespace()
        ns["service_history_checkpoint"](1000)
        ns["service_history_checkpoint"](2000)
        self.assertEqual(ns["history_store"].save_checkpoint.call_count, 1)
        ns["service_history_checkpoint"](31000)
        self.assertEqual(ns["history_store"].save_checkpoint.call_count, 2)


if __name__ == "__main__":
    unittest.main()
