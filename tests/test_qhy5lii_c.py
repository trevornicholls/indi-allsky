"""Small offline policy/transport checks; no socket, camera or production DB."""
import copy
import json
from pathlib import Path
from queue import Queue
import time
import unittest
from datetime import datetime, timezone
import numpy as np
from astropy.io import fits
from indi_allsky.exposure import qhy5lii_c as C

CONFIG={'CCD_CONFIG':{m:{'GAIN':1,'BINNING':1} for m in ('DAY','NIGHT','MOONMODE')},
        'TARGET_ADU':75,'TARGET_ADU_DEV':20,'TARGET_ADU_DAY':100,'TARGET_ADU_DEV_DAY':25,
        'CCD_EXPOSURE_MIN':0,'CCD_EXPOSURE_MIN_DAY':0,'CCD_EXPOSURE_MAX':15,
        'ADU_FOV_DIV':4,'IMAGE_STACK_COUNT':1}
CONFIG['CCD_CONFIG']['EXPOSURE_CLASSNAME']=C.STRATEGY
for mode, cap in [('DAY',1),('NIGHT',14),('MOONMODE',14)]:CONFIG['CCD_CONFIG'][mode]['GAIN']=cap
RAW={'valid':True,'clipped':False}
def command(e=15,g=1,epoch=1,mode='night',kind='normal'):
 return dict(session='test',sequence=1,epoch=epoch,mode=mode,kind=kind,exposure=e,gain=g,binning=1,bounds=C.limits(CONFIG,mode))
def frame(c):
 data=np.random.default_rng(42).integers(10,180,(960,1280),dtype=np.uint8)
 h=fits.PrimaryHDU(data)
 for k,v in {'EXPTIME':c['exposure'],'GAIN':c['gain'],'XBINNING':1,'YBINNING':1,'BAYERPAT':'GRBG','DATE-OBS':datetime.now(timezone.utc).isoformat()}.items():h.header[k]=v
 return fits.HDUList([h])
