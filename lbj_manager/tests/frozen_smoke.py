"""Verify a built arm64 .app and a faithfully copied .app, without USB I/O."""
import json
import os
import pathlib
import select
import subprocess
import sys
import time

import Quartz

app = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2]).resolve()
output.mkdir(parents=True, exist_ok=True)
result = {'checks': [], 'devices_touched': False, 'developer_certificate_used': False}


def check(name, valid):
    if not valid:
        raise AssertionError(name)
    result['checks'].append(name)
    print('APP_PASS', name, flush=True)


def run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def launch(bundle, label):
    executable = bundle / 'Contents/MacOS/LBJ Manager'
    check(label + ' executable permission', os.access(executable, os.X_OK))
    check(label + ' arm64 architecture', run(['lipo', '-archs', str(executable)]).stdout.strip() == 'arm64')
    check(label + ' local bundle integrity', run(['codesign', '--verify', '--deep', '--strict', str(bundle)]).returncode == 0)
    links = [path for path in bundle.rglob('*') if path.is_symlink()]
    check(label + ' framework links preserved', len(links) > 10 and all(path.exists() for path in links))
    dnd_libraries = list((bundle / 'Contents').rglob('libtkdnd*.dylib'))
    check(label + ' native file-drop library bundled', bool(dnd_libraries) and
          all(run(['lipo', '-archs', str(path)]).stdout.strip() == 'arm64' for path in dnd_libraries))
    check(label + ' native file-drop Tcl scripts bundled', any((bundle / 'Contents').rglob('tkdnd_macosx.tcl')))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PYTHON', 'DYLD_', 'LD_'))}
    helper = subprocess.run([str(executable), 'mpremote_internal', '--help'], cwd='/private/tmp',
                            env=env, capture_output=True, text=True, timeout=20)
    check(label + ' embedded mpremote starts', helper.returncode == 0 and 'mpremote' in helper.stdout.lower())
    process = subprocess.Popen([str(executable)], cwd='/private/tmp', env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start = time.monotonic()
    startup_stderr = ''
    try:
        window = None
        while time.monotonic() - start < 15:
            if process.poll() is not None:
                break
            windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
            window = next((w for w in windows if w.get('kCGWindowOwnerPID') == process.pid
                           and w.get('kCGWindowLayer') == 0
                           and w.get('kCGWindowBounds', {}).get('Width', 0) > 500
                           and w.get('kCGWindowBounds', {}).get('Height', 0) > 400), window)
            time.sleep(.25)
        check(label + ' real window remains alive 15 seconds', process.poll() is None and window is not None)
        screenshot = run(['screencapture', '-x', '-l', str(window['kCGWindowNumber']), str(output / (label + '.png'))])
        check(label + ' window screenshot', screenshot.returncode == 0)
    finally:
        # Separate actual startup errors from diagnostics caused by the test's
        # forced SIGTERM (e.g. a partially constructed map PhotoImage finalizer).
        chunks = []
        while select.select([process.stderr], [], [], 0)[0]:
            chunk = os.read(process.stderr.fileno(), 65536)
            if not chunk:
                break
            chunks.append(chunk)
        startup_stderr = b''.join(chunks).decode('utf-8', 'replace')
        if process.poll() is None:
            process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        result[label + '_startup_stderr'] = startup_stderr
        result[label + '_forced_shutdown_stderr'] = stderr
    check(label + ' no Python startup exception', 'Traceback' not in startup_stderr and 'Failed to execute script' not in startup_stderr)


try:
    launch(app, 'built-app')
    copied = output / 'portable-copy/LBJ Manager.app'
    copied.parent.mkdir(parents=True, exist_ok=True)
    check('ditto app copy succeeded', run(['ditto', str(app), str(copied)]).returncode == 0)
    launch(copied, 'copied-app')
    result['passed'] = True
finally:
    (output / 'app-result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
print('APP_ALL_PASSED', json.dumps(result, ensure_ascii=False))
