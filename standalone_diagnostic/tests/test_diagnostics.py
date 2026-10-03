import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from charts import minute_series, CHARTS, chart_axis_ranges
from flash_and_report import average_received_rssi, installed_version, read_sd_report
from train_records import received_rows, has_train_number
from hardware_profiles import detect_variant, resolve_variant, model_label, PINS
import flash_and_report as loader

class Pin:
    IN, OUT, PULL_UP = 0, 1, 2
    values, writes = {}, []
    def __init__(self, gpio, *args, value=None):
        self.gpio = gpio
        if value is not None: self.value(value)
    def value(self, value=None):
        if value is not None:
            self.values[self.gpio] = value
            self.writes.append((self.gpio,value))
        return self.values.get(self.gpio,1)

class Clock:
    now = 0
    def ticks_ms(self): return self.now
    def ticks_diff(self,a,b): return a-b
    def ticks_add(self,a,b): return a+b
    def sleep_ms(self,ms): self.now += ms

class LCD:
    def __init__(self): self.fills=[]; self.text=[]
    def fill(self,color): self.fills.append(color)
    def fill_rect(self,x,y,w,h,color):
        assert min(x,y,w,h)>=0 and x+w<=320 and y+h<=240,(x,y,w,h)
    def draw_gbk(self,text,x,y,color,bg=0,scale=1):
        assert x+len(text)*8*scale<=320 and y+8*scale<=240,(text,x,y,scale)
        self.text.append(text)

spec=importlib.util.spec_from_file_location('diag_fw',Path(__file__).resolve().parents[1]/'firmware.py')
fw=importlib.util.module_from_spec(spec)
with patch.dict(sys.modules,{'machine':types.SimpleNamespace(Pin=Pin)}): spec.loader.exec_module(fw)

def report(): return dict(start_tick=0,duration_s=3600,checks={},snapshots=[],events=[],errors=[],
                          events_omitted=0,errors_omitted=0,details_truncated=0,
                          snapshots_omitted=0,heap_free_min=None,
                          heap_free_sample_min_bytes=None,heap_free_sample_max_bytes=None,
                          heap_free_sample_sum_bytes=0,heap_free_sample_count=0,
                          heap_free_sample_avg_bytes=None)
def scripted(sequence):
    class Edges:
        def __init__(self,gpios): self.seq=iter(sequence)
        def poll(self): return next(self.seq,[])
    return Edges

