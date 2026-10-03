#!/usr/bin/env python3
"""Load standalone diagnostic into Pico RAM and read reports from SD only."""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
import zlib

from serial.tools import list_ports
from mpremote.transport_serial import SerialTransport
from hardware_profiles import detect_variant, resolve_variant, model_label, serial_number, PINS

HERE = pathlib.Path(__file__).resolve().parent
RUNS = HERE / 'runs'
FILES = (('receiver.py', '_diag_receiver_ns'),
         ('display.py', '_diag_display_ns'),
         ('sdcard.py', '_diag_sd_ns'),
         ('sd_report.py', '_diag_sd_report_ns'),
         ('firmware.py', '_diag_fw_ns'))
MARK = 'LBJ_DIAG_JSON:'


def find_port(wanted=None):
    ports = list(list_ports.comports())
    if wanted:
        match = next((p for p in ports if p.device == wanted), None)
        if not match:
            raise RuntimeError('串口不存在：' + wanted)
        if match.vid != 0x2E8A:
            raise RuntimeError('不是识别到的 Pico USB VID 0x2E8A：' + wanted)
        return wanted
    matches = [p.device for p in ports if p.vid == 0x2E8A]
    if len(matches) != 1:
        raise RuntimeError('需要恰好一个 Pico USB 串口；当前候选：' + repr(matches))
    return matches[0]


def transport(port, reset):
    conn = SerialTransport(port, timeout=5)
    try:
        conn.enter_raw_repl(soft_reset=reset, timeout_overall=15)
    except BaseException:
        conn.close()
        raise
    return conn


def execute(conn, source, timeout=15):
    output, error = conn.exec_raw(source, timeout=timeout)
    if error:
        raise RuntimeError('设备执行失败：' + error.decode('utf-8', 'replace'))
    return output.decode('utf-8', 'replace')


def probe(conn):
    output = execute(conn, "import os,machine,gc,ujson; print('LBJ_DIAG_JSON:'+ujson.dumps({'board':os.uname().machine,'uid':machine.unique_id().hex(),'ram':gc.mem_free(),'micropython':os.uname().release}))")
    info = parse_json(output)
    resolve_variant(info['board'])
    info['sn'] = serial_number(info['uid'])
    info['detected_variant'] = detect_variant(info['board'])
    return info


def inspect_device(port=None):
    conn = transport(find_port(port), reset=False)
    try:
        info = probe(conn)
        info['original_system'] = original_system(conn)
        return info
    finally:
        # Resume the original program after this identity-only query.
        try:
            conn.exit_raw_repl()
            conn.serial.write(b'\x04')
        finally:
            conn.close()


def original_system(conn):
    source = """import ujson
_identity={'present':False,'version':'','edition':''}
try:
 _file=open('/main.py','r')
 _identity['present']=True
 for _line in _file:
  _line=_line.strip()
  if _line.startswith('Program_ver') and '=' in _line:
   _identity['version']=_line.split('=',1)[1].split('#',1)[0].strip().strip(chr(34)).strip(chr(39))
  elif _line.startswith('is_es_ver') and '=' in _line:
   _identity['edition']='ES' if _line.split('=',1)[1].split('#',1)[0].strip()=='1' else 'Release'
 _file.close()
except OSError:
 pass
print('LBJ_DIAG_JSON:'+ujson.dumps(_identity))"""
    return parse_json(execute(conn, source))


def parse_json(output):
    for line in output.splitlines():
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    raise RuntimeError('设备未返回预期的结构化数据：' + output[-300:])


def source_chunks(path):
    data = path.read_bytes()
    if path.name != 'firmware.py':
        return [data]
    # MicroPython needs a contiguous allocation while compiling source.
    # Top-level function boundaries let each part compile independently.
    starts = [0] + [match.start() for match in re.finditer(rb'^def ', data, re.M)] + [len(data)]
    blocks = [data[a:b] for a, b in zip(starts, starts[1:]) if b > a]
    chunks = []
    current = bytearray()
    for block in blocks:
        if current and len(current) + len(block) > 3300:
            chunks.append(bytes(current))
            current = bytearray()
        current.extend(block)
    if current:
        chunks.append(bytes(current))
    return chunks


def send_module(conn, path, global_name):
    chunks = source_chunks(path)
    execute(conn, '%s={}' % global_name)
    for index, data in enumerate(chunks, 1):
        compressed = zlib.compress(data, 9).hex()
        command = "import deflate,io,binascii,gc; exec(deflate.DeflateIO(io.BytesIO(binascii.unhexlify('%s')),deflate.ZLIB).read(),%s); gc.collect(); print('LBJ_DIAG_LOADED:%s %d/%d',gc.mem_free())" % (
            compressed, global_name, path.name, index, len(chunks))
        output = execute(conn, command, timeout=45)
        if 'LBJ_DIAG_LOADED:' not in output:
            raise RuntimeError('RAM 组件加载失败：' + path.name + ' ' + output[-300:])
        print(output.strip())


