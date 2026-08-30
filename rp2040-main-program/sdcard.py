"""
MicroPython driver for SD cards using SPI bus.

Requires an SPI bus and a CS pin.  Provides readblocks and writeblocks
methods so the device can be mounted as a filesystem.

Modified from MicroPython 0.17's release by Brenton Schulz at Core Electronics - 2022-06-02

Changes for increased read throughput:

Line 19: _CMD_TIMEOUT = const(100) to _CMD_TIMEOUT = const(1000)
Line 172: time.sleep_ms(1) to time.sleep(0.0001)

https://core-electronics.com.au/guides/makerverse-micro-sd-adapter-micropython-guide/
"""

from micropython import const
import time


_CMD_TIMEOUT = const(1000)
_WRITE_TIMEOUT_MS = const(1500)
_INIT_TIMEOUT_MS = const(5000)

_R1_IDLE_STATE = const(1 << 0)
# R1_ERASE_RESET = const(1 << 1)
_R1_ILLEGAL_COMMAND = const(1 << 2)
# R1_COM_CRC_ERROR = const(1 << 3)
# R1_ERASE_SEQUENCE_ERROR = const(1 << 4)
# R1_ADDRESS_ERROR = const(1 << 5)
# R1_PARAMETER_ERROR = const(1 << 6)
_TOKEN_CMD25 = const(0xFC)
_TOKEN_STOP_TRAN = const(0xFD)
_TOKEN_DATA = const(0xFE)


def _ticks_ms():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def _ticks_diff(new, old):
    try:
        return time.ticks_diff(new, old)
    except AttributeError:
        return new - old


def _sleep_ms(value):
    try:
        time.sleep_ms(value)
    except AttributeError:
        time.sleep(value / 1000.0)


def _sectors_from_csd(csd):
    """Return the card capacity in 512-byte sectors."""
    if len(csd) < 16:
        raise OSError("invalid SD CSD")
    structure = csd[0] & 0xC0
    if structure == 0x40:  # CSD version 2.0, SDHC/SDXC
        c_size = (csd[7] << 16) | (csd[8] << 8) | csd[9]
        return (c_size + 1) * 1024
    if structure == 0x00:  # CSD version 1.0, SDSC
        read_bl_len = csd[5] & 0x0F
        c_size = (
            ((csd[6] & 0x03) << 10)
            | (csd[7] << 2)
            | ((csd[8] & 0xC0) >> 6)
        )
        c_size_mult = ((csd[9] & 0x03) << 1) | (csd[10] >> 7)
        capacity_bytes = (
            (c_size + 1)
            * (1 << (c_size_mult + 2))
            * (1 << read_bl_len)
        )
        return capacity_bytes // 512
    raise OSError("SD card CSD format not supported")


def _cdv_from_ocr(ocr):
    if len(ocr) < 1:
        raise OSError("invalid SD OCR")
    # OCR bit 30 (CCS) is the second-highest bit of the first network-order
    # byte. SDHC/SDXC use block addressing; v2 SDSC still uses byte addressing.
    return 1 if (ocr[0] & 0x40) else 512


