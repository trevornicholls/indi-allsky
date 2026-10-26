"""Controller regressions: real shared arrays, no Flask, database or camera."""
import unittest
from multiprocessing import Array
from indi_allsky import exposure as E

CLASSES = (E.exposure_autogain_exp_prio_db, E.exposure_autogain_exp_prio_db_1_10,
           E.exposure_autogain_exp_prio_iso, E.exposure_autogain_exp_prio_iso_1_100)


def controller(cls=CLASSES[0], floor=6, night=True):
    # Integer microseconds, unlike the historical interactive harness.
    e = Array('q', [10000000, -1, 0, 10000, 100, 15000000, -1])
    g = Array('d', [0] * 10)
    c = cls({'TARGET_ADU': 75, 'TARGET_ADU_DAY': 75}, e, g,
            Array('i', [1, 1, 1, 2, 3, -1]), Array('i', [int(night), 0]))
    for lo, hi in ((3, 4), (5, 6), (7, 8)):
        g[lo], g[hi] = c.dB2gain(floor), c.dB2gain(30)
    return c


class ExposureControllerTests(unittest.TestCase):
    def test_gain_floor_brightness_and_deltas(self):
        for cls in CLASSES:
            for floor in (-6, 0, 6, 12):
                for offset in (-0.01, -6, -20):
                    with self.subTest(cls=cls.__name__, floor=floor, offset=offset):
                        c = controller(cls, floor)
                        gain = c.dB2gain(floor + 6)
                        requested = 10 * 10 ** ((offset - 6) / 20)
                        exp, de, db, dg = c.reduce_gain(10, gain, requested)
                        self.assertAlmostEqual(exp, max(c.exposure_min, 10 * 10 ** (offset / 20)))
                        self.assertLessEqual(exp, 10)
                        self.assertAlmostEqual(db, floor)
                        self.assertAlmostEqual(de, exp - 10)
                        self.assertAlmostEqual(dg, c.gain_min - gain)

    def test_final_maximum_clamp_independent_of_strategy(self):
        c = controller()
        c.adjust_exposure_gain = lambda *a: (30, c.gain_min, 20, 0)
        c.recalculate_exposure(10, c.gain_min, 25, 75, 65, 85, 1)
        self.assertEqual(c._expUtils.EXPOSURE_NEXT, 15)
        self.assertEqual(c._expUtils.EXPOSURE_DELTA, 5)

    def test_existing_overmaximum_recovery(self):
        c = controller()
        c.recalculate_exposure(30, c.gain_min, 100, 75, 65, 85, 1)
        self.assertEqual(c._expUtils.EXPOSURE_NEXT, 15)
        self.assertEqual(c._expUtils.EXPOSURE_DELTA, -15)

    def test_floor_boundary_and_minimum(self):
        for cls in CLASSES:
            c = controller(cls)
            for db in (6 - 1e-8, 6, 6 + 1e-8):
                with self.subTest(cls=cls.__name__, db=db):
                    requested = 10 * 10 ** ((db - 12) / 20)
                    exp, gain, _, _ = c.adjust_exposure_gain(10, c.dB2gain(12), requested)
                    self.assertAlmostEqual(exp * 10 ** (c.gain2dB(gain) / 20), requested * 10 ** (12 / 20))
                    self.assertGreaterEqual(gain + 1e-10, c.gain_min)
            for night in (False, True):
                c = controller(cls, night=night)
                exp, gain, _, _ = c.adjust_exposure_gain(10, c.dB2gain(12), 1e-10)
                self.assertEqual(exp, c.exposure_min)
                self.assertAlmostEqual(gain, c.gain_min)

    def test_original_harness_scenarios_and_bounds(self):
        for cls in CLASSES:
            c = controller(cls, 0)
            for exp, db, adu in ((10, 1, 60), (15, 1, 60), (14, 1, 60),
                                 (15, 0, 90), (10, 6, 90), (5, 6, 200),
                                 (15, 30, 50), (0.01, 0, 190)):
                with self.subTest(cls=cls.__name__, exp=exp, db=db, adu=adu):
                    c.recalculate_exposure(exp, c.dB2gain(db), adu, 75, 65, 85, 1)
                    u = c._expUtils
                    self.assertGreaterEqual(u.EXPOSURE_NEXT, c.exposure_min - 1e-6)
                    self.assertLessEqual(u.EXPOSURE_NEXT, c.exposure_max)
                    self.assertGreaterEqual(u.GAIN_NEXT + 1e-10, c.gain_min)
                    self.assertLessEqual(u.GAIN_NEXT, c.gain_max + 1e-10)

    def test_linear_adu_convergence(self):
        for cls in CLASSES:
            for db, exp, light in ((12, 10, 10), (6, 1, 1), (6, 15, 1)):
                with self.subTest(cls=cls.__name__, db=db, light=light):
                    c = controller(cls)
                    gain = c.dB2gain(db)
                    for _ in range(12):
                        adu = light * exp * 10 ** (c.gain2dB(gain) / 20)
                        c.compare_exposure(adu, exp, gain)
                        if c.target_adu_found:
                            break
                        exp, gain = c._expUtils.EXPOSURE_NEXT, c._expUtils.GAIN_NEXT
                    self.assertTrue(c.target_adu_found)
                    self.assertTrue(65 <= adu <= 85)

    def test_target_boundaries_history_and_zero(self):
        for adu in (65, 75, 85):
            c = controller()
            c.compare_exposure(adu, 10, c.gain_min)
            self.assertTrue(c.target_adu_found)
            for _ in range(6):
                c.compare_exposure(adu, 10, c.gain_min)
            self.assertTrue(c.target_adu_found)
            for _ in range(6):
                c.compare_exposure(200, 10, c.gain_min)
            self.assertFalse(c.target_adu_found)
        for adu in (0, -1):
            c = controller()
            c.compare_exposure(adu, 10, c.gain_min)
            self.assertLessEqual(c._expUtils.EXPOSURE_NEXT, c.exposure_max)


if __name__ == '__main__':
    unittest.main()