class Checks(unittest.TestCase):
 def test_integer_grid_and_ceiling(self):
  b=C.limits(CONFIG,'night');self.assertEqual(b['codes'],[1,3,5,7,8,9,10,11,12,13,14])
  for g in (1.5,22.1,float('nan'),True):
   with self.assertRaises(C.ControlError):C.validate((1,g,1),b)
  for e in (15.000001,0,float('nan')):
   with self.assertRaises(C.ControlError):C.validate((e,1,1),b)
  self.assertEqual(C.clamp((99,22,1),b),(15.,14,1))
 def test_exposure_priority_and_limits(self):
  p=C.Policy();self.assertEqual(p.decide(command(1),0,RAW),(2.,1,1))
  p=C.Policy();self.assertEqual(p.decide(command(),0,RAW),(15.,1,1));self.assertEqual(p.decide(command(),0,RAW),(15.,3,1))
  self.assertEqual(C.Policy().decide(command(15,14),0,RAW),(15.,14,1))
  self.assertEqual(C.Policy().decide(command(.001),255,dict(RAW,clipped=True)),(.0005,1,1))
 def test_settling_and_stable(self):
  p=C.Policy();p.decide(command(),0,RAW);p.decide(command(),0,RAW)
  self.assertEqual(p.decide(command(g=3),200,RAW),(15.,3,1));self.assertIn('settling',p.status)
  p.decide(command(g=3),200,RAW);self.assertIn('qualifying',p.status)
  self.assertEqual(p.decide(command(g=3),200,RAW)[1],3);self.assertIsNotNone(p.bracket)
  p=C.Policy();p.decide(command(),75,RAW);self.assertFalse(p.stable);p.decide(command(),75,RAW);self.assertTrue(p.stable)
 def test_bracket_reconsideration(self):
  p=C.Policy();p.epoch=('test',1);p.last_settings=(15.,3,1);p.bracket={'lower':1,'upper':3,'reference':10.}
  p.decide(command(g=3),10,RAW);self.assertEqual(p.decide(command(g=3),10,RAW),(15.,5,1))
  p=C.Policy();p.epoch=('test',1);p.last_settings=(4.,3,1);p.bracket={'lower':1,'upper':3,'reference':10.}
  p.decide(command(4,3),75,RAW);self.assertEqual(p.decide(command(4,3),75,RAW),(4.,1,1));self.assertFalse(p.stable)
 def test_mode_and_invalid(self):
  p=C.Policy();p.decide(command(),75,RAW);p.decide(command(),75,RAW);self.assertTrue(p.stable)
  p.decide(command(.01,1,2,'day'),100,RAW);self.assertFalse(p.stable);self.assertEqual(p.target,100)
  p.decide(command(),float('nan'),RAW)
  with self.assertRaises(C.ControlError):p.decide(command(),float('nan'),RAW)
 def test_sqms_restore_and_single_flight(self):
  q=Queue();g=C.FrameGate(CONFIG,q,[0,0]);g.set_mode('night');g.ordinary=(.25,1,1)
  c=g.start(10,10,1,'sqm');g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':10.});g.frame(frame(c))
  with self.assertRaises(C.ControlError):g.start(1,1,1,'normal')
  q.put({'command':c,'ok':True,'proposal':None});self.assertEqual(g.poll(True),(.25,1,1))
  p=C.Policy();p.decide(command(kind='sqm'),0,RAW);p.decide(command(),75,RAW);self.assertIn('settling',p.status)
 def test_feedback_identity_order_and_idle(self):
  q=Queue();g=C.FrameGate(CONFIG,q,[0,0]);g.set_mode('night');c=g.start(.01,1,1,'normal')
  g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':.01});g.frame(frame(c))
  q.put({'command':dict(c,session='old'),'ok':True});q.put({'command':c,'ok':True,'proposal':(.02,1,1)})
  self.assertIsNone(g.poll(False));self.assertIsNotNone(g.pending);self.assertEqual(g.poll(True),(.02,1,1))
  q.put({'command':c,'ok':True,'proposal':(.02,1,1)});g.poll(True);self.assertIsNone(g.fault) # completed duplicate ignored
 def test_old_mode_feedback_not_applied(self):
  q=Queue();g=C.FrameGate(CONFIG,q,[0,0]);g.set_mode('night');c=g.start(1,3,1,'normal');g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':1.});g.frame(frame(c));g.set_mode('day')
  q.put({'command':c,'ok':True,'proposal':(15.,5,1)});self.assertEqual(g.poll(True),(1.,1,1))
 def test_fault_and_replacement(self):
  shared=[0,0];g=C.FrameGate(CONFIG,Queue(),shared);g.set_mode('day');g.start(.01,1,1,'normal')
  with self.assertRaises(C.ControlError):C.FrameGate(CONFIG,Queue(),shared)
  g.sent_time=time.monotonic()-400;g.poll(True);first=g.fault;g.fail('later');self.assertEqual(g.fault,first)
 def test_native_faults_and_metadata(self):
  self.assertFalse(C.native_statistics(np.zeros((960,1280),dtype=np.uint8),CONFIG)['uniform'])
  self.assertFalse(C.native_statistics(np.full((960,1280),99,dtype=np.uint8),CONFIG)['valid'])
  self.assertTrue(C.native_statistics(np.full((960,1280),255,dtype=np.uint8),CONFIG)['clipped'])
  g=C.FrameGate(CONFIG,Queue(),[0,0]);g.set_mode('day');c=g.start(.01,1,1,'normal');g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':.01});h=frame(c);h[0].header['GAIN']=1.5
  with self.assertRaises(C.ControlError):g.frame(h)
 def test_property_final_guard(self):
  g=C.FrameGate(CONFIG,Queue(),[0,0]);g.set_mode('night');g.start(.01,3,1,'normal')
  with self.assertRaises(C.ControlError):g.property('CCD_GAIN',{'GAIN':3.5})
  g.property('CCD_GAIN',{'GAIN':3});g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':.01})
  with self.assertRaises(C.ControlError):g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':.01})
 def test_adapter_and_truthful_telemetry(self):
  x=C.Exposure(CONFIG);c=command();job={'c_command':c,'c_native':RAW};x.prepare(job,[1,1]);x.compare_exposure(75,15,1);self.assertFalse(x.target_adu_found)
  x.prepare(job,[1,1]);x.compare_exposure(75,15,1);self.assertTrue(x.target_adu_found);self.assertEqual(x.reply['command'],c)
  x.prepare(job,[1,2]);x.compare_exposure(0,15,1);self.assertFalse(x.target_adu_found);self.assertEqual(x.reply['proposal'],(15,1,1))
