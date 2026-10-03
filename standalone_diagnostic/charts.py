"""Minute-aligned diagnostic series and a dependency-free Tk chart grid."""
import math
import tkinter as tk
import customtkinter as ctk


def minute_series(report):
    """Use actual sample timestamps; deltas are per recorded interval, not invented data."""
    rows = []
    previous = None
    for sample in sorted(report.get('snapshots', []), key=lambda item: item['s']):
        h = sample.get('health', {})
        total, bad = h.get('codewords'), h.get('uncorrectable')
        rssi = sample.get('rssi_dbm')
        try:
            rssi = float(str(rssi).replace('dBm', '').strip())
        except (TypeError, ValueError):
            rssi = None
        row = dict(minute=sample['s'] / 60, voltage=sample.get('battery_v'),
                   battery=sample.get('battery_percent'), temperature=sample.get('chip_temp_c'),
                   heap_free=sample.get('heap_free_bytes'),
                   heap_free_min=sample.get('heap_free_min_bytes'),
                   bad_total=bad, bad_percent=100 * bad / total if total and bad is not None else None,
                   bad_delta=None, interval_percent=None, fifo_delta=None, dropped_delta=None,
                   words=h.get('words'), words_delta=None, received=sample.get('received'),
                   fifo=h.get('fifo_full_hits'), dropped=h.get('raw_dropped'),
                   recoveries=h.get('recoveries'), rssi=rssi, codewords=total, syncs=h.get('syncs'),
                   received_delta=None, recoveries_delta=None)
        if previous is not None:
            ph = previous.get('health', {})
            def delta(current, prior):
                if current is None or prior is None or current < prior:
                    return None
                return current - prior
            delta_bad = delta(bad, ph.get('uncorrectable'))
            delta_total = delta(total, ph.get('codewords'))
            row['bad_delta'] = delta_bad
            row['interval_percent'] = (100 * delta_bad / delta_total
                if delta_total and delta_bad is not None and delta_bad <= delta_total else None)
            for key, counter in (('fifo_delta', 'fifo_full_hits'), ('dropped_delta', 'raw_dropped'), ('recoveries_delta', 'recoveries'), ('words_delta', 'words')):
                row[key] = delta(h.get(counter), ph.get(counter))
            row['received_delta'] = delta(sample.get('received'), previous.get('received'))
        rows.append(row)
        previous = sample
    return rows


CHARTS = (
    ('不可纠错计数', [('bad_delta', '本分钟', '#ef947e'), ('bad_total', '累计', '#e6c979')]),
    ('不可纠错比例 · %', [('interval_percent', '本分钟', '#ef947e'), ('bad_percent', '累计', '#e6c979')]),
    ('FIFO 满', [('fifo_delta', '本分钟', '#ef947e'), ('fifo', '累计', '#e6c979')]),
    ('队列丢弃', [('dropped_delta', '本分钟', '#ef947e'), ('dropped', '累计', '#e6c979')]),
    ('电池电压 · V', [('voltage', '电压', '#65d5c1')]),
    ('电压换算电量 · %', [('battery', '电量', '#75baff')]),
    ('片上温度 · °C', [('temperature', '温度', '#dfabff')]),
    ('剩余内存 · 字节', [('heap_free', '每分钟', '#65d5c1'), ('heap_free_min', '历史最低', '#ef947e')]),
    ('接收条目', [('received_delta', '本分钟', '#65d5c1'), ('received', '累计', '#75baff')]),
    ('PIO 字流', [('words_delta', '本分钟', '#65d5c1'), ('words', '累计', '#75baff')]),
    ('RSSI · dBm', [('rssi', '接收强度', '#dfabff')]),
    ('射频恢复', [('recoveries_delta', '本分钟', '#75baff'), ('recoveries', '累计', '#65d5c1')]),
    ('码字 · 累计', [('codewords', '码字', '#75baff')]),
    ('同步 · 累计', [('syncs', '同步', '#65d5c1')]),
)


