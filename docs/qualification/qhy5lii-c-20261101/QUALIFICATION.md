# Qualified QHY state reconstruction — Stage 5 PASS

This verifies source/control equivalence to the previously qualified application;
it is **not a new physical-camera or night-sky qualification**.

Historical qualified commit: `28ff8541b97e43d1b6ef1a52593b3ce49d5bda2c`.
Reconstructed application commit: `00c29c1450a77694ad46cb1fd755377845664e86`.
Generic tip G: `8fbf4612f1be85644c867eb51bbf7b7c16b00b46`.
Genuine upstream: `fc55f347d6775fcdf4a29bee8680ee5e255d4eb1` (`indi_v2026.09.01`).

The Stage 5 request contained a 41-character candidate typo. The accepted Q5
commit and branch HEAD identify the 40-character reconstructed commit above.
This documentation-only checkpoint changes no application or test source.

## Independent whole-state comparison

[manifest.json](manifest.json) records Git blob IDs, SHA-256 values, modes,
exceptions, upstream-only paths, ancestry, stage mappings and test results.
The comparison reads complete committed trees, independently of the incremental
patch checks. All 494 original `app/` payload paths are accounted for:

|Category|Count|Verification|
|---|---:|---|
|Exact same-path contents|485|Blob contents and file modes identical.|
|Package version suffix|2|Only `+trixie` differs in version.py and flask/templates/base.html.|
|PNG metadata|4|Only tIME chunks differ; all other chunks, modes, dimensions and decoded pixels identical.|
|Configuration relocation|3|Installed conf names exactly match source debian/conf names and modes.|
|Unexplained differences|0|PASS.|

PNG names under `indi_allsky/flask/static/astropanel/img/`: moon.png,
moon_rot.png, solar_system.png and sun.png.
Relocated files: 90-indi-allsky-journald.conf, 90-indi-allsky-sysctl.conf and
indi-allsky-watchdog.conf.

All eight QHY-development application paths, including the extracted precision
helper, exactly match the historical qualified blobs. The two original generic
correction files match both 187c028d and the qualified blobs. All 333 genuine
upstream-only tracked paths remain unchanged, including source/build, migration,
licensing and supporting material. No upstream tracked path was deleted.
Only the identified application paths differ from upstream; remaining additions
are tests/documentation. Complete lists appear in the manifest.

## History and tests

|Stage|Historical|Reconstructed|
|---|---|---|
|Q1|9717651|81e19f2d|
|Q2|8482598|601951b9|
|Q3|0240222|9cc6a619|
|Q4|f445f2c|d4f20f26|
|Q5|28ff854|00c29c14|

Full IDs and each exact parent are in the manifest. The Q1–Q5 chain starts at
amended G; G descends from unchanged 187c028d and the genuine upstream release.
`generic/exposure-fixes` remains at G.

All **50 test methods and 110 subtests pass**. Existing portable integration
checks pass: real ImageWorker/QHY/basic construction, synthetic-mask/SQM helper
initialisation and actual INDI gain-writer logic with in-memory properties.
No worker starts or camera connections occur. These checks do not claim a new
full image-processing or physical night run. Syntax/whitespace checks pass.

## Preserved qualification and deployment

Historical report/evidence:
`/home/trevor/indi-allsky-dev/commissioning/qhy-day-night-day-20261101/REPORT.md`.
Its assessment is **PASS WITH OBSERVATIONS**, covering 31 October 13:21:15 to
1 November 11:48:30 UTC, including Day → Night → Moon → Day. All 29 checksum-index
entries verify; all 30 files in that directory are fingerprinted for preservation.
The standalone historical repository and original evidence remain unchanged.

The qualified profile is QHY5L-II-C, SDK 26.7.21 with the recorded binary hash,
RAW8/full 1280×960/bin 1/speed 0/traffic 30. Maximum exposure 15 s; gain ceilings
Day/Night/Moon 1/14/14; day target 125±25 and night/Moon 75±20. The 262.5 µs row
model remains profile-specific, not an independently timed hardware measurement.
The ten installed generic/QHY source files match the historical qualified state
and remain unchanged during verification. No production operations are performed.

Full Stage 5 logs and result are retained outside the shared repository under
`/home/trevor/indi-allsky-dev/stage5-equivalence-20261101/` and
`/home/trevor/indi-allsky-dev/STAGE5_RESULT_20261101.md`.
No private configuration, database, raw journals or native images are imported.
Tagging, publication authentication and publication remain pending approval.