class DeviceTests(unittest.TestCase):
    def setUp(self):
        fw._configure_hardware('standard')
        Pin.values={}; Pin.writes=[]
        self.clock=Clock(); self.clock.now=0
        self.patch=patch.object(fw,'time',self.clock); self.patch.start()
        self.memory_patch=patch.object(fw.gc,'mem_free',return_value=200_000,create=True)
        self.memory_patch.start()
    def tearDown(self):
        fw._configure_hardware('standard')
        self.memory_patch.stop(); self.patch.stop()
    def test_wireless_power_routes_screen_buzzer_and_five_keys(self):
        fw._configure_hardware('wireless')
        r=report(); mapped=[]
        def screen_edges(gpios):
            mapped.append(gpios)
            return scripted([[0]])(gpios)
        def buzzer_edges(gpios):
            mapped.append(gpios)
            return scripted([[0],[1]])(gpios)
        def key_edges(gpios):
            mapped.append(gpios)
            return scripted([[0,1,2,3,4]])(gpios)
        with patch.object(fw,'_KeyEdges',side_effect=screen_edges):
            fw._screen_test(r,LCD())
        with patch.object(fw,'_KeyEdges',side_effect=buzzer_edges):
            fw._buzzer_test(r,LCD())
        with patch.object(fw,'_KeyEdges',side_effect=key_edges),patch.object(fw,'_wait_key',return_value=True) as wait:
            fw._five_keys(r,LCD())
        self.assertEqual(mapped,[(42,2),(5,42,2),(5,4,3,2,42)])
        self.assertEqual(wait.call_args.args[0].gpio,42)
        self.assertEqual(r['checks']['key_power']['detail'],'GP42')
    def test_wireless_adc_temperature_and_usb_pins(self):
        fw._configure_hardware('wireless')
        sources=[]
        def adc(source):
            sources.append(source.gpio if isinstance(source,Pin) else source)
            return types.SimpleNamespace(read_u16=lambda:38000)
        with patch.object(fw.machine,'ADC',adc,create=True):
            fw._battery(); fw._temp()
        self.assertEqual(sources,[41,8])
        self.assertEqual(fw._vbus().gpio,'WL_GPIO2')
        self.assertEqual(Pin.values[14],1)
    def test_wireless_failure_prevents_precheck_completion(self):
        fw._configure_hardware('wireless')
        r,lcd=report(),LCD(); calls=[]
        def check(result,name,fn):
            calls.append(name)
            detail={'model':'DS3231'} if name=='rtc' else 4.1 if name=='battery' else {'version':'0x12'} if name=='radio_precheck' else 'CYW43 failed'
            result['checks'][name]={'status':'fail' if name=='wireless' else 'pass','detail':detail}
            return name!='wireless'
        with patch.object(fw,'_check',side_effect=check),patch.object(fw,'_wait_key',return_value=False) as wait:
            with self.assertRaisesRegex(OSError,'precheck POWER timeout'):
                fw._precheck(r,lcd,True,defer_power=True)
        self.assertEqual(calls,['rtc','battery','radio_precheck','wireless'])
        self.assertEqual(wait.call_args.args[0].gpio,42)
        self.assertNotIn(b'UNPLUG USB',lcd.text)
        self.assertIn(b'RETRY >',lcd.text)
    def test_wireless_four_rows_keep_precheck_and_sd_prompt(self):
        fw._configure_hardware('wireless')
        r,lcd=report(),LCD()
        def check(result,name,fn):
            detail={'model':'DS3231'} if name=='rtc' else 4.1 if name=='battery' else {'version':'0x12'} if name=='radio_precheck' else 'CYW43 STA ready'
            result['checks'][name]={'status':'pass','detail':detail}
            return True
        with patch.object(fw,'_check',side_effect=check):
            self.assertTrue(fw._precheck(r,lcd,True,defer_power=True))
        self.assertIn(b'WIRELESS',lcd.text)
        self.assertIn(b'UNPLUG USB',lcd.text)
        self.assertIn(b'INSERT SD CARD',lcd.text)
        self.assertEqual(len(lcd.fills),1)
    def test_wireless_module_start_timeout_and_shutdown(self):
        fw._configure_hardware('wireless')
        for ready,stop_error in ((True,False),(False,False),(True,True)):
            actions=[]
            class Station:
                def active(self,value=None):
                    if value is not None:
                        actions.append(value)
                        if not value and stop_error:
                            raise OSError('stop failed')
                    return ready
            network=types.SimpleNamespace(STA_IF=0,WLAN=lambda interface: Station())
            with patch.dict(sys.modules,{'network':network}):
                if ready and not stop_error:
                    self.assertEqual(fw._wireless_precheck(),'CYW43 STA ready')
                else:
                    with self.assertRaisesRegex(OSError,'STA shutdown' if stop_error else 'CYW43 did not start'):
                        fw._wireless_precheck()
            self.assertEqual(actions,[True,False])
        self.assertGreaterEqual(self.clock.now,1500)
    def test_wireless_modern_sta_api_and_report_metadata(self):
        fw._configure_hardware('wireless')
        station=types.SimpleNamespace(active=lambda value=None: True)
        interfaces=[]
        def wlan(interface):
            interfaces.append(interface)
            return station
        wlan.IF_STA=7
        with patch.dict(sys.modules,{'network':types.SimpleNamespace(WLAN=wlan)}):
            self.assertEqual(fw._wireless_precheck(),'CYW43 STA ready')
        self.assertEqual(interfaces,[7])
        with patch.object(fw.machine,'unique_id',return_value=b'\x01',create=True), \
             patch.object(fw.os,'uname',return_value=types.SimpleNamespace(machine='Waveshare RP2350B PLUS W with RP2350',release='1.26')):
            result=fw._new_report(3600,True,0)
        self.assertEqual(result['hardware_variant'],'wireless')
        self.assertEqual(result['hardware_pins'],PINS['wireless'])
    def test_fullscreen_without_beep(self):
        r,lcd=report(),LCD()
        with patch.object(fw,'_KeyEdges',scripted([[0]])):
            fw._screen_test(r,lcd)
        self.assertEqual(lcd.fills[:5],[fw.RED,fw.GREEN,31,fw.WHITE,fw.BLACK])
        self.assertFalse(any(pin==22 for pin,_ in Pin.writes))
        self.assertEqual(r['checks']['screen']['status'],'pass')
    def test_ok_each_press_beeps_once(self):
        r=report()
        with patch.object(fw,'_KeyEdges',scripted([[1],[0],[],[0],[1]])):
            fw._buzzer_test(r,LCD())
        self.assertEqual(Pin.writes.count((22,1)),2)
        self.assertEqual(Pin.values[22],0)
        self.assertEqual(r['checks']['buzzer']['status'],'pass')
    def test_hold_requires_release(self):
        edges=fw._KeyEdges((5,))
        self.clock.now=61; self.assertEqual(edges.poll(),[])
        Pin.values[5]=0; edges.poll(); self.clock.now+=61
        self.assertEqual(edges.poll(),[0])
        self.clock.now+=1000; self.assertEqual(edges.poll(),[])
        Pin.values[5]=1; edges.poll(); self.clock.now+=61; edges.poll()
        Pin.values[5]=0; edges.poll(); self.clock.now+=61
        self.assertEqual(edges.poll(),[0])
    def test_layout_and_five_keys(self):
        self.assertEqual([k[0] for k in fw.KEY_LAYOUT],['OK','UP','DOWN','MENU','POWER'])
        r=report()
        with patch.object(fw,'_KeyEdges',scripted([[0,1,2,3,4]])),patch.object(fw,'_wait_key',return_value=True):
            fw._five_keys(r,LCD())
        self.assertTrue(all(v['status']=='pass' for v in r['checks'].values()))
    def test_percentage(self):
        self.assertIsNone(fw._uncorrectable_percent({'codewords':0}))
        self.assertEqual(fw._uncorrectable_percent({'codewords':40,'uncorrectable':3}),7.5)
    def test_precheck_requires_all_three_and_power(self):
        r,lcd=report(),LCD()
        calls=[]
        def check(result,name,fn):
            calls.append(name)
            ok = not (name == 'rtc' and calls.count('rtc') == 1)
            detail = ({'model':'DS3231'} if name == 'rtc' else
                      4.1 if name == 'battery' else {'version':'0x12','words':8})
            result['checks'][name]={'status':'pass' if ok else 'fail','detail':detail if ok else 'I2C error'}
            return ok
        with patch.object(fw,'_check',side_effect=check),patch.object(fw,'_wait_key',side_effect=[True,True]) as wait:
            self.assertTrue(fw._precheck(r,lcd,True))
        self.assertEqual(calls,['rtc','battery','radio_precheck']*2)
        self.assertEqual(wait.call_count,2)
        self.assertIn(b'RETRY >',lcd.text)
        self.assertIn(b'NEXT >',lcd.text)
        self.assertEqual(r['checks']['rtc']['status'],'pass')
    def test_precheck_timeout_prevents_next_page(self):
        r=report()
        def passed(result,name,fn):
            detail = {'model':'DS3231'} if name == 'rtc' else 4.1 if name == 'battery' else {'version':'0x12','words':8}
            result['checks'][name]={'status':'pass','detail':detail}
            return True
        with patch.object(fw,'_check',side_effect=passed),patch.object(fw,'_wait_key',return_value=False):
            with self.assertRaisesRegex(OSError,'POWER timeout'):
                fw._precheck(r,LCD(),True)
    def test_formal_precheck_waits_for_usb_and_sd_before_power(self):
        r,lcd=report(),LCD()
        def passed(result,name,fn):
            detail = {'model':'DS3231'} if name == 'rtc' else 4.1 if name == 'battery' else {'version':'0x12'}
            result['checks'][name]={'status':'pass','detail':detail}
            return True
        with patch.object(fw,'_check',side_effect=passed),patch.object(fw,'_wait_key') as wait:
            self.assertTrue(fw._precheck(r,lcd,True,defer_power=True))
        wait.assert_not_called()
        self.assertIn(b'UNPLUG USB',lcd.text)
        self.assertIn(b'INSERT SD CARD',lcd.text)
        Pin.values[24]=0
        fw._wait_detach(r,lcd,Pin(24))
        self.assertTrue(r['usb_detached'])
        with patch.object(fw,'_wait_key',return_value=True),patch.object(fw,'_check',return_value=True):
            fw._sd_presence_interactive(r,lcd)
        self.assertEqual(len(lcd.fills),1)
        self.assertIn(b'SD DETECTED',lcd.text)
    def test_sd_presence_retries_without_starting_other_tests(self):
        r,lcd=report(),LCD()
        def check(result,name,fn):
            result['checks'][name]={'status':'fail' if check.calls == 0 else 'pass',
                                    'detail':'no card' if check.calls == 0 else {'sectors':2048}}
            check.calls += 1
            return check.calls > 1
        check.calls=0
        with patch.object(fw,'_wait_key',return_value=True),patch.object(fw,'_check',side_effect=check) as called:
            fw._sd_presence_interactive(r,lcd)
        self.assertEqual(called.call_count,2)
        self.assertTrue(all(c.args[1]=='sd_present' and c.args[2] is fw._sd for c in called.call_args_list))
        self.assertEqual(r['checks']['sd_present']['status'],'pass')
        self.assertIn(b'SD NOT FOUND',lcd.text)
    def test_missing_sd_error_stays_visible_until_retry(self):
        r,lcd=report(),LCD()
        presses=0
        def wait(pin,timeout):
            nonlocal presses
            presses+=1
            if presses==1:
                return True
            self.assertGreaterEqual(self.clock.now,2_000)
            self.assertEqual(lcd.text[-2:],[b'SD NOT FOUND',b'INSERT / POWER RETRY'])
            return False
        with patch.object(fw,'_wait_key',side_effect=wait),patch.object(fw,'_check',return_value=False):
            with self.assertRaisesRegex(OSError,'SD presence POWER timeout'):
                fw._sd_presence_interactive(r,lcd)
        self.assertEqual(presses,2)
    def test_sd_is_ready_before_interactive_checks(self):
        r=report(); r['state']='preflight'; r['events']=[[0,'check','rtc']]
        r['errors']=[]; r['events_written']=0; r['errors_written']=0
        store=types.SimpleNamespace(root='/sd/LBJ_DIAG/test',enqueue=lambda key,item: True,
                                    flush_all=lambda: None,save_summary=lambda report: None)
        order=[]
        def mount(result,lcd):
            order.append('full_sd')
            fw.ACTIVE_SD=store
        try:
            with patch.object(fw,'_wait_detach',side_effect=lambda *args:order.append('detach')), \
                 patch.object(fw,'_sd_presence_interactive',side_effect=lambda *args:order.append('presence')), \
                 patch.object(fw,'_sd_interactive',side_effect=mount), \
                 patch.object(fw,'_record',side_effect=lambda *args:order.append('usb_log')):
                fw._prepare_sd(r,LCD(),None)
            self.assertEqual(order,['detach','presence','full_sd','usb_log'])
            self.assertEqual(r['state'],'interactive')
            self.assertEqual(r['events_written'],1)
            self.assertEqual(r['events'],[])
        finally:
            fw.ACTIVE_SD=None
    def test_average_uses_each_received_packet(self):
        r={'received_rssi_sum_dbm':0.0,'received_rssi_count':0}
        for value in ('-90.0dBm','-100.0dBm','N/A',None):
            fw._received_rssi(r,{'rssi':value})
        self.assertEqual(r['received_rssi_count'],2)
        self.assertEqual(r['received_rssi_sum_dbm']/r['received_rssi_count'],-95)
        r['received_rssi_avg_dbm']=-95
        self.assertEqual(average_received_rssi(r),'-95.00 dBm（2 条）')
        self.assertEqual(installed_version({'present':True,'version':'5.8','edition':'Release'}),'v5.8 Release')
    def test_log_memory_reserve_and_truncation(self):
        r=report()
        with patch.object(fw.gc,'mem_free',return_value=100_000):
            fw._record(r,'rx','x'*400)
        self.assertEqual(r['events_omitted'],1)
        self.assertEqual(len(r['events']),0)
        with patch.object(fw.gc,'mem_free',return_value=200_000):
            fw._record(r,'rx','x'*(fw.MAX_LOG_DETAIL+100))
        self.assertEqual(len(r['events'][0][2]),fw.MAX_LOG_DETAIL)
        self.assertEqual(r['details_truncated'],1)
    def test_preflight_log_retries_after_gc_before_omitting(self):
        r=report()
        with patch.object(fw.gc,'mem_free',side_effect=[1000,200_000]), \
             patch.object(fw.gc,'collect') as collect:
            fw._record(r,'key','POWER=pass')
        collect.assert_called_once()
        self.assertEqual(r['events_omitted'],0)
        self.assertEqual(r['events'][0][1:],["key","POWER=pass"])
    def test_snapshot_memory_error_is_counted(self):
        r=report()
        with patch.object(fw,'_sample',side_effect=MemoryError):
            self.assertIsNone(fw._safe_sample(r,None,60))
        self.assertEqual(r['snapshots_omitted'],1)
    def test_minute_memory_snapshot_and_summary(self):
        r=report()
        r.update(received=0,received_types={},last_rx_type='none',events_written=0,
                 battery_start_v=None,battery_start_percent=None,battery_end_v=None,
                 battery_end_percent=None,battery_min_v=None,battery_max_v=None,
                 chip_temp_max_c=None)
        rx=types.SimpleNamespace(get_health_snapshot=lambda: {'codewords':0},get_rssi=lambda:'N/A')
        with patch.object(fw,'_battery',return_value=4.0),patch.object(fw,'_temp',return_value=25.0), \
             patch.object(fw.gc,'mem_free',side_effect=[120000,80000]):
            first=fw._sample(r,rx,0)
            second=fw._sample(r,rx,60)
        self.assertEqual([first['heap_free_bytes'],second['heap_free_bytes']],[120000,80000])
        self.assertEqual(second['heap_free_min_bytes'],80000)
        self.assertEqual(r['heap_free_sample_min_bytes'],80000)
        self.assertEqual(r['heap_free_sample_max_bytes'],120000)
        self.assertEqual(r['heap_free_sample_avg_bytes'],100000)
        self.assertEqual(r['heap_free_sample_count'],2)
    def test_new_rf_fail_boundaries_and_recovery_warning(self):
        r=report()
        r.update(elapsed_s=3600,battery_start_v=4.1,battery_end_v=4.0,
                 battery_min_v=4.0,require_detached=True,usb_detached=True,
                 usb_reconnects=0,sd_removed=False)
        h=dict(words=100,bits_one=1,bits_total=100,syncs=1,codewords=100,
               uncorrectable=50,fifo_full_hits=9,recoveries=2)
        r['final_health']=h
        fw._verdict(r,3600)
        self.assertEqual(r['verdict'],'PASS')
        self.assertEqual(r['warnings'],['radio_recoveries=2'])
        h['fifo_full_hits']=10
        fw._verdict(r,3600)
        self.assertEqual(r['failures'],['fifo_full_hits'])
        h['fifo_full_hits']=9
        h['uncorrectable']=51
        fw._verdict(r,3600)
        self.assertEqual(r['failures'],['uncorrectable_over_50_percent'])
        h['codewords']=0
        fw._verdict(r,3600)
        self.assertNotIn('uncorrectable_over_50_percent',r['failures'])
    def test_sd_failure_cannot_leave_sd_page(self):
        r=report();lcd=LCD()
        with patch.object(fw,'_wait_key',side_effect=[True,True,False]), \
             patch.object(fw,'_check',return_value=False) as check:
            with self.assertRaisesRegex(OSError,'RF not started'):
                fw._sd_interactive(r,lcd)
        self.assertEqual(check.call_count,2)
        self.assertTrue(all(call.args[1]=='sd_storage' for call in check.call_args_list))

