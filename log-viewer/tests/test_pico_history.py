import builtins
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pico_history as history


class FakeTransport:
    """Execute the actual on-device snippets against a read-only fake file."""
    def __init__(self, data):
        self.data = data
        self.serial = SimpleNamespace()
        self.entries = []
        self.commands = []
        self.resets = []
        self.closed = False
        self.chunk_calls = 0
        self.fail_chunk = False
        self.corrupt_chunk = False
        self.fail_reset = False
        self.fail_entry = False
        self.truncate = False
        self.grow = False
        self.environ = {"__builtins__": dict(vars(builtins))}
        self.environ["__builtins__"]["open"] = self.open
        self.environ["__builtins__"]["__import__"] = self.import_module

    def open(self, path, mode):
        assert (path, mode) == ("history.jsonl", "rb"), (path, mode)
        return io.BytesIO(self.data[:-1] if self.truncate else self.data)

    def import_module(self, name, *args, **kwargs):
        if name == "os":
            return SimpleNamespace(stat=lambda path: (0,) * 6 + (len(self.data) + (1 if self.grow and self.chunk_calls else 0),))
        return builtins.__import__(name, *args, **kwargs)

    def enter_raw_repl(self, soft_reset=True):
        self.entries.append(soft_reset)
        if self.fail_entry and soft_reset:
            raise RuntimeError("could not enter raw repl")

    def exec_raw(self, command, timeout):
        self.commands.append((command, timeout))
        if "_lv_block =" in command:
            self.chunk_calls += 1
            if self.fail_chunk:
                raise RuntimeError("timeout waiting for first EOF reception")
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                exec(command, self.environ)
        except Exception as exc:
            return b"", str(exc).encode()
        result = output.getvalue().encode()
        if self.corrupt_chunk and "_lv_block =" in command:
            import ast
            block = ast.literal_eval(result.decode())
            result = repr(bytes([block[0] ^ 1]) + block[1:]).encode()
        return result, b""

    def exec_raw_no_follow(self, command):
        self.resets.append(command)
        if self.fail_reset:
            raise RuntimeError("USB disconnected")

    def close(self):
        self.closed = True


class DownloadTests(unittest.TestCase):
    def download(self, device, **kwargs):
        return history.download_history("TEST", transport_factory=lambda port: device, **kwargs)

    def test_9999_records_in_bounded_transactions_and_exact_utf8(self):
        data = b"".join((json.dumps({"t": "12:34:56", "d": {"basic": {"train_no": str(i)}, "extended": {"line_name": "京沪线", "note": "中" * 100}}}, ensure_ascii=False) + "\n").encode() for i in range(9999))
        self.assertGreater(len(data), 3_000_000)
        device = FakeTransport(data)
        progress = []
        result = self.download(device, progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(result.data, data)
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(len(result.data.decode().splitlines()), 9999)
        self.assertEqual(progress[0], (0, len(data)))
        self.assertEqual(progress[-1], (len(data), len(data)))
        self.assertTrue(all(0 < b[0] - a[0] <= history.CHUNK_SIZE for a, b in zip(progress, progress[1:])))
        self.assertTrue(all(timeout == history.COMMAND_TIMEOUT for _, timeout in device.commands))
        self.assertEqual(device.entries, [True])
        self.assertEqual(device.resets, ["import machine; machine.reset()"])
        self.assertTrue(device.closed)

    def test_empty_file(self):
        self.assertEqual(self.download(FakeTransport(b"")).data, b"")

    def test_chinese_codepoint_split_across_blocks(self):
        data = b" " * (history.CHUNK_SIZE - 1) + "中\n".encode()
        self.assertEqual(self.download(FakeTransport(data)).data.decode(), data.decode())

    def test_timeout_attempts_recovery_and_never_returns_partial_data(self):
        device = FakeTransport(b"x" * 9000)
        device.fail_chunk = True
        with self.assertRaisesRegex(history.HistoryTransferError, "数据块"):
            self.download(device)
        self.assertEqual(device.entries, [True, False])
        self.assertEqual(len(device.resets), 1)
        self.assertTrue(device.closed)

    def test_truncated_file_rejected(self):
        device = FakeTransport(b"test\n")
        device.truncate = True
        with self.assertRaisesRegex(history.HistoryTransferError, "长度不符"):
            self.download(device)

    def test_changed_size_rejected(self):
        device = FakeTransport(b"test\n")
        device.grow = True
        with self.assertRaisesRegex(history.HistoryTransferError, "长度发生变化"):
            self.download(device)

    def test_same_size_corruption_rejected_by_hash(self):
        device = FakeTransport(b"test\n")
        device.corrupt_chunk = True
        with self.assertRaisesRegex(history.HistoryTransferError, "SHA-256"):
            self.download(device)

    def test_missing_file_preserves_remote_error(self):
        device = FakeTransport(b"")
        device.open = mock.Mock(side_effect=OSError("ENOENT history.jsonl"))
        device.environ["__builtins__"]["open"] = device.open
        with self.assertRaisesRegex(history.HistoryTransferError, "ENOENT"):
            self.download(device)
        self.assertTrue(device.closed)

    def test_failed_initial_entry_attempts_recovery(self):
        device = FakeTransport(b"")
        device.fail_entry = True
        with self.assertRaisesRegex(history.HistoryTransferError, "raw repl"):
            self.download(device)
        self.assertEqual(device.entries, [True, False])
        self.assertTrue(device.closed)

    def test_reboot_failure_reported_but_verified_data_retained(self):
        device = FakeTransport(b"test\n")
        device.fail_reset = True
        result = self.download(device)
        self.assertEqual(result.data, b"test\n")
        self.assertIn("手动复位", result.recovery_warning)
        self.assertTrue(device.closed)

    def test_transfer_and_recovery_errors_both_reported(self):
        device = FakeTransport(b"test\n")
        device.fail_chunk = device.fail_reset = True
        with self.assertRaisesRegex(history.HistoryTransferError, "(?s)EOF.*手动复位"):
            self.download(device)

    def test_cannot_open_port(self):
        with self.assertRaisesRegex(history.HistoryTransferError, "busy"):
            history.download_history("TEST", transport_factory=mock.Mock(side_effect=OSError("busy")))

    def test_ui_observer_error_does_not_skip_reboot(self):
        device = FakeTransport(b"test\n")
        callback = mock.Mock(side_effect=RuntimeError("GUI unavailable"))
        result = self.download(device, progress=callback, status=callback)
        self.assertEqual(result.data, b"test\n")
        self.assertEqual(len(device.resets), 1)
        self.assertTrue(device.closed)


class ExportTests(unittest.TestCase):
    def test_atomic_export_replaces_only_with_complete_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.jsonl"
            path.write_bytes(b"previous")
            history.save_history_atomic(path, b"new verified history")
            self.assertEqual(path.read_bytes(), b"new verified history")
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_export_error_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.jsonl"
            path.write_bytes(b"previous")
            with mock.patch.object(history.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    history.save_history_atomic(path, b"new")
            self.assertEqual(path.read_bytes(), b"previous")
            self.assertEqual(list(Path(folder).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
