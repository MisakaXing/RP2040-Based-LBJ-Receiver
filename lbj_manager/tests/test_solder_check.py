import ast
from pathlib import Path
import queue
import sys
import unittest
from types import SimpleNamespace, MethodType
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import solder_check as check


class ButtonTests(unittest.TestCase):
    def test_initial_grounded_button_does_not_pass(self):
        progress=check.ButtonProgress()
        for t in (0,.1,1,2): progress.feed([0]*5,t)
        self.assertEqual(progress.passed,[False]*5)

    def test_debounce_release_then_press_latches(self):
        p=check.ButtonProgress()
        p.feed([1]*5,0);p.feed([1]*5,.1)
        p.feed([0]*5,.2)
        self.assertFalse(any(p.feed([0]*5,.23)))
        self.assertTrue(all(p.feed([0]*5,.3)))
        self.assertTrue(all(p.feed([1]*5,.4)))

    def test_each_pin_is_independent(self):
        p=check.ButtonProgress()
        p.feed([1]*5,0);p.feed([1]*5,.1)
        p.feed([1,1,0,1,1],.2)
        self.assertEqual(p.feed([1,1,0,1,1],.3),[False,False,True,False,False])

    def test_bad_sample_is_error(self):
        for v in ([1]*4,[2]*5):
            with self.assertRaises(ValueError):check.ButtonProgress().feed(v,0)


class ReportTests(unittest.TestCase):
    def test_html_report_escapes_device_and_log_text(self):
        result=check.report_html('<port>',{'board':'<script>'},[{'status':'fail','detail':'<script>alert(1)</script>'}]*len(check.STEPS),'reset')
        self.assertNotIn('<script>',result)
        self.assertIn('&lt;script&gt;',result)
        self.assertEqual(result.count('<section '),5)
    def test_skips_never_report_all_passed(self):
        for status in ("skip","pending","running"):
            rows=[{"status":"pass"} for _ in check.STEPS]
            rows[1]["status"]=status
            self.assertIn("未完成",check.overall(rows))
        self.assertEqual(check.overall([{"status":"pass"}]*len(check.STEPS)),"全部检查通过")
        self.assertEqual(check.overall([{"status":"fail"}]*4),"检查未通过")

    def test_report_has_device_manual_confirmation_and_limitations(self):
        text=check.report_text("test-port",{"uid":"abc"},[{"status":"skip","detail":"用户跳过"}]*4,"恢复失败")
        for part in ("abc","test-port","恢复失败","屏幕与蜂鸣器由用户确认","不验证写入能力"):
            self.assertIn(part,text)

    def test_missing_or_malformed_result_fails(self):
        for output in ("done",check.PREFIX+"[]",check.PREFIX+"{"):
            with self.assertRaises(ValueError):check.result_json(output)
        self.assertEqual(check.result_json("noise\n"+check.PREFIX+'{"ok":true}'),{"ok":True})


class ScriptTests(unittest.TestCase):
    def test_core_builder_keeps_battery_even_on_radio_exception(self):
        source=(ROOT/'lbj_manager.py').read_text()
        tree=ast.parse(source)
        legacy=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='HARDWARE_TEST_SCRIPT' for t in n.targets))
        built=check.core_script(legacy)
        ast.parse(built)
        self.assertIn('Core_Error',built)
        self.assertIn("test_results['Battery']",built)
        self.assertNotIn('所有核心硬件模块均工作在最佳状态',built)

    def test_board_variants_select_distinct_adc_and_wireless_check(self):
        source=(ROOT/'lbj_manager.py').read_text()
        tree=ast.parse(source)
        legacy=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='HARDWARE_TEST_SCRIPT' for t in n.targets))
        standard=check.core_script(legacy,False)
        wireless=check.core_script(legacy,True)
        ast.parse(standard)
        ast.parse(wireless)
        self.assertIn('machine.ADC(machine.Pin(27))',standard)
        self.assertNotIn("_core_step('Wireless'",standard)
        self.assertIn('machine.ADC(machine.Pin(46))',wireless)
        self.assertNotIn('machine.ADC(machine.Pin(41))',wireless)
        self.assertIn("_core_step('Wireless','running')",wireless)
        self.assertTrue(check.is_wireless_board('Waveshare RP2350B PLUS W with RP2350'))
        self.assertFalse(check.is_wireless_board('Raspberry Pi Pico with RP2040'))

    def test_scripts_compile(self):
        for script in (check.screen_script(),check.sd_script(),check.BATTERY_SCRIPT,check.battery_script(41),check.WIRELESS_SCRIPT,check.BUZZER_SCRIPT):
            ast.parse(script)

    def test_sd_probe_does_not_write_or_mount(self):
        script=check.sd_script()
        # Embedded driver defines writeblocks, but the diagnostic must not call it.
        tree=ast.parse(script)
        calls=[n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)]
        self.assertIn("readblocks",calls)
        for forbidden in ("writeblocks","mkfs","mount","remove"):
            self.assertNotIn(forbidden,calls)

    def test_drivers_bundled_for_blank_boards(self):
        for name in ("sdcard.py","ili9341.py"):
            ast.parse(check.driver_source(name))