class HardwareSelectionTests(unittest.TestCase):
    def test_detection_manual_override_and_legacy_report_label(self):
        pico='Raspberry Pi Pico with RP2040'
        waveshare='Waveshare RP2350B PLUS W with RP2350'
        self.assertEqual(resolve_variant(pico),'standard')
        self.assertEqual(resolve_variant(waveshare),'wireless')
        self.assertEqual(resolve_variant(waveshare,'standard'),'standard')
        self.assertEqual(resolve_variant('Generic RP2350','wireless'),'wireless')
        with self.assertRaisesRegex(ValueError,'GP41/42'):
            resolve_variant(pico,'wireless')
        with self.assertRaises(ValueError):
            resolve_variant('ESP32')
        self.assertEqual(model_label({'board':waveshare}),'LBJ W 版')
        self.assertEqual(model_label({'board':waveshare,'hardware_variant':'standard'}),'LBJ 普通版')
    def test_identity_probe_supports_wireless_and_resumes_program(self):
        from unittest.mock import Mock
        conn=Mock()
        with patch.object(loader,'find_port',return_value='/dev/test'),patch.object(loader,'transport',return_value=conn), \
             patch.object(loader,'execute',return_value=loader.MARK+'{"board":"Waveshare RP2350B PLUS W with RP2350","uid":"01","ram":300000,"micropython":"1.26"}'), \
             patch.object(loader,'original_system',return_value={'present':True,'version':'6'}):
            result=loader.inspect_device('/dev/test')
        self.assertEqual(result['detected_variant'],'wireless')
        self.assertEqual(result['sn'],'000000000001')
        conn.exit_raw_repl.assert_called_once()
        conn.serial.write.assert_called_once_with(b'\x04')
        conn.close.assert_called_once()
    def test_gui_manual_selection_is_sent_to_loader(self):
        from unittest.mock import Mock
        import gui
        app=types.SimpleNamespace(port=types.SimpleNamespace(get=lambda:'/dev/test'),
            variant=types.SimpleNamespace(get=lambda:'W 版'),_start_task=Mock())
        gui.DiagnosticApp.load_firmware(app)
        self.assertEqual(app._start_task.call_args.args[1],
                         ['start','--port','/dev/test','--variant','wireless'])


