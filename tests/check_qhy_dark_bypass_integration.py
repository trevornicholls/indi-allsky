"""Real image-worker ordinary/SQM/save/ACK paths; temporary DB/files, no devices."""
import copy
import json
import os
from pathlib import Path
from queue import Queue
import tempfile
import time
import uuid
from unittest.mock import patch
from multiprocessing import Array
import numpy as np
from astropy.io import fits

with tempfile.TemporaryDirectory(prefix='qhy-dark-bypass-') as tmp:
    root=Path(tmp); images=root/'images'; images.mkdir()
    flask=root/'flask.json'; flask.write_text(json.dumps({
        'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:', 'SECRET_KEY':'offline-only',
        'SQLALCHEMY_TRACK_MODIFICATIONS':False, 'MIGRATION_FOLDER':str(root/'migrations'),
        'INDI_ALLSKY_IMAGE_FOLDER':str(images)}))
    os.environ['INDI_ALLSKY_FLASK_CONFIG']=str(flask)
    from indi_allsky.allsky import IndiAllSky
    from indi_allsky.image import ImageWorker, app
    from indi_allsky.config import IndiAllSkyConfigBase
    from indi_allsky.flask import db
    from indi_allsky.flask.models import (IndiAllSkyDbCameraTable, IndiAllSkyDbImageTable,
                                         IndiAllSkyDbFitsImageTable, IndiAllSkyDbDarkFrameTable)
    from indi_allsky.exposure import qhy5lii_c as C
    from tests.test_qhy5lii_c import frame
    cfg=copy.deepcopy(IndiAllSkyConfigBase._base_config)
    cfg['CCD_CONFIG']['EXPOSURE_CLASSNAME']=C.STRATEGY
    for mode,cap in [('DAY',1),('NIGHT',14),('MOONMODE',14)]:cfg['CCD_CONFIG'][mode]['GAIN']=cap
    cfg.update(CAMERA_INTERFACE='indi', VARLIB_FOLDER=str(root), IMAGE_FOLDER=str(images),
               DETECT_MASK='', IMAGE_CALIBRATE_DARK=True, IMAGE_CALIBRATE_BPM=False,
               IMAGE_CALIBRATE_FIX_HOLES=True, IMAGE_STACK_COUNT=1, IMAGE_SAVE_FITS=True,
               IMAGE_SAVE_FITS_PRE_DARK=False, IMAGE_SAVE_FITS_PERIOD=0, DAYTIME_CAPTURE_SAVE=True,
               IMAGE_LABEL_TEMPLATE='', GENERATE_IMAGE_MASK=False)
    arrays=[Array('f',[0]*64),Array('q',[0]*64),Array('q',[0]*64),Array('i',[1]*64),
            Array('f',[0]*64),Array('f',[0]*110),Array('i',[0,0]),Array('f',[0]*64)]
    feedback=Queue(); shared=[0,0]
    worker=ImageWorker(1,cfg,Queue(),Queue(),Queue(),*arrays,c_feedback=feedback,c_shared=shared)
    # No upload worker is started. Image worker queues only local synthetic tasks.
    worker.next_save_fits_time=0
    gate=C.FrameGate(cfg,feedback,shared); gate.set_mode('day')
    gate.next=(.001,1,1)  # CaptureWorker's normal initial settings, before SQM.
    camera=IndiAllSkyDbCameraTable(name='test-camera',uuid=str(uuid.uuid4()),driver='indi_qhy_ccd',
        lensName='offline',lensFocalLength=1.55,lensFocalRatio=2.,latitude=0.,longitude=0.,
        owner='',location='',data={},width=1280,height=960,bits=8)
    with app.app_context():
        db.create_all(); db.session.add(camera); db.session.commit(); camera_id=camera.id
        for seconds in (1,2,15):
            dark=root/f'dark-{seconds}s-gain22.fits'
            fits.PrimaryHDU(np.full((960,1280),10,dtype=np.uint8)).writeto(dark)
            db.session.add(IndiAllSkyDbDarkFrameTable(filename=str(dark),bitdepth=8,
                exposure=seconds,gain=22.,binmode=1,temp=None,camera_id=camera_id,active=True))
        db.session.commit()
    steps=['sqm','normal','sqm','normal']
    with patch.object(worker.image_processor,'_apply_calibration',side_effect=AssertionError('dark lookup reached')):
        for index,kind in enumerate(steps):
            c=gate.start(.001,1,1,kind); gate.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':.001})
            hd=frame(c); hd[0].data[0,0]=20+index
            native=hd[0].data.copy(); command,raw=gate.frame(hd)
            filename=root/f'input-{index}.fits'; hd.writeto(filename)
            job={'filename':str(filename),'exposure':command['exposure'],'gain':1,'binning':1,
                 'exp_time':time.time()-60+index,'exp_elapsed':.01,'camera_id':camera_id,
                 'camera_name':'QHY CCD QHY5LII-C','sqm_exposure':kind=='sqm',
                 'c_command':command,'c_native':copy.deepcopy(raw)}
            worker.image_q.put(job); worker.image_q.put({'stop':True}); worker._shutdown=False
            worker.saferun()
            assert worker.exposure_o.reply['ok'],worker.exposure_o.reply
            assert worker.exposure_o.reply['command']==command
            assert gate.poll(True) is not None and shared[0]==0 and gate.fault is None
            assert not filename.exists()
            assert job['c_native']==raw
            if kind=='normal':
                ref=worker.image_processor.getLatestImage()
                assert not ref.calibrated and ref.hole_mask is None
                np.testing.assert_array_equal(ref.hdulist[0].data,native)
                with app.app_context():
                    saved=IndiAllSkyDbImageTable.query.order_by(IndiAllSkyDbImageTable.id.desc()).first()
                    assert saved is not None and not saved.calibrated
                    assert saved.data['qhy_native']==raw
                    saved_fits=IndiAllSkyDbFitsImageTable.query.order_by(IndiAllSkyDbFitsImageTable.id.desc()).first()
                    np.testing.assert_array_equal(fits.getdata(saved_fits.getFilesystemPath()),native)
            print('PASS actual worker',kind,'sequence',command['sequence'],'correlated ACK; gain-1 bypass')
    # Unsupported BPM is an actual processing fault: no successful ACK or gate reset.
    cfg['IMAGE_CALIBRATE_BPM']=True
    c=gate.start(.001,1,1,'normal');gate.property('CCD_EXPOSURE',{'CCD_EXPOSURE_VALUE':.001})
    hd=frame(c);hd[0].data[0,0]=25;command,raw=gate.frame(hd);path=root/'unsupported.fits';hd.writeto(path)
    worker.image_q.put(dict(job,filename=str(path),c_command=command,c_native=raw,sqm_exposure=False))
    worker.image_q.put({'stop':True});worker._shutdown=False;worker.saferun()
    assert worker.exposure_o.reply is None
    gate.poll(True);assert gate.fault and 'BPM' in gate.fault and shared[0]==2
    assert gate.pending==command  # Failed operation was not silently cleared.
    print('PASS actual BPM fault ACK retained primary cause and fail-closed gate')
    print('No socket/INDI client/SDK operation, service change or production DB/configuration used.')