class SDCard:
    def __init__(self, spi, cs, baudrate=1320000):
        self.spi = spi
        self.cs = cs

        self.cmdbuf = bytearray(6)
        self.dummybuf = bytearray(512)
        self.tokenbuf = bytearray(1)
        for i in range(512):
            self.dummybuf[i] = 0xFF
        self.dummybuf_memoryview = memoryview(self.dummybuf)

        # initialise the card
        self.init_card(baudrate)

    def init_spi(self, baudrate):
        try:
            master = self.spi.MASTER
        except AttributeError:
            # on ESP8266
            self.spi.init(baudrate=baudrate, phase=0, polarity=0)
        else:
            # on pyboard
            self.spi.init(master, baudrate=baudrate, phase=0, polarity=0)

    def _release(self):
        self.cs(1)
        try:
            self.spi.write(b"\xff")
        except Exception:
            pass

    def init_card(self, baudrate):

        # init CS pin
        self.cs.init(self.cs.OUT, value=1)

        # init SPI bus; use low data rate for initialisation
        self.init_spi(100000)

        # clock card at least 100 cycles with cs high
        for i in range(16):
            self.spi.write(b"\xff")

        # CMD0: init card; should return _R1_IDLE_STATE (allow 5 attempts)
        for _ in range(5):
            if self.cmd(0, 0, 0x95) == _R1_IDLE_STATE:
                break
        else:
            raise OSError("no SD card")

        # CMD8: determine card version
        r = self.cmd(8, 0x01AA, 0x87, 4)
        if r == _R1_IDLE_STATE:
            self.init_card_v2()
        elif r == (_R1_IDLE_STATE | _R1_ILLEGAL_COMMAND):
            self.init_card_v1()
        else:
            raise OSError("couldn't determine SD card version")

        # get the number of sectors
        # CMD9: response R2 (R1 byte + 16-byte block read)
        if self.cmd(9, 0, 0, 0, False) != 0:
            self._release()
            raise OSError("no response from SD card")
        csd = bytearray(16)
        self.readinto(csd)
        self.sectors = _sectors_from_csd(csd)
        # print('sectors', self.sectors)

        # CMD16: set block length to 512 bytes
        if self.cmd(16, 512, 0) != 0:
            raise OSError("can't set 512 block size")

        # set to high data rate now that it's initialised
        self.init_spi(baudrate)

    def init_card_v1(self):
        started = _ticks_ms()
        while _ticks_diff(_ticks_ms(), started) < _INIT_TIMEOUT_MS:
            self.cmd(55, 0, 0)
            if self.cmd(41, 0, 0) == 0:
                self.cdv = 512
                # print("[SDCard] v1 card")
                return
            _sleep_ms(10)
        raise OSError("timeout waiting for v1 card")

    def init_card_v2(self):
        started = _ticks_ms()
        while _ticks_diff(_ticks_ms(), started) < _INIT_TIMEOUT_MS:
            _sleep_ms(50)
            self.cmd(55, 0, 0)
            if self.cmd(41, 0x40000000, 0) == 0:
                response = self.cmd(58, 0, 0, release=False)
                try:
                    if response != 0:
                        raise OSError("invalid SD OCR response")
                    ocr = self.spi.read(4, 0xFF)
                finally:
                    self._release()
                self.cdv = _cdv_from_ocr(ocr)
                # print("[SDCard] v2 card")
                return
        raise OSError("timeout waiting for v2 card")

    def cmd(self, cmd, arg, crc, final=0, release=True, skip1=False):
        self.cs(0)
        try:
            # create and send the command
            buf = self.cmdbuf
            buf[0] = 0x40 | cmd
            buf[1] = arg >> 24
            buf[2] = arg >> 16
            buf[3] = arg >> 8
            buf[4] = arg
            buf[5] = crc
            self.spi.write(buf)

            if skip1:
                self.spi.readinto(self.tokenbuf, 0xFF)

            # wait for the response (response[7] == 0)
            for i in range(_CMD_TIMEOUT):
                self.spi.readinto(self.tokenbuf, 0xFF)
                response = self.tokenbuf[0]
                if not (response & 0x80):
                    # this could be a big-endian integer that we are getting here
                    for j in range(final):
                        self.spi.write(b"\xff")
                    if release:
                        self._release()
                    return response

            # timeout
            self._release()
            return -1
        except Exception:
            self._release()
            raise

    def readinto(self, buf):
        self.cs(0)
        try:
            # read until start byte (0xff)
            for i in range(_CMD_TIMEOUT):
                self.spi.readinto(self.tokenbuf, 0xFF)
                if self.tokenbuf[0] == _TOKEN_DATA:
                    break
                time.sleep(0.0001)
            else:
                raise OSError("timeout waiting for response")

            # read data
            mv = self.dummybuf_memoryview
            if len(buf) != len(mv):
                mv = mv[: len(buf)]
            self.spi.write_readinto(mv, buf)

            # read checksum
            self.spi.write(b"\xff")
            self.spi.write(b"\xff")
        finally:
            self._release()

    def write(self, token, buf):
        self.cs(0)
        try:
            # send: start of block, data, checksum
            self.spi.read(1, token)
            self.spi.write(buf)
            self.spi.write(b"\xff")
            self.spi.write(b"\xff")

            # check the response
            if (self.spi.read(1, 0xFF)[0] & 0x1F) != 0x05:
                raise OSError(5)

            # wait for write to finish
            self._wait_not_busy()
        finally:
            # An SPI exception must never leave the SD card selected while the
            # shared bus is returned to the TFT driver.
            self._release()

    def write_token(self, token):
        self.cs(0)
        try:
            self.spi.read(1, token)
            self.spi.write(b"\xff")
            # wait for write to finish
            self._wait_not_busy()
        finally:
            self._release()

    def _wait_not_busy(self):
        started = _ticks_ms()
        while self.spi.read(1, 0xFF)[0] == 0x00:
            if _ticks_diff(_ticks_ms(), started) >= _WRITE_TIMEOUT_MS:
                raise OSError(110)
            _sleep_ms(1)

    def readblocks(self, block_num, buf):
        nblocks = len(buf) // 512
        assert nblocks and not len(buf) % 512, "Buffer length is invalid"
        if nblocks == 1:
            # CMD17: set read address for single block
            if self.cmd(17, block_num * self.cdv, 0, release=False) != 0:
                # release the card
                self._release()
                raise OSError(5)  # EIO
            # receive the data and release card
            self.readinto(buf)
        else:
            # CMD18: set read address for multiple blocks
            if self.cmd(18, block_num * self.cdv, 0, release=False) != 0:
                # release the card
                self._release()
                raise OSError(5)  # EIO
            offset = 0
            mv = memoryview(buf)
            while nblocks:
                # receive the data and release card
                self.readinto(mv[offset : offset + 512])
                offset += 512
                nblocks -= 1
            if self.cmd(12, 0, 0xFF, skip1=True):
                raise OSError(5)  # EIO

    def writeblocks(self, block_num, buf):
        nblocks, err = divmod(len(buf), 512)
        assert nblocks and not err, "Buffer length is invalid"
        if nblocks == 1:
            # CMD24: set write address for single block
            if self.cmd(24, block_num * self.cdv, 0) != 0:
                raise OSError(5)  # EIO

            # send the data
            self.write(_TOKEN_DATA, buf)
        else:
            # CMD25: set write address for first block
            if self.cmd(25, block_num * self.cdv, 0) != 0:
                raise OSError(5)  # EIO
            # send the data
            offset = 0
            mv = memoryview(buf)
            while nblocks:
                self.write(_TOKEN_CMD25, mv[offset : offset + 512])
                offset += 512
                nblocks -= 1
            self.write_token(_TOKEN_STOP_TRAN)

    def ioctl(self, op, arg):
        if op == 4:  # get number of blocks
            return self.sectors