class TrendTests(unittest.TestCase):
    def test_intervals_and_missing_denominator(self):
        samples=[dict(s=t,battery_v=4.1,battery_percent=87,health=dict(codewords=n,uncorrectable=b))
                 for t,n,b in [(0,0,0),(60,100,5),(90,120,7),(120,120,7)]]
        rows=minute_series({'snapshots':samples})
        self.assertIsNone(rows[0]['bad_delta'])
        self.assertEqual(rows[1]['bad_delta'],5)
        self.assertEqual(rows[2]['minute'],1.5)
        self.assertEqual(rows[2]['interval_percent'],10)
        self.assertIsNone(rows[3]['interval_percent'])
    def test_dashboard_metrics_and_missing_samples(self):
        sample = dict(s=60, battery_v=4.0, battery_percent=75, chip_temp_c=31,
                      rssi_dbm='-103.5 dBm', received=9,
                      heap_free_bytes=85000,heap_free_min_bytes=70000,
                      health=dict(words=100, fifo_full_hits=2, raw_dropped=3,
                                  recoveries=1, codewords=20, syncs=4, uncorrectable=2))
        row = minute_series({'snapshots':[sample]})[0]
        self.assertEqual(row['rssi'], -103.5)
        self.assertEqual(row['heap_free'],85000)
        self.assertEqual(row['heap_free_min'],70000)
        self.assertEqual(row['bad_percent'], 10)
        for _, series in CHARTS:
            for key, _, _ in series:
                self.assertIn(key, row)
        rows = minute_series({'snapshots':[sample, {'s':120,'health':{}}]})
        for key in ('bad_total','bad_delta','fifo_delta','received_delta','voltage'):
            self.assertIsNone(rows[1][key])

    def test_reset_is_missing_not_negative(self):
        rows=minute_series({'snapshots':[{'s':0,'health':{'uncorrectable':5}}, {'s':60,'health':{'uncorrectable':0}}]})
        self.assertIsNone(rows[1]['bad_delta'])
    def test_minute_and_cumulative_axes_scale_independently(self):
        rows=[{'bad_delta':20,'bad_total':1000}, {'bad_delta':40,'bad_total':2000}]
        ranges=chart_axis_ranges(rows,CHARTS[0][1])
        self.assertEqual(ranges['left'][0],0)
        self.assertLess(ranges['left'][1],100)
        self.assertEqual(ranges['right'][0],0)
        self.assertGreater(ranges['right'][1],2000)
        self.assertEqual(chart_axis_ranges(rows,[('bad_delta','本分钟','#fff')])['right'],None)

