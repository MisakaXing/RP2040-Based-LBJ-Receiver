"""Real Tk GUI smoke/stress run with simulated USB and history only.

Never opens a real receiver transport or writes device firmware.
Usage: python3 lbj_manager/tests/gui_smoke.py [output directory]
"""
import json
import os
import pathlib
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lbj_manager as manager
import solder_check as inspection

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '/private/tmp/lbj-manager-gui')
out.mkdir(parents=True, exist_ok=True)
ports = [SimpleNamespace(device='SIMULATED_PICO', vid=0x2E8A, product='Pico')]
patches = [mock.patch.object(manager.serial.tools.list_ports, 'comports', side_effect=lambda: list(ports)),
           mock.patch.object(manager.messagebox, 'showinfo'),
           mock.patch.object(manager.messagebox, 'showerror'),
           mock.patch.object(manager.messagebox, 'showwarning'),
           mock.patch.object(manager.messagebox, 'askyesno', return_value=True)]
for patch in patches: patch.start()

rows = []
for i in range(9999):
    rows.append(json.dumps({'t': '2026-10-06 12:34',
        'd': {'basic': {'train_no': str(i % 1000), 'speed_kmh': '---' if i % 2 else '7'},
              'extended': {'class_tag': '0D', 'loco_type': '轨道探伤车-04782A',
                           'lat': "39°30.0'N" if i % 3 else '',
                           'lon': "116°15.0'E" if i % 3 else ''}}}, ensure_ascii=False))
fixture = out / 'simulated-9999.jsonl'
fixture.write_text('\n'.join(rows), encoding='utf-8')
result = {'checks': [], 'ui_errors': [], 'devices_touched': False}
app = manager.LBJManager()
app.report_callback_exception = lambda *exc: result['ui_errors'].append(str(exc))
started = time.monotonic()
heartbeat = {'last': time.monotonic(), 'maximum': 0, 'count': 0, 'enabled': False}


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    result['checks'].append(name)
    print('GUI_PASS', name, flush=True)


def heartbeat_tick():
    now = time.monotonic()
    if heartbeat['enabled']:
        heartbeat['maximum'] = max(heartbeat['maximum'], now - heartbeat['last'])
        heartbeat['count'] += 1
    heartbeat['last'] = now
    app.after(5, heartbeat_tick)


def screenshot(name):
    enabled = heartbeat['enabled']
    heartbeat['enabled'] = False
    try:
        app.update_idletasks()  # Capture the current label text, not a queued redraw.
        import Quartz
        import subprocess
        windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
        window = next(w for w in windows if w.get('kCGWindowOwnerPID') == os.getpid()
                      and w.get('kCGWindowLayer') == 0
                      and w.get('kCGWindowBounds', {}).get('Height', 0) > 400)
        subprocess.run(['screencapture', '-x', '-l', str(window['kCGWindowNumber']), str(out / (name + '.png'))], check=True)
    except Exception as exc:
        result.setdefault('screenshot_warnings', []).append(str(exc))
    finally:
        heartbeat['last'] = time.monotonic()
        heartbeat['enabled'] = enabled


def wait_for(condition, next_step, deadline=None):
    deadline = deadline or time.monotonic() + 15
    if condition():
        guarded(next_step)
    elif time.monotonic() > deadline:
        fail('GUI operation timed out')
    else:
        app.after(20, wait_for, condition, next_step, deadline)


