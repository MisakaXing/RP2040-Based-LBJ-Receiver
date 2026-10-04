import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import device_protection as policy

ROOT = Path(__file__).resolve().parents[1]


def functions(names, ns):
    tree = ast.parse((ROOT / 'main.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'main.py', 'exec'), ns)
    return ns


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(ticks_diff=lambda a, b: (a-b+32768)%65536-32768)
        self.mock_clock = patch.object(policy, 'time', self.clock)
        self.mock_clock.start()
        self.addCleanup(self.mock_clock.stop)
        self.p = policy.DeviceProtection()

    def test_percent_boundary_and_clamp(self):
        for voltage, expected in ((0,0),(3.44,0),(3.45,0),(3.45001,1),(4.2,100),(5,100)):
            self.assertEqual(policy.battery_percent(voltage), expected)

    def test_temp_red_boundary(self):
        for temp, red in ((None,False),(44.9,False),(45,False),(45.1,True),(60,True)):
            self.p.update(90,temp,False)
            self.assertEqual(self.p.temperature_red(),red)

    def test_high_temp_boundary(self):
        self.p.update(90,60,False)
        self.assertFalse(self.p.overheat)
        self.assertIsNone(self.p.top_warning(0))
        self.p.update(90,60.1,False)
        self.assertTrue(self.p.overheat)
        self.assertEqual(self.p.top_warning(1),b'HIGH TEMP')

    def test_usb_suppresses_only_battery_warnings(self):
        self.p.update(0,61,True)
        self.assertFalse(self.p.low_battery())
        self.assertFalse(self.p.empty_battery())
        self.assertTrue(self.p.overheat)
        self.assertEqual(self.p.top_warning(0),b'HIGH TEMP')

    def test_low_battery_boundary(self):
        for percent, expected in ((None,False),(10,False),(9,True),(0,True)):
            self.p.update(percent,30,False)
            self.assertEqual(self.p.low_battery(),expected)
            self.assertEqual(self.p.top_warning(0),b'LOW BAT' if expected else None)

    def test_two_warnings_alternate_without_blocking(self):
        self.p.update(9,61,False)
        for now,expected in ((100,b'HIGH TEMP'),(2099,b'HIGH TEMP'),
                             (2100,b'LOW BAT'),(4099,b'LOW BAT'),(4100,b'HIGH TEMP')):
            self.assertEqual(self.p.top_warning(now),expected)

    def test_alternation_is_tick_wrap_safe(self):
        self.p.update(9,61,False)
        self.assertEqual(self.p.top_warning(65000),b'HIGH TEMP')
        self.assertEqual(self.p.top_warning(1464),b'LOW BAT')

    def test_hot_lockout_hysteresis_and_unknown_sample(self):
        self.p.update(50,61,False)
        for temp in (60,59,55.1,None):
            self.p.update(50,temp,False)
            self.assertTrue(self.p.overheat)
        self.p.update(50,55,False)
        self.assertFalse(self.p.overheat)

    def test_alternation_restarts_when_both_return(self):
        self.p.update(9,61,False);self.p.top_warning(0);self.p.top_warning(2000)
        self.p.update(10,61,False)
        self.assertEqual(self.p.top_warning(2001),b'HIGH TEMP')
        self.p.update(9,61,False)
        self.assertEqual(self.p.top_warning(2002),b'HIGH TEMP')

    def ui(self):
        now=[100]
        screen=SimpleNamespace(fill_rect=Mock(),draw_gbk=Mock())
        ns=dict(protection=self.p,tft=screen,RED=1,WHITE=2,GREEN=3,BLACK=0,
                last_top_status=None,current_status=b'READY',current_status_color=3,
                time=SimpleNamespace(ticks_ms=lambda:now[0]),
                sample_device_status=Mock(),last_hw_draw=None,last_hw_update=100,
                system_state='DASHBOARD',last_usb_power=False,last_battery_v='3.5V',
                last_battery_p='9%',last_temp_str='61.0C',last_rssi_str='-89dBm')
        functions(('draw_battery_top_status','draw_hardware_bar'),ns)
        return ns,screen,now

    def test_top_warning_only_redraws_when_text_changes(self):
        ns,screen,now=self.ui();self.p.update(9,61,False)
        ns['draw_battery_top_status']();ns['draw_battery_top_status']()
        self.assertEqual(screen.draw_gbk.call_count,1)
        now[0]=2100;ns['draw_battery_top_status']()
        self.assertEqual(screen.draw_gbk.call_args.args,(b'LOW BAT',230,4,1,0x01CF))
        ns['draw_battery_top_status'](force=True)
        self.assertEqual(screen.draw_gbk.call_count,3)

    def test_temperature_and_low_battery_are_red(self):
        ns,screen,_=self.ui();self.p.update(9,61,False)
        ns['draw_hardware_bar'](force=True)
        calls=screen.draw_gbk.call_args_list
        self.assertEqual(calls[0].args[3],1)
        self.assertEqual(calls[-1].args,(b'61.0C',265,218,1,0))

    def test_usb_chrg_does_not_suppress_hot_red(self):
        ns,screen,_=self.ui();self.p.update(0,61,True);ns['last_usb_power']=True
        ns['draw_hardware_bar'](force=True)
        self.assertEqual(screen.draw_gbk.call_args_list[1].args,(b'CHRG',85,218,3,0))
        self.assertEqual(screen.draw_gbk.call_args.args[3],1)

    def test_zero_policy_does_not_shutdown_on_usb_or_one_percent(self):
        ns=functions(('service_low_battery',),dict(protection=self.p,
                     low_battery_shutdown=False,enter_low_battery_shutdown=Mock()))
        for percent,usb in ((0,True),(1,False),(None,False)):
            self.p.update(percent,30,usb);ns['service_low_battery'](0)
        ns['enter_low_battery_shutdown'].assert_not_called()
        self.p.update(0,30,False);ns['service_low_battery'](0)
        ns['enter_low_battery_shutdown'].assert_called_once()

    def test_shutdown_waits_for_radio_and_requires_two_usb_samples(self):
        class Restart(Exception): pass
        cpu=SimpleNamespace(freq=Mock(return_value=18000000),reset=Mock(side_effect=Restart))
        usb=iter((False,True,False,True,True))
        state=[0,0,0,0,True,True,False]
        ns=dict(low_battery_shutdown=False,radio_state=state,RADIO_RUNNING=4,RADIO_STOPPED=5,
                LOW_BATTERY_CPU_HZ=18000000,machine=cpu,last_battery_v='3.4V',last_battery_p='0%',
                tft=SimpleNamespace(fill_rect=Mock(),draw_gbk=Mock()),RED=1,WHITE=2,BLACK=0,YELLOW=4,
                buzzer=SimpleNamespace(value=Mock()),time=SimpleNamespace(sleep_ms=Mock()),
                set_screen_power=Mock(),stop_buzzer=Mock(),cfg_buzzer=False,
                wifi_portal=SimpleNamespace(set_enabled=Mock()),
                usb_power_present=lambda:next(usb),print=Mock())
        functions(('enter_low_battery_shutdown',),ns)
        with self.assertRaises(Restart): ns['enter_low_battery_shutdown']()
        self.assertFalse(state[4]);self.assertTrue(ns['low_battery_shutdown'])
        ns['set_screen_power'].assert_called_once_with(False)
        cpu.freq.assert_any_call(18000000)
        cpu.reset.assert_called_once()
        self.assertEqual(ns['tft'].draw_gbk.call_args_list[-1].args[0],b'CONNECT USB TO RESTART')

    def test_shutdown_never_changes_clock_if_radio_not_stopped(self):
        class End(Exception): pass
        cpu=SimpleNamespace(freq=Mock(),reset=Mock(side_effect=End))
        ns=dict(low_battery_shutdown=False,radio_state=[0,0,0,0,True,False,False],
                RADIO_RUNNING=4,RADIO_STOPPED=5,LOW_BATTERY_CPU_HZ=18000000,machine=cpu,
                last_battery_v='3.4V',last_battery_p='0%',tft=SimpleNamespace(fill_rect=Mock(),draw_gbk=Mock()),
                RED=1,WHITE=2,BLACK=0,YELLOW=4,buzzer=SimpleNamespace(value=Mock()),
                time=SimpleNamespace(sleep_ms=Mock()),set_screen_power=Mock(),
                stop_buzzer=Mock(),cfg_buzzer=False,wifi_portal=SimpleNamespace(set_enabled=Mock()),
                usb_power_present=lambda:True,print=Mock())
        functions(('enter_low_battery_shutdown',),ns)
        with self.assertRaises(End): ns['enter_low_battery_shutdown']()
        cpu.freq.assert_not_called()

    def test_failed_stop_never_acknowledges_safe_clock_change(self):
        class Parked(Exception): pass
        state=[0,0,0,0,False,False,False]
        ns=dict(radio_state=state,RADIO_AUTO_RECOVERY_ENABLED=False,
                RADIO_ERROR_RECOVERY_THRESHOLD=3,print=Mock())
        functions(('radio_core_task',),ns)
        radio=SimpleNamespace(stop=Mock(side_effect=OSError('stop failed')))
        clock=SimpleNamespace(sleep_ms=Mock(side_effect=Parked))
        with patch.dict('sys.modules',{'time':clock}):
            try: ns['radio_core_task'](radio)
            except Parked: pass
        radio.stop.assert_called_once()
        self.assertFalse(state[5])

    def test_radio_stop_orders_capture_before_rf_sleep(self):
        tree=ast.parse((ROOT/'lbj_receiver.py').read_text())
        receiver=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LBJReceiver')
        node=next(n for n in receiver.body if isinstance(n,ast.FunctionDef) and n.name=='stop')
        ns={};exec(compile(ast.Module(body=[node],type_ignores=[]),'receiver','exec'),ns)
        calls=[]
        radio=SimpleNamespace(sm=SimpleNamespace(active=lambda x:calls.append(('pio',x))),
                _dma_rx=SimpleNamespace(stop=lambda:calls.append(('dma',))),
                cs_pin=SimpleNamespace(value=lambda x:calls.append(('cs',x))),
                _w=lambda a,b:calls.append(('reg',a,b)))
        ns['stop'](radio)
        self.assertEqual(calls,[('pio',0),('dma',),('cs',1),('reg',1,0)])


if __name__=='__main__': unittest.main()
