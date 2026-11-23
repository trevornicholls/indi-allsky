# Independent Chelmsford Skies JPEG publisher

External host utility, **not deployed or commissioned**. It is stored here for
review/versioning, outside `indi_allsky/`, `misc/` and Debian install payloads.
It imports no INDI-AllSky application code, opens no camera, changes no application
configuration/database, and has no dependency on either Map service.

## Contract and policy

Production destination is fixed HTTPS POST to
`https://chelmsfordskies.co.uk/api/camera/latest`. No redirects or proxy discovery.
Body is unchanged JPEG bytes, `Content-Type: image/jpeg`, explicit Content-Length,
`Authorization: Bearer` and canonical decimal `X-Frame-Sequence`.

Sequence range: 1..9007199254740991. ACK is bounded JSON with `committed: true` and
exact string sequence. Every 409 blocks for review, including structured
`obsolete_sequence` / `sequence_content_mismatch`; never learn a counter from it.
400/401/413/415/422, invalid ACK, unexpected statuses and TLS verification errors
also block. 503/unknown transport outcomes retain exact pending bytes; 429 honours
whole-second Retry-After. No immediate retry; one POST per eligible invocation.

A local lock covers the entire run. State and snapshot creation use fsynced,
same-filesystem replacements and directory fsync before any network attempt.
Persistent state records the allocation high-water mark, one pending sequence /
source / filename / SHA-256 / creation time, last ACK, backoff and review status.
A provisional attempt delay is durable before networking, including process death.
After a committed response, the ACK is durable before snapshot cleanup.

Normal eligibility is ACK time +330 seconds. Timer is completion-relative six
minutes (also six minutes after boot). Transient backoff is 6, 12, 24, then 30
minutes and survives restart. An eligible pending frame at least 30 minutes old
is superseded only by a **newer fresh source key** with a newly allocated sequence.
A longer Retry-After remains binding. No backlog. Corrupt pending bytes block,
even when old enough to supersede. Missing/broken state never automatically resets.
A maximum-sequence pending frame can retry/ACK; new allocation then blocks forever
until operator/server namespace reconciliation. Counters never wrap.

## Source and JPEG validation

Read-only SQLite query selects the newest ordinary `image` row for the configured
camera by createDate/id; no backwards directory scan or fallback to older images.
The archived image was completed before that row committed. The public
`images/latest.jpg` is **not atomically published** and is not the upload source.

Resolve paths inside the image root, bound reads, check file identity/size/times
before/after reading, compare recorded size, and decode the JPEG fully with Pillow.
Limits are inclusive: encoded bytes <=2097152; decoded width*height <=4000000.
No re-encoding, calibration, overlay changes or resizing. Up to two source selection
attempts tolerate retention races. Default new-source maximum age 600 seconds;
future clock skew beyond 60 seconds is rejected. Exact pending retries deliberately
have a separate 30-minute lifetime. Already acknowledged source keys are skipped.

Private snapshots are immutable by convention within the trusted service account;
their digest is checked on every later invocation. The utility neither repairs nor
regenerates a missing/corrupt pending snapshot. It retains blocked evidence.
Atomicity protects normal crashes/power loss on reliable local storage, **not**
restoring old state backups or a malicious writer. Provision one sole writer for
this server sequence namespace. A source database replacement needs review too.

## Requirements and inactive templates

Python 3.13 stdlib is the HTTP/state/SQLite implementation; Pillow is needed for
full JPEG decoding. Tests currently use the existing application venv interpreter
without changing it. Production should use host Python/Pillow so an application
venv upgrade does not affect this independent utility. No HTTP SDK/rclone required.

Included `.service`/`.timer` are **templates, not installed/enabled**. They expect:

- dedicated `chelmsford-publisher` unprivileged user/group;
- utility under `/usr/local/lib/chelmsford-publisher/`;
- host Pillow and read access to source image directory and SQLite DB/WAL/SHM;
- non-secret config `/etc/chelmsford-publisher/publisher.json` (example provided);
- private local `/var/lib/chelmsford-publisher` owned by publisher, mode 0700;
- root-protected token source `/etc/chelmsford-publisher/upload-token`, loaded with
  systemd LoadCredential as `upload-token` for this user only.

Read access to the application database exposes more metadata than one image;
agree permissions at commissioning. Templates use filesystem protections and no
capture service dependency. Adapt source paths deliberately if different. The
oneshot has explicit 60-second start / 5-second stop deadlines; stdlib blocking I/O
uses 20 seconds. Direct manual `run` has the socket bound but not the systemd hard
whole-job deadline: use the service for real network runs. No restart loop.

