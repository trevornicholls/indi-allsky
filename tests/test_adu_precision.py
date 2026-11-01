"""Exercise the actual ADU methods without application/database initialisation."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

import cv2
import numpy as np


def processing_methods():
    # Compile the source methods unchanged; avoid importing Flask and its database.
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'indi_allsky/processing.py').read_text())
    methods = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)
               and node.name in ('calculate_8bit_adu', '_calculate_8bit_adu')]
    assert len(methods) == 2
    namespace = {'cv2': cv2, 'logger': logging.getLogger(__name__)}
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

    def test_unsupported_bit_depth_still_rejected(self):
        meter = self.meter(np.array([[10, 11]], dtype=np.uint8), bitpix=32)
        with self.assertRaisesRegex(Exception, 'Unsupported bit depth'):
            meter.calculate_8bit_adu(preserve_precision=True)


if __name__ == '__main__':
    unittest.main()
