import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from history_store import (
    APPEND_FULL,
    APPEND_INVALID,
    APPEND_NO_SPACE,
    APPEND_OK,
    HistoryStore,
    history_limit_for_filesystem,
    is_valid_history_record,
    make_history_record,
    storage_write_due,
)


def fake_statvfs(total_bytes=14 * 1024 * 1024, free_bytes=None):
    if free_bytes is None:
        free_bytes = total_bytes
    block_size = 4096
    return lambda _path: (
        block_size,
        block_size,
        total_bytes // block_size,
        free_bytes // block_size,
        free_bytes // block_size,
        0,
        0,
        0,
        0,
        255,
    )


def record(number, raw=None):
    data = {
        "type": "train_data_full",
        "rssi": -44,
        "basic": {"train_no": str(number), "speed_kmh": 80, "km_post": 12.3},
        "extended": {"class_tag": "K", "route_hex": "BDF2C9BDCFDF2020"},
    }
    if raw is not None:
        data["raw"] = raw
    return {"t": "2026-08-29 18:00:00", "d": data}


def extended_record(route="BDF2C9BDCFDF2020"):
    return {
        "t": "2026-08-29 18:01:00",
        "d": {
            "type": "extended_only",
            "rssi": -51,
            "ric": "1234002-F1",
            "extended": {
                "class_tag": "K",
                "route_hex": route,
                "loco_type": "前进-1234",
                "cab_end": "31",
                "lon": "117°12.3456' E",
                "lat": "39°01.2345' N",
            },
        },
    }


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(pathlib.Path(self.temp.name) / "history.jsonl")

    def tearDown(self):
        self.temp.cleanup()

    def make_store(self, **kwargs):
        return HistoryStore(
            path=self.path,
            root=self.temp.name,
            statvfs_fn=kwargs.pop("statvfs_fn", fake_statvfs()),
            **kwargs,
        )

    def test_16m_layout_uses_four_digit_ui_limit(self):
        self.assertEqual(history_limit_for_filesystem(14 * 1024 * 1024), 9999)
        self.assertEqual(history_limit_for_filesystem(6 * 1024 * 1024), 5000)
        self.assertEqual(history_limit_for_filesystem(2 * 1024 * 1024), 2500)

    def test_capacity_recovers_after_transient_initial_statvfs_failure(self):
        calls = 0

        def flaky_statvfs(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected statvfs failure")
            return fake_statvfs()(path)

        store = self.make_store(statvfs_fn=flaky_statvfs)
        self.assertEqual(store.max_records, 2500)
        store.scan()
        self.assertEqual(store.max_records, 9999)
        self.assertEqual(store.total_bytes, 14 * 1024 * 1024)

    def test_capacity_recovery_rescans_more_than_fallback_limit(self):
        with open(self.path, "w") as target:
            for number in range(1, 3002):
                target.write(json.dumps(record(number)) + "\n")
        calls = 0

        def twice_flaky_statvfs(path):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise OSError("injected statvfs failure")
            return fake_statvfs()(path)

        store = self.make_store(statvfs_fn=twice_flaky_statvfs)
        self.assertEqual(store.scan(), 3001)
        self.assertEqual(store.max_records, 9999)
        self.assertFalse(store.full)
        self.assertEqual(store.latest()["d"]["basic"]["train_no"], "3001")

    def test_compact_record_keeps_ui_fields_and_drops_raw(self):
        source = record(1234, "X" * 4000)["d"]
        source["basic"]["diagnostic"] = "Y" * 4000
        source["extended"]["loco_raw"] = "Z" * 4000
        compact = make_history_record("2026-08-29 18:00:00", source)
        self.assertIsNotNone(compact)
        self.assertNotIn("raw", compact["d"])
        self.assertNotIn("diagnostic", compact["d"]["basic"])
        self.assertNotIn("loco_raw", compact["d"]["extended"])
        self.assertEqual(compact["d"]["basic"]["train_no"], "1234")
        source["basic"]["train_no"] = "9999"
        self.assertEqual(compact["d"]["basic"]["train_no"], "1234")
        empty_extension = make_history_record("now", {"type": "extended_only"})
        self.assertIsNotNone(empty_extension)
        self.assertTrue(is_valid_history_record(empty_extension))

    def test_numeric_train_with_missing_speed_and_km_is_valid_history(self):
        source = {
            "type": "basic_only",
            "basic": {
                "train_no": "57721",
                "speed_kmh": "---",
                "km_post": "---",
                "partial": True,
            },
        }
        compact = make_history_record("2026-08-30 12:00:00", source)

        self.assertIsNotNone(compact)
        self.assertTrue(is_valid_history_record(compact))
        self.assertEqual(compact["d"]["type"], "basic_only")
        self.assertEqual(compact["d"]["basic"]["train_no"], "57721")
        self.assertEqual(compact["d"]["basic"]["speed_kmh"], "---")
        self.assertEqual(compact["d"]["basic"]["km_post"], "---")

        store = self.make_store(index_stride=1)
        self.assertEqual(store.scan(), 0)
        self.assertEqual(store.append(compact), APPEND_OK)
        restarted = self.make_store(index_stride=1)
        self.assertEqual(restarted.scan(), 1)
        loaded = restarted.latest()
        self.assertEqual(loaded["d"]["basic"]["train_no"], "57721")
        self.assertEqual(loaded["d"]["basic"]["speed_kmh"], "---")
        self.assertEqual(loaded["d"]["basic"]["km_post"], "---")

    def test_extended_only_is_compacted_and_valid_without_train_number(self):
        source = extended_record()["d"]
        source["raw"] = "X" * 4000
        source["extended"]["loco_raw"] = "12345678"
        compact = make_history_record("2026-08-29 18:01:00", source)
        self.assertIsNotNone(compact)
        self.assertTrue(is_valid_history_record(compact))
        self.assertEqual(compact["d"]["type"], "extended_only")
        self.assertEqual(compact["d"]["basic"], {})
        self.assertEqual(compact["d"]["extended"]["route_hex"], "BDF2C9BDCFDF2020")
        self.assertNotIn("raw", compact["d"])
        self.assertNotIn("loco_raw", compact["d"]["extended"])
        diagnostics_only = make_history_record("now", {
            "type": "extended_only", "extended": {"block_start": 12}
        })
        self.assertIsNotNone(diagnostics_only)
        self.assertTrue(is_valid_history_record(diagnostics_only))
        malformed_extension = make_history_record("now", {
            "type": "extended_only", "extended": "bad"
        })
        self.assertIsNotNone(malformed_extension)
        self.assertEqual(malformed_extension["d"]["extended"], {})
        partial = make_history_record("now", {
            "type": "extended_only", "extended": {"route_hex": ""}
        })
        self.assertIsNotNone(partial)
        self.assertTrue(is_valid_history_record(partial))

    def test_placeholder_full_message_is_normalized_to_extended_only(self):
        source = extended_record()["d"]
        source["type"] = "train_data_full"
        source["basic"] = {
            "train_no": "---",
            "speed_kmh": "---",
            "km_post": "---",
            "placeholder": True,
        }
        compact = make_history_record("now", source)
        self.assertIsNotNone(compact)
        self.assertEqual(compact["d"]["type"], "extended_only")
        self.assertEqual(compact["d"]["basic"], {})
        self.assertEqual(
            compact["d"]["extended"]["loco_type"], "前进-1234"
        )

    def test_storage_deadline_prevents_continuous_radio_starvation(self):
        diff = lambda value, reference: value - reference
        args = dict(
            last_write=0,
            ticks_diff=diff,
            quiet_ms=100,
            max_defer_ms=1500,
            hard_defer_ms=5000,
            write_gap_ms=80,
        )
        self.assertFalse(storage_write_due(
            500, 499, 0, raw_pending=0, **args
        ))
        self.assertTrue(storage_write_due(
            1500, 1499, 0, raw_pending=0, **args
        ))
        self.assertFalse(storage_write_due(
            1500, 1499, 0, raw_pending=1, **args
        ))
        self.assertTrue(storage_write_due(
            5000, 4999, 0, raw_pending=1, **args
        ))
        self.assertTrue(storage_write_due(
            500, 300, 400, raw_pending=0, **args
        ))
        self.assertFalse(storage_write_due(
            5050, 5049, 0, last_write=5000, raw_pending=1,
            ticks_diff=diff, quiet_ms=100, max_defer_ms=1500,
            hard_defer_ms=5000, write_gap_ms=80,
        ))

    def test_scan_and_load_include_extended_only_records(self):
        with open(self.path, "w") as target:
            target.write(json.dumps(record(1)) + "\n")
            target.write(json.dumps(extended_record()) + "\n")
            target.write(json.dumps({"t": "bad", "d": {"type": "unknown"}}) + "\n")
            target.write(json.dumps(record(2)) + "\n")
        store = self.make_store(index_stride=2)
        self.assertEqual(store.scan(), 3)
        self.assertEqual(store.invalid_lines, 1)
        extension = store.load(1)
        self.assertEqual(extension["d"]["type"], "extended_only")
        self.assertEqual(extension["d"].get("basic", {}), {})
        self.assertEqual(extension["d"]["extended"]["cab_end"], "31")

    def test_append_and_restart_keep_mixed_extended_only_index_order(self):
        store = self.make_store(index_stride=2)
        self.assertEqual(store.scan(), 0)
        compact_extension = make_history_record(
            "2026-08-29 18:01:00", extended_record()["d"]
        )
        self.assertEqual(store.append(record(1)), APPEND_OK)
        self.assertEqual(store.append(compact_extension), APPEND_OK)
        self.assertEqual(store.append(record(2)), APPEND_OK)

        restarted = self.make_store(index_stride=2)
        self.assertEqual(restarted.scan(), 3)
        self.assertEqual(restarted.load(0)["d"]["basic"]["train_no"], "1")
        self.assertEqual(restarted.load(1)["d"]["type"], "extended_only")
        self.assertEqual(restarted.load(1)["d"]["basic"], {})
        self.assertEqual(restarted.latest()["d"]["basic"]["train_no"], "2")

    def test_sparse_index_loads_first_middle_and_last(self):
        with open(self.path, "w") as target:
            for number in range(1, 66):
                target.write(json.dumps(record(number)) + "\n")
        store = self.make_store(index_stride=16)
        self.assertEqual(store.scan(), 65)
        self.assertEqual(len(store.offsets), 5)
        self.assertEqual(store.load(0)["d"]["basic"]["train_no"], "1")
        self.assertEqual(store.load(31)["d"]["basic"]["train_no"], "32")
        self.assertEqual(store.latest()["d"]["basic"]["train_no"], "65")

    def test_sparse_block_cache_reuses_decoded_records(self):
        class CountingHistoryStore(HistoryStore):
            def __init__(self, *args, **kwargs):
                self.decode_calls = 0
                super().__init__(*args, **kwargs)

            def _decode_line(self, *args):
                self.decode_calls += 1
                return HistoryStore._decode_line(*args)

        with open(self.path, "w") as target:
            for number in range(1, 33):
                target.write(json.dumps(record(number)) + "\n")
        store = CountingHistoryStore(
            path=self.path,
            root=self.temp.name,
            statvfs_fn=fake_statvfs(),
            index_stride=16,
        )
        self.assertEqual(store.scan(), 32)
        store.decode_calls = 0

        self.assertEqual(store.load(15)["d"]["basic"]["train_no"], "16")
        self.assertEqual(store.decode_calls, 16)
        self.assertEqual(store.load(14)["d"]["basic"]["train_no"], "15")
        self.assertEqual(store.load(0)["d"]["basic"]["train_no"], "1")
        self.assertEqual(store.decode_calls, 16)

        self.assertEqual(store.load(16)["d"]["basic"]["train_no"], "17")
        self.assertEqual(store.decode_calls, 32)

    def test_sparse_block_cache_reloads_after_append_and_clear(self):
        with open(self.path, "w") as target:
            for number in range(1, 16):
                target.write(json.dumps(record(number)) + "\n")
        store = self.make_store(index_stride=16)
        self.assertEqual(store.scan(), 15)
        self.assertEqual(store.load(14)["d"]["basic"]["train_no"], "15")
        self.assertEqual(len(store._cache_records), 15)

        self.assertEqual(store.append(record(16)), APPEND_OK)
        self.assertEqual(store.load(15)["d"]["basic"]["train_no"], "16")
        self.assertEqual(len(store._cache_records), 16)

        self.assertTrue(store.clear())
        self.assertEqual(store._cache_checkpoint, -1)
        self.assertEqual(store._cache_records, [])

    def test_decode_memory_error_never_shifts_history_indexes(self):
        with open(self.path, "w") as target:
            for number in range(1, 18):
                target.write(json.dumps(record(number)) + "\n")
        store = self.make_store(index_stride=16)
        self.assertEqual(store.scan(), 17)
        real_loads = json.loads

        with mock.patch(
            "history_store.json.loads", side_effect=MemoryError("injected")
        ):
            self.assertIsNone(store.load(5))

        self.assertEqual(store._cache_checkpoint, -1)
        self.assertEqual(store._cache_records, [])
        self.assertIn("injected", store.last_error)

        calls = 0

        def fail_once(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise MemoryError("injected once")
            return real_loads(value)

        with mock.patch("history_store.json.loads", side_effect=fail_once):
            loaded = store.load(5)
        self.assertEqual(loaded["d"]["basic"]["train_no"], "6")
        self.assertEqual(store._cache_checkpoint, -1)

    def test_scan_memory_error_is_not_counted_as_a_bad_line(self):
        with open(self.path, "w") as target:
            target.write(json.dumps(record(1)) + "\n")
        store = self.make_store(index_stride=16)
        with mock.patch(
            "history_store.json.loads", side_effect=MemoryError("injected")
        ):
            self.assertEqual(store.scan(), 0)
        self.assertFalse(store.index_complete)
        self.assertTrue(store.read_only)
        self.assertEqual(store.invalid_lines, 0)
        self.assertEqual(store.last_error, "INDEX MEMORY")

    def test_cache_rejects_a_block_changed_after_scan(self):
        with open(self.path, "w") as target:
            for number in range(1, 33):
                target.write(json.dumps(record(number)) + "\n")
        store = self.make_store(index_stride=16)
        self.assertEqual(store.scan(), 32)

        with open(self.path, "rb") as source:
            lines = source.readlines()
        lines[5] = b"{" + (b" " * (len(lines[5]) - 2)) + b"\n"
        with open(self.path, "wb") as target:
            target.writelines(lines)

        self.assertIsNone(store.load(10))
        self.assertEqual(store._cache_checkpoint, -1)
        self.assertIn("INDEX MISMATCH", store.last_error)

    def test_clear_preallocation_failure_preserves_file_and_cache(self):
        with open(self.path, "w") as target:
            target.write(json.dumps(record(1)) + "\n")
        store = self.make_store(index_stride=16)
        self.assertEqual(store.scan(), 1)
        self.assertEqual(store.load(0)["d"]["basic"]["train_no"], "1")

        with mock.patch(
            "history_store.array.array", side_effect=MemoryError("injected")
        ):
            self.assertFalse(store.clear())

        self.assertEqual(store.count, 1)
        self.assertEqual(store.load(0)["d"]["basic"]["train_no"], "1")
        with open(self.path, "r") as source:
            self.assertEqual(len(source.readlines()), 1)

    def test_scan_skips_blank_corrupt_and_non_train_records(self):
        with open(self.path, "w") as target:
            target.write(json.dumps(record(1)) + "\n")
            target.write("\n")
            target.write("{broken json}\n")
            target.write(json.dumps({"t": "now", "d": {"type": "unknown"}}) + "\n")
            target.write(json.dumps(record(2)))
        store = self.make_store(index_stride=2)
        self.assertEqual(store.scan(), 2)
        self.assertEqual(store.invalid_lines, 3)
        self.assertTrue(store.tail_needs_separator)
        self.assertEqual(store.latest()["d"]["basic"]["train_no"], "2")

    def test_append_after_partial_tail_cannot_merge_json_objects(self):
        with open(self.path, "w") as target:
            target.write(json.dumps(record(1)) + "\n")
            target.write('{"t":"cut off"')
        store = self.make_store(index_stride=2)
        self.assertEqual(store.scan(), 1)
        self.assertTrue(store.tail_needs_separator)
        self.assertEqual(store.append(record(2)), APPEND_OK)
        self.assertEqual(store.count, 2)
        self.assertEqual(store.load(1)["d"]["basic"]["train_no"], "2")
        with open(self.path, "r") as source:
            self.assertIn('\n{"t":', source.read())

    def test_unicode_record_is_written_and_indexed_by_bytes(self):
        item = record(88)
        item["d"]["extended"]["loco_type"] = "前进-1234"
        store = self.make_store(index_stride=1)
        store.scan()
        self.assertEqual(store.append(item), APPEND_OK)
        expected_size = len(json.dumps(item).encode("utf-8")) + 1
        self.assertEqual(pathlib.Path(self.path).stat().st_size, expected_size)
        self.assertEqual(store.load(0)["d"]["extended"]["loco_type"], "前进-1234")

    def test_truncated_utf8_tail_is_skipped_without_disabling_history(self):
        valid = (json.dumps(record(1)) + "\n").encode("utf-8")
        pathlib.Path(self.path).write_bytes(valid + b'{"loco":"\xe5\x89')
        store = self.make_store(index_stride=1)
        self.assertEqual(store.scan(), 1)
        self.assertTrue(store.index_complete)
        self.assertTrue(store.tail_needs_separator)
        self.assertEqual(store.append(record(2)), APPEND_OK)
        self.assertEqual(store.load(1)["d"]["basic"]["train_no"], "2")

    def test_oversized_line_is_drained_before_next_record(self):
        valid = (json.dumps(record(9)) + "\n").encode("utf-8")
        pathlib.Path(self.path).write_bytes(b"X" * 10000 + b"\n" + valid)
        store = self.make_store(index_stride=1)
        self.assertEqual(store.scan(), 1)
        self.assertEqual(store.invalid_lines, 1)
        self.assertEqual(store.load(0)["d"]["basic"]["train_no"], "9")

    def test_uncertain_flush_failure_disables_further_appends(self):
        class FlushFailure:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def seek(self, *_args):
                return 0

            def tell(self):
                return 0

            def write(self, value):
                return len(value)

            def flush(self):
                raise OSError("injected flush failure")

        store = self.make_store(max_records=10)
        store.scan()
        with mock.patch("builtins.open", return_value=FlushFailure()) as mocked_open:
            self.assertLess(store.append(record(1)), 0)
            self.assertTrue(store.read_only)
            self.assertFalse(store.index_complete)
            self.assertLess(store.append(record(1)), 0)
            self.assertEqual(mocked_open.call_count, 1)

    def test_capacity_and_space_reserve_stop_writes(self):
        store = self.make_store(max_records=1)
        store.scan()
        self.assertEqual(store.append(record(1)), APPEND_OK)
        self.assertEqual(store.append(record(2)), APPEND_FULL)

        low_space = self.make_store(
            max_records=10,
            statvfs_fn=fake_statvfs(14 * 1024 * 1024, 2 * 1024 * 1024),
        )
        low_space.scan()
        self.assertEqual(low_space.append(record(3)), APPEND_NO_SPACE)

    def test_oversized_or_invalid_records_are_rejected(self):
        store = self.make_store(max_records=10, max_record_bytes=120)
        store.scan()
        self.assertEqual(store.append(record(1234)), APPEND_INVALID)
        self.assertEqual(store.append({"t": "now", "d": {}}), APPEND_INVALID)

    def test_clear_replaces_log_and_resets_index(self):
        store = self.make_store(index_stride=4)
        store.scan()
        self.assertEqual(store.append(record(1)), APPEND_OK)
        self.assertTrue(store.clear())
        self.assertEqual(store.count, 0)
        self.assertEqual(len(store.offsets), 0)
        self.assertEqual(pathlib.Path(self.path).read_text(), "")


if __name__ == "__main__":
    unittest.main()
