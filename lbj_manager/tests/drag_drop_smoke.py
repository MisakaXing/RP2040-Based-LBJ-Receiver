"""Exercise native TkDND's macOS event path with local fixture files.

Uses real Tcl/native drag bindings, not a Python callback-only simulation.
Injects native enter/position/drop events; it does not drive a Finder gesture.
Serial access, firmware workers and history device reads are forbidden.
"""
import json
import pathlib
import sys
import time
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lbj_manager as manager

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '/private/tmp/lbj-drop-test')
out.mkdir(parents=True, exist_ok=True)
result = {'checks': [], 'devices_touched': False, 'ui_errors': []}


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    result['checks'].append(name)
    print('DROP_PASS', name, flush=True)


def forbidden(*args, **kwargs):
    raise AssertionError('File drops must not contact or flash a receiver')


patches = [mock.patch.object(manager.serial.tools.list_ports, 'comports', return_value=[]),
           mock.patch.object(manager.serial, 'Serial', side_effect=forbidden),
           mock.patch.object(manager.LBJManager, 'run_mpremote', side_effect=forbidden),
           mock.patch.object(manager, 'download_history', side_effect=forbidden),
           mock.patch.object(manager.LBJManager, 'start_offline_zip_update', side_effect=forbidden),
           mock.patch.object(manager.messagebox, 'showerror'),
           mock.patch.object(manager.messagebox, 'showwarning')]
for patch in patches:
    patch.start()
app = manager.LBJManager()
app.report_callback_exception = lambda *error: result['ui_errors'].append(str(error))


def wait(condition):
    deadline = time.monotonic() + 10
    while not condition():
        app.update()
        if time.monotonic() > deadline:
            raise AssertionError('Drop operation timed out')
        time.sleep(.01)
    app.update()


def native_drop(widget, *files):
    app.update()
    types = app.tk.call('tkdnd::platform_specific_types', ('DND_Files',))
    data = app.tk.call('list', *map(str, files))
    path = str(widget)
    app.tk.call('tkdnd::macdnd::HandleEnter', path, 'fixture-drag-source', types, data)
    app.tk.call('tkdnd::macdnd::HandlePosition', path, widget.winfo_rootx() + 4, widget.winfo_rooty() + 4)
    action = app.tk.call('tkdnd::macdnd::HandleDrop', path, data)
    app.tk.call('tkdnd::macdnd::HandleLeave')
    app.update()
    return action


try:
    app.update()
    check('native arm64 TkDND loaded', app._dnd_available and bool(app.tk.call('package', 'provide', 'tkdnd')))
    result['tkdnd_version'] = app._dnd_version
    check('ZIP target registered on visible label', bool(app.tk.call('bind', str(app.zip_drop_label._label), '<<Drop>>')))
    check('log target registered on import button', bool(app.tk.call('bind', str(app.history.load_btn._canvas), '<<Drop>>')))
    check('no Pico still permits local ZIP validation', app.offline_zip_btn.cget('state') == 'disabled')
    archive = out / '普通版 {测试} 固件.ZIP'
    profile = manager.get_firmware_profile('main')
    with zipfile.ZipFile(archive, 'w') as package:
        for name in manager.runtime_file_order(profile):
            data = 'Program_ver = "5.12"\nfrom device_protection import DeviceProtection\n' if name == 'main.py' else 'pass\n'
            if name == 'lbj_receiver.py':
                data = 'from pio_dma_rx import PioDmaRx\n'
            package.writestr('repo/rp2040-main-program/' + name, data)
    check('native ZIP drop accepted', native_drop(app.zip_drop_label._label, archive) == 'copy')
    wait(lambda: not app._zip_loading)
    check('ZIP validated and staged without flashing', app._pending_zip_path == str(archive))
    check('ZIP success shown beside flash button', archive.name in app.zip_drop_label.cget('text'))
    check('ZIP still needs explicit click and device', app.offline_zip_btn.cget('text') == '刷入已载入 ZIP' and app.offline_zip_btn.cget('state') == 'disabled')
    app.branch_var.set(manager.WIRELESS_CHANNEL_LABEL)
    app._on_branch_selected(manager.WIRELESS_CHANNEL_LABEL)
    check('branch change clears staged ZIP', app._pending_zip_path is None)
    check('wrong branch drop accepted for validation', native_drop(app.zip_drop_zone._canvas, archive) == 'copy')
    wait(lambda: not app._zip_loading)
    check('wrong firmware branch rejected visibly', app._pending_zip_path is None and 'ZIP 载入失败' in app.footer_status.cget('text'))
    app.branch_var.set(manager.STANDARD_CHANNEL_LABEL)
    app._on_branch_selected(manager.STANDARD_CHANNEL_LABEL)
    bad = out / '损坏.zip'
    bad.write_text('not a zip', encoding='utf-8')
    check('broken ZIP queued for validation', native_drop(app.zip_drop_zone._canvas, bad) == 'copy')
    wait(lambda: not app._zip_loading)
    check('broken ZIP rejected without stale file', app._pending_zip_path is None and '未通过校验' in app.zip_drop_label.cget('text'))
    app.show_page('历史记录')
    log = out / '接收记录 {中文 空格}.jsonl'
    log.write_text(json.dumps({'t': '2026-10-06 12:34', 'd': {'basic': {'train_no': '139', 'speed_kmh': '---'},
        'extended': {'class_tag': '0D', 'loco_type': '轨道探伤车-04782A'}}}, ensure_ascii=False), encoding='utf-8')
    check('native log-label drop accepted', native_drop(app.history.log_drop_label._label, log) == 'copy')
    wait(lambda: not app.history._loading_data)
    check('log imported directly with date and prefix', len(app.history.log_data) == 1 and app.history.log_data[0]['train_no'] == '0D139' and app.history.log_data[0]['time'] == '2026-10-06 12:34')
    check('log source filename visible', log.name in app.history.source_status.cget('text'))
    check('ZIP in log zone rejected', native_drop(app.history.log_drop_zone._canvas, archive) == 'refuse_drop')
    check('wrong-file hint visible', '此区域支持' in app.footer_status.cget('text'))
    check('multiple log drop rejected', native_drop(app.history.load_btn._canvas, log, log) == 'refuse_drop')
    invalid = out / '无效.log'; invalid.write_text('not valid JSON', encoding='utf-8')
    check('invalid log accepted for parser check', native_drop(app.history.load_btn._canvas, invalid) == 'copy')
    wait(lambda: not app.history._loading_data)
    check('invalid log preserves loaded record', len(app.history.log_data) == 1 and app.history.log_data[0]['train_no'] == '0D139')
    lease = app.begin_task('模拟读取', 'SIMULATED_PICO')
    check('device-busy drop rejected', native_drop(app.history.log_drop_label._label, log) == 'refuse_drop')
    app.tasks.release(lease); app.sync_controls()
    check('normal drop resumes after task', native_drop(app.history.load_btn._canvas, log) == 'copy')
    wait(lambda: not app.history._loading_data)
    check('native callbacks had no Tk exceptions', not result['ui_errors'])
    result['passed'] = True
finally:
    app.destroy()
    for patch in reversed(patches):
        patch.stop()
    (out / 'drop-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print('DROP_ALL_PASSED', json.dumps(result, ensure_ascii=False))