class WorkerTests(unittest.TestCase):
    def test_bytewise_log_burst_is_batched_and_eof_excluded(self):
        w=check.InspectionWorker('test','');w.transport=Mock()
        payload=('line of output\n'*3000).encode()
        def stream(script,timeout,data_consumer):
            for byte in payload+b'\x04':data_consumer(bytes([byte]))
            return b'',b''
        w.transport.exec_raw.side_effect=stream
        self.assertEqual(w.execute('test',live=True),payload.decode())
        self.assertLess(w.events.qsize(),100)
        self.assertEqual(''.join(w.events.get_nowait()[1] for _ in range(w.events.qsize())),payload.decode())

    def test_timeout_flushes_last_partial_log(self):
        w=check.InspectionWorker('test','');w.transport=Mock()
        def stream(script,timeout,data_consumer):
            data_consumer(b'last error')
            raise TimeoutError('timeout')
        w.transport.exec_raw.side_effect=stream
        with self.assertRaises(TimeoutError):w.execute('test',live=True)
        self.assertEqual(w.events.get_nowait(),('log','last error'))
    def test_core_progress_is_streamed_before_completion(self):
        w=check.InspectionWorker('test','');w.transport=Mock()
        payload=(check.CORE_PREFIX+'{"key":"RTC","state":"running"}\n'+check.CORE_PREFIX+'{"key":"RTC","state":"pass"}\n').encode()
        def stream(script,timeout,data_consumer):
            for byte in payload:data_consumer(bytes([byte]))
            return b'',b''
        w.transport.exec_raw.side_effect=stream
        w.execute('test',live=True)
        events=[]
        while not w.events.empty():
            kind,value=w.events.get_nowait()
            if kind=='core_step':events.append(value['state'])
        self.assertEqual(events,['running','pass'])
    def test_live_log_preserves_split_utf8_and_full_result(self):
        w=check.InspectionWorker('test','');w.transport=Mock()
        payload=('检查通过\n'+check.PREFIX+'{"ok":true}').encode()
        def stream(script,timeout,data_consumer):
            for byte in payload:data_consumer(bytes([byte]))
            return b'',b''
        w.transport.exec_raw.side_effect=stream
        output=w.execute('test',live=True)
        self.assertEqual(output,payload.decode())
        text=''.join(w.events.get_nowait()[1] for _ in range(w.events.qsize()))
        self.assertEqual(text,payload.decode())
        self.assertEqual(check.result_json(output),{'ok':True})
    def run_jobs(self, transport, *jobs):
        w=check.InspectionWorker("test","",transport_factory=lambda *a,**k:transport)
        for job in jobs:w.jobs.put(job)
        w.start();w.join(3)
        self.assertFalse(w.is_alive())
        events=[]
        while not w.events.empty():events.append(w.events.get_nowait())
        return events

    def test_connect_and_close_restore_and_release_serial(self):
        t=Mock()
        t.exec_raw.return_value=(b'LBJ_CHECK_JSON:{"board":"RP2040","uid":"abc"}\n',b'')
        events=self.run_jobs(t,"connect","close")
        self.assertEqual(events[0][0],"connect")
        self.assertEqual(events[-1][0],"closed")
        t.exec_raw_no_follow.assert_called_once()
        t.close.assert_called_once()

    def test_w_board_connect_selects_wireless_diagnostics(self):
        t=Mock()
        t.exec_raw.return_value=(b'LBJ_CHECK_JSON:{"board":"Waveshare RP2350B PLUS W with RP2350"}\n',b'')
        w=check.InspectionWorker('test','',transport_factory=lambda *a,**k:t)
        w.jobs.put('connect');w.jobs.put('close');w.start();w.join(3)
        self.assertTrue(w.wireless)
        self.assertFalse(w.is_alive())

    def test_errors_are_reported_and_close_still_runs(self):
        t=Mock();t.enter_raw_repl.side_effect=OSError("USB disconnected")
        events=self.run_jobs(t,"connect","close")
        self.assertEqual(events[0][0],"error")
        t.close.assert_called_once()

    def test_reset_failure_is_not_reported_as_success(self):
        t=Mock()
        t.exec_raw.return_value=(b'LBJ_CHECK_JSON:{"board":"RP2350","uid":"abc"}',b'')
        t.exec_raw_no_follow.side_effect=OSError("lost USB")
        events=self.run_jobs(t,"connect","close")
        self.assertIn("恢复失败",events[-1][1])

    def test_no_device_all_skip_does_not_open_serial(self):
        t=Mock();events=self.run_jobs(t,"close")
        self.assertEqual(events[-1],("closed","未连接，无需恢复"))
        t.enter_raw_repl.assert_not_called()

    def test_exec_errors_are_not_passes(self):
        w=check.InspectionWorker("test","");w.transport=Mock()
        w.transport.exec_raw.return_value=(b"",b"OSError: no SD card")
        with self.assertRaisesRegex(RuntimeError,"no SD card"):w.execute("test")