RIGHT_AXIS_KEYS = frozenset((
    'bad_total', 'bad_percent', 'fifo', 'dropped', 'received', 'words',
    'recoveries', 'codewords', 'syncs', 'heap_free_min',
))
ZERO_BASED_KEYS = frozenset((
    'bad_delta', 'bad_total', 'fifo_delta', 'dropped_delta', 'fifo', 'dropped',
    'received_delta', 'received', 'words_delta', 'words',
    'recoveries_delta', 'recoveries', 'codewords', 'syncs',
))


def chart_axis_ranges(rows, series):
    """Compute independent left/current and right/cumulative Y scales."""
    ranges = {}
    for axis in ('left', 'right'):
        keys = [key for key, _, _ in series
                if ('right' if key in RIGHT_AXIS_KEYS else 'left') == axis]
        values = [row[key] for row in rows for key in keys
                  if isinstance(row.get(key), (int, float)) and math.isfinite(row[key])]
        if not values:
            ranges[axis] = None
            continue
        lo, hi = min(values), max(values)
        zero_based = all(key in ZERO_BASED_KEYS for key in keys)
        if zero_based:
            lo = min(0, lo)
        if lo == hi:
            pad = max(abs(lo) * 0.02, 1 if zero_based else .01)
        else:
            pad = (hi - lo) * 0.05
        ranges[axis] = (lo if zero_based else lo - pad, hi + pad)
    return ranges


