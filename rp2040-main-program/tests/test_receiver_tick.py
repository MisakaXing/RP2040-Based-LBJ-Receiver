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
        )
        def record_sync(now):
            radio.synced = True
            radio.sync_times.append(now)
        radio._record_sync = record_sync
        radio._decode_codeword = lambda word, now: radio.decoded.append(word)
        radio._process_raw_queue = lambda: None
        radio._flush_pending_fragments = lambda now: None
        radio._service_radio_health = lambda now: None
        return radio

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
