"""Focused evidence-based short-exposure checks. No hardware or services."""
import copy
import subprocess
import types
import tempfile
from unittest.mock import patch
import json
from pathlib import Path
from queue import Queue
import unittest
from indi_allsky.exposure import qhy5lii_c as C
from tests.test_qhy5lii_c import CONFIG, RAW, command, frame

READOUT={'sdk':'26.7.21','sdk_sha256':'dbee1afda782c80f3a90d0c4a5a8de38430d592d56b97ce0052c95872248c078','format':'INDI_RAW','bits':8.,'speed':0.,'traffic':30.,'width':1280.,'height':960.,'x':0.,'y':0.,'bin_x':1.,'bin_y':1.}

def cmd(e,**kwargs):
 c=command(e,mode=kwargs.pop('mode','day'),**kwargs);c['readout']=dict(READOUT);c['bounds'].update(target=125.,deviation=25.);return c

def run(p,e,response,count=60,**kwargs):
 out=[]
 for n in range(count):
  a,clip=response(e);c=cmd(e,**kwargs);c['sequence']=n+1
  result=p.decide(c,a,dict(RAW,clipped=clip>=.01,clip_fraction=clip));out.append((e,a,clip,result[0],p.status,p.stable));e=result[0]
 return e,out

class Quantisation(unittest.TestCase):
 def test_sdk_row_model_and_measured_boundaries(self):
  grid=C.exposure_quantum(READOUT);self.assertEqual(grid,.0002625)
  for us in [1,5,10,20,30,50,100,200,488,500]:self.assertEqual(C.exposure_rows(us/1e6,grid),1)
  for us in [550,600,610,650,750]:self.assertEqual(C.exposure_rows(us/1e6,grid),2)
  for us in [800,850,900,1000]:self.assertEqual(C.exposure_rows(us/1e6,grid),3)
  for k,v in [('speed',1.),('traffic',31.),('format','INDI_RAW16'),('sdk_sha256','unknown'),('bin_x',2.),('width',640.)]:
   self.assertIsNone(C.exposure_quantum(dict(READOUT,**{k:v})))
 def test_recorded_488_610_conflict_stops(self):
  response=lambda e:(72.,.001) if C.exposure_rows(e,C.exposure_quantum(READOUT))==1 else (135.,.02)
  # Preserve the historical clipping-veto case in Night; Day is ADU-primary.
  p=C.Policy();e,out=run(p,.000488,response,mode='night')
  self.assertEqual(len({x[0] for x in out[-30:]}),1)
  self.assertEqual(C.exposure_rows(e,C.exposure_quantum(READOUT)),1)
  self.assertIn('exposure-resolution-limited',p.status);self.assertFalse(p.stable)
  self.assertLessEqual(sum(x[2]>=.01 for x in out),2)
  self.assertTrue(all(a[0]==a[3] or C.exposure_rows(a[0],.0002625)!=C.exposure_rows(a[3],.0002625) for a in out))
 def test_unclipped_straddle_chooses_nearest_and_holds(self):
  for upper,expected in [(180.,1),(155.,2)]:
   p=C.Policy();e,out=run(p,.000488,lambda e:(72. if C.exposure_rows(e,.0002625)==1 else upper,0.))
   self.assertEqual(C.exposure_rows(e,.0002625),expected);self.assertEqual(len({x[0] for x in out[-20:]}),1);self.assertIn('exposure-resolution-limited',p.status);self.assertFalse(p.stable)
 def test_in_band_upper_is_stable(self):
  p=C.Policy();e,out=run(p,.000488,lambda e:(72. if C.exposure_rows(e,.0002625)==1 else 135.,0.))
  self.assertEqual(C.exposure_rows(e,.0002625),2);self.assertTrue(p.stable)
 def test_minimum_clipping_and_minimum_bounds(self):
  p=C.Policy();e,out=run(p,.000001,lambda e:(200.,.1))
  self.assertEqual(e,.000001);self.assertIn('exposure-resolution-limited',p.status)
  b=cmd(.0006);b['bounds']['lo']=.0006;b['bounds']['hi']=.0007;p=C.Policy()
  for _ in range(8):
   e=p.decide(b,20.,dict(RAW,clip_fraction=0.))[0];self.assertTrue(.0006<=e<=.0007);b['exposure']=e
  self.assertIn('exposure-resolution-limited',p.status)
 def test_scene_changes_reopen_closed_pair(self):
  p=C.Policy();e,_=run(p,.000488,lambda e:(72.,.001) if C.exposure_rows(e,.0002625)==1 else (135.,.02),mode='night')
  e,out=run(p,e,lambda e:(55.,.001) if C.exposure_rows(e,.0002625)==1 else (110.,.001),mode='night')
  self.assertEqual(C.exposure_rows(e,.0002625),2);self.assertTrue(p.stable)
  e,out=run(p,e,lambda e:(125.,.001) if C.exposure_rows(e,.0002625)==1 else (240.,.03),mode='night')
  self.assertEqual(C.exposure_rows(e,.0002625),1);self.assertTrue(p.stable)
 def test_mode_gain_profile_reset_and_sqm(self):
  p=C.Policy();e,_=run(p,.000488,lambda e:(72.,.001) if C.exposure_rows(e,.0002625)==1 else (135.,.02),mode='night')
  self.assertIsNotNone(p.resolution_lock)
  c=cmd(e,epoch=2,mode='night');p.decide(c,75.,RAW);self.assertIsNone(p.resolution_lock)
  p.decide(cmd(e,epoch=3,mode='moon'),75.,RAW);self.assertIsNone(p.resolution_lock)
  run(p,e,lambda e:(72.,.001) if C.exposure_rows(e,.0002625)==1 else (135.,.02),mode='night')
  p.decide(cmd(e,g=3,mode='night'),75.,RAW);self.assertIsNone(p.resolution_lock)
  c=cmd(e,g=3,mode='night');c['readout']['traffic']=31.;p.decide(c,75.,RAW);self.assertIsNone(p.resolution_lock)
  p=C.Policy();p.decide(cmd(e,kind='sqm'),0.,RAW);self.assertIsNone(p.resolution_lock)
  p.decide(cmd(e),72.,RAW);self.assertIn('settling',p.status)
 def test_outside_region_and_unmapped_are_unchanged(self):
  base=types.ModuleType('baseline_qhy')
  exec(subprocess.check_output(['git','show','0223be9fe00a079c573f7eec2c276679fd02d455:indi_allsky/exposure/qhy5lii_c.py'],cwd=Path(__file__).parents[1],text=True),base.__dict__)
  for known in [True,False]:
   p=C.Policy();prior=base.Policy();e=.125
   for n in range(80):
    c=cmd(e,mode='night');c['readout']=dict(READOUT) if known else dict(READOUT,traffic=31.)
    adu=800*e;native=dict(RAW,clipped=e>.075,clip_fraction=.02 if e>.075 else .001)
    a=p.decide(c,adu,native);b=prior.decide(c,adu,native);self.assertEqual((a,p.status,p.stable),(b,prior.status,prior.stable));e=a[0]
 def test_frame_context_detects_readout_change(self):
  readout=dict(READOUT);g=C.FrameGate(CONFIG,Queue(),[0,0],readout=lambda:dict(readout));g.set_mode('day');c=g.start(.0005,1,1,'normal');self.assertEqual(c['readout'],READOUT)
  g.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':.0005});readout['speed']=1.
  with self.assertRaisesRegex(C.ControlError,'readout'):g.frame(frame(c))
 def test_different_sqm_gain_preserves_ordinary_clipping_scope(self):
  p=C.Policy();e,_=run(p,.000488,lambda e:(72.,.001) if C.exposure_rows(e,.0002625)==1 else (135.,.02),mode='night')
  before=(p.resolution_scope,dict(p.resolution_lock),p.clip_bad,p.clip_limit)
  p.decide(cmd(10.,g=10,kind='sqm',mode='night'),0.,RAW)
  self.assertEqual(before,(p.resolution_scope,p.resolution_lock,p.clip_bad,p.clip_limit))
  p.decide(cmd(e,mode='night'),72.,RAW);self.assertIn('settling',p.status)
 def test_region_edge_does_not_cycle(self):
  p=C.Policy();e,out=run(p,.0009,lambda e:(90.,0.) if C.exposure_rows(e,.0002625)<=3 else (180.,0.))
  self.assertEqual(len({x[0] for x in out[-30:]}),1);self.assertIn('exposure-resolution-limited',p.status)
 def test_minimum_recovers_and_noise_does_not_reopen(self):
  p=C.Policy();e,_=run(p,.000001,lambda e:(200.,.1))
  e,_=run(p,e,lambda e:(70.,0.) if C.exposure_rows(e,.0002625)==1 else (125.,0.))
  self.assertEqual(C.exposure_rows(e,.0002625),2);self.assertTrue(p.stable)
  p=C.Policy();e,_=run(p,.000488,lambda e:(72.,.001) if C.exposure_rows(e,.0002625)==1 else (135.,.02),mode='night')
  for n in range(30):
   result=p.decide(cmd(e,mode='night'),72.+(n%3-1)*2,dict(RAW,clip_fraction=.001));self.assertEqual(result[0],e)
 def test_cached_indi_readout_reader_and_telemetry(self):
  evidence=Path(__file__).parent/'fixtures/qhy-exposure-quantisation.json'
  props=json.loads(evidence.read_text())['props']
  class Element:
   def __init__(self,k,v):self.k,self.v=k,v
   def getName(self):return self.k
   def getValue(self):return float(self.v)
   def getText(self):return self.v
   def getState(self):return 1 if self.v=='On' else 0
  class Device:
   def elements(self,n):return [Element(k,v) for k,v in props[n]['values'].items()]
   getNumber=getText=getSwitch=elements
  # A temporary SDK stand-in avoids stat/read access to any installed library.
  with tempfile.TemporaryDirectory() as directory:
   sdk=Path(directory)/'libqhyccd.so';sdk.write_bytes(b'offline fixture')
   with patch('pathlib.Path',return_value=sdk), patch.object(C,'_sdk_digest',return_value=C.SDK_QUANT_SHA):
    self.assertEqual(C.readout_identity(Device()),READOUT)
  p=C.Exposure(CONFIG);c=cmd(.000488);job={'c_command':c,'c_native':dict(RAW,clip_fraction=.001)}
  p.prepare(job,[1,1]);p.compare_exposure(72.,c['exposure'],1)
  self.assertEqual(p.reply['exposure_resolution']['rows'],1);self.assertFalse(p.reply['exposure_resolution']['limited'])
  p.prepare(job,[1,2]);p.compare_exposure(72.,c['exposure'],1)
  self.assertEqual(p.reply['exposure_resolution']['model'],'not applied')
 def test_original_native_evidence_matches_grid(self):
  evidence=Path(__file__).parent/'fixtures/qhy-exposure-quantisation.json'
  rows=json.loads(evidence.read_text())['boundary_frames']
  groups={}
  for row in rows:
   n=C.exposure_rows(row['exposure_requested'],.0002625);groups.setdefault(n,[]).append(row['stats']['B']['mean'])
  self.assertEqual(set(groups),{1,2,3});self.assertLess(max(groups[1]),min(groups[2]));self.assertLess(max(groups[2]),min(groups[3]))