class TrendCharts(ctk.CTkFrame):
    """A compact four-column dashboard with every trend visible together."""
    def __init__(self, parent, chart_height=155):
        super().__init__(parent, fg_color='transparent')
        self.rows = []
        self.rx_types = {}
        for column in range(4):
            self.grid_columnconfigure(column, weight=1, uniform='charts')
        for row in range(4):
            self.grid_rowconfigure(row, weight=1)
        self.canvases = []
        for i, _ in enumerate(CHARTS):
            canvas = tk.Canvas(self, height=chart_height, background='#192630',
                               highlightthickness=0, bd=0)
            canvas.grid(row=i//4, column=i%4, sticky='nsew', padx=4, pady=4)
            canvas.bind('<Configure>', lambda event, j=i: self.draw(j))
            canvas.bind('<Motion>', lambda event, j=i: self.hover(j, event.x))
            canvas.bind('<Leave>', lambda event, c=canvas: c.delete('hover'))
            self.canvases.append(canvas)
        self.type_canvas = tk.Canvas(self, height=chart_height, background='#192630',
                                     highlightthickness=0, bd=0)
        self.type_canvas.grid(row=3, column=2, columnspan=2, sticky='nsew', padx=4, pady=4)
        self.type_canvas.bind('<Configure>', lambda event: self.draw_types())

    def set_report(self, report):
        self.rows = minute_series(report)
        self.rx_types = report.get('received_types', {})
        for i in range(len(CHARTS)):
            self.draw(i)
        self.draw_types()

    def draw(self, index):
        c = self.canvases[index]
        c.delete('all')
        width, height = max(250, c.winfo_width()), max(120, c.winfo_height())
        left, right, top, bottom = 46, width-48, 43, height-36
        title, series = CHARTS[index]
        c.create_text(10, 13, text=title, anchor='w', fill='#e5f1fa', font=('Arial', 10, 'bold'))
        for i, (_, label, color) in enumerate(series):
            key = series[i][0]
            side = '右' if key in RIGHT_AXIS_KEYS else '左'
            c.create_text(10+i*118, 30, text='● %s %s' % (side, label),
                          anchor='w', fill=color, font=('Arial', 9))
        ranges = chart_axis_ranges(self.rows, series)
        if ranges['left'] is None and ranges['right'] is None:
            c.create_text(width/2, (top+bottom)/2, text='暂无可计算样本', fill='#93aabd')
            return
        xmax = max([r['minute'] for r in self.rows] or [1]) or 1
        for n in range(3):
            y = top+(bottom-top)*n/2
            c.create_line(left, y, right, y, fill='#2a3b48')
            for axis, bounds in ranges.items():
                if bounds is None:
                    continue
                lo, hi = bounds
                value = hi-(hi-lo)*n/2
                x = left-4 if axis == 'left' else right+4
                anchor = 'e' if axis == 'left' else 'w'
                c.create_text(x, y, text='%.3g' % value, anchor=anchor,
                              fill='#a8bcd0' if axis == 'left' else '#e6c979', font=('Arial', 8))
        for n in range(4):
            x = left+(right-left)*n/3
            c.create_text(x, bottom+12, text='%.2g' % (xmax*n/3), fill='#93aabd', font=('Arial', 8))
        c.create_text(right, bottom+24, text='分钟', anchor='e', fill='#93aabd', font=('Arial', 8))
        for key, _, color in series:
            axis = 'right' if key in RIGHT_AXIS_KEYS else 'left'
            bounds = ranges[axis]
            if bounds is None:
                continue
            lo, hi = bounds
            prior = None
            for row in self.rows:
                value = row.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    prior = None
                    continue
                x, y = left+row['minute']/xmax*(right-left), bottom-(value-lo)/(hi-lo)*(bottom-top)
                if prior:
                    c.create_line(prior[0], prior[1], x, y, fill=color, width=2)
                c.create_oval(x-2, y-2, x+2, y+2, fill=color, outline=color)
                prior = (x, y)

    def hover(self, index, xpos):
        if not self.rows:
            return
        c = self.canvases[index]
        c.delete('hover')
        xmax = max(r['minute'] for r in self.rows) or 1
        target = (xpos-46)/max(1, c.winfo_width()-94)*xmax
        row = min(self.rows, key=lambda r: abs(r['minute']-target))
        parts = ['%.2f 分钟' % row['minute']]
        for key, label, _ in CHARTS[index][1]:
            value = row.get(key)
            side = '右' if key in RIGHT_AXIS_KEYS else '左'
            parts.append(side+' '+label+': '+('—' if value is None else '%.4g' % value))
        height = c.winfo_height()
        c.create_rectangle(4, height-18, c.winfo_width()-4, height, fill='#253949', outline='', tags='hover')
        c.create_text(8, height-9, text='   '.join(parts), anchor='w', fill='#ffffff', font=('Arial', 8), tags='hover')

    def draw_types(self):
        c = self.type_canvas
        c.delete('all')
        width, height = max(340, c.winfo_width()), max(120, c.winfo_height())
        baseline = height - 34
        c.create_text(12, 13, text='RX TYPE · 各接收类型数量', anchor='w', fill='#e5f1fa', font=('Arial', 10, 'bold'))
        values = sorted(self.rx_types.items(), key=lambda item: (-item[1], item[0]))
        if not values:
            c.create_text(width/2, height/2, text='尚无 RX TYPE 数据', fill='#93aabd')
            return
        maximum = max(value for _, value in values) or 1
        slot = (width-50)/len(values)
        colors = ('#65d5c1', '#75baff', '#dfabff', '#ef947e', '#e6c979')
        c.create_line(32, baseline, width-12, baseline, fill='#43596a')
        for i, (label, count) in enumerate(values):
            x = 35+slot*i
            bar_height = max(2, (baseline-42)*count/maximum)
            c.create_rectangle(x+slot*.15, baseline-bar_height, x+slot*.85, baseline,
                               fill=colors[i%len(colors)], outline='')
            c.create_text(x+slot*.5, baseline-bar_height-9, text=str(count), fill='#e5f1fa', font=('Arial', 9))
            c.create_text(x+slot*.5, baseline+4, text=label, width=max(35,slot-4),
                          anchor='n', fill='#b9cede', font=('Arial', 8))
