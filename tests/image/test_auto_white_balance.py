"""Real automatic-white-balance primitive; no camera, DB or application startup."""
import ast
from pathlib import Path

import cv2
import numpy
import pytest


@pytest.fixture
def processor():
    # Same isolated actual-method pattern as the existing MTF white-balance tests.
    path = Path(__file__).resolve().parents[2] / 'indi_allsky/processing.py'
    cls = next(node for node in ast.parse(path.read_text()).body
               if isinstance(node, ast.ClassDef) and node.name == 'ImageProcessor')
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name == '_white_balance_auto_bgr']
    namespace = {'cv2': cv2}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['ImageProcessor']()


@pytest.mark.parametrize('dtype', [numpy.uint8, numpy.uint16, numpy.float32, numpy.float64])
def test_automatic_white_balance_preserved_live_failure_reproducer(processor, dtype):
    original = numpy.empty((4, 4, 3), dtype=dtype)
    original[:] = [11, 12, 13]
    processor.image = original.copy()
    processor._white_balance_auto_bgr()
    numpy.testing.assert_array_equal(processor.image, numpy.full_like(original, 12))
    assert processor.image.dtype == original.dtype
    assert processor.image.shape == original.shape


@pytest.mark.parametrize('dtype', [numpy.uint8, numpy.uint16, numpy.float32, numpy.float64])
@pytest.mark.parametrize('fixture', ['black', 'zero_blue', 'highlights', 'random'])
def test_automatic_white_balance_preserves_scaling_saturation_and_dtype(processor, dtype, fixture):
    rng = numpy.random.default_rng(123)
    original = rng.integers(0, 256, (8, 8, 3)).astype(dtype)
    maximum = numpy.iinfo(dtype).max if numpy.issubdtype(dtype, numpy.integer) else 255
    if fixture == 'black':
        original[:] = 0
    elif fixture == 'zero_blue':
        original[:, :, 0] = 0
    elif fixture == 'highlights':
        original[:] = [1, maximum, maximum]
        original[0, 0] = [maximum, maximum, maximum]
    elif dtype == numpy.uint16:
        original *= 257
    # Independent numerical reference: existing mean-channel gain and zero-mean
    # denominator, followed by the original integer rounding/saturation semantics.
    means = original.astype(numpy.float64).mean(axis=(0, 1))
    target = means.mean()
    gains = target / numpy.where(means == 0, .1, means)
    expected = original.astype(numpy.float64) * gains
    if numpy.issubdtype(dtype, numpy.integer):
        expected = numpy.clip(numpy.rint(expected), 0, maximum).astype(dtype)
    else:
        expected = expected.astype(dtype)
    processor.image = original.copy()
    input_array = processor.image
    processor._white_balance_auto_bgr()
    numpy.testing.assert_array_equal(input_array, original)  # No in-place source changes.
    assert processor.image.dtype == original.dtype
    assert processor.image.shape == original.shape
    if numpy.issubdtype(dtype, numpy.integer):
        numpy.testing.assert_array_equal(processor.image, expected)
    else:
        # Float32 scalar-vs-array kernels may differ by normal rounding error.
        numpy.testing.assert_allclose(processor.image, expected, rtol=1e-6, atol=1e-5)