class TrainRecordTests(unittest.TestCase):
    def test_every_rx_callback_has_a_row_with_basic_and_extended_fields(self):
        import json
        train={'type':'train_data_merged','basic':{'train_no':'501','speed_kmh':'26','km_post':10.4},
               'extended':{'loco_type':'HXD3D-0369','lat':'39N','lon':'116E'},
               'rssi':'-85.0dBm','ric':'1234002-F1','raw':'raw packet'}
        report={'received':3,'events':[[10,'check','rtc'],[42,'rx_train_data_merged',json.dumps(train)],
                                       [43,'rx_time_sync',json.dumps({'type':'time_sync','time':'18:34'})]],
                'errors':[[44,'rx_error',json.dumps({'type':'error','raw':'damaged'})],
                          [45,'radio_recovery','1']]}
        rows=received_rows(report)
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]['train_no'],'501')
        self.assertEqual(rows[0]['loco'],'HXD3D-0369')
        self.assertEqual(rows[0]['rssi'],'-85.0dBm')
        self.assertTrue(has_train_number(rows[0]))
        self.assertFalse(has_train_number(rows[1]))
        self.assertEqual(rows[2]['type'],'error')
    def test_truncated_rx_json_still_appears(self):
        rows=received_rows({'events':[[5,'rx_basic_only','{"type":"basic_only"']]})
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['parse_error'])
        self.assertIn('raw_log_detail',rows[0]['payload'])

