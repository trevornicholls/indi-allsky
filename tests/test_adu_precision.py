"""Exercise the actual ADU methods without application/database initialisation."""
import ast
import logging
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np


def processing_methods():
    # Compile the source methods unchanged; avoid importing Flask and its database.
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'indi_allsky/processing.py').read_text())
    methods = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)
               and node.name in ('calculate_8bit_adu', '_calculate_8bit_adu')]
    assert len(methods) == 2
    namespace = {'cv2': cv2, 'numpy': np, 'math': math, 'logger': logging.getLogger(__name__)}
    exec(compile(ast.Module(body=methods, type_ignores=[]), 'processing.py', 'exec'), namespace)
    return type('Meter', (), {node.name: namespace[node.name] for node in methods})


class AduPrecisionTests(unittest.TestCase):
    def meter(self, image, bitpix=8, depth=16, mask=None):
        meter = processing_methods()()
        meter.image = image
        meter.max_bit_depth = depth
        meter._adu_mask_dict = {1: np.ones(image.shape[:2], dtype=np.uint8) if mask is None else mask}
        meter.getLatestImage = lambda: SimpleNamespace(binning=1, image_bitpix=bitpix)
        return meter

    def test_default_and_explicit_legacy_integer(self):
        meter = self.meter(np.array([[10, 11]], dtype=np.uint8))
        for result in (meter.calculate_8bit_adu(), meter.calculate_8bit_adu(preserve_precision=False)):
            self.assertEqual(result, 10)
            self.assertIs(type(result), int)

    def test_fractional_precision_and_public_forwarding(self):
        meter = self.meter(np.array([[10, 11]], dtype=np.uint8))
        result = meter.calculate_8bit_adu(preserve_precision=True)
        self.assertEqual(result, 10.5)
        self.assertIs(type(result), float)
        self.assertEqual(meter._calculate_8bit_adu(meter.getLatestImage(), preserve_precision=True), 10.5)

    def test_mask_and_colour_conversion_preserved(self):
        image = np.array([[[10, 10, 10], [11, 11, 11], [255, 255, 255]]], dtype=np.uint8)
        meter = self.meter(image, mask=np.array([[1, 1, 0]], dtype=np.uint8))
        self.assertEqual(meter.calculate_8bit_adu(), 10)
        self.assertEqual(meter.calculate_8bit_adu(preserve_precision=True), 10.5)

    def test_sixteen_bit_semantics_unchanged(self):
        for depth, expected in ((16, 1), (12, 16)):
            meter = self.meter(np.array([[256, 257]], dtype=np.uint16), bitpix=16, depth=depth)
            for flag in (False, True):
                result = meter.calculate_8bit_adu(preserve_precision=flag)
                self.assertEqual(result, expected)
                self.assertIs(type(result), int)

    def test_saturated_mask_population_roundoff(self):
        # Synthetic populations from the failure and previous-mask comparison;
        # no installation paths, private image data or camera needed.
        for count in (383610, 976088):
            for colour in (False, True):
                with self.subTest(count=count, colour=colour):
                    image = np.full((960, 1280, 3) if colour else (960, 1280), 255, dtype=np.uint8)
                    mask = np.zeros((960, 1280), dtype=np.uint8)
                    mask.flat[:count] = 255
                    meter = self.meter(image, mask=mask)
                    self.assertEqual(meter.calculate_8bit_adu(preserve_precision=True), 255.0)
                    self.assertEqual(meter.calculate_8bit_adu(), 255)

    def test_only_one_upper_ulp_of_uint8_mean_is_normalised(self):
        upper = math.nextafter(255.0, math.inf)
        beyond = math.nextafter(upper, math.inf)
        for value in (upper, beyond, 256.0, -math.ulp(255.0), -1.0,
                      float('inf'), float('-inf'), float('nan'), 254.999, 0.0):
            with self.subTest(value=value), patch.object(cv2, 'mean', return_value=(value, 0., 0., 0.)):
                actual = self.meter(np.zeros((1, 1), dtype=np.uint8)).calculate_8bit_adu(preserve_precision=True)
                if math.isnan(value):
                    self.assertTrue(math.isnan(actual))
                else:
                    self.assertEqual(actual, 255.0 if value == upper else value)
        # A declared 8-bit frame backed by non-uint8 data must not acquire a
        # rounding exemption for a genuinely out-of-range source value.
        image = np.full((1, 1), upper, dtype=np.float64)
        self.assertEqual(self.meter(image).calculate_8bit_adu(preserve_precision=True), upper)

    def test_normalised_saturation_reaches_existing_clipping_policy(self):
        from indi_allsky.exposure import qhy5lii_c as C
        from tests.test_qhy5lii_c import command
        image = np.full((960, 1280), 255, dtype=np.uint8)
        mask = np.zeros(image.shape, dtype=np.uint8)
        mask.flat[:383610] = 255
        adu = self.meter(image, mask=mask).calculate_8bit_adu(preserve_precision=True)
        for mode in ('day', 'night', 'moon'):
            with self.subTest(mode=mode):
                policy = C.Policy()
                exposure = .1
                for _ in range(3):
                    result = policy.decide(command(exposure, mode=mode), adu,
                                           {'valid': True, 'clipped': True, 'clip_fraction': 1.0})
                    self.assertLess(result[0], exposure)
                    self.assertEqual(policy.invalid, 0)
                    self.assertIsNone(policy.halted)
                    exposure = result[0]

    def test_invalid_native_frame_still_halts_with_normalised_adu(self):
        from indi_allsky.exposure import qhy5lii_c as C
        from tests.test_qhy5lii_c import command
        with patch.object(cv2, 'mean', return_value=(math.nextafter(255., math.inf), 0., 0., 0.)):
            adu = self.meter(np.zeros((1, 1), dtype=np.uint8)).calculate_8bit_adu(preserve_precision=True)
        policy = C.Policy()
        native = {'valid': False, 'clipped': True}
        self.assertEqual(policy.decide(command(.1), adu, native), (.1, 1, 1))
        with self.assertRaisesRegex(C.ControlError, 'repeated invalid brightness'):
            policy.decide(command(.1), adu, native)

    def test_policy_rejects_untrusted_out_of_range_and_nonfinite_feedback(self):
        from indi_allsky.exposure import qhy5lii_c as C
        from tests.test_qhy5lii_c import command
        for value in (math.nextafter(255., math.inf), 255.001, 256., -.001,
                      float('nan'), float('inf'), float('-inf'), None, '255'):
            with self.subTest(value=value):
                policy = C.Policy()
                self.assertEqual(policy.decide(command(.1), value, {'valid': True}), (.1, 1, 1))
                with self.assertRaisesRegex(C.ControlError, 'repeated invalid brightness'):
                    policy.decide(command(.1), value, {'valid': True})

    def test_unsupported_bit_depth_still_rejected(self):
        meter = self.meter(np.array([[10, 11]], dtype=np.uint8), bitpix=32)
        with self.assertRaisesRegex(Exception, 'Unsupported bit depth'):
            meter.calculate_8bit_adu(preserve_precision=True)


if __name__ == '__main__':
    unittest.main()
