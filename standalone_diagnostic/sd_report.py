"""Stream the diagnostic journal to a mandatory FAT SD card."""
import json
import machine
import os

MOUNT = '/sd'
QUEUE_LIMIT = 8


class SDRemoved(OSError):
    pass


class SDReport:
    def __init__(self, sd_class, run_id):
        machine.Pin(9, machine.Pin.OUT, value=1)
        self.spi = machine.SPI(1, baudrate=100000, sck=machine.Pin(10),
                               mosi=machine.Pin(11),
                               miso=machine.Pin(8, machine.Pin.IN, machine.Pin.PULL_UP))
        self.cs = machine.Pin(7, machine.Pin.OUT, value=1)
        self.card = None
        self.queue = []
        self.run_id = run_id
        self.root = MOUNT + '/LBJ_DIAG/' + run_id
        self.sector = bytearray(512)
        self._mount(sd_class)

    def _sd_speed(self):
        machine.Pin(9, machine.Pin.OUT, value=1)
        self.spi.init(baudrate=1_320_000, polarity=0, phase=0)

    def _display_speed(self):
        self.cs.value(1)
        self.spi.init(baudrate=20_000_000, polarity=0, phase=0)

    def _mount(self, sd_class):
        self._sd_speed()
        mounted = False
        try:
            self.card = sd_class(self.spi, self.cs)
            self.card.readblocks(0, self.sector)
            os.mount(os.VfsFat(self.card), MOUNT)
            mounted = True
            try:
                os.mkdir(MOUNT + '/LBJ_DIAG')
            except OSError:
                pass
            try:
                os.mkdir(self.root)
            except OSError:
                pass
            probe = self.root + '/write_check.tmp'
            with open(probe, 'w') as f:
                f.write('LBJ SD WRITE OK')
                f.flush()
            with open(probe) as f:
                if f.read() != 'LBJ SD WRITE OK':
                    raise OSError('SD write verification failed')
            os.remove(probe)
        except Exception:
            if mounted:
                try:
                    os.umount(MOUNT)
                except OSError:
                    pass
            raise
        finally:
            self._display_speed()

    def reopen(self, sd_class):
        try:
            os.umount(MOUNT)
        except OSError:
            pass
        self._mount(sd_class)

    def check_present(self):
        self._sd_speed()
        try:
            self.card.readblocks(0, self.sector)
        except OSError as exc:
            raise SDRemoved('SD card removed or unreadable: ' + str(exc))
        finally:
            self._display_speed()

    def enqueue(self, kind, item):
        if len(self.queue) >= QUEUE_LIMIT:
            return False
        self.queue.append((kind, item))
        return True

    def flush_one(self):
        if not self.queue:
            return False
        kind, item = self.queue[0]
        self._sd_speed()
        try:
            with open(self.root + '/' + kind + '.jsonl', 'a') as f:
                f.write(json.dumps(item) + '\n')
                f.flush()
        except OSError as exc:
            raise SDRemoved('SD log write failed: ' + str(exc))
        finally:
            self._display_speed()
        self.queue.pop(0)
        return True

    def flush_all(self):
        while self.flush_one():
            pass

    def save_summary(self, report):
        self.flush_all()
        payload = {key: value for key, value in report.items()
                   if key not in ('events', 'errors', 'snapshots', 'start_tick')}
        payload['events'] = []
        payload['errors'] = []
        payload['snapshots'] = []
        self._sd_speed()
        try:
            if report.get('state') in ('done', 'error'):
                completed = self.root + '/complete.json'
                with open(completed + '.tmp', 'w') as f:
                    f.write(json.dumps(payload))
                    f.flush()
                try:
                    os.remove(completed)
                except OSError:
                    pass
                os.rename(completed + '.tmp', completed)
            target = self.root + '/report.json'
            temporary = target + '.tmp'
            with open(temporary, 'w') as f:
                f.write(json.dumps(payload))
                f.flush()
            try:
                os.remove(target)
            except OSError:
                pass
            os.rename(temporary, target)
        except OSError as exc:
            raise SDRemoved('SD summary write failed: ' + str(exc))
        finally:
            self._display_speed()
