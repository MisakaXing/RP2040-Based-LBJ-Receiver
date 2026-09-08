from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jsondecode as viewer
from pico_history import HistoryDownload, HistoryTransferError


class Widget:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


class ViewerTransferTests(unittest.TestCase):
    def setUp(self):
        self.app = viewer.TrainLogApp.__new__(viewer.TrainLogApp)
        self.app.read_pico_btn = Widget()
        self.app.export_pico_btn = Widget()
        self.app.load_btn = Widget()
        self.app._process_memory_lines = mock.Mock()
        self.app.after = lambda delay, callback, *args: callback(*args)
        self.messages = mock.patch.object(viewer, "messagebox")
        self.messagebox = self.messages.start()
        self.addCleanup(self.messages.stop)

    def test_read_downloads_and_decodes_after_device_restoration(self):
        result = HistoryDownload('{"line":"京沪线"}\n'.encode(), "verified")
        with mock.patch.object(viewer, "download_history", return_value=result) as download:
            self.app._pico_worker("PORT")
        self.assertEqual(download.call_args.args, ("PORT",))
        self.app._process_memory_lines.assert_called_once_with(['{"line":"京沪线"}'], "Pico 设备（已校验）")
        self.assertEqual(self.app.read_pico_btn.options["state"], "normal")

    def test_export_uses_same_verified_download(self):
        result = HistoryDownload(b"exact bytes\n", "verified")
        with mock.patch.object(viewer, "download_history", return_value=result), mock.patch.object(viewer, "save_history_atomic") as save:
            self.app._export_worker("PORT", "chosen.jsonl")
        save.assert_called_once_with("chosen.jsonl", result.data)
        self.app._process_memory_lines.assert_not_called()
        self.messagebox.showinfo.assert_called_once()

    def test_transfer_error_never_loads_partial_data_or_overwrites_export(self):
        for destination in (None, "chosen.jsonl"):
            with self.subTest(destination=destination):
                with mock.patch.object(viewer, "download_history", side_effect=HistoryTransferError("EOF")), mock.patch.object(viewer, "save_history_atomic") as save:
                    self.app._device_history_worker("PORT", destination)
                self.app._process_memory_lines.assert_not_called()
                save.assert_not_called()
                self.assertEqual(self.app.read_pico_btn.options["state"], "normal")
                self.assertEqual(self.app.export_pico_btn.options["state"], "normal")
                self.assertEqual(self.app.load_btn.options["state"], "normal")

    def test_invalid_utf8_is_not_silently_discarded(self):
        with mock.patch.object(viewer, "download_history", return_value=HistoryDownload(b"\xff", "verified")):
            self.app._pico_worker("PORT")
        self.app._process_memory_lines.assert_not_called()
        self.messagebox.showerror.assert_called_once()

    def test_recovery_failure_warns_user(self):
        with mock.patch.object(viewer, "download_history", return_value=HistoryDownload(b"{}\n", "verified", "请手动复位")):
            self.app._pico_worker("PORT")
        self.messagebox.showwarning.assert_called_once_with("设备恢复提示", "请手动复位")

    def test_progress_is_throttled_to_percent_changes(self):
        calls = []
        self.app.read_pico_btn.configure = lambda **kwargs: calls.append(kwargs)

        def download(port, progress, status):
            for i in range(10000):
                progress(i, 9999)
            return HistoryDownload(b"{}\n", "verified")

        with mock.patch.object(viewer, "download_history", side_effect=download):
            self.app._pico_worker("PORT")
        self.assertEqual(sum(item.get("text", "").startswith("传输") for item in calls), 101)

    def test_9999_record_viewer_parser_keeps_all_rows(self):
        self.app.log_data = []
        self.app.source_status = Widget()
        self.app.header_subtitle = Widget()
        self.app.refresh_treeview = mock.Mock()
        lines = ['{"t":"12:34:56","d":{"basic":{"train_no":"57721","speed_kmh":"---"},"extended":{}}}'] * 9999
        self.app._process_memory_lines = viewer.TrainLogApp._process_memory_lines.__get__(self.app)
        self.app._process_memory_lines(lines)
        self.assertEqual(len(self.app.log_data), 9999)
        self.assertEqual(self.app.log_data[-1]["_index"], 9998)
        self.assertEqual(self.app.log_data[-1]["train_no"], "57721")

    def test_window_cannot_close_before_recovery_attempt(self):
        self.app._pico_busy = True
        self.app.destroy = mock.Mock()
        self.app._close_app()
        self.app.destroy.assert_not_called()
        self.messagebox.showwarning.assert_called_once()
        self.app._finish_device_transfer()
        self.app._close_app()
        self.app.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
