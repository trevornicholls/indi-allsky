# Generic ADU precision helper reconstruction — 1 November 2026

Stage 3 of MIGRATION_RECONSTRUCTION_PLAN_20261101.md, based on generic
commit 187c028d25d906f551b16a532e14b8ce84f75c71.

The only application change adds the optional `preserve_precision=False`
argument to `calculate_8bit_adu` and `_calculate_8bit_adu`. Opting in returns
floating-point mean ADU for 8-bit input. Default integer truncation, masking,
colour conversion and 16-bit shifting remain unchanged. No caller opts in
in this generic layer; QHY policy and SQM initialisation remain deferred.

The complete resulting processing.py is byte-identical to
28ff8541b97e43d1b6ef1a52593b3ce49d5bda2c:app/indi_allsky/processing.py
in the preserved qualified snapshot. This reconstructs the original helper,
without extending its semantics.

## Verification

Five focused tests were added before the helper: they failed because the
optional argument was absent. After reconstruction all five pass, covering
fractional means, public forwarding, legacy return types, masked colour input,
16-bit behaviour and unsupported depth. The tests compile the two actual
source methods unchanged, isolating metering from Flask/database startup;
they do not claim full application integration coverage.

Command (from repository root):

```sh
/var/lib/indi-allsky/venv/bin/python3 -B -m unittest tests.test_adu_precision tests.test_exposure_controller
```

Result: 15 test methods passed, comprising five new metering tests and the
10 existing generic controller tests. `git diff --check` passed. Existing
generic correction source and regression files are unchanged from the parent.
No production files, services, camera state or historical evidence were changed.

The resulting commit is the generic tip G; its ID is reported at the Stage 3
checkpoint rather than embedded in its own contents. Stage 4 is not performed.