class WorkflowTests(unittest.TestCase):
    def test_pump_yields_even_with_large_backlog(self):
        w=SimpleNamespace(worker=SimpleNamespace(events=queue.Queue()),handle=Mock(),winfo_exists=lambda:True,after=Mock(),pump=Mock())
        for _ in range(1000):w.worker.events.put(('log','data'))
        check.InspectionWindow.pump(w)
        self.assertLessEqual(w.handle.call_count,20)
        self.assertGreater(w.worker.events.qsize(),0)
        w.after.assert_called_once()
    def test_progress_event_keeps_worker_busy(self):
        w=self.window(0);w.busy=True
        w.handle('core_step',{'key':'RTC','state':'running'})
        self.assertTrue(w.busy)
        w.core_panel.update_step.assert_called_once_with('RTC','running','')
        w.advance.assert_not_called()
    def test_auto_start_next_buttons_screen_and_sd_but_not_buzzer(self):
        for previous,delay in ((0,350),(1,350),(3,5000)):
            w=self.window(previous)
            check.InspectionWindow.advance(w)
            self.assertEqual(w.after.call_args.args[0],delay)
            callback=w.after.call_args.args[1]
            callback();w.start_step.assert_called_once()
        w=self.window(2)
        check.InspectionWindow.advance(w)
        w.after.assert_not_called()

    def test_scheduled_start_is_ignored_after_skip_or_close(self):
        w=self.window(0)
        check.InspectionWindow.advance(w)
        callback=w.after.call_args.args[1]
        w.index=3;callback();w.start_step.assert_not_called()
        w.index=1;w.closing=True;callback();w.start_step.assert_not_called()

    def test_button_gpio_mapping(self):
        self.assertEqual(dict(check.BUTTONS)['UP'],4)
        self.assertEqual(dict(check.BUTTONS)['DOWN'],3)
        self.assertEqual(check.BUTTON_PINS,(2,4,3,5,28))
        self.assertEqual(tuple(pin for _,pin in check.button_mapping(False)),(2,4,3,5,28))
        self.assertEqual(tuple(pin for _,pin in check.button_mapping(True)),(2,4,3,5,42))

    def test_w_button_report_uses_gp42(self):
        w=self.window(1);w.wireless=True
        w.handle('buttons',[True,True,True,True,False])
        self.assertEqual(w.results[1]['status'],'fail')
        self.assertIn('POWER/GP42：未识别',w.results[1]['detail'])
    def window(self,index=0):
        window=SimpleNamespace(index=index,busy=False,closing=False,connected=True,wireless=False,
            skip_pending=False,results=[{"status":"pending","detail":""} for _ in check.STEPS],
            worker=SimpleNamespace(cancel=Mock(),jobs=queue.Queue()),
            primary=Mock(),skip_btn=Mock(),fail_btn=Mock(),confirm_btn=Mock(),note=Mock(),diagram=Mock(),buzzer_count=0,
            buzzer_big=Mock(),buzzer_caption=Mock(),step_labels=[Mock() for _ in check.STEPS],subtitle=Mock(),port='test',core_panel=Mock(),
            render=Mock(),advance=Mock(),after=Mock(),start_step=Mock())
        for method in ("handle","skip","confirm_screen","confirm_buzzer","continue_core"):
            setattr(window,method,MethodType(getattr(check.InspectionWindow,method),window))
        return window

    def test_screen_output_alone_is_not_a_pass(self):
        w=self.window(2);w.handle("screen",{"patterns":True})
        self.assertEqual(w.results[2]["status"],"pending")
        w.advance.assert_not_called()
        w.confirm_screen(True)
        self.assertEqual(w.results[2]["status"],"pass")
        w.advance.assert_called_once()

    def test_screen_rejection_stays_failed(self):
        w=self.window(2);w.confirm_screen(False);w.skip()
        self.assertEqual(w.results[2]["status"],"fail")

    def test_skip_running_waits_for_response(self):
        w=self.window(1);w.busy=True;w.skip()
        self.assertTrue(w.skip_pending)
        w.advance.assert_not_called()
        w.worker.cancel.set.assert_called_once()
        w.handle("buttons",[True]*5)
        self.assertEqual(w.results[1]["status"],"skip")
        w.advance.assert_called_once()

    def test_all_green_delays_then_auto_advances(self):
        w=self.window(1);w.handle("buttons",[True]*5)
        self.assertEqual(w.results[1]["status"],"pass")
        w.advance.assert_not_called()
        delay,callback=w.after.call_args.args
        self.assertEqual(delay,650)
        callback();w.advance.assert_called_once()

    def test_incomplete_keys_fail_not_pass(self):
        w=self.window(1);w.handle("buttons",[True,False,True,True,True])
        self.assertEqual(w.results[1]["status"],"fail")
        w.advance.assert_not_called()

    def test_no_sd_exception_is_failure_and_retry_reconnects(self):
        w=self.window(4);w.handle("error",("sd","no SD card"))
        self.assertEqual(w.results[4]["status"],"fail")
        self.assertFalse(w.connected)

    def test_buzzer_pulse_requires_human_confirmation(self):
        w=self.window(3);w.handle("buzzer",{"pulse_ms":120})
        self.assertEqual(w.results[3]["status"],"pending")
        self.assertEqual(w.buzzer_count,1)
        w.render.assert_not_called()
        w.confirm_btn.pack.assert_not_called()
        w.fail_btn.pack.assert_not_called()
        w.advance.assert_not_called()
        w.confirm_buzzer(True)
        self.assertEqual(w.results[3]["status"],"pass")
        w.advance.assert_called_once()

    def test_core_pass_waits_for_user_confirmation(self):
        w=self.window(0)
        w.handle('core',({'RTC':True,'SX1276_SPI':True,'SX1276_Signal':True,'Battery':True,'Battery_V':4.1},'live log'))
        self.assertEqual(w.results[0]['status'],'pass')
        w.render.assert_not_called()
        w.advance.assert_not_called()
        w.continue_core()
        w.advance.assert_called_once()

    def test_w_core_requires_wireless_result(self):
        w=self.window(0);w.wireless=True
        base={'RTC':True,'SX1276_SPI':True,'SX1276_Signal':True,
              'Battery':True,'Battery_V':4.1}
        w.handle('core',(base,'log'))
        self.assertEqual(w.results[0]['status'],'fail')
        w.advance.assert_not_called()
        w.handle('core',({**base,'Wireless':True},'log'))
        self.assertEqual(w.results[0]['status'],'pass')
        w.advance.assert_not_called()

    def test_core_failure_cannot_continue_as_passed(self):
        w=self.window(0);w.results[0]['status']='fail'
        w.continue_core();w.advance.assert_not_called()

    def test_connect_populates_dedicated_identity_line(self):
        w=self.window();w.identity=Mock()
        device={'SN':'0123456789AB','board':'Pico with RP2040','micropython':'1.25.0','cpu_mhz':150}
        w.handle('connect',device)
        self.assertEqual(w.device,device)
        w.identity.configure.assert_called_once_with(text=check.identity_text(device))
        self.assertIn('S/N：0123456789AB',check.identity_text(device))

    def test_completed_close_returns_to_log_without_another_reset(self):
        w=self.window();w.finished=True;w.parent=Mock()
        check.InspectionWindow.close(w)
        w.parent.dismiss_inspection.assert_called_once()
        self.assertTrue(w.worker.jobs.empty())

    def test_identity_sn_matches_firmware_twelve_character_format(self):
        import io
        from contextlib import redirect_stdout
        os=SimpleNamespace(uname=lambda:SimpleNamespace(machine='Pico with RP2040',release='1.25.0'))
        for uid,expected in ((b'\xff'*8,'W5E11264SGSF'),(b'\x00'*8,'000000000000'),(b'\x01','000000000001')):
            with self.subTest(uid=uid):
                machine=SimpleNamespace(unique_id=lambda:uid,freq=lambda:150000000)
                with patch.dict('sys.modules',{'machine':machine,'os':os}),redirect_stdout(io.StringIO()) as output:
                    exec(check.IDENTITY_SCRIPT,{})
                result=check.result_json(output.getvalue())
                self.assertEqual(result['SN'],expected)
                self.assertEqual(result['cpu_mhz'],150)

    def test_device_identity_avoids_unsupported_padding_methods(self):
        # CPython execution alone does not detect this MicroPython API gap.
        import ast
        attributes={node.attr for node in ast.walk(ast.parse(check.IDENTITY_SCRIPT)) if isinstance(node,ast.Attribute)}
        self.assertFalse(attributes & {'rjust','ljust','zfill'})