class ClippingRegression(unittest.TestCase):
 def sample(self,p,e,adu,clip,epoch=1,mode='day',gain=1,kind='normal'):
  return p.decide(command(e,gain,epoch,mode,kind),adu,dict(RAW,clipped=clip>=.01,clip_fraction=clip))
 def test_recorded_live_conflict(self):
  p=C.Policy()
  # Original live startup, followed by its first safe frame. A hard clipping
  # reduction must establish a ceiling that the ADU loop cannot undo.
  for e,a,clip in [(0.5,244.718,.986606),(.25,202.372,.445638),(.125,114.608,.066192)]:
   self.sample(p,e,a,clip)
  self.assertEqual(self.sample(p,.0625,57.9406,.0063855),(.0625,1,1))
  # The observed bright/dark pairs must return to the last measured safe
  # exposure, rather than halve independently then immediately ramp back up.
  for e,a,clip in [(.085185,74.6737,.0166874),(.101481,78.0486,.0193),(.116135,78.5712,.0201)]:
   self.assertEqual(self.sample(p,e,a,clip),(.0625,1,1))
   for _ in range(3):
    self.assertEqual(self.sample(p,.0625,57.9406,.0063855),(.0625,1,1))
    self.assertIn('clipping-limited',p.status);self.assertFalse(p.stable)
 def test_clip_release_requires_actual_change_and_hysteresis(self):
  p=C.Policy();self.sample(p,.125,114.6,.066)
  for _ in range(4):self.assertEqual(self.sample(p,.0625,58,.006),(.0625,1,1))
  # Crossing the clear threshold alone is insufficient at unchanged ADU.
  for _ in range(3):self.assertEqual(self.sample(p,.0625,58,.004),(.0625,1,1))
  self.assertEqual(self.sample(p,.0625,49,.004),(.0625,1,1))
  released=self.sample(p,.0625,49,.004);self.assertEqual(released,(.078125,1,1))
  self.assertEqual(self.sample(p,.078125,60,.02),(.0625,1,1))
  for _ in range(4):self.assertEqual(self.sample(p,.0625,49,.004),(.0625,1,1))
  p=C.Policy();self.sample(p,.125,114.6,.066)
  self.assertEqual(self.sample(p,.062499,58,.006),(.062499,1,1))
 def test_clip_state_scoped_to_ordinary_gain_and_mode(self):
  p=C.Policy();self.sample(p,.125,114.6,.066)
  self.sample(p,.0625,58,.006)
  self.sample(p,10,0,0,kind='sqm')
  for _ in range(4):self.sample(p,.0625,58,.006)
  self.assertEqual(p.clip_limit,.0625)
  self.sample(p,.0625,40,0,epoch=2,mode='night')
  self.assertIsNone(p.clip_limit)
  self.assertGreater(self.sample(p,.0625,40,0,epoch=2,mode='night')[0],.0625)
  self.sample(p,.125,100,.02,epoch=2,mode='night')
  self.assertIsNotNone(p.clip_limit)
  self.sample(p,.0625,45,0,epoch=2,mode='night',gain=3)
  self.assertIsNone(p.clip_limit)

class FitsRepresentationRegression(unittest.TestCase):
 def gate(self,e):
  g=C.FrameGate(CONFIG,Queue(),[0,0]);g.set_mode('night');c=g.start(e,1,1,'normal');g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':e});return g,c
 def test_preserved_native_header(self):
  path=Path(__file__).parent/'fixtures/qhy-failed-feedback-header.txt'
  self.assertTrue(path.is_file())
  g,c=self.gate(10.002973)
  # Recreate the original request boundary with selected original header cards.
  # Pixels are deterministic synthetic data; original native evidence stays external.
  g.sent_wall=datetime(2026,10,30,17,17,14,tzinfo=timezone.utc).timestamp()
  with frame(c) as h:
   h[0].header.update(fits.Header.fromtextfile(path))
   self.assertEqual(h[0].header['EXPTIME'],10.002970)
   cmd,raw=g.frame(h)
  self.assertEqual(cmd['exposure'],10.002973);self.assertTrue(raw['valid'])
 def test_scientific_header_precision_and_strict_rejections(self):
  for e in (.000123,.123456,1.234567,9.999999,10.002973,14.999999,15.):
   g,c=self.gate(e);h=frame(c);h[0].header['EXPTIME']=float(format(e,'.6E'));g.frame(h)
  for key,value in [('EXPTIME',10.002983),('EXPTIME',10.003973),('GAIN',2.),('XBINNING',2),('BAYERPAT','RGGB'),('DATE-OBS','2020-01-01T00:00:00')]:
   with self.subTest(key=key,value=value):
    g,c=self.gate(10.002973);h=frame(c);h[0].header[key]=value
    with self.assertRaises(C.ControlError):g.frame(h)
  g,c=self.gate(10.002973);h=frame(c);h[0].header['EXPTIME']=10.002970;g.frame(h)
  with self.assertRaises(C.ControlError):g.frame(h)

if __name__=='__main__':unittest.main()
