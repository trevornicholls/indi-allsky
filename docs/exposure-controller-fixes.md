> Historical report carried forward from `187c028d25d906f551b16a532e14b8ce84f75c71`.
> Its baseline, paths and results describe the original work. Stage 10A reapplies
> the generic change on upstream `fc82078de0e7764cc4ae1629260c025357c05491`;
> current integration results are recorded outside this tree in `STAGE10A_RESULT_20261101.md`.

# Exposure controller corrections — review candidate

Baseline: `fc55f347d6775fcdf4a29bee8680ee5e255d4eb1` (`indi_v2026.09.01`).
Branch: `fix/exposure-controller`, separate worktree at
`/home/trevor/indi-allsky-dev/development/indi-allsky-exposure`.
Original source checkout, preserved archives/packages, clean and observed VM
images remain unchanged. No deployment, production database access, camera
access, service operation or QHY/Core change was performed.

## Minimal corrections

1. In `reduce_gain()`, convert the remaining gain shortfall into exposure using
   `10 ** ((requested_gain_db - minimum_gain_db) / 20)`.
   Previously the minimum was added; this only happens to work for a zero-dB
   floor. The requested dB value need not be negative. The corrected exponent
   is negative whenever the requested gain falls below its floor.
   Example: 10-second exposure, current gain 12 dB, requested equivalent gain
   0 dB, floor 6 dB: old exposure 19.952623 s, corrected 5.011872 s.
   Both respect the existing minimum-exposure handling. The intended linear
   brightness relationship is exposure times `10 ** (gain_db / 20)`.
2. After strategy adjustment, cap the result at `self.exposure_max` and
   recompute `exposure_delta` from the final value. Gain allocation still sees
   the uncapped requested exposure, preserving exposure-priority behaviour.
   The base check protects against a strategy returning an excessive value,
   and recovers an already excessive exposure when the recalculation path
   reduces it but leaves it above the configured maximum.

The existing in-target early return is unchanged: this is a ceiling on newly
calculated settings, not a redesign of mode-change handling or a camera safety
interlock. No smoothing, hysteresis, dB convention or gain limits were changed.

## Tests and results

`tests/test_exposure_controller.py` uses real controllers and
`IndiAllSkyExposureUtils`, multiprocessing arrays with integer microseconds
and milligain, synthetic configuration, and no application/database context.

- Untouched baseline: 10 methods, 104 parameterized subtests, 45 failures,
  zero errors (expected regression reproduction).
- Corrected controller: 10 methods, 104 parameterized subtests, zero failures
  or errors.
- `git diff --check` passed.

Coverage includes all four dB/ISO mappings, negative/zero/positive floor dB,
values immediately below/at/above the floor, remaining brightness allocation,
exposure/gain deltas, day/night exposure minima, final maximum protection,
existing overmaximum recovery, unchanged requests, gain/exposure saturation,
ADU convergence in a linear brightness model, target tolerance endpoints,
six-sample history reacquisition, zero/negative measured ADU guarding, day
scaling, day/night/moon binning, and basic/legacy strategy compatibility.

The existing `testing/autogain_test.py` was inspected before editing. It is an
interactive database-backed harness, not an executable regression suite: it
loads Flask/config from the application DB, uses obsolete floating-point array
units and calls the nonexistent `calculate_exposure()` method. It was not run
against the production DB. Its eight adjustment scenarios are represented in
`test_original_harness_scenarios_and_bounds`, across all four gain mappings.
Existing `tests/` contains no controller unit suite; camera/libcamera exposure
scripts require devices and are outside this task. Unrelated image/lens tests
were not run. No hardware or full capture pipeline validation is claimed.

Test fixtures initially omitted the milligain scaling; this was corrected in
a separate test commit. Authoritative baseline/fixed logs use identical final
fixtures. The initial scratch logs are retained but are superseded.

## Repeating the tests

No host packages were installed. `ephem` (imported by `utils.py`) was extracted
only into workspace `development/test-deps` from the preserved original vendor
Python archive `vm/stage4/downloads/venv.tar.zst`. No mock of controller or
utility implementation is used. Python 3.13 on this host runs the tests.

From `/home/trevor/indi-allsky-dev`:

```sh
python3 -B development/exposure-results/run_tests.py --baseline
python3 -B development/exposure-results/run_tests.py
```

The baseline command intentionally exits 1; the corrected command exits 0.
The runner selects source explicitly and uses the same final test file.
Logs, source-only diff, review report, dependency hashes and commit metadata
are retained in `development/exposure-results`.

## Dusk oscillation — remaining uncertainty

The wrong floor arithmetic can reverse a brightness reduction, so it is a
credible contributor to oscillation. The synthetic tests establish the
arithmetic correction and convergence under a static linear scene, not that
all dusk oscillation is resolved. Actual scene brightness variation, gain
quantization/settling, delayed exposure feedback, day/night transitions and
history logic remain unverified in combination. No new damping is introduced.
A later authorized simulator/recorded-input replay should compare identical
brightness sequences before/after and log ADU, target state, mode, requested
and measured exposure/gain, and frame timing. VM deployment and rollback
rehearsal remain subject to review. Physical-camera testing is not authorized.
