import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class Fifo:
    def __init__(self, decoded_words):
        self.words = [word ^ 0xFFFFFFFF for word in decoded_words]

    def rx_fifo(self):
        return len(self.words)

    def get(self):
        return self.words.pop(0)


class ReceiverTickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'lbj_receiver.py'
        tree = ast.parse(source.read_text())
        receiver = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                        and node.name == 'LBJReceiver')
        tick = next(node for node in receiver.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'tick')
        clock = SimpleNamespace(ticks_ms=lambda: 1000, ticks_us=lambda: 100000,
                                ticks_diff=lambda a, b: a - b)
        namespace = {'time': clock}
        exec(compile(ast.Module(body=[tick], type_ignores=[]), str(source), 'exec'), namespace)
        cls.tick = staticmethod(namespace['tick'])
        buffered = next(node for node in receiver.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'input_is_buffered')
        exec(compile(ast.Module(body=[buffered], type_ignores=[]), str(source), 'exec'), namespace)
        cls.buffered = staticmethod(namespace['input_is_buffered'])

    def receiver(self, decoded_words, synced):
        radio = SimpleNamespace(
            sm=Fifo(decoded_words), POCSAG_SYNC=0x7CD215D8,
            COUNTER_MASK=0x3FFFFFFF, _last_tick_entry_ms=None,
            max_tick_gap_ms=0, tick_gaps_over_200ms=0, max_tick_us=0,
            fifo_highwater=0, fifo_full_hits=0, words_seen=0,
            last_word_time=0, synced=synced, sync_window=0,
            corrected_sync_words=0, soft_sync_locks=0,
            current_cw=0, bit_count=0, last_timeout_check=1000,
            decoded=[], sync_times=[],
            _dma_rx=None, _dma_seen_overruns=0,
        )
        def record_sync(now):
            radio.synced = True
            radio.sync_times.append(now)
        radio._refresh_afc_on_preamble = lambda word, now: False
        radio._record_sync = record_sync
        radio._decode_codeword = lambda word, now: radio.decoded.append(word)
        radio._process_raw_queue = lambda: None
        radio._flush_pending_fragments = lambda now: None
        radio._service_radio_health = lambda now: None
        return radio

    def test_buffered_status_requires_dma_to_be_running(self):
        radio = self.receiver([], synced=True)
        self.assertFalse(self.buffered(radio))
        radio._dma_rx = SimpleNamespace(running=True)
        self.assertTrue(self.buffered(radio))
        radio._dma_rx.running = False
        self.assertFalse(self.buffered(radio))

    def test_dma_words_are_decoded_without_reading_hardware_fifo(self):
        radio = self.receiver([], synced=True)
        captured = Fifo([0x12345678] * 9)
        radio._dma_rx = SimpleNamespace(available=captured.rx_fifo,
                                        get=captured.get, overruns=0)
        self.tick(radio)
        self.assertEqual(radio.words_seen, 8)
        self.assertEqual(captured.rx_fifo(), 1)
        self.assertEqual(radio.fifo_full_hits, 0)
        self.tick(radio)
        self.assertEqual(radio.words_seen, 9)

    def test_dma_overrun_resets_decoder_before_using_latest_words(self):
        radio = self.receiver([], synced=True)
        captured = Fifo([0x12345678])
        resets = []
        radio._dma_rx = SimpleNamespace(available=captured.rx_fifo,
            get=captured.get, overruns=1, dropped_words=3)
        radio._reset_decoder = lambda **kwargs: resets.append(kwargs)
        radio._advance_rx_group = lambda: resets.append("advance")
        self.tick(radio)
        self.assertEqual(resets, [{"discard_message": True}, "advance"])
        self.tick(radio)
        self.assertEqual(len(resets), 2)

    def test_fifo_drain_is_bounded_to_eight_words(self):
        radio = self.receiver([0x12345678] * 9, synced=True)
        self.tick(radio)
        self.assertEqual(radio.words_seen, 8)
        self.assertEqual(radio.sm.rx_fifo(), 1)
        self.assertEqual(len(radio.decoded), 8)
        self.tick(radio)
        self.assertEqual(radio.words_seen, 9)
        self.assertEqual(radio.sm.rx_fifo(), 0)

    def test_sync_accepts_two_bad_bits_but_not_three(self):
        sync = 0x7CD215D8
        for errors, expected in ((0, 1), (0b11, 1), (0b111, 0)):
            with self.subTest(errors=errors):
                radio = self.receiver([sync ^ errors], synced=False)
                self.tick(radio)
                self.assertEqual(len(radio.sync_times), expected)
                self.assertEqual(radio.corrected_sync_words,
                                 1 if errors and expected else 0)


if __name__ == '__main__':
    unittest.main()
