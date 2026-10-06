"""Exercise Scan against real USB enumeration, never opening its transport."""
import json
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lbj_manager as manager

ports = [p for p in manager.serial.tools.list_ports.comports() if manager.is_pico_port(p)]
if len(ports) != 1:
    raise SystemExit('This test needs exactly one enumerated Pico; no device was opened.')
expected = ports[0].device
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '/private/tmp/lbj-live-scan.json')
result = {'transport_opened': False, 'port': expected, 'checks': []}
guards = [mock.patch.object(manager.serial, 'Serial', side_effect=AssertionError('Transport forbidden')),
          mock.patch.object(manager, 'run_command', side_effect=AssertionError('Device commands forbidden')),
          mock.patch.object(manager, 'download_history', side_effect=AssertionError('Download forbidden'))]
for guard in guards:
    guard.start()
app = manager.LBJManager()


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    result['checks'].append(name)
    print('LIVE_SCAN_PASS', name, flush=True)


def exercise():
    try:
        check('startup confirms actual Pico automatically selected', '已自动选中 Pico' in app.device_status.cget('text') and expected in app.device_status.cget('text'))
        # Recreate a Pico attached after the initial empty startup scan.
        app.port_var.set(manager.PLACEHOLDER_PORT)
        app._scan_initialized = True
        app.refresh_ports(silent=True)
        check('background scan leaves selection untouched', app.port_var.get() == manager.PLACEHOLDER_PORT)
        check('background scan identifies actual Pico', expected in app.pico_candidate_ports)
        check('actions disabled before explicit choice', app.action_btn.cget('state') == 'disabled')
        app.refresh_btn.invoke()
        check('Scan button selects actual Pico', app.port_var.get() == expected)
        check('Scan success prominent beside selector', '已自动选中 Pico' in app.device_status.cget('text'))
        app.refresh_ports(silent=True)
        check('periodic scan preserves prominent success', '已自动选中 Pico' in app.device_status.cget('text'))
        check('firmware and inspection enabled', app.action_btn.cget('state') == app.test_btn.cget('state') == 'normal')
        check('history uses same selected Pico', app.history.port_var.get() == expected and app.history.read_pico_btn.cget('state') == 'normal')
        check('Scan result visible in footer', expected in app.footer_status.cget('text') and '扫描完成' in app.footer_status.cget('text'))
        check('scan has no serial owner', app.tasks.active is None)
        manager.serial.Serial.assert_not_called()
        manager.run_command.assert_not_called()
        manager.download_history.assert_not_called()
        check('no device transport or command opened', True)
        result['passed'] = True
    except Exception as exc:
        result['failure'] = str(exc)
    finally:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        app._on_close()


app.after(500, exercise)
app.mainloop()
for guard in reversed(guards):
    guard.stop()
if not result.get('passed'):
    raise SystemExit(result.get('failure', 'No result'))
