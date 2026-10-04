import ast,sys,unittest
from pathlib import Path
from types import SimpleNamespace

class PreambleRefreshTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  p=Path(__file__).resolve().parents[1]/'lbj_receiver.py'
  tree=ast.parse(p.read_text())
  receiver=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LBJReceiver')
  ns={'time':SimpleNamespace(ticks_diff=lambda a,b:a-b),'machine':SimpleNamespace(mem32={0x502000cc:25<<7})}
  nodes=[n for n in receiver.body if isinstance(n,ast.FunctionDef) and n.name in ('_refresh_afc_on_preamble','_restart_afc_capture','_service_acquisition_timeout')]
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'refresh','exec'),ns)
  cls.refresh=staticmethod(ns['_refresh_afc_on_preamble'])
  cls.restart=staticmethod(ns['_restart_afc_capture'])
  cls.timeout=staticmethod(ns['_service_acquisition_timeout'])
 def fixture(self):
  events=[]
  sm=SimpleNamespace(active=lambda v:events.append(('pio',v)),restart=lambda:events.append(('restart',)),
                     rx_fifo=lambda:0,exec=lambda v:events.append(('pc',v)))
  dma=SimpleNamespace(stop=lambda:events.append(('stop',)),reset=lambda:events.append(('dma',)))
  r=SimpleNamespace(synced=False,_afc_preamble_words=0,_last_afc_refresh=-1000,_afc_backlog_at=-100,
      input_pending=lambda:0,_r=lambda a:{0x3e:0xdb,1:5}[a],sm=sm,_dma_rx=dma,
      _reset_decoder=lambda **k:events.append(('decoder',)),_w=lambda a,v:events.append(('write',a,v)),
      afc_refreshes=0,afc_no_sync_refreshes=0,words_seen=1024,last_sync_time=0,_last_acquisition_probe=-250,
      REG_RXCONFIG=0x0d,RXCONFIG_AFC_PREAMBLE=0x16)
  r._restart_afc_capture=lambda now:self.restart(r,now)
  return r,events
 def test_two_current_preamble_words_restart_once_in_safe_order(self):
  r,e=self.fixture()
  self.assertFalse(self.refresh(r,0xaaaaaaaa,0))
  self.assertTrue(self.refresh(r,0xaaaaaaaa,27))
  self.assertEqual(e,[('pio',0),('stop',),('restart',),('pc',25),('dma',),('decoder',),('write',13,54),('pio',1)])
  self.assertEqual(r.afc_refreshes,1)
  self.assertFalse(self.refresh(r,0x55555555,54))
  self.assertFalse(self.refresh(r,0x55555555,81))
  self.assertEqual(r.afc_refreshes,1)
 def test_inverted_phase_and_last_bit_errors_are_accepted(self):
  for pattern in (0xaaaaaaaa,0x55555555):
   for errors in (0,1,3,0x80000001):
    r,e=self.fixture()
    self.refresh(r,pattern^errors,0)
    self.assertTrue(self.refresh(r,pattern^errors,27))
 def test_three_errors_or_noise_reset_detector(self):
  r,e=self.fixture()
  self.refresh(r,0xaaaaaaaa,0)
  self.assertFalse(self.refresh(r,0xaaaaaaaa^7,27))
  self.assertEqual(r._afc_preamble_words,0)
  self.assertFalse(e)
 def test_backlog_never_restarts_current_air_payload(self):
  r,e=self.fixture();r.input_pending=lambda:2
  for _ in range(30):self.assertFalse(self.refresh(r,0xaaaaaaaa,0))
  self.assertFalse(e)
 def test_old_header_tail_never_restarts_after_backlog_drains(self):
  r,e=self.fixture();r.input_pending=lambda:1
  self.assertFalse(self.refresh(r,0xaaaaaaaa,1000))
  r.input_pending=lambda:0
  self.assertFalse(self.refresh(r,0xaaaaaaaa,1027))
  self.assertFalse(self.refresh(r,0xaaaaaaaa,1054))
  self.assertFalse(e)
  # A later fresh header remains eligible after the caught-up interval.
  self.assertFalse(self.refresh(r,0xaaaaaaaa,1100))
  self.assertTrue(self.refresh(r,0xaaaaaaaa,1127))
 def test_synced_payload_is_not_touched(self):
  r,e=self.fixture();r.synced=True
  for _ in range(30):self.assertFalse(self.refresh(r,0xaaaaaaaa,0))
  self.assertFalse(e)
 def test_rx_not_ready_or_wrong_mode_skips(self):
  for mode,flags in ((4,0x18),(5,0x10),(1,0x90)):
   r,e=self.fixture();r._r=lambda a,m=mode,f=flags:f if a==0x3e else m
   self.refresh(r,0xaaaaaaaa,0)
   self.assertFalse(self.refresh(r,0xaaaaaaaa,27))
   self.assertFalse(e)
 def test_fifo_fallback_also_restarts_without_dma_access(self):
  r,e=self.fixture();r._dma_rx=None
  self.refresh(r,0xaaaaaaaa,0)
  self.assertTrue(self.refresh(r,0xaaaaaaaa,27))
  self.assertEqual(e,[('pio',0),('restart',),('pc',25),('decoder',),('write',13,54),('pio',1)])
 def test_failed_spi_write_does_not_leave_pio_disabled(self):
  r,e=self.fixture()
  def fail(*a):raise OSError('spi')
  r._w=fail
  self.refresh(r,0xaaaaaaaa,0)
  with self.assertRaises(OSError):self.refresh(r,0xaaaaaaaa,27)
  self.assertEqual(e[-1],('pio',1))
 def test_clocked_noise_without_sync_rearms_after_fifteen_seconds(self):
  r,e=self.fixture()
  self.timeout(r,14999)
  self.assertFalse(e)
  self.timeout(r,15000)
  self.assertEqual(r.afc_no_sync_refreshes,1)
  self.assertEqual(r.afc_refreshes,1)
  self.timeout(r,15001)
  self.assertEqual(r.afc_no_sync_refreshes,1)
  self.timeout(r,30000)
  self.assertEqual(r.afc_no_sync_refreshes,2)
 def test_timeout_does_not_interrupt_payload_backlog_or_missing_clock(self):
  for key,value in (('synced',True),('words_seen',0),('input_pending',lambda:2),('last_sync_time',10000)):
   r,e=self.fixture();setattr(r,key,value)
   self.timeout(r,20000)
   self.assertFalse(e)
 def test_timeout_leaves_fsrx_preamble_wait_alone(self):
  r,e=self.fixture();r._r=lambda a:0x18 if a==0x3e else 4
  self.timeout(r,60000)
  self.assertFalse(e)
 def test_waiting_fsrx_register_probe_is_rate_limited(self):
  r,e=self.fixture();reads=[]
  r._r=lambda register:reads.append(register) or 0x18
  for now in range(15000,16000):self.timeout(r,now)
  self.assertEqual(reads,[0x3e]*4)
  self.assertFalse(e)
class ProfileModeTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  source=Path(__file__).resolve().parents[1]/'lbj_receiver.py'
  tree=ast.parse(source.read_text())
  radio=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LBJReceiver')
  radio.body=[n for n in radio.body if isinstance(n,ast.Assign) or isinstance(n,ast.FunctionDef) and n.name=='_profile_fault']
  namespace={}
  exec(compile(ast.Module(body=[radio],type_ignores=[]),str(source),'exec'),namespace)
  cls.receiver_class=namespace['LBJReceiver']
 def fixture(self,mode=5,flags=0xdb):
  r=self.receiver_class()
  r._expected_frf=0xcd4ba3
  regs={0x42:0x12,1:mode,0x3e:flags,r.REG_FRF_MSB:0xcd,r.REG_FRF_MID:0x4b,
        r.REG_FRF_LSB:0xa3,r.REG_RXBW:r.RXBW,r.REG_AFCBW:r.AFCBW,
        0x0c:r.LNA_FIXED_GAIN_BOOST,0x31:0,0x40:0,
        r.REG_PREAMBLEDETECT:r.PREAMBLE_DETECT,r.REG_RXCONFIG:r.RXCONFIG_AFC_PREAMBLE}
  r._r=lambda register:regs[register]
  return r,regs
 def test_fsrx_wait_and_ready_rx_are_valid(self):
  for mode,flags in ((4,0x18),(5,0xdb)):
   r,_=self.fixture(mode,flags)
   self.assertIsNone(r._profile_fault())
 def test_sleep_standby_unlocked_pll_and_inconsistent_flags_are_faults(self):
  for mode,flags in ((0,0x18),(1,0x90),(4,0x08),(4,0xd8),(0x85,0xdb)):
   r,_=self.fixture(mode,flags)
   self.assertEqual(r._profile_fault(),'op_mode')
 def test_fsrx_does_not_hide_wrong_bandwidth_or_spi_version(self):
  r,regs=self.fixture(4,0x18)
  regs[r.REG_RXBW]^=1
  self.assertEqual(r._profile_fault(),'rxbw')
  regs[0x42]=0xff
  self.assertEqual(r._profile_fault(),'spi_version')

if __name__=='__main__':unittest.main()