def fail(error):
    result['failure'] = str(error)
    result['elapsed_seconds'] = time.monotonic() - started
    (out / 'gui-result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
    app.destroy()


def guarded(callback):
    try:
        callback()
    except Exception as exc:
        fail(exc)


def begin():
    check('one Tk root', manager.tk._default_root is app)
    check('one shared device selection', app.history.port_var is app.port_var)
    check('single simulated Pico selected', app.port_var.get() == 'SIMULATED_PICO')
    check('startup selection has prominent automatic confirmation', '已自动选中 Pico' in app.device_status.cget('text'))
    check('startup automatic selection reported in footer', '已自动选中 Pico' in app.footer_status.cget('text'))
    ports.clear()
    app.refresh_ports(silent=True)
    app.refresh_btn.invoke()
    check('scan button reports no device', '未发现 Pico' in app.footer_status.cget('text'))
    ports.append(SimpleNamespace(device='SIMULATED_PICO', vid=0x2E8A, product='Pico'))
    app.refresh_ports(silent=True)
    check('late attach still waits for explicit scan', app.port_var.get() == manager.PLACEHOLDER_PORT)
    app.refresh_btn.invoke()
    check('scan button auto-selects sole attached Pico', app.port_var.get() == 'SIMULATED_PICO')
    check('scan button enables firmware and history actions', app.action_btn.cget('state') == 'normal' and app.history.read_pico_btn.cget('state') == 'normal')
    check('scan button shows completed result', '扫描完成：已自动选中 Pico' in app.footer_status.cget('text'))
    check('scan result shown next to device selector', '已自动选中 Pico' in app.device_status.cget('text'))
    app.refresh_ports(silent=True)
    check('automatic scan does not erase success confirmation', '已自动选中 Pico' in app.device_status.cget('text'))
    screenshot('device-page')
    check('history navigation', app.show_page('历史记录'))
    heartbeat['enabled'] = True
    result['history_load_started'] = time.monotonic()
    with mock.patch.object(manager.filedialog, 'askopenfilename', return_value=str(fixture)):
        app.history.load_json_file()
    wait_for(lambda: not app.history._loading_data, loaded)


def loaded():
    result['history_load_seconds'] = round(time.monotonic() - result.pop('history_load_started'), 3)
    check('9999 rows parsed and rendered', len(app.history.log_data) == len(app.history.tree.get_children()) == 9999)
    check('minute/date history survives', app.history.log_data[-1]['time'] == '2026-10-06 12:34')
    check('unknown speed not displayed as zero', app.history.log_data[1]['speed'] == '---')
    check('UI continued processing during 9999 rows', heartbeat['count'] > 10)
    screenshot('history-9999')
    app.history.train_no_entry.insert(0, '0D13')
    app.history.apply_filter()
    wait_for(lambda: not app.history._loading_data, filtered)


def filtered():
    check('train prefix filtering', all('0D13' in r['train_no'] for r in app.history.displayed_data))
    check('filtered rows reflect full backing history', len(app.history.displayed_data) == len(app.history.tree.get_children()) < 9999)
    app.history.reset_filter()
    wait_for(lambda: not app.history._loading_data, reset_done)


def reset_done():
    check('reset restores all rows', len(app.history.tree.get_children()) == 9999)
    check('selection does not duplicate map markers', len(app.history.map_widget.canvas_marker_list) == 1)
    app.geometry('1120x800')
    app.after(200, lambda: guarded(resize_done))


def resize_done():
    check('minimum window size', app.winfo_width() == 1120)
    check('shared device header remains visible', app.port_menu.winfo_ismapped())
    check('coordinates fit inside compact details', app.history.coordinate_value.winfo_y() + app.history.coordinate_value.winfo_height() <= app.history.detail_panel.winfo_height())
    check('latest timestamp keeps date', app.history.latest_value.cget('text') == '2026-10-06 12:34')
    screenshot('history-minimum-size')
    app.geometry('1320x900')
    selection_stress(0)


def selection_stress(index):
    if index == 150:
        check('150 changing selections leave at most one marker', len(app.history.map_widget.canvas_marker_list) <= 1)
        check('150 changing selections preserve correct detail', app.history.detail_time_value.cget('text') == '2026-10-06 12:34')
        for _ in range(40):
            app.show_page('设备管理')
            app.show_page('历史记录')
        check('80 page switches retain history and one root', len(app.history.log_data) == 9999 and manager.tk._default_root is app)
        device_operations()
        return
    record_id = str(index)
    app.history.tree.selection_set(record_id)
    app.history.on_tree_select(None)
    app.after(1, lambda: guarded(lambda: selection_stress(index + 1)))


def device_operations():
    app.show_page('设备管理')
    app.branch_var.set(manager.WIRELESS_CHANNEL_LABEL)
    app._on_branch_selected(manager.WIRELESS_CHANNEL_LABEL)
    check('branch switches to W', app.active_profile['branch'] == 'Wireless-Enabled')
    app.branch_var.set(manager.STANDARD_CHANNEL_LABEL)
    app._on_branch_selected(manager.STANDARD_CHANNEL_LABEL)
    check('branch switches back to ordinary', app.active_profile['branch'] == 'main')
    ports.clear()
    app.refresh_ports()
    check('unplug disables updater', app.action_btn.cget('state') == 'disabled')
    check('unplug disables history read', app.history.read_pico_btn.cget('state') == 'disabled')
    ports.append(SimpleNamespace(device='SECOND_PICO', vid=0x2E8A, product='Pico'))
    app.refresh_ports()
    check('new device not silently selected', app.port_var.get() == manager.PLACEHOLDER_PORT)
    ports.append(SimpleNamespace(device='THIRD_PICO', vid=0x2E8A, product='Pico'))
    app.refresh_btn.invoke()
    check('scan button multiple devices require choice', app.port_var.get() == manager.PLACEHOLDER_PORT and '发现 2 台 Pico' in app.footer_status.cget('text'))
    ports.pop()
    app.refresh_btn.invoke()
    check('manual selection shared across tools', app.history.port_var.get() == 'SECOND_PICO')
    check('manual selection enables operations', app.action_btn.cget('state') == 'normal')
    with mock.patch.object(manager.serial.tools.list_ports, 'comports', side_effect=OSError('simulated failure')):
        app.refresh_btn.invoke()
    check('scan failure disables firmware and read actions', app.action_btn.cget('state') == 'disabled' and app.history.read_pico_btn.cget('state') == 'disabled')
    app.refresh_btn.invoke()
    check('scan retry restores device actions', app.action_btn.cget('state') == 'normal')
    simulated_history_read()


transfer_gate = threading.Event()
download_patch = None


def simulated_history_read():
    global download_patch
    def download(port, progress, status):
        assert port == 'SECOND_PICO'
        status('模拟传输；不会打开实际串口')
        progress(0, 100)
        transfer_gate.wait(5)
        progress(100, 100)
        return manager.HistoryDownload(rows[1].encode(), 'simulated-test-sha')
    download_patch = mock.patch.object(manager, 'download_history', side_effect=download)
    download_patch.start()
    app.show_page('历史记录')
    app.history.start_pico_read()
    check('history owns shared serial lease', app.tasks.active.owner == '读取历史')
    check('firmware disabled during history transfer', app.action_btn.cget('state') == 'disabled')
    check('device selector disabled during history transfer', app.port_menu.cget('state') == 'disabled')
    check('second history task not started', app.history.read_pico_btn.cget('state') == 'disabled')
    with mock.patch.object(manager.LBJManager, '_update_worker') as worker:
        app.start_update_process()
        check('busy firmware entry cannot launch worker', not worker.called)
    app._on_close()
    check('busy close refused', app.winfo_exists())
    transfer_gate.set()
    wait_for(lambda: app.tasks.active is None and not app.history._loading_data, transfer_done)


class FakeWorker:
    def __init__(self, *args, **kwargs):
        self.jobs, self.events = queue.Queue(), queue.Queue()
        self.cancel = threading.Event()
    def start(self): pass


def transfer_done():
    download_patch.stop()
    check('history releases shared lease', app.tasks.active is None)
    check('history result loaded after simulated recovery', len(app.history.log_data) == 1)
    check('device controls restored', app.action_btn.cget('state') == 'normal')
    app.show_page('设备管理')
    with mock.patch.object(inspection, 'InspectionWorker', FakeWorker):
        app.start_hardware_test()
    panel = app.inspection_window
    check('inspection lives in main app', panel.master is app.console_panel)
    check('inspection owns serial lease', app.tasks.active is not None)
    check('cannot navigate away from unfinished inspection', not app.show_page('历史记录'))
    device = {'board': 'Waveshare RP2350B PLUS W with RP2350', 'sn': 'TEST-ONLY', 'version': 'test', 'cpu': 150000000}
    panel.handle('connect', device)
    values = {key: True for key, label in inspection.CORE_ITEMS}
    values.update(Wireless=True, Battery_V=4.6)
    for key, label in inspection.CORE_ITEMS + (inspection.WIRELESS_ITEM,):
        panel.handle('core_step', {'key': key, 'state': 'pass', 'detail': 'SIMULATED'})
    panel.handle('core', (values, 'This is a simulated UI test, not a device hardware result.'))
    check('core pass waits for user confirmation', panel.primary.cget('text') == '确认结果，继续')
    check('W diagnosis reports VSYS not GP41', 'VSYS' in panel.results[0]['detail'])
    screenshot('inspection-simulated')
    panel.handle('closed', 'SIMULATED: no real hardware transport was opened')
    check('inspection completion releases lease', app.tasks.active is None)
    check('inspection report stays in same page', panel.finished)
    app.dismiss_inspection()
    app.after(100, lambda: guarded(report_dismissed))


def report_dismissed():
    check('dismiss report restores running log', app.inspection_window is None and app.log_textbox.winfo_ismapped())
    app.show_page('历史记录')
    app.after(200, lambda: guarded(finish))


def finish():
    check('no Tk callback exceptions', not result['ui_errors'])
    result['maximum_heartbeat_gap_ms'] = round(heartbeat['maximum'] * 1000, 1)
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    result['passed'] = True
    (out / 'gui-result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print('GUI_ALL_PASSED', json.dumps(result, ensure_ascii=False), flush=True)
    app._on_close()


app.after(5, heartbeat_tick)
app.after(1000, lambda: guarded(begin))
app.after(45000, lambda: fail('overall timeout'))
app.mainloop()
for patch in reversed(patches): patch.stop()
if result.get('failure'):
    raise SystemExit(result['failure'])
