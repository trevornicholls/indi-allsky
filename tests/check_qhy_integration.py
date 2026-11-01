"""Import/construct the real workers with isolated DB/config; never start a worker."""
from pathlib import Path
import copy,json,os,tempfile
# Temporary configuration must precede all application imports.
_tmp = tempfile.TemporaryDirectory(prefix="indi-allsky-q1-")
root = Path(_tmp.name)
flask_config = root / "flask.json"
flask_config.write_text(json.dumps({"SQLALCHEMY_DATABASE_URI":"sqlite:///:memory:",
    "MIGRATION_FOLDER":str(root / "migrations"), "SECRET_KEY":"offline-only",
    "SQLALCHEMY_TRACK_MODIFICATIONS":False}))
os.environ["INDI_ALLSKY_FLASK_CONFIG"] = str(flask_config)
from multiprocessing import Array,Queue
from types import SimpleNamespace
from threading import Event
import PyIndi
from indi_allsky.allsky import IndiAllSky  # normal application import order
from indi_allsky.image import ImageWorker
from indi_allsky.camera.indi import IndiClient
from indi_allsky.exposure import qhy5lii_c as C
from indi_allsky.config import IndiAllSkyConfigBase
cfg=copy.deepcopy(IndiAllSkyConfigBase._base_config)
cfg['CCD_CONFIG']['EXPOSURE_CLASSNAME']=C.STRATEGY
for mode,cap in [('DAY',1),('NIGHT',14),('MOONMODE',14)]:cfg['CCD_CONFIG'][mode]['GAIN']=cap
cfg['DETECT_MASK']=''
cfg['VARLIB_FOLDER']=str(root);cfg['IMAGE_FOLDER']=str(root/'images')
arrays=[Array('f',[0]*64),Array('q',[0]*64),Array('q',[0]*64),Array('i',[1]*64),Array('f',[0]*64),Array('f',[0]*64),Array('i',[0,0]),Array('f',[0]*64)]
w=ImageWorker(1,cfg,Queue(),Queue(),Queue(),*arrays,c_feedback=Queue(),c_shared=Array('i',[0,0]))
assert isinstance(w.exposure_o,C.Exposure)
w.image_processor.post_init() # prerequisites required even when SQM is first
assert w.image_processor._sqm is not None
print('Real ImageWorker construction, strategy selection and synthetic-mask/SQM initialization passed')
legacy=copy.deepcopy(cfg)
legacy['CCD_CONFIG']['EXPOSURE_CLASSNAME']='exposure_basic'
legacy_worker=ImageWorker(2,legacy,Queue(),Queue(),Queue(),*arrays)
assert not isinstance(legacy_worker.exposure_o,C.Exposure)
assert legacy_worker.c_feedback is None and legacy_worker.c_shared is None
print('Existing basic strategy constructs without QHY coordination')
# Exercise the actual final INDI writer against an in-memory property, including
# a fresh callback ACK before returning; no PyIndi client/socket is constructed.
class Element:
 def __init__(self):self.value=0
 def getName(self):return 'GAIN'
 def getValue(self):return self.value
 def setValue(self,v):self.value=v
 def getLabel(self):return 'Gain'
class Control(list):
 def getPermission(self):return PyIndi.IP_RW
ctl=Control([Element()]);calls=[]
f=SimpleNamespace(config=cfg,c_gate=None,c_gain_event=Event(),c_gain_ack=None)
f.get_control=lambda *args:ctl
f._IndiClient__map_indexes=lambda c,keys:{'GAIN':0}
f._IndiClient__wait_for_ctl_statuses=lambda *args,**kwargs:None
def send(c):calls.append(c[0].getValue());f.c_gain_ack=(PyIndi.IPS_OK,c[0].getValue());f.c_gain_event.set()
f.sendNewNumber=send
IndiClient.set_number(f,None,'CCD_GAIN',{'GAIN':3})
try:IndiClient.set_number(f,None,'CCD_GAIN',{'GAIN':3.5})
except C.ControlError:pass
else:raise AssertionError('fractional gain accepted')
assert calls==[3]
print('Real INDI final writer: integer accepted with fresh acknowledgement; fractional rejected before write')

_tmp.cleanup()
