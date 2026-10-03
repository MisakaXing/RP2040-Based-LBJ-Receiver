#!/usr/bin/env python3
"""Independent graphical loader and report reader for the RAM diagnostic."""
import json
import pathlib
import queue
import subprocess
import sys
import threading
from tkinter import filedialog, ttk

import customtkinter as ctk
from serial.tools import list_ports
from charts import TrendCharts
from train_records import received_rows, has_train_number
from hardware_profiles import VARIANT_OPTIONS, PINS, MODELS, resolve_variant, model_label

HERE = pathlib.Path(__file__).resolve().parent
CLI = HERE / 'flash_and_report.py'


def _value(value, suffix=''):
    return '—' if value is None else '%s%s' % (value, suffix)


def _cell(value):
    return '—' if value is None or value == '' else value


class DetailWindow(ctk.CTkToplevel):
    def __init__(self, parent, report):
        super().__init__(parent)
        from flash_and_report import analyze
        self.title('LBJ · 详细数据与日志')
        width = min(self.winfo_screenwidth()-60, 1250)
        height = min(self.winfo_screenheight()-90, 820)
        self.geometry('%dx%d+35+35' % (width, height))
        self.configure(fg_color='#10151b')
        tabs = ctk.CTkTabview(self, fg_color='#1b2630')
        tabs.pack(fill='both', expand=True, padx=16, pady=16)
        health = report.get('final_health') or {}
        checks = '\n'.join('%-22s  %-11s  %s' % (key, value.get('status', ''), value.get('detail', ''))
                           for key, value in sorted(report.get('checks', {}).items()))
        minute_lines = ['分钟    电压/V   电量/%   温度/°C  内存/B    RX累计  FIFO满  不可纠错/%']
        for sample in report.get('snapshots', []):
            minute_lines.append('%5.1f   %7s   %6s   %7s   %8s   %6s   %6s   %s' % (
                sample['s']/60, sample.get('battery_v', '—'), sample.get('battery_percent', '—'),
                sample.get('chip_temp_c', '—'), sample.get('heap_free_bytes', '—'),
                sample.get('received', '—'), (sample.get('health') or {}).get('fifo_full_hits', '—'),
                sample.get('uncorrectable_percent', '—')))
        errors = ('失败：%s\n提示：%s\n未完成：%s\n\n错误日志：\n%s\n\n最终射频统计：\n%s' % (
            ', '.join(report.get('failures', [])) or '无',
            ', '.join(report.get('warnings', [])) or '无',
            ', '.join(report.get('incomplete', [])) or '无',
            '\n'.join(repr(item) for item in report.get('errors', [])) or '无',
            '\n'.join('%s: %s' % pair for pair in sorted(health.items()))))
        pages = (
            ('功能检查', checks), ('每分钟数据', '\n'.join(minute_lines)),
            ('接收事件', '\n'.join(repr(item) for item in report.get('events', [])) or '无'),
            ('错误与射频统计', errors), ('完整原始报告', analyze(report)),
        )
        for title, content in pages:
            page = tabs.add(title)
            box = ctk.CTkTextbox(page, wrap='none', font=ctk.CTkFont(family='Menlo', size=12))
            box.pack(fill='both', expand=True, padx=9, pady=9)
            box.insert('1.0', content)
            box.configure(state='disabled')
        self.lift()


