import ast
from array import array
from pathlib import Path
from types import SimpleNamespace
import unittest


class FakeDMA:
    next_channel = 0
    def __init__(self):
        self.channel = FakeDMA.next_channel
        FakeDMA.next_channel += 1
        self.registers = list(range(16))
        self.ctrl = 0
        self.count = 0
        self.closed = False
        self.settings = None
        self.busy = False
    def pack_ctrl(self, default=None, **kwargs):
        self.settings = kwargs
        return 1 if kwargs.get("enable", True) else 0
    def config(self, **kwargs):
        self.configured = kwargs
        self.count = kwargs.get("count", self.count)
        self.ctrl = kwargs.get("ctrl", self.ctrl)
        self.busy = kwargs.get("trigger", False)
    def active(self, value=None):
        if value is not None:
            if not value:
                assert not (self.ctrl & 1), "RP2350-E5: EN must clear before abort"
            self.busy = bool(value)
        return self.busy
    def close(self):
        self.closed = True
        self.busy = False


class PioDmaRxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "pio_dma_rx.py"
        tree = ast.parse(path.read_text())
        cls.memory = {}
        namespace = {"machine": SimpleNamespace(mem32=cls.memory),
                     "rp2": SimpleNamespace(DMA=FakeDMA),
                     "uctypes": SimpleNamespace(addressof=lambda buffer: 0x20000014),
                     "array": array}
        node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
        cls.capture_class = namespace["PioDmaRx"]

    def setUp(self):
        self.rx = self.capture_class(object(), ring_bits=5, transfer_count=31)
        self.producer_at = 0
        self.addCleanup(self.rx.close)

    def feed(self, values):
        for value in values:
            self.memory[self.rx.address + self.producer_at * 4] = value
            self.producer_at = (self.producer_at + 1) & self.rx.mask
            self.rx._dma.count -= 1
            if self.rx._dma.count == 0:
                self.rx._dma.count = self.rx._count

    def test_ring_alignment_and_paced_rx_configuration(self):
        self.assertEqual(self.rx.address % 32, 0)
        self.assertGreaterEqual(self.rx.address, 0x20000014)
        self.assertLessEqual(self.rx.address + 32, 0x20000014 + len(self.rx._buffer))
        self.assertEqual(self.rx._dma.configured["write"], self.rx.address)
        self.assertEqual(self.rx._reload.configured["write"], [7])
        self.assertEqual(self.rx._reload_count[0], 31)

    def test_order_wrap_and_count_reload(self):
        for value in range(100):
            self.feed([value])
            self.assertEqual(self.rx.available(), 1)
            self.assertEqual(self.rx.get(), value)
        self.assertEqual(self.rx.available(), 0)
        self.assertEqual(self.rx.overruns, 0)

    def test_advisory_pending_does_not_mutate_consumer(self):
        self.feed([1, 2, 3])
        before = (self.rx._remaining, self.rx._pending, self.rx._read_at)
        self.assertEqual(self.rx.pending(), 3)
        self.assertEqual(self.rx.pending(), 3)
        self.assertEqual((self.rx._remaining, self.rx._pending, self.rx._read_at), before)
        self.assertEqual(self.rx.get(), 1)
        self.assertEqual(self.rx.pending(), 2)

    def test_overrun_counts_lost_words_and_keeps_newest_samples(self):
        self.feed(range(11))
        self.assertEqual(self.rx.available(), 8)
        self.assertEqual((self.rx.overruns, self.rx.dropped_words, self.rx.highwater), (1, 3, 11))
        self.assertEqual([self.rx.get() for _ in range(8)], list(range(3, 11)))
        self.assertEqual(self.rx.available(), 0)
        with self.assertRaises(IndexError):
            self.rx.get()

    def test_stop_and_reset_abort_safely_and_discard_old_samples(self):
        self.feed([1, 2])
        self.rx.stop()
        self.assertEqual(self.rx.pending(), 0)
        self.assertEqual(self.rx.available(), 0)
        self.assertFalse(self.rx._dma.busy)
        self.assertFalse(self.rx._reload.busy)
        self.rx.reset()
        self.producer_at = 0
        self.feed([5])
        self.assertEqual(self.rx.get(), 5)

    def test_dma_bus_fault_is_not_hidden_as_no_signal(self):
        self.rx._dma.ctrl |= 0x80000000
        with self.assertRaisesRegex(OSError, "DMA bus error"):
            self.rx.available()

    def test_close_releases_both_channels_and_is_idempotent(self):
        a, b = self.rx._dma, self.rx._reload
        self.rx.close()
        self.rx.close()
        self.assertTrue(a.closed and b.closed)
        self.assertEqual(self.rx.pending(), 0)

    def test_bad_ring_and_reserved_count_modes_are_rejected(self):
        for count in (0, 0x10000000, 0xFFFFFFFF):
            with self.assertRaises(ValueError):
                self.capture_class(object(), transfer_count=count)
        for bits in (4, 16):
            with self.assertRaises(ValueError):
                self.capture_class(object(), ring_bits=bits)