Token is never accepted in argv, config or environment: only the credential
**directory path** is read from `CREDENTIALS_DIRECTORY`. Tokens must contain exactly
64 lowercase hexadecimal characters (the agreed 256-bit token format). Only
terminal CR/LF characters are removed in memory, including repeated line endings;
spaces, tabs, leading whitespace and embedded line breaks are never stripped.
The source file is never rewritten; its existing 256-byte size cap still applies.
The server supplies its dedicated 256-bit secret. There is no
secret generator in this tool. The original token source must remain root:root 0600. Systemd-delivered
0400/0440 copies are accepted only inside its root-managed, private, read-only
`/run/credentials/<unit>.service` store; 0440 may represent its named-user ACL
mask, not general group sharing. Arbitrary credential directories are rejected.
Logs contain only fixed categories. No request/response body or arbitrary exception
is printed; state/snapshot data has no credentials. Root/privileged administrators
necessarily retain access. No actual token is included in this repository.

## Local tests (no deployment/network to production)

From this directory:

```
/var/lib/indi-allsky/venv/bin/python -B -m unittest -v
```

Tests use disposable SQLite/JPEG fixtures, synthetic secrets, fake time, pure fault
injection and loopback HTTP only. No production database, token or external URL
is used by a test. Production CLI exposes **no HTTP/test endpoint override**.
A crash hook emulates interruption around persistent write/ACK boundaries;
physical power-loss durability is not claimed from tests alone.

## Explicit commissioning boundary

Do **not** run `initialise` against production until Sites endpoint and existing
server sequence namespace have been confirmed. There is **no default sequence**.
The offline command requires both a value and an explicit operator confirmation:

```
python3 publisher.py --config /path/to/non-secret-config.json initialise \
    --next-sequence CONFIRMED_VALUE --confirm-server-namespace
```

The state directory must already be privately provisioned; initialise refuses an
existing state. Sequence 1 is only demonstrated in fresh test namespaces.
Initialisation makes no HTTP request. Runtime missing state fails closed.

Blocked state has **no automated reset or counter-reseed action**. Stop the timer
and preserve state/snapshots, reconcile the reason/remote namespace, then perform
an explicitly reviewed recovery. Do not delete state and reinitialise merely to
clear an error. A 409 never authorises an automatic leap to current_sequence+1.

Next separately authorised steps: confirm live Sites contract → provision account,
permissions/host decoder and secret → confirm production next sequence → offline
initialisation → one controlled real upload → anonymous GET/ETag/bytes check → only
then enable periodic timer. No Map registration/update is needed merely to replace
JPEG bytes at a stable public URL. Changing the Map image URL remains separate.

## Exact-frame metadata companion (Sites v10)

`frame_metadata.py` is an independent, bounded companion, not an INDI-AllSky
worker or a second JPEG uploader. `publisher.py` freezes the explicit metadata
allowlist from the same selected SQL row and validated JPEG (EXIF Model) into
`STATE/metadata/candidate.json`, after durable sequence allocation and before
POST. Source identity, JPEG state schema, POST, ACK, deduplication, cooldown,
retry/backoff and the six-minute timer are unchanged. Failed capture/freeze
skips metadata rather than blocking JPEG publication. Legacy ACKs/pending
frames are never reconstructed. A crash between allocation and freezing can
therefore safely omit that frame's metadata.

Provision `STATE/metadata` owner chelmsford-publisher, mode0700. The producer alone
replaces candidate.json; the companion alone replaces delivery.json and owns its
own lock. Files are0600 and atomically replaced/fsynced. Root JPEG cleanup does
not scan this subdirectory. Only a candidate matching the durable JPEG last_ack
(sequence, digest AND unchanged source identity) is eligible. No post-ACK DB
lookup. Only the newest candidate exists; metadata retries never cause JPEG
resends. Receipt timestamps use host `/etc/localtime`; DST-fold/gap uncertainty
becomes null. jSQM is relative and uncalibrated. Optional driver DATE-OBS is
UTC, not hardware-certified shutter timing. No full raw DB JSON is published.

Install the additional service/timer separately. It reuses LoadCredential and
reads JPEG state through a read-only mount; only the metadata directory is
writable. One PUT/run,10s socket timeout,25s service budget, one-minute timer,
metadata-only60/120/240/480/960/1800s backoff. Honor429 Retry-After. A matching200
ACK persists success; stale_sequence drops; jpeg_not_committed defers; transport
or503 ambiguity retries the same frozen JSON. Conflicts/auth/validation/TLS/bad
ACK stop that candidate for review, without touching JPEG state. A new candidate
has an independent delivery identity/budget; review outcomes remain in the
journal, not a backlog. Never log arbitrary responses or exceptions.

Tests use local fixtures/loopback only. Run from this directory:
`/usr/bin/python3 -B -m unittest discover -p 'test*.py'`.
For independent old/new JPEG-policy comparison, export the parent publisher
outside the checkout and set `BASELINE_PUBLISHER_PATH` to that nonsecret file.
No production credential or endpoint is used by the suite.
