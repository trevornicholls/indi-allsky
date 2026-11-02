"""Actual calibration methods with temporary catalogue fixtures; no application/DB IO."""
import ast
import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import time
import unittest

import cv2
import numpy as np
import psutil
from astropy.io import fits
from sqlalchemy import column
from sqlalchemy.sql.expression import true as sa_true
from indi_allsky import constants
from indi_allsky.exceptions import CalibrationNotFound
from indi_allsky.exposure import qhy5lii_c as C
from tests.test_qhy5lii_c import CONFIG

ROOT = Path(__file__).resolve().parents[1]

def calibration_types():
    tree = ast.parse((ROOT/'indi_allsky/processing.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ImageProcessor')
    names = {'calibrate', '_calibrate', '_apply_calibration', 'fix_holes_early', '_fix_holes_early',
             'calculate_8bit_adu', '_calculate_8bit_adu', '_debayer'}
    body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names
            or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
                ('__cfa_bgr_map', '__cfa_gray_map') for t in n.targets)]
    isolated = ast.ClassDef(name='ImageProcessor', bases=[], keywords=[], body=body, decorator_list=[])
    image = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ImageData')
    ns = dict(cv2=cv2, numpy=np, psutil=psutil, time=time, Path=Path, constants=constants,
              logger=logging.getLogger('dark-bypass-test'), qhy_c=C,
              CalibrationNotFound=CalibrationNotFound, sa_true=sa_true)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[isolated, image], type_ignores=[])),
                 str(ROOT/'indi_allsky/processing.py'), 'exec'), ns)
    return ns['ImageProcessor'], ns['ImageData'], ns

