"""RAM-only production inspection. Launched by flash_and_report.py, never saved on Pico FS."""
import gc
import json
import machine
import os
import time

VERSION = 1
GREEN, RED, WHITE, BLACK, YELLOW = 0x07E0, 0xF800, 0xFFFF, 0, 0xFFE0
SNAPSHOT_MS = 60_000
DISPLAY_MS = 1_000
MAX_EVENTS = 300
MAX_ERRORS = 100
MAX_LOG_DETAIL = 2048
HEAP_RESERVE_BYTES = 35_000
SNAPSHOT_RESERVE_BYTES = 1_500
ACTIVE_SD = None
ORIGINAL_SYSTEM = {}
RUN_ID = 'UNSET'
HARDWARE_VARIANT = 'standard'
HARDWARE_SELECTION = 'auto'
POWER_GPIO, BATTERY_GPIO, TEMP_CHANNEL = 28, 27, 4
VBUS_PIN = 24


def _now():
    return time.ticks_ms()


def _diff(a, b):
    return time.ticks_diff(a, b)


def _sn():
    n = int.from_bytes(machine.unique_id(), 'big')
    value = ''
    while n:
        n, rem = divmod(n, 36)
        value = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'[rem] + value
    return ('000000000000' + value)[-12:]


def _log_reserve(report):
    # Formal runs stream snapshots to SD after USB disconnect.
    if report.get('require_detached'):
        return HEAP_RESERVE_BYTES
    remaining = max(0, (report['duration_s'] + 59) // 60 + 2 - len(report['snapshots']))
    return HEAP_RESERVE_BYTES + remaining * SNAPSHOT_RESERVE_BYTES


def _record(report, kind, detail, error=False):
    key, limit = ('errors', MAX_ERRORS) if error else ('events', MAX_EVENTS)
    free = gc.mem_free()
    low = report.get('heap_free_min')
    report['heap_free_min'] = free if low is None or free < low else low
    # Before SD is mounted, short-lived display allocations can leave very
    # little free heap. Reclaim them before discarding a preflight log entry.
    if ACTIVE_SD is None and free <= _log_reserve(report) + 1_000:
        gc.collect()
        free = gc.mem_free()
    if ACTIVE_SD is not None:
        detail = str(detail)
        if len(detail) > MAX_LOG_DETAIL:
            detail = detail[:MAX_LOG_DETAIL]
            report['details_truncated'] += 1
        item = [int(_diff(_now(), report['start_tick']) // 1000), kind, detail]
        if ACTIVE_SD.enqueue(key, item):
            report[key + '_written'] += 1
            if report['state'] == 'interactive':
                ACTIVE_SD.flush_all()
        else:
            report[key + '_omitted'] += 1
    elif len(report[key]) < limit and free > _log_reserve(report) + 1_000:
        detail = str(detail)
        if len(detail) > MAX_LOG_DETAIL:
            detail = detail[:MAX_LOG_DETAIL]
            report['details_truncated'] += 1
        item = [int(_diff(_now(), report['start_tick']) // 1000), kind, detail]
        report[key].append(item)
    else:
        report[key + '_omitted'] += 1


def _check(report, name, fn):
    try:
        detail = fn()
        report['checks'][name] = {'status': 'pass', 'detail': detail}
        _record(report, 'check', name + ': ' + str(detail))
        return True
    except Exception as exc:
        report['checks'][name] = {'status': 'fail', 'detail': repr(exc)}
        _record(report, 'check_failed', name + ': ' + repr(exc), True)
        return False


def _battery():
    gate = machine.Pin(14, machine.Pin.OUT, value=1)
    adc = machine.ADC(machine.Pin(BATTERY_GPIO))
    try:
        gate.value(0)
        time.sleep_ms(8)
        raw = sum(adc.read_u16() for _ in range(8)) // 8
    finally:
        gate.value(1)
    voltage = raw / 65535 * 3.3 * 2 + 0.174
    if not 2.5 <= voltage <= 4.5:
        raise ValueError('battery voltage %.3f V, raw %d' % (voltage, raw))
    return voltage


def _temp():
    # RP2350B has eight external ADC channels; its core sensor is channel 8.
    channel = getattr(machine.ADC, 'CORE_TEMP', TEMP_CHANNEL)
    raw = machine.ADC(channel).read_u16()
    return round(27 - ((raw * 3.3 / 65535) - 0.706) / 0.001721, 2)


def _battery_percent(voltage):
    # Same linear display estimate and clamp as rp2040-main-program/main.py.
    value = int((voltage - 3.4) / (4.2 - 3.4) * 100)
    return max(0, min(100, value))


def _rtc():
    i2c = machine.I2C(0, sda=machine.Pin(0), scl=machine.Pin(1), freq=400000)
    devices = i2c.scan()
    if 0x68 in devices:
        data = i2c.readfrom_mem(0x68, 0, 7)
        model = 'DS3231'
    elif 0x51 in devices:
        data = i2c.readfrom_mem(0x51, 2, 7)
        model = 'PCF8563'
    else:
        raise OSError('RTC not found; I2C=' + repr(devices))
    return {'model': model, 'registers': list(data)}


def _sd():
    machine.Pin(9, machine.Pin.OUT, value=1)
    spi = machine.SPI(1, baudrate=100000, sck=machine.Pin(10),
                      mosi=machine.Pin(11), miso=machine.Pin(8, machine.Pin.IN, machine.Pin.PULL_UP))
    cs = machine.Pin(7, machine.Pin.OUT, value=1)
    try:
        card = _diag_sd_ns['SDCard'](spi, cs)
        sectors = card.ioctl(4, None)
        block = bytearray(512)
        card.readblocks(0, block)
        if sectors <= 0 or len(block) != 512:
            raise OSError('SD capacity/sector invalid')
        return {'sectors': sectors, 'first_sector_prefix': bytes(block[:16]).hex()}
    finally:
        cs.value(1)
        spi.init(baudrate=20_000_000, polarity=0, phase=0)


def _open_sd():
    global ACTIVE_SD
    store = _diag_sd_report_ns['SDReport'](_diag_sd_ns['SDCard'], RUN_ID)
    ACTIVE_SD = store
    return {'sectors': store.card.ioctl(4, None), 'folder': store.root}


def _display():
    machine.Pin(7, machine.Pin.OUT, value=1)
    spi = machine.SPI(1, baudrate=20_000_000, sck=machine.Pin(10),
                      mosi=machine.Pin(11), miso=machine.Pin(8))
    lcd = _diag_display_ns['ILI9341'](spi, cs=9, dc=12, rst=13)
    machine.Pin(6, machine.Pin.OUT, value=0)
    return lcd


def _screen(lcd, lines, color=WHITE):
    lcd.fill(BLACK)
    for index, line in enumerate(lines[:9]):
        lcd.draw_gbk(str(line).encode('ascii', 'replace'), 8, 8 + index * 25,
                     color if index == 0 else WHITE, BLACK, 2 if index == 0 else 1)


def _line(lcd, index, value, color=WHITE):
    y = 8 + index * 25
    lcd.fill_rect(0, y, 320, 18, BLACK)
    lcd.draw_gbk(str(value).encode('ascii', 'replace'), 8, y, color, BLACK,
                 2 if index == 0 else 1)


def _stress_frame(lcd):
    lcd.fill(BLACK)
    lcd.fill_rect(0, 0, 320, 40, 0x1082)
    lcd.draw_gbk(b'RF STRESS', 10, 10, WHITE, 0x1082, 2)
    labels = (b'RX EVENTS', b'PIO WORDS', b'FIFO FULL / DROP',
              b'UNCORRECTABLE', b'RECOVERIES', b'LAST RX TYPE')
    for index, label in enumerate(labels):
        x = 7 + (index % 2) * 157
        y = 56 + (index // 2) * 48
        lcd.fill_rect(x, y, 149, 43, 0x18C3)
        lcd.fill_rect(x, y, 3, 43, GREEN if index < 2 else 0x4C5F)
        lcd.draw_gbk(label, x + 9, y + 5, 0x9CD3, 0x18C3)
    lcd.fill_rect(7, 205, 306, 28, 0x1082)


def _stress_value(lcd, index, value):
    x = 7 + (index % 2) * 157
    y = 56 + (index // 2) * 48
    lcd.fill_rect(x + 8, y + 21, 136, 17, 0x18C3)
    lcd.draw_gbk(str(value)[:16].encode('ascii', 'replace'),
                 x + 9, y + 22, WHITE, 0x18C3, 2 if len(str(value)) <= 8 else 1)


def _result_screen(lcd, report):
    verdict = report['verdict']
    accent = GREEN if verdict == 'PASS' else (YELLOW if verdict == 'INCOMPLETE' else RED)
    lcd.fill(BLACK)
    lcd.fill_rect(0, 0, 320, 47, accent)
    lcd.draw_gbk(('RESULT ' + verdict).encode(), 12, 14, BLACK, accent, 2)
    lcd.fill_rect(12, 64, 296, 62, 0x18C3)
    lcd.draw_gbk(b'VOLTAGE DROP', 24, 77, 0x9CD3, 0x18C3)
    lcd.draw_gbk(('%s V' % report['battery_drop_v']).encode(),
                 24, 99, WHITE, 0x18C3, 2)
    lcd.fill_rect(12, 136, 296, 62, 0x18C3)
    lcd.draw_gbk(b'BATTERY DISPLAY DROP', 24, 149, 0x9CD3, 0x18C3)
    lcd.draw_gbk(('%s pct points' % report['battery_drop_percent_points']).encode(),
                 24, 171, WHITE, 0x18C3)
    recoveries = (report.get('final_health') or {}).get('recoveries', 0)
    if recoveries:
        lcd.draw_gbk(('RF RECOVERY %d  NOTICE' % recoveries).encode(),
                     12, 202, YELLOW, BLACK)
    footer = b'POWER OFF / READ SD ON PC' if report.get('sd_folder') else b'NO SD REPORT'
    lcd.draw_gbk(footer, 27, 215, YELLOW, BLACK)


def _wait_key(pin, timeout_ms):
    start = _now()
    # Require stable release before accepting a press.
    released = False
    stable = _now()
    previous = pin.value()
    while _diff(_now(), start) < timeout_ms:
        value = pin.value()
        if value != previous:
            previous, stable = value, _now()
        if _diff(_now(), stable) >= 60:
            if value:
                released = True
            elif released:
                return True
        time.sleep_ms(10)
    return False


KEY_LAYOUT = (('OK', 5, 4, 48), ('UP', 4, 4, 94),
              ('DOWN', 3, 4, 140), ('MENU', 2, 4, 186), ('POWER', 28, 246, 48))
BG, PANEL, MUTED, ACCENT = 0x0861, 0x18E3, 0x9CF3, 0x4DFB


def _configure_hardware(variant):
    global HARDWARE_VARIANT, POWER_GPIO, BATTERY_GPIO, TEMP_CHANNEL, VBUS_PIN, KEY_LAYOUT
    if variant not in ('standard', 'wireless'):
        raise ValueError('unknown hardware variant')
    HARDWARE_VARIANT = variant
    POWER_GPIO, BATTERY_GPIO, TEMP_CHANNEL = (42, 41, 8) if variant == 'wireless' else (28, 27, 4)
    VBUS_PIN = 'WL_GPIO2' if variant == 'wireless' else 24
    KEY_LAYOUT = KEY_LAYOUT[:-1] + (('POWER', POWER_GPIO, 246, 48),)


def _vbus():
    return machine.Pin(VBUS_PIN, machine.Pin.IN)


def _text(lcd, text, x, y, color=WHITE, bg=BG, scale=1):
    lcd.draw_gbk(str(text).encode('ascii', 'replace'), x, y, color, bg, scale)


def _page(lcd, number, title, subtitle):
    lcd.fill(BG)
    lcd.fill_rect(12, 12, 27, 22, ACCENT)
    _text(lcd, number, 18, 19, BG, ACCENT)
    _text(lcd, title, 49, 15, WHITE, BG, 2)
    _text(lcd, subtitle, 82, 54, MUTED)


def _key_tile(lcd, name, x, y, passed=False):
    color = GREEN if passed else PANEL
    lcd.fill_rect(x, y, 70, 36, color)
    lcd.fill_rect(x, y, 3, 36, GREEN if passed else MUTED)
    _text(lcd, name, x+8, y+8, BG if passed else WHITE, color)
    if passed:
        for offset in range(4):
            lcd.fill_rect(x+47+offset, y+22+offset, 2, 2, BG)
        for offset in range(8):
            lcd.fill_rect(x+51+offset, y+25-offset, 2, 2, BG)


def _next_hint(lcd, enabled=True, label='NEXT'):
    color = ACCENT if enabled else MUTED
    lcd.fill_rect(240, 42, 80, 63, BG)
    _key_tile(lcd, 'POWER', 246, 48, enabled)
    _text(lcd, label, 248, 91, color)
    for offset in range(7):
        lcd.fill_rect(307+offset, 91+offset//2, 1, 7-offset, color)


def _precheck_row(lcd, index, title, state, detail='', count=3):
    y, height = (72 + index * 30, 27) if count == 4 else (78 + index * 38, 34)
    color = GREEN if state == 'pass' else (RED if state == 'fail' else ACCENT)
    lcd.fill_rect(82, y, 150, height, PANEL)
    lcd.fill_rect(82, y, 3, height, color)
    _text(lcd, title, 90, y+3, WHITE, PANEL)
    _text(lcd, {'pass': 'OK', 'fail': 'FAIL', 'running': '...'}[state],
          187, y+3, color, PANEL)
    _text(lcd, str(detail)[:18], 90, y+(16 if count == 4 else 19), MUTED, PANEL)


def _precheck_footer(lcd, first, second='', color=ACCENT):
    lcd.fill_rect(80, 192, 240, 45, BG)
    _text(lcd, first, 82, 197, color)
    if second:
        _text(lcd, second, 82, 215, color)


def _radio_precheck():
    spi = machine.SPI(0, baudrate=2_000_000, polarity=0, phase=0,
                      sck=machine.Pin(18), mosi=machine.Pin(19), miso=machine.Pin(16))
    cs = machine.Pin(17, machine.Pin.OUT, value=1)
    rst = machine.Pin(15, machine.Pin.OUT, value=1)
    data = machine.Pin(21, machine.Pin.IN, machine.Pin.PULL_UP)
    clock = machine.Pin(20, machine.Pin.IN, machine.Pin.PULL_UP)
    def read_reg(register):
        cs.value(0)
        try:
            spi.write(bytes((register & 0x7f,)))
            return spi.read(1)[0]
        finally:
            cs.value(1)
    def write_reg(register, value):
        cs.value(0)
        try:
            spi.write(bytes((register | 0x80, value)))
        finally:
            cs.value(1)
    try:
        rst.value(0)
        time.sleep_ms(10)
        rst.value(1)
        time.sleep_ms(10)
        version = read_reg(0x42)
        if version in (0, 0xff):
            raise OSError('SX1276 SPI version 0x%02X' % version)
        write_reg(0x01, 0x00)
        time.sleep_ms(10)
        write_reg(0x01, 0x01)
        time.sleep_ms(10)
        for reg, value in ((0x06, 0xD2), (0x07, 0x51), (0x08, 0x99),
                           (0x31, 0x00), (0x40, 0x00)):
            write_reg(reg, value)
        write_reg(0x01, 0x05)
        time.sleep_ms(50)
        counts = [0, 0]
        def on_clock(pin):
            if counts[0] < 2000:
                counts[0] += 1
                counts[1] += data.value()
        clock.irq(trigger=machine.Pin.IRQ_RISING, handler=on_clock)
        time.sleep_ms(1000)
        clock.irq(handler=None)
        total, ones = counts
        if not total:
            raise OSError('RF DCLK: no rising edge')
        if not 0 < ones < total:
            raise OSError('RF DATA stuck: ones=%d/%d' % (ones, total))
        return {'version': '0x%02X' % version, 'edges': total,
                'ones': ones, 'zeros': total-ones,
                'ones_percent': round(100 * ones / total, 1)}
    finally:
        clock.irq(handler=None)
        try:
            write_reg(0x01, 0x01)
        except Exception:
            pass


def _wireless_precheck():
    station = None
    try:
        import network
        interface = getattr(network.WLAN, 'IF_STA', None)
        if interface is None:
            interface = getattr(network, 'STA_IF', None)
        if interface is None:
            raise OSError('STA interface missing')
        station = network.WLAN(interface)
        station.active(True)
        deadline = time.ticks_add(_now(), 1500)
        while not station.active() and _diff(deadline, _now()) > 0:
            time.sleep_ms(25)
        if not station.active():
            raise OSError('CYW43 did not start')
        # The Waveshare board routes VBUS_DET to CYW43 GPIO2.
        _vbus().value()
        return 'CYW43 STA ready'
    finally:
        if station is not None:
            try:
                station.active(False)
            except Exception as exc:
                raise OSError('STA shutdown: ' + str(exc))


def _precheck(report, lcd, interactive, defer_power=False):
    items = (('rtc', 'RTC', _rtc), ('battery', 'BATTERY', _battery),
             ('radio_precheck', 'RF MODULE', _radio_precheck))
    if HARDWARE_VARIANT == 'wireless':
        items += (('wireless', 'WIRELESS', _wireless_precheck),)
    count = len(items)
    power = machine.Pin(POWER_GPIO, machine.Pin.IN, machine.Pin.PULL_UP)
    while True:
        _page(lcd, '00', 'PRECHECK', 'RTC BAT RF WIFI' if count == 4 else 'RTC / BATTERY / RF')
        for index, (_, title, _) in enumerate(items):
            _precheck_row(lcd, index, title, 'running', 'WAIT', count)
        passed = True
        for index, (name, title, fn) in enumerate(items):
            _precheck_row(lcd, index, title, 'running', 'TESTING', count)
            ok = _check(report, name, fn)
            passed = passed and ok
            detail = report['checks'][name]['detail']
            if name == 'rtc' and ok:
                detail = detail['model']
            elif name == 'battery' and ok:
                detail = '%.3f V' % detail
            elif name == 'radio_precheck' and ok:
                detail = 'REG VERSION ' + detail['version']
            _precheck_row(lcd, index, title, 'pass' if ok else 'fail', detail, count)
            gc.collect()
        _next_hint(lcd, enabled=not (passed and defer_power),
                   label=('UNPLUG' if defer_power else 'NEXT >') if passed else 'RETRY >')
        if passed and defer_power:
            _precheck_footer(lcd, 'UNPLUG USB', 'INSERT SD CARD')
        else:
            _precheck_footer(lcd, '%d / %d PASS' % (count, count) if passed else 'CHECK FAILED',
                             color=GREEN if passed else RED)
        if not interactive:
            return passed
        if passed and defer_power:
            return True
        if not _wait_key(power, 90_000):
            raise OSError('precheck POWER timeout')
        if passed:
            return True
        _record(report, 'precheck', 'operator retry')


class _KeyEdges:
    def __init__(self, gpios):
        self.pins = [machine.Pin(pin, machine.Pin.IN, machine.Pin.PULL_UP) for pin in gpios]
        self.level = [p.value() for p in self.pins]
        self.changed = [_now()] * len(gpios)
        self.armed = [False] * len(gpios)

    def poll(self):
        now = _now()
        edges = []
        for i, pin in enumerate(self.pins):
            value = pin.value()
            if value != self.level[i]:
                self.level[i], self.changed[i] = value, now
            elif _diff(now, self.changed[i]) >= 60:
                if value:
                    self.armed[i] = True
                elif self.armed[i]:
                    self.armed[i] = False
                    edges.append(i)
        return edges


def _screen_test(report, lcd):
    for color in (RED, GREEN, 0x001F, WHITE, BLACK):
        lcd.fill(color)
        time.sleep_ms(1200)
    _page(lcd, '02', 'DISPLAY', 'RGB / WHITE / BLACK')
    _next_hint(lcd, label='PASS >')
    _text(lcd, 'All full-screen', 82, 112)
    _text(lcd, 'colors correct?', 82, 130)
    _key_tile(lcd, 'MENU', 4, 186)
    _text(lcd, 'MENU = FAIL', 82, 195, MUTED)
    keys = _KeyEdges((POWER_GPIO, 2))
    start, status = _now(), 'unverified'
    while _diff(_now(), start) < 90_000:
        edges = keys.poll()
        if 1 in edges:
            status = 'fail'
            break
        if 0 in edges:
            status = 'pass'
            break
        time.sleep_ms(10)
    report['checks']['screen'] = {'status': status, 'detail': 'full-screen RGB/white/black; POWER pass, MENU fail'}
    _record(report, 'screen', status)


def _buzzer_test(report, lcd):
    _page(lcd, '03', 'BUZZER', 'OK = ONE BEEP')
    _key_tile(lcd, 'OK', 4, 48)
    _next_hint(lcd, False, 'HEARD >')
    _text(lcd, 'Press OK to play', 82, 113)
    _text(lcd, 'POWER if heard', 82, 133)
    _key_tile(lcd, 'MENU', 4, 186)
    _text(lcd, 'MENU = NOT HEARD', 82, 195, MUTED)
    keys = _KeyEdges((5, POWER_GPIO, 2))
    buzzer = machine.Pin(22, machine.Pin.OUT, value=0)
    count, status, start = 0, 'unverified', _now()
    try:
        while _diff(_now(), start) < 120_000:
            edges = keys.poll()
            if 0 in edges:
                buzzer.value(1)
                time.sleep_ms(120)
                buzzer.value(0)
                count += 1
                lcd.fill_rect(82, 157, 155, 18, BG)
                _text(lcd, 'BEEPS: %d' % count, 82, 160, ACCENT)
                _next_hint(lcd, True, 'HEARD >')
                _record(report, 'buzzer_pulse', str(count))
            if 2 in edges:
                status = 'fail'
                break
            if 1 in edges and count:
                status = 'pass'
                break
            time.sleep_ms(10)
    finally:
        buzzer.value(0)
    report['checks']['buzzer'] = {'status': status, 'detail': 'OK pulses=%d; POWER heard confirmation' % count}


def _five_keys(report, lcd):
    _page(lcd, '04', 'FIVE KEYS', 'PRESS EACH KEY')
    for name, _, x, y in KEY_LAYOUT:
        _key_tile(lcd, name, x, y)
    _text(lcd, '0 / 5', 105, 109, WHITE, BG, 2)
    _text(lcd, 'Release then press', 82, 147, MUTED)
    _text(lcd, 'Green = passed', 82, 165, MUTED)
    keys = _KeyEdges(tuple(k[1] for k in KEY_LAYOUT))
    passed, start = [False] * 5, _now()
    while _diff(_now(), start) < 120_000 and not all(passed):
        for index in keys.poll():
            if not passed[index]:
                passed[index] = True
                name, _, x, y = KEY_LAYOUT[index]
                _key_tile(lcd, name, x, y, True)
                lcd.fill_rect(100, 105, 128, 26, BG)
                _text(lcd, '%d / 5' % sum(passed), 105, 109, GREEN, BG, 2)
                _record(report, 'key', name + '=pass')
        time.sleep_ms(10)
    for i, (name, gpio, _, _) in enumerate(KEY_LAYOUT):
        report['checks']['key_' + name.lower()] = {'status': 'pass' if passed[i] else 'fail', 'detail': 'GP%d' % gpio}
    _next_hint(lcd)
    lcd.fill_rect(80, 145, 161, 40, BG)
    _text(lcd, 'POWER to continue', 82, 152, ACCENT)
    _wait_key(machine.Pin(POWER_GPIO, machine.Pin.IN, machine.Pin.PULL_UP), 60_000)


def _interactive(report, lcd):
    _screen_test(report, lcd)
    _buzzer_test(report, lcd)
    _five_keys(report, lcd)


def _sd_presence_interactive(report, lcd):
    power = machine.Pin(POWER_GPIO, machine.Pin.IN, machine.Pin.PULL_UP)
    _next_hint(lcd, label='CHECK >')
    _precheck_footer(lcd, 'INSERT SD CARD', 'POWER TO CHECK')
    while True:
        if not _wait_key(power, 90_000):
            raise OSError('SD presence POWER timeout; tests not started')
        _precheck_footer(lcd, 'CHECKING SD', 'PLEASE WAIT')
        if _check(report, 'sd_present', _sd):
            _precheck_footer(lcd, 'SD DETECTED', 'NEXT: SD TEST', GREEN)
            return
        _next_hint(lcd, label='RETRY >')
        _precheck_footer(lcd, 'SD NOT FOUND', 'INSERT / POWER RETRY', RED)
        time.sleep_ms(2_000)


def _sd_interactive(report, lcd):
    power = machine.Pin(POWER_GPIO, machine.Pin.IN, machine.Pin.PULL_UP)
    attempt = 0
    while True:
        attempt += 1
        _page(lcd, '01', 'SD CARD', 'READ / WRITE')
        _next_hint(lcd, label='TEST >')
        _text(lcd, 'Read + write test', 82, 117)
        _text(lcd, 'Attempt %d' % attempt, 82, 140, MUTED)
        if not _wait_key(power, 90_000):
            raise OSError('SD test POWER timeout; RF not started')
        if _check(report, 'sd_storage', _open_sd):
            sectors = report['checks']['sd_storage']['detail']['sectors']
            _page(lcd, '01', 'SD CARD', 'TEST PASSED')
            _text(lcd, '%d MiB' % (sectors // 2048), 82, 118, GREEN, BG, 2)
            _text(lcd, 'Read + write OK', 82, 155, MUTED)
            return
        _page(lcd, '01', 'SD CARD', 'TEST FAILED')
        _next_hint(lcd, label='RETRY >')
        _text(lcd, 'Check card / format', 82, 120, RED)


def _uncorrectable_percent(health):
    total = health.get('codewords', 0)
    return round(100.0 * health.get('uncorrectable', 0) / total, 2) if total else None


def _uncorrectable_text(health):
    percent = _uncorrectable_percent(health)
    return '%d %s' % (health.get('uncorrectable', 0),
                      ('%.1f%%' % percent) if percent is not None else '--%')


def _sample(report, rx, elapsed_s):
    gc.collect()
    health = rx.get_health_snapshot()
    voltage = None
    temp = None
    try:
        voltage = _battery()
    except Exception as exc:
        _record(report, 'battery_sample', repr(exc), True)
    try:
        temp = _temp()
    except Exception as exc:
        _record(report, 'temp_sample', repr(exc), True)
    free = gc.mem_free()
    low = report.get('heap_free_min')
    report['heap_free_min'] = free if low is None or free < low else low
    sampled_low = report.get('heap_free_sample_min_bytes')
    sampled_high = report.get('heap_free_sample_max_bytes')
    report['heap_free_sample_min_bytes'] = free if sampled_low is None or free < sampled_low else sampled_low
    report['heap_free_sample_max_bytes'] = free if sampled_high is None or free > sampled_high else sampled_high
    report['heap_free_sample_sum_bytes'] += free
    report['heap_free_sample_count'] += 1
    report['heap_free_sample_avg_bytes'] = round(
        report['heap_free_sample_sum_bytes'] / report['heap_free_sample_count'])
    sample = {'s': elapsed_s, 'battery_v': round(voltage, 3) if voltage is not None else None,
              'chip_temp_c': temp,
              'heap_free_bytes': free,
              'heap_free_min_bytes': report['heap_free_min'],
              'vbus_present': bool(_vbus().value()),
              'battery_percent': _battery_percent(voltage) if voltage is not None else None,
              'rssi_dbm': rx.get_rssi(), 'health': health,
              'uncorrectable_percent': _uncorrectable_percent(health),
              'received': report['received'], 'received_types': report['received_types'].copy(),
              'last_rx_type': report['last_rx_type'],
              'event_count': report['events_written'] + len(report['events'])}
    if ACTIVE_SD is not None:
        if ACTIVE_SD.enqueue('snapshots', sample):
            report['snapshots_written'] += 1
        else:
            report['snapshots_omitted'] += 1
    else:
        report['snapshots'].append(sample)
    if voltage is not None:
        if report['battery_start_v'] is None:
            report['battery_start_v'] = round(voltage, 3)
            report['battery_start_percent'] = _battery_percent(voltage)
        report['battery_end_v'] = round(voltage, 3)
        report['battery_end_percent'] = _battery_percent(voltage)
        change = round(report['battery_start_v'] - voltage, 3)
        report['battery_drop_v'] = 0.0 if change == 0 else change
        report['battery_drop_percent_points'] = report['battery_start_percent'] - report['battery_end_percent']
        low = report['battery_min_v']
        report['battery_min_v'] = voltage if low is None or voltage < low else low
        high = report['battery_max_v']
        report['battery_max_v'] = voltage if high is None or voltage > high else high
    if temp is not None:
        high = report['chip_temp_max_c']
        report['chip_temp_max_c'] = temp if high is None or temp > high else high
    return sample


def _safe_sample(report, rx, elapsed_s):
    try:
        return _sample(report, rx, elapsed_s)
    except MemoryError:
        report['snapshots_omitted'] += 1
        gc.collect()
        return None


def _verdict(report, duration_s):
    failures = []
    incomplete = []
    warnings = []
    for key, value in report['checks'].items():
        if value['status'] == 'fail':
            failures.append(key)
        elif value['status'] != 'pass':
            incomplete.append(key)
    if report['elapsed_s'] < duration_s:
        failures.append('early_stop')
    if (report['events_omitted'] or report['errors_omitted'] or
            report['details_truncated'] or report['snapshots_omitted']):
        incomplete.append('log_truncated')
    if report['battery_start_v'] is None or report['battery_end_v'] is None:
        incomplete.append('battery_trend_unmeasured')
    health = report.get('final_health') or {}
    for key in ('raw_dropped', 'spi_errors', 'callback_errors'):
        if health.get(key, 0):
            failures.append(key)
    if health.get('fifo_full_hits', 0) >= 10:
        failures.append('fifo_full_hits')
    codewords = health.get('codewords', 0)
    if codewords and health.get('uncorrectable', 0) * 2 > codewords:
        failures.append('uncorrectable_over_50_percent')
    if health.get('recoveries', 0):
        warnings.append('radio_recoveries=%d' % health['recoveries'])
    if report['require_detached'] and not report['usb_detached']:
        failures.append('usb_not_detached')
    if report['usb_reconnects']:
        failures.append('usb_reconnected_during_stress')
    if report.get('sd_removed'):
        failures.append('sd_removed_during_stress')
    if report['battery_min_v'] is not None:
        if report['battery_min_v'] < 3.5:
            failures.append('battery_critical')
        elif report['battery_min_v'] < 3.7:
            incomplete.append('battery_low_warning')
    if not health.get('words', 0):
        incomplete.append('no_observed_radio_words')
    elif not 0 < health.get('bits_one', 0) < health.get('bits_total', 0):
        failures.append('data_line_stuck')
    if health.get('words', 0) and not health.get('syncs', 0):
        incomplete.append('no_observed_transmission')
    report['failures'] = failures
    report['incomplete'] = incomplete
    report['warnings'] = warnings
    report['verdict'] = 'FAIL' if failures else ('INCOMPLETE' if incomplete else 'PASS')


def _stress_details(report, lcd, rx, health):
    if health['word_age_ms'] > 2000:
        report['no_word_10s_samples'] += 1
        _record(report, 'no_radio_word', 'word_age_ms=' + str(health['word_age_ms']))
    lines = (str(report['received']), str(health['words']),
             '%d / %d' % (health['fifo_full_hits'], health['raw_dropped']),
             _uncorrectable_text(health), str(health['recoveries']),
             report['last_rx_type'])
    lcd.fill_rect(12, 211, 294, 16, 0x1082)
    lcd.draw_gbk(('RSSI %s  V %s  T %s' % (
        str(rx.get_rssi())[:8], str(report['battery_min_v'])[:5],
        str(report['chip_temp_max_c'])[:5])).encode('ascii', 'replace'),
        13, 211, WHITE, 0x1082)
    return lines


def _run_stress(report, lcd, rx, vbus, duration_s, require_detached):
    report['state'] = 'running'
    run_start = _now()
    previous_sample = time.ticks_add(run_start, -SNAPSHOT_MS)
    previous_display = time.ticks_add(run_start, -DISPLAY_MS)
    previous_detail = time.ticks_add(run_start, -10_000)
    previous_detail_line = time.ticks_add(run_start, -150)
    detail_lines = ()
    detail_cursor = 0
    previous_recoveries = 0
    usb_high = False
    previous_sd_check = time.ticks_add(run_start, -1_000)
    previous_sd_flush = run_start
    while _diff(_now(), run_start) < duration_s * 1000:
        rx.tick()
        now = _now()
        elapsed = _diff(now, run_start) // 1000
        if ACTIVE_SD is not None:
            if _diff(now, previous_sd_check) >= 1_000:
                ACTIVE_SD.check_present()
                previous_sd_check = now
            if ACTIVE_SD.queue and _diff(now, previous_sd_flush) >= 200 and rx.sm.rx_fifo() == 0:
                ACTIVE_SD.flush_one()
                previous_sd_flush = now
        if rx.radio_recoveries != previous_recoveries:
            _record(report, 'radio_recovery', str(rx.radio_recoveries), True)
            previous_recoveries = rx.radio_recoveries
        if _diff(now, previous_sample) >= SNAPSHOT_MS:
            _safe_sample(report, rx, elapsed)
            previous_sample = now
            gc.collect()
        if _diff(now, previous_display) >= DISPLAY_MS:
            if require_detached:
                if vbus.value() and not usb_high:
                    report['usb_reconnects'] += 1
                    _record(report, 'usb', 'VBUS returned during RF stress', True)
                    usb_high = True
                elif not vbus.value():
                    usb_high = False
            remaining = max(0, duration_s - elapsed)
            lcd.fill_rect(192, 5, 120, 30, 0x1082)
            lcd.draw_gbk(('%02d:%02d' % (remaining // 60, remaining % 60)).encode(),
                         197, 10, GREEN, 0x1082, 2)
            previous_display = now
        if _diff(now, previous_detail) >= 10_000:
            health = rx.get_health_snapshot()
            detail_lines = _stress_details(report, lcd, rx, health)
            detail_cursor = 0
            previous_detail = now
        if detail_cursor < len(detail_lines) and _diff(now, previous_detail_line) >= 150:
            _stress_value(lcd, detail_cursor, detail_lines[detail_cursor])
            detail_cursor += 1
            previous_detail_line = now
    report['elapsed_s'] = _diff(_now(), run_start) // 1000
    _safe_sample(report, rx, report['elapsed_s'])
    if ACTIVE_SD is not None:
        ACTIVE_SD.flush_all()
    report['final_health'] = rx.get_health_snapshot()
    report['uncorrectable_percent'] = _uncorrectable_percent(report['final_health'])
    h = report['final_health']
    words = h.get('words', 0)
    signal_ok = words > 0 and 0 < h.get('bits_one', 0) < h.get('bits_total', 0)
    report['checks']['radio_clock_data'] = {
        'status': 'pass' if signal_ok else ('unverified' if not words else 'fail'),
        'detail': 'words=%d bits_one=%d/%d' % (
            h.get('words', 0), h.get('bits_one', 0), h.get('bits_total', 0))}
    report['state'] = 'done'


def _new_report(duration_s, require_detached, start):
    report = {'format': VERSION, 'sn': _sn(), 'board': os.uname().machine,
              'micropython': os.uname().release, 'duration_s': duration_s,
              'start_tick': start, 'elapsed_s': 0, 'state': 'preflight',
              'checks': {}, 'snapshots': [], 'events': [], 'errors': [],
              'events_omitted': 0, 'errors_omitted': 0, 'details_truncated': 0,
              'snapshots_omitted': 0, 'events_written': 0, 'errors_written': 0,
              'snapshots_written': 0,
              'heap_free_min': None,
              'heap_free_sample_min_bytes': None, 'heap_free_sample_max_bytes': None,
              'heap_free_sample_sum_bytes': 0, 'heap_free_sample_count': 0,
              'heap_free_sample_avg_bytes': None, 'received': 0,
              'verdict_policy': 'fifo_full_hits>=10; uncorrectable>50%; recoveries=warning',
              'received_types': {}, 'last_rx_type': 'none',
              'received_rssi_sum_dbm': 0.0, 'received_rssi_count': 0,
              'received_rssi_avg_dbm': None,
              'battery_min_v': None, 'battery_max_v': None,
              'battery_start_v': None, 'battery_end_v': None, 'battery_drop_v': None,
              'battery_start_percent': None, 'battery_end_percent': None,
              'battery_drop_percent_points': None,
              'chip_temp_max_c': None, 'final_health': None, 'verdict': None,
              'no_word_10s_samples': 0}
    report['require_detached'] = require_detached
    report['hardware_variant'] = HARDWARE_VARIANT
    report['hardware_selection'] = HARDWARE_SELECTION
    report['hardware_auto_variant'] = 'wireless' if 'RP2350' in report['board'].upper() else 'standard'
    report['hardware_model'] = 'LBJ W' if HARDWARE_VARIANT == 'wireless' else 'LBJ standard'
    report['hardware_pins'] = {'power': POWER_GPIO, 'battery_adc': BATTERY_GPIO,
                               'temp_adc': TEMP_CHANNEL, 'vbus': VBUS_PIN}
    report['usb_detached'] = False
    report['usb_reconnects'] = 0
    report['original_system'] = ORIGINAL_SYSTEM
    report['sd_folder'] = None
    report['sd_removed'] = False
    return report


def _received_rssi(report, entry):
    try:
        value = float(str(entry.get('rssi', '')).replace('dBm', '').strip())
    except (TypeError, ValueError):
        return
    if -200 <= value <= 0:
        report['received_rssi_sum_dbm'] += value
        report['received_rssi_count'] += 1


def _preflight(report, interactive):
    lcd = _display()
    report['checks']['screen_spi'] = {'status': 'pass', 'detail': 'ILI9341 commands sent'}
    _precheck(report, lcd, interactive, report['require_detached'])
    return lcd


def _wait_detach(report, lcd, vbus):
    report['state'] = 'await_usb_detach'
    wait_start = _now()
    low_since = None
    while _diff(_now(), wait_start) < 600_000:
        if vbus.value():
            low_since = None
        elif low_since is None:
            low_since = _now()
        elif _diff(_now(), low_since) >= 500:
            report['usb_detached'] = True
            return
        time.sleep_ms(20)
    raise OSError('USB remained connected for 10 minutes')


def _prepare_sd(report, lcd, vbus):
    _wait_detach(report, lcd, vbus)
    _sd_presence_interactive(report, lcd)
    _sd_interactive(report, lcd)
    report['sd_folder'] = ACTIVE_SD.root
    for key in ('events', 'errors'):
        for item in report[key]:
            if ACTIVE_SD.enqueue(key, item):
                report[key + '_written'] += 1
            else:
                report[key + '_omitted'] += 1
            ACTIVE_SD.flush_all()
        report[key] = []
    report['state'] = 'interactive'
    _record(report, 'usb', 'detached before SD check')
    ACTIVE_SD.save_summary(report)


def _operator_tests(report, lcd, interactive):
    if interactive:
        _interactive(report, lcd)
    else:
        for name in ('screen', 'buzzer', 'key_menu', 'key_up', 'key_down', 'key_ok', 'key_power'):
            report['checks'][name] = {'status': 'unverified', 'detail': 'interactive check skipped'}


def run(duration_s=3600, interactive=True, require_detached=True):
    global LBJ_DIAG_REPORT, ACTIVE_SD
    ACTIVE_SD = None
    if duration_s < 1 or duration_s > 7200:
        raise ValueError('duration must be 1..7200 seconds')
    start = _now()
    report = _new_report(duration_s, require_detached, start)
    LBJ_DIAG_REPORT = report
    lcd = None
    rx = None
    try:
        lcd = _preflight(report, interactive)

        def on_rx(entry):
            kind = str(entry.get('type', 'unknown'))
            report['received'] += 1
            _received_rssi(report, entry)
            report['received_types'][kind] = report['received_types'].get(kind, 0) + 1
            report['last_rx_type'] = kind
            # The SD journal is drained outside the receiver callback.
            key = 'errors' if kind == 'error' else 'events'
            limit = MAX_ERRORS if kind == 'error' else MAX_EVENTS
            if ACTIVE_SD is not None and len(ACTIVE_SD.queue) >= 8:
                report[key + '_omitted'] += 1
            elif ACTIVE_SD is None and (len(report[key]) >= limit or gc.mem_free() <= _log_reserve(report) + 4_000):
                report[key + '_omitted'] += 1
            else:
                try:
                    _record(report, 'rx_' + kind, json.dumps(entry), kind == 'error')
                except MemoryError:
                    report[key + '_omitted'] += 1

        vbus = _vbus()
        if require_detached:
            _prepare_sd(report, lcd, vbus)
            _operator_tests(report, lcd, interactive)
            ACTIVE_SD.save_summary(report)
        else:
            report['checks']['sd_storage'] = {'status':'unverified',
                                              'detail':'USB development run; SD unavailable'}
            _operator_tests(report, lcd, interactive)
        _stress_frame(lcd)
        rx = _diag_receiver_ns['LBJReceiver'](loco_file='/locos.json')
        rx.set_callback(on_rx)
        report['checks']['radio_spi'] = {'status': 'pass', 'detail': 'SX1276 version and receiver initialized'}
        _run_stress(report, lcd, rx, vbus, duration_s, require_detached)
    except BaseException as exc:
        report['state'] = 'error'
        report['fatal_error'] = repr(exc)
        if ACTIVE_SD is not None and isinstance(exc, _diag_sd_report_ns['SDRemoved']):
            report['sd_removed'] = True
        _record(report, 'fatal', repr(exc), True)
        if rx:
            report['final_health'] = rx.get_health_snapshot()
    finally:
        _finish_run(report, rx, lcd, start, duration_s)
    return report


def _finish_run(report, rx, lcd, start, duration_s):
    if report['received_rssi_count']:
        report['received_rssi_avg_dbm'] = round(
            report['received_rssi_sum_dbm'] / report['received_rssi_count'], 2)
    if rx:
        try:
            rx.sm.active(0)
            rx._w(0x01, 0x01)
        except Exception as exc:
            _record(report, 'radio_shutdown', repr(exc), True)
    report['elapsed_total_s'] = _diff(_now(), start) // 1000
    _verdict(report, duration_s)
    if report['state'] == 'error' and report['verdict'] != 'FAIL':
        report['verdict'] = 'FAIL'
        report['failures'].append('fatal_error')
    if ACTIVE_SD is not None:
        try:
            ACTIVE_SD.save_summary(report)
        except Exception:
            report['sd_removed'] = True
            report['verdict'] = 'FAIL'
            report['failures'].append('sd_report_write')
            lcd = _retry_sd_save(report, lcd)
    if lcd:
        _result_screen(lcd, report)
    print('LBJ_DIAG_DONE:' + report['verdict'])


def _retry_sd_save(report, lcd):
    if lcd is None:
        return None
    _screen(lcd, ['SD REPORT ERROR', 'Reinsert card', 'POWER: SAVE RETRY'])
    retry_start = _now()
    while _diff(_now(), retry_start) < 600_000:
        if not _wait_key(machine.Pin(POWER_GPIO, machine.Pin.IN, machine.Pin.PULL_UP), 30_000):
            continue
        try:
            ACTIVE_SD.reopen(_diag_sd_ns['SDCard'])
            ACTIVE_SD.save_summary(report)
            report['sd_folder'] = ACTIVE_SD.root
            return lcd
        except Exception:
            _screen(lcd, ['SD REPORT ERROR', 'Check / reinsert card', 'POWER: SAVE RETRY'])
    return None
