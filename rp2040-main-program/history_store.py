"""Bounded, power-loss-tolerant history storage for the LBJ receiver.

The onboard log is JSON Lines so an interrupted final write can be isolated
without rewriting the whole file.  A sparse in-RAM index keeps 9,999 records
practical on RP2350: one 32-bit offset is stored for every 16 valid records.
"""

import array
import errno
import gc
import json
import os


MIB = 1024 * 1024
LARGE_FLASH_FS_BYTES = 12 * MIB
MEDIUM_FLASH_FS_BYTES = 4 * MIB
LARGE_FLASH_HISTORY_LIMIT = 9999
MEDIUM_FLASH_HISTORY_LIMIT = 5000
SMALL_FLASH_HISTORY_LIMIT = 2500
DEFAULT_INDEX_STRIDE = 16
DEFAULT_MAX_RECORD_BYTES = 1024
DEFAULT_MAX_SCAN_LINE_BYTES = 4096

BASIC_HISTORY_FIELDS = ("speed_kmh", "km_post")
EXTENDED_HISTORY_FIELDS = (
    "route_hex",
    "class_tag",
    "loco_type",
    "cab_end",
    "lon",
    "lat",
)

APPEND_OK = 1
APPEND_FULL = 0
APPEND_RETRY = -1
APPEND_INVALID = -2
APPEND_NO_SPACE = -3


def filesystem_size(path="/", statvfs_fn=None):
    statvfs_fn = os.statvfs if statvfs_fn is None else statvfs_fn
    values = statvfs_fn(path)
    block_size = int(values[0])
    return block_size * int(values[2]), block_size * int(values[3])


def history_limit_for_filesystem(total_bytes):
    if total_bytes >= LARGE_FLASH_FS_BYTES:
        return LARGE_FLASH_HISTORY_LIMIT
    if total_bytes >= MEDIUM_FLASH_FS_BYTES:
        return MEDIUM_FLASH_HISTORY_LIMIT
    return SMALL_FLASH_HISTORY_LIMIT


def reserve_for_filesystem(total_bytes):
    # Preserve room for config updates, LittleFS metadata and future program
    # files.  A small legacy filesystem cannot afford the 2 MiB large-board
    # reserve, so it uses a smaller fixed floor.
    if total_bytes >= LARGE_FLASH_FS_BYTES:
        return 2 * MIB
    if total_bytes >= MEDIUM_FLASH_FS_BYTES:
        return 1 * MIB
    return 256 * 1024


def _valid_train_number(value):
    text = str(value if value is not None else "")
    return 1 <= len(text) <= 8 and text.isdigit()


def _has_history_extended_fields(extended):
    return (
        isinstance(extended, dict)
        and any(name in extended for name in EXTENDED_HISTORY_FIELDS)
    )


def is_valid_history_record(record):
    if not isinstance(record, dict) or not isinstance(record.get("d"), dict):
        return False
    data = record["d"]
    basic = data.get("basic")
    extended = data.get("extended", {})
    if not isinstance(extended, dict):
        return False
    has_train = isinstance(basic, dict) and _valid_train_number(
        basic.get("train_no")
    )
    # An explicit extended-only message is still a real receiver event when
    # the extension parser could not recover any display field.  Keep its
    # timestamp/RIC/RSSI instead of silently deleting it from the machine log.
    has_extension = data.get("type") == "extended_only"
    return has_train or has_extension


def _bounded_field(value, max_chars):
    if isinstance(value, (int, float)):
        return value
    return str(value if value is not None else "---")[:max_chars]


def make_history_record(received_at, data):
    """Return the compact record kept on internal Flash, or ``None``.

    Raw POCSAG text and pairing diagnostics remain available in the SD log,
    but are deliberately excluded from internal history because the device UI
    only needs the fields below.
    """
    if not isinstance(data, dict):
        return None
    msg_type = str(data.get("type", "basic_only"))[:24]
    basic = data.get("basic")
    has_train = isinstance(basic, dict) and _valid_train_number(
        basic.get("train_no")
    )
    source_extended = data.get("extended")
    if not isinstance(source_extended, dict):
        source_extended = {}
    has_extension_fields = _has_history_extended_fields(source_extended)
    explicit_extension = msg_type == "extended_only"
    if not has_train and not explicit_extension and not has_extension_fields:
        return None

    # Some transmitters send a placeholder basic half ("--- --- ---") with a
    # valid extension.  Older parser versions labelled that train_data_full,
    # but semantically it is extended-only because there is no train number.
    if not has_train:
        msg_type = "extended_only"

    stored_basic = {}
    if has_train:
        stored_basic["train_no"] = str(basic.get("train_no"))[:8]
        for name in BASIC_HISTORY_FIELDS:
            if name in basic:
                stored_basic[name] = _bounded_field(basic[name], 16)
    stored_extended = {}
    for name in EXTENDED_HISTORY_FIELDS:
        if name in source_extended:
            limit = 48 if name == "loco_type" else 32
            stored_extended[name] = _bounded_field(source_extended[name], limit)
    stored_data = {
        "type": msg_type,
        "basic": stored_basic,
        "extended": stored_extended,
    }
    if "rssi" in data:
        stored_data["rssi"] = _bounded_field(data["rssi"], 16)
    if "ric" in data:
        stored_data["ric"] = _bounded_field(data["ric"], 24)
    return {"t": str(received_at)[:32], "d": stored_data}