class TrainWindow(ctk.CTkToplevel):
    """Searchable receive ledger; one row for every saved RX callback."""
    COLUMNS = (
        ('number', '序号', 55), ('elapsed', '时间', 68),
        ('type', 'RX TYPE', 150), ('train_no', '车次', 90),
        ('loco', '机车', 160), ('speed', '速度', 70),
        ('km_post', '公里标', 80), ('rssi', 'RSSI', 88),
        ('ric', 'RIC', 125), ('location', '坐标', 245),
    )

    def __init__(self, parent, report):
        super().__init__(parent)
        self.title('LBJ · 接收车次与全部 RX 记录')
        width = min(self.winfo_screenwidth()-60, 1500)
        height = min(self.winfo_screenheight()-80, 900)
        self.geometry('%dx%d+25+25' % (width, height))
        self.minsize(min(1000, width), min(650, height))
        self.configure(fg_color='#10151b')
        self.rows = received_rows(report)
        self.visible = {}
        self.report = report
        self.search = ctk.StringVar(value='')
        self.only_train = ctk.BooleanVar(value=False)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self, fg_color='#18232d', corner_radius=0)
        header.grid(row=0, column=0, sticky='ew')
        ctk.CTkLabel(header, text='接收车次 · 逐条记录', font=ctk.CTkFont(size=23, weight='bold'),
                     text_color='#e8f4ff').pack(anchor='w', padx=20, pady=(12, 1))
        ctk.CTkLabel(header, text='每条成功解析的 RX 回调各占一行；无车次号的时间同步和未知消息也保留。',
                     text_color='#a6bfd0').pack(anchor='w', padx=20, pady=(0, 12))

        tools = ctk.CTkFrame(self, fg_color='#1b2630')
        tools.grid(row=1, column=0, sticky='ew', padx=14, pady=(10, 4))
        tools.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tools, text='筛选', text_color='#c9dce9').grid(row=0, column=0, padx=(14, 8), pady=10)
        ctk.CTkEntry(tools, textvariable=self.search, placeholder_text='车次 / 机车 / RIC / 类型 / 原始内容',
                     height=34).grid(row=0, column=1, sticky='ew', pady=10)
        ctk.CTkCheckBox(tools, text='只看有车次号', variable=self.only_train,
                        command=self._refresh, width=130).grid(row=0, column=2, padx=15)
        self.search.trace_add('write', lambda *_: self._refresh())
        self.summary = ctk.CTkLabel(self, text='', text_color='#9eb4c6', anchor='w')
        self.summary.grid(row=2, column=0, sticky='ew', padx=22, pady=(0, 4))

        table_frame = ctk.CTkFrame(self, fg_color='#1b2630')
        table_frame.grid(row=3, column=0, sticky='nsew', padx=14, pady=4)
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.configure('LBJ.Treeview', background='#192630', fieldbackground='#192630',
                        foreground='#e5f1fa', rowheight=27, borderwidth=0)
        style.configure('LBJ.Treeview.Heading', background='#314557', foreground='#e5f1fa',
                        font=('Arial', 11, 'bold'))
        style.map('LBJ.Treeview', background=[('selected', '#326fa4')])
        columns = [key for key, _, _ in self.COLUMNS]
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings',
                                 style='LBJ.Treeview', selectmode='browse')
        for key, label, width_px in self.COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width_px, minwidth=50, stretch=(key in ('loco','location')))
        self.tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self._show_selected)

        detail_frame = ctk.CTkFrame(self, fg_color='#1b2630')
        detail_frame.grid(row=4, column=0, sticky='ew', padx=14, pady=(4, 12))
        ctk.CTkLabel(detail_frame, text='选中记录的完整信息与原始内容',
                     text_color='#c9dce9', anchor='w').pack(fill='x', padx=12, pady=(8, 2))
        self.detail = ctk.CTkTextbox(detail_frame, height=190, wrap='word',
                                      font=ctk.CTkFont(family='Menlo', size=12))
        self.detail.pack(fill='x', padx=10, pady=(0, 9))
        self.detail.configure(state='disabled')
        self._refresh()
        self.lift()

    def _refresh(self):
        query = self.search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.visible = {}
        for index, row in enumerate(self.rows, 1):
            if self.only_train.get() and not has_train_number(row):
                continue
            if query and query not in json.dumps(row['payload'], ensure_ascii=False).lower() \
                    and query not in str(row['seconds']) and query not in str(row['type']).lower():
                continue
            iid = str(index)
            self.visible[iid] = row
            seconds = int(row['seconds'])
            location = ' / '.join(str(row[key]) for key in ('lat','lon') if row[key])
            values = (index, '%02d:%02d' % divmod(seconds, 60), row['type'],
                      _cell(row['train_no']), _cell(row['loco']), _cell(row['speed']),
                      _cell(row['km_post']), _cell(row['rssi']), _cell(row['ric']), _cell(location))
            self.tree.insert('', 'end', iid=iid, values=values)
        expected = self.report.get('received', len(self.rows))
        missing = max(0, expected-len(self.rows))
        train_count = sum(has_train_number(row) for row in self.rows)
        unique_trains = len({str(row['train_no']) for row in self.rows if has_train_number(row)})
        note = ('  ·  缺少 %d 条日志' % missing) if missing else ''
        if self.report.get('details_truncated', 0):
            note += '  ·  %d 条详情被截短' % self.report['details_truncated']
        self.summary.configure(text='已保存 RX 记录 %d / 接收计数 %d  ·  有车次号记录 %d  ·  不同车次 %d  ·  当前显示 %d%s' % (
            len(self.rows), expected, train_count, unique_trains, len(self.visible), note),
            text_color='#ef8f8f' if missing or self.report.get('details_truncated') else '#9eb4c6')
        self._set_detail('选择上方任意记录，查看完整解码字段和原始无线数据。')

    def _set_detail(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def _show_selected(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        row = self.visible.get(selected[0])
        if row is None:
            return
        header = '测试经过 %s 秒  ·  %s  ·  %s\n' % (
            row['seconds'], row['type'], ('JSON 解码失败：'+row['parse_error']) if row['parse_error'] else '完整记录')
        self._set_detail(header + json.dumps(row['payload'], ensure_ascii=False, indent=2))


class ReportWindow(ctk.CTkToplevel):
    def __init__(self, parent, report, source_path):
        super().__init__(parent)
        from flash_and_report import installed_version, average_received_rssi
        self.report = report
        self.detail_window = None
        self.train_window = None
        self.title('LBJ · STRESS TEST 结果总览')
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = min(screen_w-35, 1640), min(screen_h-65, 1020)
        self.geometry('%dx%d+15+20' % (width, height))
        self.minsize(min(width, 1050), min(height, 730))
        self.configure(fg_color='#10151b')
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(self, fg_color='#18232d', corner_radius=0)
        header.grid(row=0, column=0, sticky='ew')
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text='LBJ  ·  60 分钟压力测试', font=ctk.CTkFont(size=23, weight='bold'),
                     text_color='#e8f4ff').grid(row=0, column=0, sticky='w', padx=20, pady=(10, 0))
        ctk.CTkLabel(header, text='SN %s    %s    原系统 %s    %s / %s 秒' % (
            report.get('sn', '—'), model_label(report), installed_version(report.get('original_system', {})),
            report.get('elapsed_s', '—'), report.get('duration_s', '—')),
            text_color='#a6bfd0').grid(row=1, column=0, sticky='w', padx=20, pady=(0, 8))
        ctk.CTkButton(header, text='接收车次', width=112, command=self.show_trains,
                      fg_color='#326fa4').grid(row=0, column=1, rowspan=2, padx=(8, 9))
        ctk.CTkButton(header, text='详细数据与日志', width=138, command=self.show_details,
                      fg_color='#314557').grid(row=0, column=2, rowspan=2, padx=(0, 9))
        ctk.CTkButton(header, text='关闭结果', width=90, command=self.destroy,
                      fg_color='#314557').grid(row=0, column=3, rowspan=2, padx=(0, 18))

        verdict = report.get('verdict', '—')
        color = {'PASS':'#48c995','FAIL':'#ef8f8f','INCOMPLETE':'#e5c474'}.get(verdict,'#b4c8d6')
        banner = ctk.CTkFrame(self, fg_color='#1b2630', corner_radius=10)
        banner.grid(row=1, column=0, sticky='ew', padx=14, pady=(10, 6))
        banner.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(banner, text=verdict, font=ctk.CTkFont(size=26, weight='bold'),
                     text_color=color).grid(row=0, column=0, sticky='w', padx=16, pady=10)
        if verdict == 'INCOMPLETE':
            causes = '未完成：' + (', '.join(report.get('incomplete', [])) or '报告明细不完整')
            if 'log_truncated' in report.get('incomplete', []):
                causes += '（事件省略 %d 条；RX 已保存 %d/%d 条）' % (
                    report.get('events_omitted', 0), len(received_rows(report)), report.get('received', 0))
        elif verdict == 'FAIL':
            causes = '失败：' + (', '.join(report.get('failures', [])) or '设备运行错误')
        else:
            causes = '全部判定条件通过'
        warning = ', '.join(report.get('warnings', []))
        if not warning and (report.get('final_health') or {}).get('recoveries'):
            warning = '射频恢复 %s 次（旧版报告）' % report['final_health']['recoveries']
        ctk.CTkLabel(banner, text='%s%s' % (causes, ('    提示：'+warning) if warning else ''),
                     text_color='#e5f1fa', anchor='w', wraplength=max(500,width-220)
                     ).grid(row=0, column=1, sticky='ew', padx=10)

        h = report.get('final_health') or {}
        codewords = h.get('codewords') or 0
        bad_percent = 100*h.get('uncorrectable',0)/codewords if codewords else None
        metrics = (
            ('电压降低', _value(report.get('battery_drop_v'),' V'), '#65d5c1'),
            ('换算电量降低', _value(report.get('battery_drop_percent_points'),' 个百分点'), '#75baff'),
            ('平均接收 RSSI', average_received_rssi(report), '#dfabff'),
            ('不可纠错比例', _value(('%.2f' % bad_percent) if bad_percent is not None else None,'%'), '#e6c979'),
            ('FIFO 满 / 射频恢复', '%s 次 / %s 次' % (h.get('fifo_full_hits',0), h.get('recoveries',0)), '#ef947e'),
            ('每分钟最低剩余内存', _value(report.get('heap_free_sample_min_bytes'),' B'), '#65d5c1'),
        )
        cards = ctk.CTkFrame(self, fg_color='transparent')
        cards.grid(row=2, column=0, sticky='ew', padx=10)
        for col, (label, value, accent) in enumerate(metrics):
            cards.grid_columnconfigure(col, weight=1, uniform='metric')
            card = ctk.CTkFrame(cards, fg_color='#1b2630', corner_radius=9)
            card.grid(row=0, column=col, sticky='ew', padx=4, pady=3)
            ctk.CTkLabel(card, text=label, text_color='#9eb4c6',
                         font=ctk.CTkFont(size=11)).pack(anchor='w', padx=10, pady=(8, 1))
            ctk.CTkLabel(card, text=value, text_color=accent,
                         font=ctk.CTkFont(size=17, weight='bold'),
                         wraplength=max(130,width//6-30), justify='left'
                         ).pack(anchor='w', padx=10, pady=(0, 8))

        ctk.CTkLabel(self, text='每分钟趋势  ·  左轴：本分钟/当前值    右轴：累计/历史值  ·  鼠标停留查看该分钟数据',
                     text_color='#9eb4c6', anchor='w').grid(row=3, column=0, sticky='ew', padx=19, pady=(6, 0))
        chart_height = max(118, min(170, (height-280)//4))
        self.trends = TrendCharts(self, chart_height=chart_height)
        self.trends.grid(row=4, column=0, sticky='nsew', padx=10, pady=(0, 5))
        self.trends.set_report(report)
        ctk.CTkLabel(self, text='SD 报告：' + str(source_path), text_color='#809eaf',
                     anchor='w').grid(row=5, column=0, sticky='ew', padx=18, pady=(0, 5))
        self.lift()
        self.focus_force()

    def show_details(self):
        if self.detail_window is not None and self.detail_window.winfo_exists():
            self.detail_window.lift()
        else:
            self.detail_window = DetailWindow(self, self.report)

    def show_trains(self):
        if self.train_window is not None and self.train_window.winfo_exists():
            self.train_window.lift()
        else:
            self.train_window = TrainWindow(self, self.report)


class DiagnosticApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode('dark')
        self.title('LBJ · 独立内部检测刷入器')
        self.geometry('1060x900')
        self.minsize(850, 740)
        self.configure(fg_color='#10151b')
        self.events = queue.Queue()
        self.busy = False
        self.run_dir = None
        self.report_window = None
        self.last_report = None
        self.last_report_path = None
        self.port = ctk.StringVar(value='')
        self.variant = ctk.StringVar(value='自动识别')
        self.device_info = None
        self._build()
        self.refresh_ports()
        self.after(100, self._pump)
        self.after(150, self._bring_front)

    def _bring_front(self):
        self.lift()
        self.attributes('-topmost', True)
        self.after(1200, lambda: self.attributes('-topmost', False))

    def _build(self):
        head = ctk.CTkFrame(self, fg_color='#18232d', corner_radius=0)
        head.pack(fill='x')
        ctk.CTkLabel(head, text='LBJ  内部检测', font=ctk.CTkFont(size=27, weight='bold'),
                     text_color='#e8f4ff').pack(anchor='w', padx=26, pady=(19, 0))
        ctk.CTkLabel(head, text='普通版 / W 版 · 固件在 RAM 运行 · 一小时脱机射频压力测试',
                     font=ctk.CTkFont(size=13), text_color='#9eb4c6').pack(
                         anchor='w', padx=26, pady=(2, 20))

        body = ctk.CTkFrame(self, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=22, pady=17)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)

        device = ctk.CTkFrame(body, fg_color='#1b2630', corner_radius=12)
        device.grid(row=0, column=0, sticky='ew')
        device.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(device, text='设备连接', font=ctk.CTkFont(size=17, weight='bold')).grid(
            row=0, column=0, columnspan=3, sticky='w', padx=18, pady=(14, 7))
        ctk.CTkLabel(device, text='Pico 串口', text_color='#9eb4c6').grid(
            row=1, column=0, padx=(18, 8), pady=(0, 16))
        self.port_menu = ctk.CTkOptionMenu(device, variable=self.port, values=['未发现 Pico'],
                                           fg_color='#314557', button_color='#476b82',
                                           command=lambda _: self._probe_port())
        self.port_menu.grid(row=1, column=1, sticky='ew', pady=(0, 16))
        self.refresh_btn = ctk.CTkButton(device, text='刷新', width=80, fg_color='#314557',
                                        command=self.refresh_ports)
        self.refresh_btn.grid(row=1, column=2, padx=16, pady=(0, 16))
        ctk.CTkLabel(device, text='检测型号', text_color='#9eb4c6').grid(
            row=2, column=0, padx=(18, 8), pady=(0, 9))
        self.variant_menu = ctk.CTkOptionMenu(device, variable=self.variant,
            values=list(VARIANT_OPTIONS), command=self._profile_changed,
            fg_color='#314557', button_color='#476b82')
        self.variant_menu.grid(row=2, column=1, sticky='ew', pady=(0, 9))
        ctk.CTkLabel(device, text='可手动修改', text_color='#9eb4c6').grid(
            row=2, column=2, padx=16, pady=(0, 9))
        self.profile_label = ctk.CTkLabel(device,
            text='连接后自动识别；W 版为 Waveshare RP2350B-Plus-W',
            anchor='w', justify='left', wraplength=900, text_color='#9eb4c6')
        self.profile_label.grid(row=3, column=0, columnspan=3, sticky='ew', padx=18, pady=(0, 8))
        self.identity_label = ctk.CTkLabel(
            device, text='SN：—    机器型号：—    原系统版本：—',
            text_color='#c9dce9', anchor='w', justify='left', wraplength=810)
        self.identity_label.grid(row=4, column=0, columnspan=3,
                                 sticky='ew', padx=18, pady=(0, 12))

        actions = ctk.CTkFrame(body, fg_color='#1b2630', corner_radius=12)
        actions.grid(row=1, column=0, sticky='ew', pady=12)
        actions.grid_columnconfigure((0, 1), weight=1)
        self.load_btn = ctk.CTkButton(actions, text='加载检测固件到 RAM', height=48,
                                      font=ctk.CTkFont(size=16, weight='bold'),
                                      fg_color='#1e9e91', hover_color='#16867b', command=self.load_firmware)
        self.load_btn.grid(row=0, column=0, sticky='ew', padx=(16, 8), pady=(16, 10))
        self.read_btn = ctk.CTkButton(actions, text='从 SD 卡读取报告', height=48,
                                         font=ctk.CTkFont(size=16, weight='bold'),
                                         fg_color='#326fa4', hover_color='#275c8b', command=self.open_report)
        self.read_btn.grid(row=0, column=1, sticky='ew', padx=(8, 16), pady=(16, 10))
        self.state_label = ctk.CTkLabel(actions, text='等待连接设备', text_color='#a9bac8',
                                        anchor='w', wraplength=360)
        self.state_label.grid(row=1, column=0, sticky='ew', padx=17, pady=(0, 14))

        self.tabs = ctk.CTkTabview(body, fg_color='#1b2630', segmented_button_fg_color='#263745')
        self.tabs.grid(row=2, column=0, sticky='nsew')
        overview_tab = self.tabs.add('操作指引')
        overview = ctk.CTkScrollableFrame(overview_tab, fg_color='transparent')
        overview.pack(fill='both', expand=True)
        report = self.tabs.add('原始文本与日志')
        overview.grid_columnconfigure(0, weight=1)
        guide = (
            '① 连接 USB，GUI 自动识别型号；可在“检测型号”中手动选择普通版或 W 版，再加载 RAM 固件。\n\n'
            '② PRECHECK 检查 RTC、电池和射频；W 版另检查 CYW43 无线模块。失败时按 POWER 重试。\n\n'
            '③ 全部通过后，同屏出现 UNPLUG USB / INSERT SD CARD。保持电池供电，拔线并插卡，再按 POWER；无卡显示红色提示并等待重试。\n\n'
            '④ 独立 SD 页验证读写，通过后进入全屏色块测试。蜂鸣器页按 OK 响一次，POWER 确认。五键同页：'
            '左侧从上到下 OK / UP / DOWN / MENU，右上 POWER；按图形箭头继续。\n\n'
            '⑤ 机器独立进行一小时射频测试，日志持续写入 SD；拔卡会立即终止测试。\n\n'
            '⑥ 屏幕出现 RESULT 后关闭设备，取卡接到电脑，点击“从 SD 卡读取报告”。'
        )
        ctk.CTkLabel(overview, text=guide, justify='left', anchor='nw',
                     font=ctk.CTkFont(size=15), text_color='#d8e7f1',
                     wraplength=790).grid(row=0, column=0, sticky='new', padx=24, pady=25)
        self.path_label = ctk.CTkLabel(overview, text='SD 报告：尚未读取', text_color='#8fb4ce',
                                       anchor='w', wraplength=790)
        self.path_label.grid(row=1, column=0, sticky='ew', padx=24, pady=(15, 12))

        report.grid_columnconfigure(0, weight=1)
        report.grid_rowconfigure(1, weight=1)
        self.summary = ctk.CTkLabel(report, text='从 SD 卡读取后显示结论与电量变化',
                                    font=ctk.CTkFont(size=16, weight='bold'),
                                    text_color='#e6f4fc', anchor='w', justify='left')
        self.summary.grid(row=0, column=0, sticky='ew', padx=18, pady=(12, 8))
        self.big_result_btn = ctk.CTkButton(report, text='打开大屏结果总览',
                                            command=self.show_report_window, state='disabled')
        self.big_result_btn.grid(row=0, column=1, padx=(0, 15), pady=(12, 8))
        self.report_text = ctk.CTkTextbox(report, wrap='word', font=ctk.CTkFont(size=12))
        self.report_text.grid(row=1, column=0, columnspan=2, sticky='nsew', padx=15, pady=(0, 14))
        self.report_text.configure(state='disabled')
        self._set_buttons()

    def refresh_ports(self):
        if self.busy:
            return
        ports = [p.device for p in list_ports.comports() if p.vid == 0x2E8A]
        if ports:
            self.port_menu.configure(values=ports)
            if self.port.get() not in ports:
                self.port.set(ports[0])
            self._probe_port()
        else:
            self.port_menu.configure(values=['未发现 Pico'])
            self.port.set('未发现 Pico')
            self.device_info = None
            self._profile_changed()
            self.state_label.configure(text='未发现 Pico；完成的报告可直接从 SD 卡读取',
                                       text_color='#e7bd78')
        self._set_buttons()

    def _probe_port(self):
        if self.busy or not self.port.get().startswith('/dev/'):
            return
        self.device_info = None
        self.identity_label.configure(text='正在读取 SN、机器型号和原系统版本…')
        self._profile_changed()
        self._start_task('probe', ['probe', '--port', self.port.get()], '正在识别普通版 / W 版…')

    def _profile_changed(self, *_):
        requested = VARIANT_OPTIONS[self.variant.get()]
        detected = (self.device_info or {}).get('detected_variant')
        variant = detected if requested == 'auto' else requested
        if variant is None:
            text = '连接后自动识别；W 版为 Waveshare RP2350B-Plus-W'
        else:
            pins = PINS[variant]
            text = '%s；POWER GP%d / 电池 ADC GP%d%s' % (
                ('自动识别：' if requested == 'auto' else '手动选择：') + MODELS[variant],
                pins['power'], pins['battery_adc'], '；Precheck 含 CYW43 无线模块' if variant == 'wireless' else '')
            if detected and requested != 'auto':
                text = '设备识别为 %s；%s' % (MODELS[detected], text)
        color = '#9eb4c6'
        if self.device_info:
            try:
                resolve_variant(self.device_info['board'], requested)
            except ValueError as exc:
                text, color = str(exc), '#ef8f8f'
        self.profile_label.configure(text=text, text_color=color)
        self._set_buttons()

    def _set_buttons(self):
        connected = self.port.get().startswith('/dev/')
        if self.device_info:
            try:
                resolve_variant(self.device_info['board'], VARIANT_OPTIONS[self.variant.get()])
            except ValueError:
                connected = False
        self.load_btn.configure(state='normal' if connected and not self.busy else 'disabled')
        self.read_btn.configure(state='normal' if not self.busy else 'disabled')
        for widget in (self.port_menu, self.variant_menu, self.refresh_btn):
            widget.configure(state='disabled' if self.busy else 'normal')

    def _run_cli(self, name, argv):
        try:
            result = subprocess.run([sys.executable, str(CLI)] + argv, cwd=HERE,
                                    text=True, capture_output=True, timeout=180)
            self.events.put((name, result.returncode, result.stdout, result.stderr))
        except Exception as exc:
            self.events.put((name, 1, '', str(exc)))

    def _start_task(self, name, argv, status):
        self.busy = True
        self._set_buttons()
        self.state_label.configure(text=status, text_color='#8fc7f0')
        threading.Thread(target=self._run_cli, args=(name, argv), daemon=True).start()

    def load_firmware(self):
        self._start_task('load', ['start', '--port', self.port.get(),
                                 '--variant', VARIANT_OPTIONS[self.variant.get()]], '正在加载 RAM 固件…')

    def _show_report(self, report, source_path):
        from flash_and_report import analyze, installed_version, average_received_rssi
        self.identity_label.configure(
            text='SN：%s    机器型号：%s / %s\n原系统版本：%s    MicroPython：%s' % (
                report.get('sn', '未知'), model_label(report), report.get('board', '未知'),
                installed_version(report.get('original_system', {})),
                report.get('micropython', '未知')))
        recovery_count = (report.get('final_health') or {}).get('recoveries', 0)
        memory_min = report.get('heap_free_sample_min_bytes')
        memory_text = ('%d 字节' % memory_min if memory_min is not None else '未记录')
        summary = ('结论：%s    电压降低：%s V    电量降低：%s 个百分点\n'
                   '每条接收记录的平均 RSSI：%s    射频恢复：%d 次（仅提示）\n'
                   '每分钟剩余内存最低：%s' %
                   (report['verdict'], report['battery_drop_v'],
                    report['battery_drop_percent_points'], average_received_rssi(report),
                    recovery_count, memory_text))
        self.summary.configure(text=summary)
        analysis = analyze(report)
        self.report_text.configure(state='normal')
        self.report_text.delete('1.0', 'end')
        self.report_text.insert('1.0', analysis)
        self.report_text.configure(state='disabled')
        self.last_report = report
        self.last_report_path = source_path
        self.big_result_btn.configure(state='normal')
        self.show_report_window()

    def show_report_window(self):
        if self.last_report is None:
            return
        if self.report_window is not None and self.report_window.winfo_exists():
            self.report_window.destroy()
        self.report_window = ReportWindow(self, self.last_report, self.last_report_path)

    def open_report(self):
        filename = filedialog.askopenfilename(title='从 SD 卡读取检测报告', initialdir='/Volumes',
                                              filetypes=[('JSON 报告', '*.json')])
        if filename:
            try:
                from flash_and_report import read_sd_report
                report = read_sd_report(filename)
                self._show_report(report, filename)
                self.path_label.configure(text='SD 报告：' + filename)
                self.state_label.configure(text='已从 SD 卡读取报告', text_color='#81d3ad')
            except Exception as exc:
                self.state_label.configure(text='读取报告失败：' + str(exc), text_color='#ef8f8f')

    def _pump(self):
        try:
            while True:
                name, code, stdout, stderr = self.events.get_nowait()
                self.busy = False
                if code:
                    self.state_label.configure(text='操作失败：' + (stderr or stdout)[-240:],
                                               text_color='#ef8f8f')
                elif name == 'load':
                    self.state_label.configure(text='加载成功。按设备提示拔 USB 并测试 SD 卡。',
                                               text_color='#81d3ad')
                if not code and name in ('probe', 'load'):
                    from flash_and_report import parse_json, installed_version
                    info = parse_json(stdout)
                    self.device_info = info
                    self.identity_label.configure(text='SN：%s    %s\n原系统版本：%s    MicroPython：%s' % (
                        info['sn'], info['board'], installed_version(info.get('original_system', {})),
                        info.get('micropython', '未知')))
                    self._profile_changed()
                    if name == 'probe':
                        self.state_label.configure(text='设备识别完成，可手动修改检测型号', text_color='#81d3ad')
                self._set_buttons()
        except queue.Empty:
            pass
        self.after(100, self._pump)


if __name__ == '__main__':
    app = DiagnosticApp()
    if len(sys.argv) == 2:
        def open_initial_report():
            try:
                from flash_and_report import read_sd_report
                path = sys.argv[1]
                app._show_report(read_sd_report(path), path)
                app.path_label.configure(text='SD 报告：' + path)
                app.state_label.configure(text='已从 SD 卡读取报告', text_color='#81d3ad')
            except Exception as exc:
                app.state_label.configure(text='读取报告失败：' + str(exc), text_color='#ef8f8f')
        app.after(250, open_initial_report)
    app.mainloop()