def start(args):
    if args.seconds != 3600 and not args.development_short_run:
        raise RuntimeError('正式检测固定为 3600 秒；短时测试须加 --development-short-run')
    if not 1 <= args.seconds <= 7200:
        raise RuntimeError('运行时间超出范围')
    port = find_port(args.port)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    conn = transport(port, reset=True)
    try:
        info = probe(conn)
        requested = getattr(args, 'variant', 'auto')
        variant = resolve_variant(info['board'], requested)
        installed = original_system(conn)
        print('设备：', info)
        print('原系统：', installed)
        print('检测型号：', model_label({'hardware_variant': variant}), PINS[variant])
        print(MARK + json.dumps(dict(info, original_system=installed, hardware_variant=variant,
                                    hardware_selection=requested), ensure_ascii=False))
        for filename, global_name in FILES:
            send_module(conn, HERE / filename, global_name)
        locos = json.loads((HERE / 'locos.json').read_text(encoding='utf-8'))
        execute(conn, "_diag_receiver_ns['LOCO_TYPES']=" + repr(locos))
        execute(conn, "_diag_fw_ns['_diag_receiver_ns']=_diag_receiver_ns; _diag_fw_ns['_diag_display_ns']=_diag_display_ns; _diag_fw_ns['_diag_sd_ns']=_diag_sd_ns; _diag_fw_ns['_diag_sd_report_ns']=_diag_sd_report_ns")
        execute(conn, "_diag_fw_ns['ORIGINAL_SYSTEM']=" + repr(installed))
        execute(conn, "_diag_fw_ns['RUN_ID']=" + repr(stamp + '-' + info['uid']))
        execute(conn, "_diag_fw_ns['HARDWARE_SELECTION']=" + repr(requested) +
                "; _diag_fw_ns['_configure_hardware'](" + repr(variant) + ")")
        launch = "LBJ_DIAG_REPORT=_diag_fw_ns['run'](duration_s=%d,interactive=%s,require_detached=%s)" % (
            args.seconds, 'False' if args.skip_interactive else 'True',
            'False' if args.development_short_run else 'True')
        conn.exec_raw_no_follow(launch)
    finally:
        conn.close()
    RUNS.mkdir(exist_ok=True)
    run_dir = RUNS / (stamp + '-' + info['uid'])
    run_dir.mkdir()
    manifest = {'port': port, 'board': info['board'], 'uid': info['uid'],
                'hardware_variant': variant, 'hardware_selection': requested,
                'hardware_auto_variant': info['detected_variant'], 'hardware_pins': PINS[variant],
                'original_system': installed,
                'host_started_epoch': time.time(), 'duration_s': args.seconds,
                'interactive': not args.skip_interactive, 'development': args.development_short_run,
                'require_detached': not args.development_short_run,
                'firmware_files': {name: zlib.crc32((HERE / name).read_bytes()) & 0xffffffff
                                   for name, _ in FILES + (('locos.json', ''),)}}
    (run_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('RAM 检测已启动。可拔 USB 并改用电池供电；请保持设备不断电。')
    print('拔 USB 后在设备上检查 SD 卡；通过才启动射频测试。')
    print('完成后从 SD 卡的 LBJ_DIAG/%s/report.json 读取报告。' % run_dir.name)


def is_sd_report_path(path):
    path = pathlib.Path(path).resolve()
    parts = path.parts
    if path.name not in ('report.json', 'complete.json') or len(parts) < 5 or parts[-3] != 'LBJ_DIAG':
        return False
    return (str(path).startswith('/Volumes/') or str(path).startswith('/media/') or
            str(path).startswith('/run/media/'))


def read_sd_report(path):
    path = pathlib.Path(path).resolve()
    if not is_sd_report_path(path):
        raise ValueError('请选择已挂载 SD 卡中 LBJ_DIAG/<运行目录>/report.json 或 complete.json')
    completed = path.parent / 'complete.json'
    if completed.exists():
        path = completed
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('format') != 1 or not report.get('sn'):
        raise ValueError('不是有效的 LBJ 检测报告')
    for kind in ('events', 'errors', 'snapshots'):
        rows = []
        journal = path.parent / (kind + '.jsonl')
        if journal.exists():
            with journal.open(encoding='utf-8') as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        report[kind + '_omitted'] = report.get(kind + '_omitted', 0) + 1
        report[kind] = rows
        if len(rows) != report.get(kind + '_written', len(rows)):
            report.setdefault('incomplete', []).append(kind + '_journal_mismatch')
            if report.get('verdict') == 'PASS':
                report['verdict'] = 'INCOMPLETE'
    if report.get('state') not in ('done', 'error'):
        report['verdict'] = 'INCOMPLETE'
        report.setdefault('incomplete', []).append('run_not_finished')
    return report


def installed_version(info):
    if not info.get('present'):
        return '未检测到原系统 main.py'
    version = info.get('version')
    return ('v' + str(version) + ' ' + info.get('edition', '')).strip() if version else '版本未标注'


def average_received_rssi(report):
    count = report.get('received_rssi_count')
    value = report.get('received_rssi_avg_dbm')
    if count is None:
        return '旧报告未记录'
    if not count or value is None:
        return '无有效接收记录'
    return '%.2f dBm（%d 条）' % (value, count)


def analyze(report):
    h = report.get('final_health') or {}
    total = h.get('codewords', 0)
    error_rate = ('%.2f%%' % (100 * h.get('uncorrectable', 0) / total)) if total else '未观测到码字'
    def memory_value(key):
        value = report.get(key)
        return str(value) if value is not None else '未记录'
    lines = ['LBJ 独立检测详细报告', '=' * 40,
             'SN：%s' % report['sn'],
             '机器型号：%s / %s' % (model_label(report), report['board']),
             '型号选择：%s；检测引脚：%s' % (report.get('hardware_selection', '旧报告未记录'),
                                             report.get('hardware_pins', '旧报告未记录')),
             '原系统版本：%s' % installed_version(report.get('original_system', {})),
             'MicroPython：%s' % report.get('micropython', '未知'),
             '结论：%s，射频测试 %s / %s 秒' % (report['verdict'], report['elapsed_s'], report['duration_s']),
             '每条接收记录的平均 RSSI：%s' % average_received_rssi(report),
             '不可纠错：%s / %s 码字，比例 %s' % (h.get('uncorrectable', 0), total, error_rate),
             '电压降低：%s V（%s → %s V）' % (report['battery_drop_v'], report['battery_start_v'], report['battery_end_v']),
             '按主程序电压公式换算的电量降低：%s 个百分点（%s%% → %s%%）' % (
                 report['battery_drop_percent_points'], report['battery_start_percent'], report['battery_end_percent']),
             '失败条件：' + (', '.join(report.get('failures', [])) or '无'),
             '运行提示：' + (', '.join(report.get('warnings', [])) or '无'),
             '未验证条件：' + (', '.join(report.get('incomplete', [])) or '无'),
             '堆内存最低剩余：%s' % (
                 '%d 字节' % report['heap_free_min']
                 if report.get('heap_free_min') is not None else '未记录'),
             '每分钟剩余内存：最低 %s，最高 %s，平均 %s 字节（%s 个样本）' % (
                 memory_value('heap_free_sample_min_bytes'),
                 memory_value('heap_free_sample_max_bytes'),
                 memory_value('heap_free_sample_avg_bytes'),
                 report.get('heap_free_sample_count', 0)),
             ('射频判定：FIFO 满计数 ≥10、不可纠错比例 >50% 才判 FAIL；恢复次数仅提示。'
              if report.get('verdict_policy') else '射频判定：该报告使用旧版固件规则。'),
             '', '功能检查：']
    for name, value in report['checks'].items():
        lines.append('  %s: %s — %s' % (name, value['status'], value['detail']))
    lines.extend(['', '射频统计：'])
    for key, value in h.items():
        lines.append('  %s: %s' % (key, value))
    lines.extend(['', '接收条目：%d，分类：%s' % (report['received'], report['received_types']),
                  'RP2040 片上最高温：%s °C' % report['chip_temp_max_c'],
                  '逐分钟样本：%d，事件：%d（省略 %d），错误：%d（省略 %d），详情截短：%d' % (
                      len(report['snapshots']), len(report['events']), report['events_omitted'],
                      len(report['errors']), report['errors_omitted'], report.get('details_truncated', 0)),
                  '说明：FIFO 满计数是溢出风险指标，不等于可精确测得的丢失字节数；',
                  '无外部已知信号源时不能证明接收灵敏度或计算空口真实丢包率。',
                  '', '逐分钟样本：'])
    for sample in report['snapshots']:
        lines.append('  t=%ss V=%s T=%s RSSI=%s RX=%s 剩余内存=%s 字节 历史最低=%s 字节 health=%s' % (
            sample['s'], sample['battery_v'], sample['chip_temp_c'], sample['rssi_dbm'],
            sample['received'], sample.get('heap_free_bytes', '未记录'),
            sample.get('heap_free_min_bytes', '未记录'), sample['health']))
    lines.extend(['', '全部保留事件：'])
    lines += ['  ' + repr(item) for item in report['events']]
    lines.extend(['', '全部保留错误：'])
    lines += ['  ' + repr(item) for item in report['errors']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start_p = sub.add_parser('start', help='加载到 RAM 并启动')
    start_p.add_argument('--port')
    start_p.add_argument('--seconds', type=int, default=3600)
    start_p.add_argument('--development-short-run', action='store_true')
    start_p.add_argument('--skip-interactive', action='store_true')
    start_p.add_argument('--variant', choices=('auto', 'standard', 'wireless'), default='auto')
    probe_p = sub.add_parser('probe', help='读取设备身份并自动识别普通版 / W 版')
    probe_p.add_argument('--port')
    import_p = sub.add_parser('import-sd', help='只读 SD 卡中的报告')
    import_p.add_argument('report_json')
    args = parser.parse_args()
    try:
        if args.command == 'start':
            start(args)
        elif args.command == 'probe':
            print(MARK + json.dumps(inspect_device(args.port), ensure_ascii=False))
        else:
            print(analyze(read_sd_report(args.report_json)))
    except Exception as exc:
        print('错误：', exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
