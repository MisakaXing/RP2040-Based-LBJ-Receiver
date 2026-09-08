"""Bounded, verified history downloads; no writes to the Pico filesystem."""

import ast
from dataclasses import dataclass
import hashlib
import os
import tempfile


CHUNK_SIZE = 4096
COMMAND_TIMEOUT = 20


class HistoryTransferError(RuntimeError):
    pass


@dataclass
class HistoryDownload:
    data: bytes
    sha256: str
    recovery_warning: str = ""


def _connect(port):
    # Also bundled by PyInstaller; no child process or command-line timeout.
    from mpremote.transport_serial import SerialTransport

    return SerialTransport(port)


def _execute(transport, command):
    output, error = transport.exec_raw(command, timeout=COMMAND_TIMEOUT)
    if error:
        raise HistoryTransferError(error.decode("utf-8", errors="replace").strip())
    return output.strip()


def download_history(port, progress=None, status=None, transport_factory=None):
    """Pause/reset the interpreter, download in small transactions, then reboot.

    Each block has its own EOF and timeout, independent of total file size.
    The initial raw-REPL reset stops the receiver's second core/timers too, so
    background radio prints cannot be mistaken for file data. main.py is not
    executed in this raw-REPL session. The final hardware reset runs it again.
    """
    def observe(callback, *args):
        # A closed/busy GUI must not prevent the finally block from rebooting
        # the receiver. Observers do not control the transport lifecycle.
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                pass

    def notify(text):
        observe(status, text)

    def report(done, total):
        observe(progress, done, total)

    transport = None
    failure = None
    recovery_warning = ""
    data = bytearray()
    digest = hashlib.sha256()
    synchronized = False
    try:
        notify("正在暂停接收程序...")
        transport = (transport_factory or _connect)(port)
        # Bound low-level reads/writes too (older mpremote defaults to None).
        transport.serial.timeout = 2
        transport.serial.write_timeout = 5
        transport.use_raw_paste = False
        transport.enter_raw_repl(soft_reset=True)
        synchronized = True
        size = ast.literal_eval(_execute(transport,
            "import os as _lv_os, gc as _lv_gc, hashlib as _lv_hashlib\n"
            "_lv_gc.collect()\n"
            "_lv_file = open('history.jsonl', 'rb')\n"
            "_lv_hash = _lv_hashlib.sha256()\n"
            "print(_lv_os.stat('history.jsonl')[6])"
        ).decode("ascii"))
        if type(size) is not int or size < 0:
            raise HistoryTransferError("设备返回了无效的文件长度。")
        report(0, size)
        while len(data) < size:
            requested = min(CHUNK_SIZE, size - len(data))
            # Decode a complete bytes literal, never individual UTF-8 chunks.
            # The device holds only one small block, not the whole history.
            chunk = ast.literal_eval(_execute(transport,
                "_lv_block = _lv_file.read(%d)\n"
                "_lv_hash.update(_lv_block)\n"
                "print(repr(_lv_block))\n"
                "del _lv_block\n_lv_gc.collect()" % requested
            ).decode("ascii"))
            if not isinstance(chunk, bytes) or len(chunk) != requested:
                raise HistoryTransferError("历史文件提前结束或数据块长度不符，请重新读取。")
            data.extend(chunk)
            digest.update(chunk)
            report(len(data), size)

        notify("正在校验文件完整性...")
        remote_size, tail, remote_digest = ast.literal_eval(_execute(transport,
            "print(repr((_lv_os.stat('history.jsonl')[6], "
            "_lv_file.read(1), _lv_hash.digest())))\n_lv_file.close()"
        ).decode("ascii"))
        if remote_size != size or tail != b"":
            raise HistoryTransferError("读取期间历史文件长度发生变化，请重新读取。")
        if remote_digest != digest.digest():
            raise HistoryTransferError("历史文件 SHA-256 校验失败，未采用不完整数据。")
    except Exception as exc:
        synchronized = False
        failure = exc
    finally:
        if transport is not None:
            try:
                notify("正在恢复设备运行...")
                if not synchronized:
                    # A timed-out command can still be printing. Interrupt it
                    # and reacquire raw REPL before issuing a reset command.
                    transport.enter_raw_repl(soft_reset=False)
                # A reset has no normal EOF; do not wait for one.
                transport.exec_raw_no_follow("import machine; machine.reset()")
            except Exception as exc:
                recovery_warning = "未能确认已发送设备重启指令，请手动复位。原因：%s" % exc
            finally:
                try:
                    transport.close()
                except Exception as exc:
                    recovery_warning = recovery_warning or "关闭串口失败：%s" % exc

    if failure is not None:
        detail = str(failure)
        if "EOF" in detail or "timeout" in detail.lower():
            detail = "传输某个数据块时设备未及时响应（已读取 %d 字节）。\n%s" % (len(data), detail)
        if recovery_warning:
            detail += "\n\n" + recovery_warning
        raise HistoryTransferError(detail) from failure
    return HistoryDownload(bytes(data), digest.hexdigest(), recovery_warning)


def save_history_atomic(path, data):
    """Only replace the chosen export after all verified bytes reach disk."""
    path = os.path.abspath(path)
    fd, temporary = tempfile.mkstemp(prefix=".lbj-history-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