class SDImportTests(unittest.TestCase):
    def test_reads_journals_and_flags_missing_rows(self):
        import json, tempfile
        from flash_and_report import is_sd_report_path
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'LBJ_DIAG'/'run1';root.mkdir(parents=True)
            path=root/'report.json'
            path.write_text(json.dumps({'format':1,'sn':'ABC','state':'done',
                'verdict':'PASS','events_written':2,'errors_written':0,'snapshots_written':1}))
            (root/'events.jsonl').write_text('[1,"rx","data"]\n')
            (root/'snapshots.jsonl').write_text('{"s":60,"health":{}}\n')
            self.assertFalse(is_sd_report_path(path))
            with patch('flash_and_report.is_sd_report_path',return_value=True):
                report=read_sd_report(path)
            self.assertEqual(len(report['snapshots']),1)
            self.assertEqual(report['verdict'],'INCOMPLETE')
            self.assertIn('events_journal_mismatch',report['incomplete'])
    def test_sd_stream_writes_summary_and_detects_removal(self):
        import json, tempfile, importlib
        with patch.dict(sys.modules,{'machine':types.SimpleNamespace(Pin=Pin)}):
            storage=importlib.import_module('sd_report')
        class SPI:
            def __init__(self,*args,**kwargs): pass
            def init(self,**kwargs): pass
        class Card:
            removed=False
            def __init__(self,spi,cs): pass
            def readblocks(self,block,buffer):
                if self.removed: raise OSError('removed')
                buffer[:]=bytes(512)
            def ioctl(self,op,arg): return 100000
        with tempfile.TemporaryDirectory() as temp:
            mount=Path(temp)/'sd';mount.mkdir()
            with patch.object(storage,'MOUNT',str(mount)), \
                 patch.object(storage,'machine',types.SimpleNamespace(Pin=Pin,SPI=SPI)), \
                 patch.object(storage.os,'mount',create=True), \
                 patch.object(storage.os,'umount',create=True), \
                 patch.object(storage.os,'VfsFat',side_effect=lambda c:c,create=True):
                card=storage.SDReport(Card,'run1')
                self.assertTrue(card.enqueue('events',[0,'rx','data']))
                card.flush_all()
                card.save_summary({'format':1,'sn':'ABC','state':'done','verdict':'PASS',
                                   'events':[],'errors':[],'snapshots':[],'start_tick':1})
                self.assertEqual(json.loads((mount/'LBJ_DIAG'/'run1'/'report.json').read_text())['sn'],'ABC')
                self.assertTrue((mount/'LBJ_DIAG'/'run1'/'complete.json').exists())
                self.assertEqual(json.loads((mount/'LBJ_DIAG'/'run1'/'events.jsonl').read_text()),[0,'rx','data'])
                card.card.removed=True
                with self.assertRaises(storage.SDRemoved): card.check_present()

if __name__=='__main__': unittest.main()