class DayClippingPolicy(unittest.TestCase):
 def native(self,clip):
  return dict(RAW,clipped=clip>=.01,clip_fraction=clip)
 def assert_no_clip_state(self,p):
  for name in ('clip_limit','clip_bad','clip_reference','clip_safe','clip_lower'):
   self.assertIsNone(getattr(p,name),name)
  self.assertEqual(p.clip_clear,0);self.assertFalse(p.clip_retested)
 def test_measured_useful_upper_row_is_not_vetoed(self):
  for upper,clip in [(120.24,.07078),(103.47,.00737)]:
   p=C.Policy();e,out=run(p,.000656,lambda e:(80.93,0.) if C.exposure_rows(e,.0002625)<3 else (upper,clip),count=16)
   self.assertEqual(C.exposure_rows(e,.0002625),3);self.assertTrue(p.stable)
   self.assertEqual(len({v[0] for v in out[-8:]}),1);self.assert_no_clip_state(p)
 def test_low_adu_increases_and_high_adu_reduces_despite_clipping(self):
  for known in (True,False):
   for e in (.000656,.01):
    for adu,direction in ((70.,1),(245.,-1)):
     with self.subTest(known=known,exposure=e,adu=adu):
      p=C.Policy();c=cmd(e)
      if not known:c['readout']['traffic']=31.
      for _ in range(3):result=p.decide(c,adu,self.native(.99))
      self.assertGreater((result[0]-e)*direction,0)
      self.assertEqual(result[1:],(1,1));self.assert_no_clip_state(p)
 def test_clipping_alone_cannot_unlock_day_adu_bracket(self):
  p=C.Policy();e,_=run(p,.000394,lambda e:(72.,0.) if C.exposure_rows(e,.0002625)==1 else (180.,0.))
  before=copy.deepcopy(p.resolution_lock);self.assertIsNotNone(before)
  for clip in [0.,.01,.07,.99,0.,.8]*3:
   result=p.decide(cmd(e),72.,self.native(clip))
   self.assertEqual(result[0],e);self.assertEqual(p.resolution_lock,before)
   self.assertEqual(p.resolution_changed,0)
  self.assert_no_clip_state(p)
 def test_real_adu_change_reopens_straddling_rows(self):
  for upper,expected in [(180.,1),(155.,2)]:
   p=C.Policy();e,out=run(p,.000394,lambda e:(72.,.07) if C.exposure_rows(e,.0002625)==1 else (upper,.2))
   self.assertEqual(C.exposure_rows(e,.0002625),expected)
   self.assertEqual(len({x[0] for x in out[-10:]}),1);self.assertIsNotNone(p.resolution_lock)
   e,_=run(p,e,lambda e:(55.,.07) if C.exposure_rows(e,.0002625)==1 else (125.,.2))
   self.assertEqual(C.exposure_rows(e,.0002625),2);self.assertTrue(p.stable)
 def test_neighbour_above_short_region_is_neutralised(self):
  p=C.Policy();native=self.native(.1323)
  p.decide(cmd(.001272),152.10,native)
  self.assertEqual(p.resolution_observed[4],{'adu':152.10,'clip':0.,'clipped':False})
  # Revisit from row 3: a useful row-4 measurement must not carry a raw veto.
  p.decide(cmd(.001272),135.93,self.native(.04976))
  for _ in range(2):result=p.decide(cmd(.000919),90.,self.native(.07))
  self.assertEqual(C.exposure_rows(result[0],.0002625),4)
  self.assertFalse(p.resolution_observed[4]['clipped']);self.assert_no_clip_state(p)
 def test_native_telemetry_unchanged_through_adapter(self):
  x=C.Exposure(CONFIG);c=cmd(.000919);native=dict(self.native(.07078),raw_mean=127.397,pixel_sha='original-pixels')
  original=copy.deepcopy(native);job={'c_command':c,'c_native':native}
  for _ in range(3):x.prepare(job,[1,c['epoch']]);x.compare_exposure(120.24,c['exposure'],1)
  self.assertEqual(native,original);self.assertIs(job['c_native'],native)
  self.assertTrue(x.reply['stable']);self.assertEqual(x.reply['proposal'],(.000919,1,1))
  self.assertEqual(x.reply['exposure_resolution']['clipping_policy'],'day-adu-primary')
  self.assert_no_clip_state(x.policy)
 def test_day_policy_is_independent_of_both_raw_clipping_fields(self):
  for e in (.000656,.001272,.01):
   for known in (True,False):
    for clipped,clip in ((True,0.),(False,.99),(True,.99)):
     with self.subTest(exposure=e,known=known,clipped=clipped,fraction=clip):
      p=C.Policy();clear=C.Policy();c=cmd(e)
      if not known:c['readout']['traffic']=31.
      native=dict(RAW,clipped=clipped,clip_fraction=clip);before=copy.deepcopy(native)
      for adu in (70.,70.,125.,245.):
       self.assertEqual(p.decide(c,adu,native),clear.decide(c,adu,self.native(0.)))
       self.assertEqual(p.__dict__,clear.__dict__)
      self.assertEqual(native,before);self.assert_no_clip_state(p)
 def test_day_neutralisation_preserves_invalid_feedback_protection(self):
  p=C.Policy();c=cmd(.000919);native=dict(self.native(.99),valid=False)
  self.assertEqual(p.decide(c,120.,native),(.000919,1,1))
  self.assertEqual(p.status,'invalid brightness; hold')
  with self.assertRaisesRegex(C.ControlError,'repeated invalid brightness'):p.decide(c,120.,native)
 def test_mode_transitions_match_fresh_policy(self):
  p=C.Policy();p.decide(cmd(.125,mode='night'),120.,self.native(.07));self.assertIsNotNone(p.clip_bad)
  c=cmd(.000656,epoch=2);fresh=C.Policy()
  self.assertEqual(p.decide(c,80.,self.native(.07)),fresh.decide(c,80.,self.native(.07)))
  self.assertEqual(p.__dict__,fresh.__dict__);self.assert_no_clip_state(p)
  # Day creates an ADU lock; the next Night/Moon epoch must not inherit it.
  for mode in ('night','moon'):
   p=C.Policy();e,_=run(p,.000394,lambda e:(72.,.07) if C.exposure_rows(e,.0002625)==1 else (180.,.2))
   self.assertIsNotNone(p.resolution_lock);c=cmd(.01,mode=mode,epoch=2);fresh=C.Policy()
   self.assertEqual(p.decide(c,120.,self.native(.07)),fresh.decide(c,120.,self.native(.07)))
   self.assertEqual(p.__dict__,fresh.__dict__);self.assertIsNotNone(p.clip_bad)
 def test_night_moon_and_sqm_match_committed_policy(self):
  baseline=types.ModuleType('before_day_clipping')
  exec(subprocess.check_output(['git','show','ccdb57afdcc62f2a2d93fd21362dd3530fbaa720:indi_allsky/exposure/qhy5lii_c.py'],cwd=Path(__file__).parents[1],text=True),baseline.__dict__)
  for mode in ('night','moon'):
   for short in (True,False):
    p=C.Policy(clock=lambda:0.);old=baseline.Policy(clock=lambda:0.);e=.000919 if short else .125
    for n in range(60):
     c=cmd(e,mode=mode);adu,clip=(80.,.02) if n%12==0 else (50.,.001)
     result=p.decide(c,adu,self.native(clip));prior=old.decide(c,adu,self.native(clip))
     self.assertEqual(result,prior)
     self.assertEqual({k:v for k,v in p.__dict__.items() if k!='clock'},{k:v for k,v in old.__dict__.items() if k!='clock'})
     e=result[0]
  for mode in ('day','night','moon'):
   p=C.Policy();old=baseline.Policy();c=cmd(1.,mode=mode,kind='sqm')
   self.assertEqual(p.decide(c,0.,self.native(.99)),old.decide(c,0.,self.native(.99)))
   self.assertEqual({k:v for k,v in p.__dict__.items() if k!='clock'},{k:v for k,v in old.__dict__.items() if k!='clock'})

if __name__=='__main__':unittest.main()