class ElectricalTests(unittest.TestCase):
    def hardware(self,raw=36000):
        pin=Mock()
        pin_type=Mock(return_value=pin);pin_type.OUT=1
        adc=Mock();adc.read_u16.return_value=raw
        machine=SimpleNamespace(Pin=pin_type,ADC=Mock(return_value=adc))
        return machine,pin,adc

    def test_battery_voltage_and_gate_restored(self):
        machine,pin,adc=self.hardware()
        ns=dict(machine=machine,time=Mock(),test_results={})
        exec(check.BATTERY_SCRIPT,ns)
        self.assertTrue(ns['test_results']['Battery'])
        self.assertAlmostEqual(ns['test_results']['Battery_V'],36000/65535*6.6+.174,places=3)
        self.assertEqual(pin.value.call_args_list[-1].args,(1,))
        self.assertEqual(adc.read_u16.call_count,8)

    def test_w_battery_uses_gp41_and_restores_gate(self):
        machine,pin,adc=self.hardware()
        ns=dict(machine=machine,time=Mock(),test_results={})
        exec(check.battery_script(41),ns)
        self.assertEqual(machine.ADC.call_args.args[0],pin)
        self.assertIn(41,[call.args[0] for call in machine.Pin.call_args_list])
        self.assertTrue(ns['test_results']['Battery'])
        self.assertEqual(pin.value.call_args_list[-1].args,(1,))

    def test_wireless_probe_starts_and_stops_sta(self):
        wlan=Mock()
        wlan.active.side_effect=lambda enabled=None: enabled if enabled is not None else True
        network=SimpleNamespace(WLAN=Mock(return_value=wlan),STA_IF=0)
        events=[]
        ns={'_core_step':lambda *args:events.append(args),
            'test_results':{},'time':SimpleNamespace(ticks_ms=lambda:0,
            ticks_add=lambda a,b:a+b,ticks_diff=lambda a,b:a-b,sleep_ms=lambda n:None)}
        with patch.dict(sys.modules,{'network':network}):
            exec(check.WIRELESS_SCRIPT,ns)
        self.assertTrue(ns['test_results']['Wireless'])
        self.assertEqual([call.args for call in wlan.active.call_args_list],[(True,),(),(),(False,)])
        self.assertEqual(events[-1][:2],('Wireless','pass'))

    def test_wireless_probe_failure_is_not_pass_and_stops_sta(self):
        wlan=Mock();wlan.active.side_effect=[None,False,False,None]
        network=SimpleNamespace(WLAN=Mock(return_value=wlan),STA_IF=0)
        events=[]
        ns={'_core_step':lambda *args:events.append(args),'test_results':{},
            'time':SimpleNamespace(ticks_ms=lambda:0,ticks_add=lambda a,b:a+b,
            ticks_diff=lambda a,b:-1,sleep_ms=lambda n:None)}
        with patch.dict(sys.modules,{'network':network}):
            exec(check.WIRELESS_SCRIPT,ns)
        self.assertFalse(ns['test_results']['Wireless'])
        wlan.active.assert_called_with(False)
        self.assertEqual(events[-1][:2],('Wireless','fail'))

    def test_battery_failure_restores_gate_and_is_reported(self):
        machine,pin,adc=self.hardware();adc.read_u16.side_effect=OSError('ADC unavailable')
        ns=dict(machine=machine,time=Mock(),test_results={})
        exec(check.BATTERY_SCRIPT,ns)
        self.assertFalse(ns['test_results']['Battery'])
        self.assertIn('ADC unavailable',ns['test_results']['Battery_Error'])
        self.assertEqual(pin.value.call_args_list[-1].args,(1,))

    def test_buzzer_single_pulse_and_off_on_error(self):
        for raises in (False,True):
            machine,pin,adc=self.hardware()
            clock=Mock()
            if raises:clock.sleep_ms.side_effect=OSError('interrupted')
            with patch.dict('sys.modules',{'machine':machine,'time':clock}):
                if raises:
                    with self.assertRaises(OSError):exec(check.BUZZER_SCRIPT,{})
                else:exec(check.BUZZER_SCRIPT,{})
            self.assertEqual([call.args for call in pin.value.call_args_list],[(1,),(0,)])
            clock.sleep_ms.assert_called_once_with(120)


if __name__=="__main__":unittest.main()
