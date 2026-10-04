"""PIO RX -> aligned RAM ring, serviced by hardware without Python IRQs.

The consumer is core 1 only. pending() is a non-mutating advisory read for
core 0's low-priority web work. Never use pending() to consume data.
"""
import machine
import rp2
import uctypes
from array import array


class PioDmaRx:
    TRANSFER_COUNT = 0x0FFFFFFF  # Normal count mode on RP2040 and RP2350.

    def __init__(self, sm, sm_id=0, ring_bits=11, transfer_count=TRANSFER_COUNT):
        if not 5 <= ring_bits <= 15:
            raise ValueError("invalid DMA ring size")
        if not 1 <= transfer_count <= self.TRANSFER_COUNT:
            raise ValueError("invalid DMA transfer count")
        self.sm = sm
        self.words = 1 << (ring_bits - 2)
        self.mask = self.words - 1
        ring_bytes = 1 << ring_bits
        self._buffer = bytearray(ring_bytes * 2 - 1)
        address = uctypes.addressof(self._buffer)
        self.address = (address + ring_bytes - 1) & ~(ring_bytes - 1)
        self._reload_count = array("I", [transfer_count])
        self._count = transfer_count
        self._read_at = 0
        self._pending = 0
        self._remaining = transfer_count
        self.highwater = 0
        self.overruns = 0
        self.dropped_words = 0
        self.running = False
        self._dma = None
        self._reload = None
        try:
            self._dma = rp2.DMA()
            self._reload = rp2.DMA()
            # PIO0 RX0 = DREQ 4. Each PIO owns 8 DREQs (4 TX + 4 RX).
            self._ctrl = self._dma.pack_ctrl(
                size=2, inc_read=False, inc_write=True,
                ring_size=ring_bits, ring_sel=True,
                treq_sel=(sm_id // 4) * 8 + (sm_id % 4) + 4,
                high_pri=True, irq_quiet=True, chain_to=self._reload.channel)
            self._reload_ctrl = self._reload.pack_ctrl(
                size=2, inc_read=False, inc_write=False,
                high_pri=True, irq_quiet=True)
            self.reset()
        except Exception:
            self.close()
            raise

    def stop(self):
        self.running = False
        # RP2350-E5: clear EN before aborting a channel stalled on DREQ.
        # Disable BOTH channels before abort so a completion cannot restart RX.
        for dma in (self._reload, self._dma):
            if dma is not None:
                dma.ctrl = dma.pack_ctrl(default=dma.ctrl, enable=False,
                                         read_err=True, write_err=True)
        for dma in (self._reload, self._dma):
            if dma is not None:
                dma.active(0)

    def reset(self):
        self.stop()
        self._read_at = 0
        self._pending = 0
        self._remaining = self._count
        # Alias 1 TRANS_COUNT_TRIG reloads the count and restarts RX after
        # ~83 days at 1200 bit/s. No Python rearm/IRQ gap or finite lifetime.
        self._reload.config(read=self._reload_count,
                            write=self._dma.registers[7:8], count=1,
                            ctrl=self._reload_ctrl)
        self._dma.config(read=self.sm, write=self.address, count=self._count,
                         ctrl=self._ctrl, trigger=True)
        self.running = True

    def _new_words(self, remaining):
        # The hardware reload is much longer than any valid consumer pause.
        # Handle the one count reload boundary without losing a sample.
        if remaining <= self._remaining:
            return self._remaining - remaining
        return self._remaining + self._count - remaining

    def pending(self):
        if not self.running:
            return 0
        return self._pending + self._new_words(self._dma.count)

    def available(self):
        if not self.running:
            return 0
        if self._dma.ctrl & 0xE0000000:  # AHB_ERROR / READ_ERROR / WRITE_ERROR
            raise OSError("PIO DMA bus error")
        remaining = self._dma.count
        self._pending += self._new_words(remaining)
        self._remaining = remaining
        self.highwater = max(self.highwater, self._pending)
        if self._pending > self.words:
            lost = self._pending - self.words
            self.dropped_words += lost
            self.overruns += 1
            self._read_at = (self._read_at + lost) & self.mask
            self._pending = self.words
        return self._pending

    def get(self):
        if not self.available():
            raise IndexError("empty DMA ring")
        value = machine.mem32[self.address + self._read_at * 4]
        self._read_at = (self._read_at + 1) & self.mask
        self._pending -= 1
        return value

    def close(self):
        # Also protect older board firmware whose DMA.close() aborts before
        # clearing EN (RP2350-E5). Do not rely on its finaliser ordering.
        self.stop()
        for name in ("_reload", "_dma"):
            dma = getattr(self, name, None)
            if dma is not None:
                dma.close()
                setattr(self, name, None)