def storage_write_due(now, last_word_time, pending_since, last_write,
                      raw_pending, ticks_diff, quiet_ms, max_defer_ms,
                      hard_defer_ms, write_gap_ms):
    """Return whether one queued storage write may run now.

    A normal write still waits for a quiet receiver and an empty raw queue.
    The soft deadline prevents a continuous but already-decoded signal from
    starving storage forever.  The longer hard deadline is a final bound even
    if the raw queue never reports empty.
    """
    if pending_since is None:
        return False
    if ticks_diff(now, last_write) < write_gap_ms:
        return False

    pending_age = ticks_diff(now, pending_since)
    if pending_age >= hard_defer_ms:
        return True
    if raw_pending:
        return False
    if ticks_diff(now, last_word_time) >= quiet_ms:
        return True
    return pending_age >= max_defer_ms


class HistoryStore:
    def __init__(self, path="history.jsonl", root="/", max_records=None,
                 index_stride=DEFAULT_INDEX_STRIDE,
                 max_record_bytes=DEFAULT_MAX_RECORD_BYTES,
                 statvfs_fn=None):
        if index_stride < 1:
            raise ValueError("index_stride must be positive")
        self.path = path
        self.root = root
        self.index_stride = int(index_stride)
        self.max_record_bytes = int(max_record_bytes)
        self.max_scan_line_bytes = DEFAULT_MAX_SCAN_LINE_BYTES
        self._statvfs = os.statvfs if statvfs_fn is None else statvfs_fn
        self._automatic_limit = max_records is None
        self.total_bytes = 0
        self.free_bytes = 0
        self.last_error = ""
        self._refresh_space()
        self.max_records = (
            history_limit_for_filesystem(self.total_bytes)
            if max_records is None else int(max_records)
        )
        self.reserve_bytes = reserve_for_filesystem(self.total_bytes)
        self.offsets = array.array("I")
        self._cache_checkpoint = -1
        self._cache_records = []
        self.count = 0
        self.invalid_lines = 0
        self.tail_needs_separator = False
        self.index_complete = True
        self.read_only = False
        self.full = False

    def _clear_cache(self):
        self._cache_checkpoint = -1
        self._cache_records = []

    def _refresh_capacity_policy(self):
        if self._automatic_limit:
            self.max_records = history_limit_for_filesystem(self.total_bytes)
        self.reserve_bytes = reserve_for_filesystem(self.total_bytes)

    def _refresh_space(self):
        try:
            self.total_bytes, self.free_bytes = filesystem_size(
                self.root, self._statvfs
            )
            return True
        except Exception as exc:
            self.last_error = "STATVFS " + str(exc)[:40]
            return False

    @staticmethod
    def _decode_line(line, max_bytes=DEFAULT_MAX_SCAN_LINE_BYTES):
        if not line or len(line) > max_bytes:
            return None
        try:
            record = json.loads(line)
        except MemoryError:
            # Low memory is not corrupt input.  Propagate it so scan/load can
            # fail safely instead of silently shifting every later index.
            raise
        except Exception:
            return None
        return record if is_valid_history_record(record) else None

    def _read_bounded_line(self, source):
        """Read one physical line without ever allocating an unbounded tail."""
        limit = self.max_scan_line_bytes
        line = source.readline(limit + 1)
        if not line:
            return None, True, False
        had_newline = line.endswith(b"\n")
        if len(line) <= limit:
            return line, had_newline, False

        # The line is too large to be a valid internal record.  Drain its
        # remaining chunks so the next offset still starts at a JSON line.
        while not had_newline:
            chunk = source.readline(limit + 1)
            if not chunk:
                break
            had_newline = chunk.endswith(b"\n")
        return None, had_newline, True

    def scan(self, _capacity_rescan=False):
        self.offsets = array.array("I")
        self._clear_cache()
        self.count = 0
        self.invalid_lines = 0
        self.tail_needs_separator = False
        self.index_complete = True
        self.read_only = False
        self.full = False
        self.last_error = ""
        if self._refresh_space():
            # A transient statvfs failure during module import must not leave a
            # 16 MiB board locked to the conservative 2500-record fallback.
            self._refresh_capacity_policy()
            self.last_error = ""
        last_line_had_newline = True
        saw_line = False
        try:
            with open(self.path, "rb") as source:
                physical_lines = 0
                while self.count < self.max_records:
                    offset = source.tell()
                    line, line_had_newline, oversized = self._read_bounded_line(source)
                    if line is None and not oversized:
                        break
                    saw_line = True
                    last_line_had_newline = line_had_newline
                    physical_lines += 1
                    record = None if oversized else self._decode_line(
                        line, self.max_scan_line_bytes
                    )
                    if record is None:
                        self.invalid_lines += 1
                    else:
                        if self.count % self.index_stride == 0:
                            self.offsets.append(offset)
                        self.count += 1
                    if physical_lines & 0x7F == 0:
                        gc.collect()
            self.tail_needs_separator = saw_line and not last_line_had_newline
            self.full = self.count >= self.max_records
        except OSError as exc:
            code = getattr(exc, "errno", None)
            if code is None and getattr(exc, "args", None):
                code = exc.args[0]
            if code in (getattr(errno, "ENOENT", 2), 2):
                try:
                    with open(self.path, "ab"):
                        pass
                except Exception as create_exc:
                    self.index_complete = False
                    self.read_only = True
                    self.last_error = "OPEN " + str(create_exc)[:40]
            else:
                self.index_complete = False
                self.read_only = True
                self.last_error = "OPEN " + str(exc)[:40]
        except MemoryError:
            # Never reinterpret an index-allocation failure as an empty log;
            # doing so would allow appending past the capacity limit.
            self.index_complete = False
            self.read_only = True
            self.last_error = "INDEX MEMORY"
        except Exception as exc:
            self.index_complete = False
            self.read_only = True
            self.last_error = "SCAN " + str(exc)[:40]
        if self._refresh_space():
            old_limit = self.max_records
            self._refresh_capacity_policy()
            if self._automatic_limit and self.max_records != old_limit:
                if not _capacity_rescan:
                    # The filesystem size recovered only after the first pass.
                    # Rebuild using the correct limit so a >2500-record file is
                    # never reported as a full small-board history.
                    return self.scan(_capacity_rescan=True)
                self.index_complete = False
                self.read_only = True
                self.last_error = "CAPACITY UNSTABLE"
            elif self.index_complete:
                self.last_error = ""
        return self.count

    def load(self, index):
        if (
            not self.index_complete
            or index < 0
            or index >= self.count
        ):
            return None
        checkpoint = index // self.index_stride
        if checkpoint >= len(self.offsets):
            return None
        slot = index - checkpoint * self.index_stride
        if (
            self._cache_checkpoint == checkpoint
            and slot < len(self._cache_records)
        ):
            return self._cache_records[slot]

        # Decode one sparse-index block at a time.  History browsing is
        # sequential, so keeping these records in RAM turns the next fifteen
        # pages into constant-time lookups instead of reopening LittleFS and
        # decoding the same JSON lines again for every key repeat.
        block_base = checkpoint * self.index_stride
        expected = min(self.index_stride, self.count - block_base)
        next_offset = (
            self.offsets[checkpoint + 1]
            if checkpoint + 1 < len(self.offsets)
            else None
        )
        records = []
        try:
            with open(self.path, "rb") as source:
                source.seek(self.offsets[checkpoint])
                while len(records) < expected:
                    if next_offset is not None and source.tell() >= next_offset:
                        raise ValueError("INDEX MISMATCH")
                    line, _, oversized = self._read_bounded_line(source)
                    if line is None and not oversized:
                        raise ValueError("INDEX MISMATCH")
                    record = None if oversized else self._decode_line(
                        line, self.max_scan_line_bytes
                    )
                    if record is None:
                        continue
                    records.append(record)
            self._cache_checkpoint = checkpoint
            self._cache_records = records
            return records[slot]
        except MemoryError:
            # A 16-record decoded window is an optimization, not a reason to
            # lose access to history under pressure.  Release it and fall
            # back to the former one-record sparse-index lookup.
            records = None
            self._clear_cache()
            gc.collect()
            return self._load_uncached(index, checkpoint, next_offset)
        except Exception as exc:
            self._clear_cache()
            self.last_error = "READ " + str(exc)[:40]
            return None

    def _load_uncached(self, index, checkpoint, next_offset=None):
        remaining = index - checkpoint * self.index_stride
        try:
            with open(self.path, "rb") as source:
                source.seek(self.offsets[checkpoint])
                while True:
                    if next_offset is not None and source.tell() >= next_offset:
                        raise ValueError("INDEX MISMATCH")
                    line, _, oversized = self._read_bounded_line(source)
                    if line is None and not oversized:
                        raise ValueError("INDEX MISMATCH")
                    record = None if oversized else self._decode_line(
                        line, self.max_scan_line_bytes
                    )
                    if record is None:
                        continue
                    if remaining == 0:
                        self.last_error = ""
                        return record
                    remaining -= 1
        except Exception as exc:
            self.last_error = "READ " + str(exc)[:40]
            return None

    def latest(self):
        return self.load(self.count - 1) if self.count else None

    def append(self, record):
        if self.read_only or not self.index_complete:
            return APPEND_RETRY
        if not is_valid_history_record(record):
            self.last_error = "INVALID RECORD"
            return APPEND_INVALID
        if self.count >= self.max_records:
            self.full = True
            self.last_error = "RECORD LIMIT"
            return APPEND_FULL
        try:
            json_line = json.dumps(record)
            json_bytes = json_line.encode("utf-8")
        except MemoryError:
            gc.collect()
            self.last_error = "ENCODE MEMORY"
            return APPEND_RETRY
        except Exception as exc:
            self.last_error = "ENCODE " + str(exc)[:40]
            return APPEND_INVALID
        if len(json_bytes) > self.max_record_bytes:
            self.last_error = "RECORD TOO LARGE"
            return APPEND_INVALID
        if not self._refresh_space():
            return APPEND_RETRY
        required = len(json_bytes) + 1 + 4096
        if self.free_bytes - required < self.reserve_bytes:
            self.full = True
            self.last_error = "FLASH RESERVE"
            return APPEND_NO_SPACE

        prefix = b"\n" if self.tail_needs_separator else b""
        payload = prefix + json_bytes + b"\n"
        try:
            # MicroPython text streams return the number of UTF-8 bytes, not
            # Python characters.  Binary mode keeps short-write checks,
            # offsets and free-space accounting in the same byte unit.
            with open(self.path, "ab") as target:
                target.seek(0, 2)
                record_offset = target.tell() + len(prefix)
                written = target.write(payload)
                if written is not None and written != len(payload):
                    raise OSError("short history write")
                try:
                    target.flush()
                except AttributeError:
                    pass
            if self.count % self.index_stride == 0:
                self.offsets.append(record_offset)
            self.count += 1
            self.tail_needs_separator = False
            self.full = self.count >= self.max_records
            self.last_error = ""
            self.free_bytes -= len(payload)
            return APPEND_OK
        except MemoryError:
            self.index_complete = False
            self.read_only = True
            self.last_error = "APPEND MEMORY"
            return APPEND_RETRY
        except Exception as exc:
            # Once writing has begun, a write/flush/close failure has an
            # uncertain commit state.  Do not retry blindly: a complete line
            # may already be on Flash and writing it again would create a
            # hidden duplicate that shifts every later sparse-index lookup.
            # A reboot/scan can safely recover either a full or partial tail.
            self.tail_needs_separator = True
            self.index_complete = False
            self.read_only = True
            self.last_error = "WRITE " + str(exc)[:40]
            return APPEND_RETRY

    def clear(self):
        temp_path = self.path + ".tmp"
        try:
            # Allocate the replacement structures before committing the empty
            # file.  Once rename succeeds, only non-allocating assignments are
            # needed, so deleted records can never survive in a stale cache.
            empty_offsets = array.array("I")
            empty_cache = []
            with open(temp_path, "wb") as target:
                try:
                    target.flush()
                except AttributeError:
                    pass
            os.rename(temp_path, self.path)
            self.offsets = empty_offsets
            self._cache_checkpoint = -1
            self._cache_records = empty_cache
            self.count = 0
            self.invalid_lines = 0
            self.tail_needs_separator = False
            self.index_complete = True
            self.read_only = False
            self.full = False
            self.last_error = ""
            self._refresh_space()
            return True
        except Exception as exc:
            self.last_error = "CLEAR " + str(exc)[:40]
            try:
                os.remove(temp_path)
            except Exception:
                pass
            return False