class GainOneDarkBypass(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.Processor, self.Image, self.ns = calibration_types()
        self.config = copy.deepcopy(CONFIG)
        self.config.update(CAMERA_INTERFACE='indi', IMAGE_CALIBRATE_DARK=True,
                           IMAGE_CALIBRATE_BPM=False, IMAGE_CALIBRATE_FIX_HOLES=True,
                           IMAGE_CALIBRATE_HOLE_THOLD=30, IMAGE_CALIBRATE_MANUAL_OFFSET=1024)
        self.dark = np.full((960,1280),10,dtype=np.uint8); self.dark[8,8] = 255
        self.entries = []
        for seconds in (1,2,15):
            path = Path(self.temp.name)/f'dark-{seconds}s-gain22.fits'
            fits.PrimaryHDU(self.dark).writeto(path)
            self.entries.append(SimpleNamespace(getFilesystemPath=lambda p=path:str(p)))
        for name in ('IndiAllSkyDbDarkFrameTable', 'IndiAllSkyDbBadPixelMapTable'):
            query = Mock(); query.filter.return_value=query; query.order_by.return_value=query
            query.first.return_value = self.entries[0]
            self.ns[name] = SimpleNamespace(query=query, **{n:column(n) for n in
                ('camera_id','active','bitdepth','binmode','gain','exposure','temp','createDate')})
        self.processor = self.Processor()
        self.processor.config=self.config; self.processor.libcamera_raw=False
        self.processor.sensors_temp_av=[-273.15]*64; self.processor.dark_temperature_range=5
        self.processor.max_bit_depth=8; self.processor.focus_mode=False
        self.processor.night_av=[0,0]; self.processor._adu_mask_dict={1:np.ones((960,1280),dtype=np.uint8)}
        self.current = None; self.processor.getLatestImage=lambda:self.current

    def image(self,gain=1.,name='QHY CCD QHY5LII-C',exposure=.000656):
        from datetime import datetime
        data=np.random.default_rng(42).integers(70,151,(960,1280),dtype=np.uint8)
        data[10,10]=255
        hd=fits.HDUList([fits.PrimaryHDU(data)])
        hd[0].header.update(GAIN=gain, BAYERPAT='GRBG')
        now=datetime.now()
        image=self.Image(self.config,hd,exposure,gain,1,now,0.,now.date(),1,
                         'editable camera label','test-uuid','','',8,'GRBG',125,
                         detected_camera_name=name)
        self.current=image
        return image

    def assert_no_lookup(self):
        for name in ('IndiAllSkyDbDarkFrameTable','IndiAllSkyDbBadPixelMapTable'):
            self.ns[name].query.filter.assert_not_called()
            self.ns[name].query.first.assert_not_called()

    def test_gain_one_has_no_lookup_subtraction_or_longer_fallback(self):
        image=self.image(); original=image.hdulist[0].data.copy()
        with patch.object(self.processor,'_apply_calibration', wraps=self.processor._apply_calibration) as apply, \
             patch('astropy.io.fits.open', side_effect=AssertionError('calibration file opened')), \
             patch('cv2.subtract', side_effect=AssertionError('subtraction attempted')):
            with self.assertLogs('dark-bypass-test',level='INFO') as logs:
                self.processor._calibrate(image)
            apply.assert_not_called()
        self.assertIn('intentionally bypassing dark calibration',logs.output[0])
        self.assert_no_lookup(); np.testing.assert_array_equal(image.hdulist[0].data,original)
        self.assertFalse(image.calibrated); self.assertIsNone(image.hole_mask)

    def test_hole_repair_noop_and_previous_gain_mask_cannot_leak(self):
        old=self.image(3.); self.processor._calibrate(old)
        self.assertTrue(old.calibrated); self.assertIsNotNone(old.hole_mask)
        self.processor.fix_holes_early()
        image=self.image(); original=image.hdulist[0].data.copy()
        self.processor._calibrate(image)
        with patch.object(self.processor,'_fix_holes_early') as fix:
            self.processor.fix_holes_early(); fix.assert_not_called()
        self.assertIsNone(image.hole_mask); self.assertIsNotNone(old.hole_mask)
        np.testing.assert_array_equal(image.hdulist[0].data,original)

    def test_gain_above_one_and_fractional_gains_retain_dark_calibration(self):
        for gain in (1.000001,1.5,3.,14.):
            with self.subTest(gain=gain):
                image=self.image(gain); original=image.hdulist[0].data.copy()
                self.processor._calibrate(image)
                np.testing.assert_array_equal(image.hdulist[0].data,cv2.subtract(original,self.dark))
                self.assertTrue(image.calibrated); self.assertIsNotNone(image.hole_mask)
        self.assertEqual(self.ns['IndiAllSkyDbDarkFrameTable'].query.first.call_count,4)

    def test_other_camera_strategy_interface_or_missing_identity_do_not_bypass(self):
        cases=[('QHY CCD QHY5LII-M',C.STRATEGY,'indi'),('',C.STRATEGY,'indi'),
               (None,C.STRATEGY,'indi'),('ZWO CCD ASI120MC',C.STRATEGY,'indi'),
               ('QHY CCD QHY5LII-C2',C.STRATEGY,'indi'),
               ('QHY CCD QHY5LII-C','exposure_basic','indi'),
               ('QHY CCD QHY5LII-C',C.STRATEGY,'libcamera_imx477')]
        for name,strategy,interface in cases:
            with self.subTest(name=name,strategy=strategy,interface=interface):
                self.config['CCD_CONFIG']['EXPOSURE_CLASSNAME']=strategy
                self.config['CAMERA_INTERFACE']=interface
                image=self.image(name=name); original=image.hdulist[0].data.copy()
                self.processor._calibrate(image)
                self.assertTrue(image.calibrated)
                np.testing.assert_array_equal(image.hdulist[0].data,cv2.subtract(original,self.dark))

    def test_bpm_true_explicitly_refused_without_lookup_or_pixel_change(self):
        self.config['IMAGE_CALIBRATE_BPM']=True
        image=self.image(); original=image.hdulist[0].data.copy()
        with self.assertRaisesRegex(C.ControlError,'BPM'):
            self.processor._calibrate(image)
        self.assert_no_lookup(); np.testing.assert_array_equal(image.hdulist[0].data,original)
        self.assertFalse(image.calibrated); self.assertIsNone(image.hole_mask)

    def test_gain_above_one_preserves_enabled_bpm_and_hole_processing(self):
        self.config['IMAGE_CALIBRATE_BPM']=True
        image=self.image(3.); original=image.hdulist[0].data.copy()
        self.processor._calibrate(image)
        self.ns['IndiAllSkyDbBadPixelMapTable'].query.first.assert_called_once()
        self.ns['IndiAllSkyDbDarkFrameTable'].query.first.assert_called_once()
        np.testing.assert_array_equal(image.hdulist[0].data,cv2.subtract(original,self.dark))
        self.assertTrue(image.calibrated); self.assertTrue(image.hole_mask[8,8])
        self.processor.fix_holes_early()
        self.assertEqual(image.hdulist[0].data[8,8],max(image.hdulist[0].data[6,8],image.hdulist[0].data[8,6]))

    def test_legacy_missing_dark_offset_fallback_remains_for_non_bypassed_images(self):
        query=self.ns['IndiAllSkyDbDarkFrameTable'].query; query.first.return_value=None
        for gain,name in ((3.,'QHY CCD QHY5LII-C'),(1.,'ZWO CCD ASI120MC')):
            image=self.image(gain,name); original=image.hdulist[0].data.copy()
            self.processor._calibrate(image)
            np.testing.assert_array_equal(image.hdulist[0].data,cv2.subtract(original,4))
        self.assertEqual(query.first.call_count,4)  # Temperature lookup + fallback each.

    def test_colour_name_normalisation_and_database_label_isolation(self):
        image=self.image(name='qhy ccd qhy5l-ii-c'); self.processor._calibrate(image)
        self.assert_no_lookup(); self.assertFalse(image.calibrated)
        image=self.image(name=''); image._camera_name='QHY CCD QHY5LII-C'
        self.processor._calibrate(image); self.assertTrue(image.calibrated)

    def test_image_gain_wins_over_current_settings_and_editable_header(self):
        self.processor.gain_av=[14.]*64
        image=self.image(); image.hdulist[0].header['GAIN']=22
        self.processor._calibrate(image)
        self.assert_no_lookup(); self.assertFalse(image.calibrated)
        image=self.image(3.); image.hdulist[0].header['GAIN']=1
        self.processor.gain_av=[1.]*64; self.processor._calibrate(image)
        self.assertTrue(image.calibrated)

    def test_native_clipping_and_no_dark_adu_unchanged(self):
        image=self.image(); raw=image.hdulist[0].data.copy()
        native=C.native_statistics(raw,self.config); telemetry=copy.deepcopy(native)
        expected=cv2.cvtColor(cv2.cvtColor(raw,cv2.COLOR_BAYER_GB2BGR),cv2.COLOR_BGR2GRAY).mean()
        self.processor._calibrate(image); self.processor.fix_holes_early()
        self.processor.image=self.processor._debayer(image)
        self.assertEqual(self.processor.calculate_8bit_adu(preserve_precision=True),expected)
        self.assertEqual(C.native_statistics(image.hdulist[0].data,self.config),telemetry)
        self.assertEqual(native,telemetry); self.assertFalse(image.calibrated)

    def test_disabled_and_already_calibrated_guards_remain_first(self):
        self.config['IMAGE_CALIBRATE_BPM']=True
        image=self.image(); self.config['IMAGE_CALIBRATE_DARK']=False
        self.processor._calibrate(image)
        self.config['IMAGE_CALIBRATE_DARK']=True; image.calibrated=True
        self.processor._calibrate(image); self.assert_no_lookup()
        self.assertTrue(image.calibrated)

    def test_all_modes_and_short_long_gain_one_use_same_bypass(self):
        for night,moon in ((False,False),(True,False),(True,True)):
            for exposure in (.000001,.000656,.5,15.):
                with self.subTest(night=night,moon=moon,exposure=exposure):
                    self.processor.night_av=[night,moon]; image=self.image(exposure=exposure)
                    self.processor._calibrate(image); self.assertFalse(image.calibrated)
                    self.assertIsNone(image.hole_mask)
        self.assert_no_lookup()

if __name__=='__main__':unittest.main()
