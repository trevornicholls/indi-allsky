#!/usr/bin/env python3
"""Independent, single-slot JPEG publisher. No INDI-AllSky application imports.

State/snapshots must be on reliable local storage. The CLI never initialises a
sequence namespace implicitly and never exposes an HTTP/test destination option.
"""
import argparse
import datetime
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import stat
import time

MAX_SEQUENCE = 9007199254740991
MAX_BYTES = 2097152
MAX_PIXELS = 4000000
MAX_RESPONSE = 4096
HOST = 'chelmsfordskies.co.uk'
UPLOAD_PATH = '/api/camera/latest'
_CREDENTIAL_ROOT = Path('/run/credentials')
LOG = logging.getLogger('chelmsford-publisher')


class PublisherError(Exception):
    """Only fixed, non-secret diagnostic categories may leave this module."""


class Busy(PublisherError):
    pass


class SourceUnavailable(PublisherError):
    pass


def sequence(value):
    return (isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,15}', value)
            is not None and int(value) <= MAX_SEQUENCE)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_file(path, data, hook=lambda point: None):
    """Same-filesystem replacement; no network may precede successful return."""
    tmp = path.with_name('.write-' + secrets.token_hex(16))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        hook('before_replace')
        os.replace(tmp, path)
        sync_directory(path.parent)
        hook('after_replace')
    finally:
        tmp.unlink(missing_ok=True)


def validate_jpeg(data):
    if not 0 < len(data) <= MAX_BYTES:
        raise SourceUnavailable('jpeg_encoded_size')
    # Marker checks are supplementary, never a substitute for full decoding.
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise SourceUnavailable('jpeg_incomplete')
    from PIL import Image, ImageFile
    from io import BytesIO
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with Image.open(BytesIO(data)) as image:
            if (image.format != 'JPEG' or min(image.size) <= 0
                    or image.width * image.height > MAX_PIXELS):
                raise SourceUnavailable('jpeg_decoded_size_or_format')
            image.load()
    except SourceUnavailable:
        raise
    except Exception:
        raise SourceUnavailable('jpeg_decode') from None


@dataclass(frozen=True)
class Config:
    database: Path
    image_root: Path
    camera_id: int
    state_dir: Path
    source_max_age: int = 600

    @classmethod
    def load(cls, path):
        try:
            d = json.loads(Path(path).read_text())
            if set(d) - {'database', 'image_root', 'camera_id', 'state_dir', 'source_max_age'}:
                raise ValueError
            c = cls(**{k: Path(d[k]) for k in ('database', 'image_root', 'state_dir')},
                    camera_id=d['camera_id'], source_max_age=d.get('source_max_age', 600))
            if (type(c.camera_id) is not int or c.camera_id < 1
                    or type(c.source_max_age) is not int or c.source_max_age < 1
                    or not all(p.is_absolute() for p in (c.database, c.image_root, c.state_dir))):
                raise ValueError
            return c
        except Exception:
            raise PublisherError('invalid_configuration') from None


class ImageSource:
    def __init__(self, config):
        self.config = config

    def newest(self, now):
        """Bounded reselection only; never search backwards for an older image."""
        c = self.config
        for attempt in range(2):
            try:
                db = sqlite3.connect(c.database.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=1)
                try:
                    row = db.execute('SELECT id, createDate, filename, fileSize '
                                     'FROM image WHERE camera_id=? '
                                     'ORDER BY createDate DESC, id DESC LIMIT 1',
                                     (c.camera_id,)).fetchone()
                finally:
                    db.close()  # no database connection while copying or networking
                if row is None:
                    raise SourceUnavailable('no_image')
                ident, created, relative, size = row
                if type(ident) is not int or ident < 1:
                    raise SourceUnavailable('invalid_source_identity')
                stamp = datetime.datetime.fromisoformat(created)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                age = now - stamp.timestamp()
                if age < -60 or age > c.source_max_age:
                    raise SourceUnavailable('stale_or_future_image')
                root = c.image_root.resolve()
                rel = Path(relative)
                path = (root / rel).resolve()
                if (rel.is_absolute() or not path.is_relative_to(root)
                        or path.suffix.lower() not in ('.jpg', '.jpeg')):
                    raise SourceUnavailable('image_path')
                with path.open('rb') as f:
                    before = os.fstat(f.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise SourceUnavailable('not_regular_image')
                    data = f.read(MAX_BYTES + 1)
                    after = os.fstat(f.fileno())
                sig = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                if (sig(before) != sig(after) or len(data) != after.st_size
                        or (size is not None and len(data) != size)):
                    raise SourceUnavailable('image_changed')
                validate_jpeg(data)
                return {'camera_id': c.camera_id, 'image_id': ident,
                        'created': created}, data
            except (OSError, sqlite3.Error, ValueError, TypeError):
                if attempt:
                    raise SourceUnavailable('source_unavailable') from None
            except SourceUnavailable:
                if attempt:
                    raise
        raise SourceUnavailable('source_unavailable')


def source_order(source):
    stamp = datetime.datetime.fromisoformat(source['created'])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp(), source['image_id']


class StateStore:
    def __init__(self, directory, hook=lambda point: None):
        self.directory = directory
        self.path = directory / 'state.json'
        self.hook = hook

    @contextmanager
    def lock(self):
        # Directory must be provisioned privately; never initialise on a run.
        if (not self.directory.is_dir() or self.directory.is_symlink()
                or self.directory.stat().st_mode & 0o077):
            raise PublisherError('state_directory_permissions')
        fd = os.open(self.directory / 'publisher.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy('already_running') from None
            yield
        finally:
            os.close(fd)

    def initialise(self, next_sequence):
        if self.path.exists() or not sequence(next_sequence):
            raise PublisherError('initialisation_refused')
        self.save({'schema_version': 1, 'next_sequence': next_sequence,
                   'pending': None, 'last_ack': None, 'failure_count': 0,
                   'next_eligible_utc': 0, 'blocked_reason': None})

    def save(self, state):
        durable_file(self.path, json.dumps(state, sort_keys=True).encode() + b'\n',
                     lambda p: self.hook('state_' + p))

    def load(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                raise ValueError
            s = json.loads(self.path.read_text())
            if set(s) != {'schema_version', 'next_sequence', 'pending', 'last_ack',
                          'failure_count', 'next_eligible_utc', 'blocked_reason'}:
                raise ValueError
            n = s['next_sequence']
            if (s['schema_version'] != 1 or not isinstance(n, str)
                    or not re.fullmatch(r'[1-9][0-9]{0,16}', n)
                    or not 1 <= int(n) <= MAX_SEQUENCE + 1
                    or type(s['failure_count']) is not int or s['failure_count'] < 0
                    or type(s['next_eligible_utc']) not in (float, int)
                    or not math.isfinite(s['next_eligible_utc']) or s['next_eligible_utc'] < 0
                    or (s['blocked_reason'] is not None and not isinstance(s['blocked_reason'], str))):
                raise ValueError
            p = s['pending']
            a = s['last_ack']
            for v in (p, a):
                if v is not None:
                    if not sequence(v['sequence']) or int(v['sequence']) >= int(n):
                        raise ValueError
                    src = v['source']
                    if (set(src) != {'camera_id', 'image_id', 'created'}
                            or type(src['camera_id']) is not int or src['camera_id'] < 1
                            or type(src['image_id']) is not int or src['image_id'] < 1
                            or not isinstance(src['created'], str)
                            or not re.fullmatch('[0-9a-f]{64}', v['sha256'])):
                        raise ValueError
            if p is not None:
                if (not re.fullmatch('snapshot-[0-9a-f]{32}\\.jpg', p['file'])
                        or type(p['created_utc']) not in (float, int)
                        or not math.isfinite(p['created_utc']) or p['created_utc'] < 0
                        or (a and int(p['sequence']) <= int(a['sequence']))):
                    raise ValueError
            return s
        except Exception:
            raise PublisherError('missing_or_invalid_state_requires_review') from None

    def cleanup(self, state):
        keep = state['pending']['file'] if state['pending'] else None
        for p in self.directory.glob('snapshot-*.jpg'):
            if p.name != keep:
                p.unlink()
        for p in self.directory.glob('.write-*'):
            p.unlink()

    def snapshot(self, data):
        name = 'snapshot-' + secrets.token_hex(16) + '.jpg'
        durable_file(self.directory / name, data,
                     lambda p: self.hook('snapshot_' + p))
        return name

    def pending_bytes(self, p):
        try:
            path = self.directory / p['file']
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError
            with path.open('rb') as f:
                data = f.read(MAX_BYTES + 1)
            if digest(data) != p['sha256']:
                raise ValueError
            validate_jpeg(data)
            return data
        except Exception:
            raise PublisherError('pending_snapshot_integrity') from None


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes
    oversized: bool = False
    incomplete: bool = False


class TransportFailure(Exception):
    def __init__(self, category):
        self.category = category


class HTTPTransport:
    """Only tests inject a loopback HTTP host; production uses fixed HTTPS."""
    def __init__(self, host=HOST, *, secure=True, timeout=20):
        self.host, self.secure, self.timeout = host, secure, timeout

    def post(self, data, seq, token):
        cls = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        conn = cls(self.host, timeout=self.timeout)
        try:
            conn.request('POST', UPLOAD_PATH, body=data, headers={
                'Content-Type': 'image/jpeg', 'Content-Length': str(len(data)),
                'Authorization': 'Bearer ' + token, 'X-Frame-Sequence': seq,
                'Accept': 'application/json'})
            r = conn.getresponse()
            headers = {k.lower(): v for k, v in r.getheaders()}
            try:
                body = r.read(MAX_RESPONSE + 1)
            except (OSError, http.client.HTTPException):
                # Retain known HTTP status even if its body is incomplete.
                return Response(r.status, headers, b'', incomplete=True)
            return Response(r.status, headers, body[:MAX_RESPONSE], len(body) > MAX_RESPONSE)
        except ssl.SSLCertVerificationError:
            raise TransportFailure('tls_verification') from None
        except (OSError, http.client.HTTPException):
            # Send completion cannot reliably be inferred from these exceptions.
            raise TransportFailure('transport_outcome_unknown') from None
        finally:
            conn.close()


def load_token():
    """Read only a private, read-only systemd system-service credential store.

    systemd v257 uses a named-user ACL: its ACL mask appears as group read
    (0440), even though the credential remains root-owned. Trust the protected
    runtime boundary, not a blanket ban on every group permission bit.
    """
    try:
        directory = Path(os.environ['CREDENTIALS_DIRECTORY'])
        root = _CREDENTIAL_ROOT
        if (not directory.is_absolute() or directory.parent != root
                or not directory.name.endswith('.service')
                or root.resolve(strict=True) != root):
            raise ValueError
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        rootfd = os.open(root, flags)
        try:
            r = os.fstat(rootfd)
            if r.st_uid != 0 or r.st_mode & 0o022:
                raise ValueError
            dirfd = os.open(directory.name, flags, dir_fd=rootfd)
            try:
                d = os.fstat(dirfd)
                if (d.st_uid not in (0, os.geteuid())
                        or d.st_gid not in (0, os.getegid())
                        or stat.S_IMODE(d.st_mode) not in (0o500, 0o550)
                        or not os.fstatvfs(dirfd).f_flag & os.ST_RDONLY):
                    raise ValueError
                fd = os.open('upload-token', os.O_RDONLY | os.O_NOFOLLOW |
                             os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
                with os.fdopen(fd, 'r', encoding='utf-8', newline=None) as f:
                    t = os.fstat(f.fileno())
                    if (not stat.S_ISREG(t.st_mode) or t.st_nlink != 1
                            or t.st_uid not in (0, os.geteuid())
                            or t.st_gid not in (0, os.getegid())
                            or stat.S_IMODE(t.st_mode) not in (0o400, 0o440)
                            or not 0 < t.st_size <= 256):
                        raise ValueError
                    text = f.read(257).removesuffix('\n')
            finally:
                os.close(dirfd)
        finally:
            os.close(rootfd)
        if not re.fullmatch(r'[A-Za-z0-9_\-+/=]{32,128}', text):
            raise ValueError
        return text
    except Exception:
        raise PublisherError('credential_unavailable_or_invalid') from None


class Publisher:
    def __init__(self, store, source, transport, credential=load_token,
                 clock=time.time, hook=lambda point: None):
        self.store, self.source, self.transport = store, source, transport
        self.credential, self.clock, self.hook = credential, clock, hook

    def block(self, s, reason):
        s['blocked_reason'] = reason
        self.store.save(s)
        return 'blocked:' + reason

    def run(self):
        with self.store.lock():
            s = self.store.load()
            if s['blocked_reason']:
                return 'blocked'  # never log untrusted persistent text
            self.store.cleanup(s)
            now = self.clock()
            if now < s['next_eligible_utc']:
                return 'deferred'
            pending = s['pending']
            # Verify existing bytes even if superseding, so corruption never hides.
            try:
                data = self.store.pending_bytes(pending) if pending else None
            except PublisherError:
                return self.block(s, 'pending_snapshot_integrity')
            if pending is None or now - pending['created_utc'] >= 1800:
                try:
                    src, fresh = self.source.newest(now)
                except SourceUnavailable:
                    return 'no_fresh_source'
                if pending and src['camera_id'] != pending['source']['camera_id']:
                    return self.block(s, 'source_camera_changed')
                if pending and source_order(src) <= source_order(pending['source']):
                    return 'no_fresh_replacement'  # do not POST expired data
                if not pending and s['last_ack'] and src == s['last_ack']['source']:
                    return 'unchanged'
                if int(s['next_sequence']) > MAX_SEQUENCE:
                    return self.block(s, 'sequence_exhausted')
                validate_jpeg(fresh)
                name = self.store.snapshot(fresh)
                self.hook('snapshot_durable')
                pending = {'sequence': s['next_sequence'], 'source': src,
                           'file': name, 'sha256': digest(fresh), 'created_utc': now}
                s['next_sequence'] = str(int(s['next_sequence']) + 1)
                s['pending'] = pending
                self.store.save(s)
                self.hook('allocation_durable')
                self.store.cleanup(s)
                data = fresh
            try:
                token = self.credential()
            except PublisherError:
                return self.block(s, 'credential_unavailable_or_invalid')
            # Provisional backoff committed before I/O covers process death/restart.
            s['failure_count'] += 1
            delay = min(1800, 360 * 2 ** min(s['failure_count'] - 1, 3))
            s['next_eligible_utc'] = now + delay
            self.store.save(s)
            self.hook('attempt_durable')
            started = time.monotonic()
            try:
                r = self.transport.post(data, pending['sequence'], token)
            except TransportFailure as exc:
                if exc.category == 'tls_verification':
                    return self.block(s, 'tls_verification')
                return 'uncertain_transport'  # pending and provisional delay survive
            finally:
                LOG.info('upload sequence=%s image=%d bytes=%d sha256=%s duration_seconds=%.3f',
                         pending['sequence'], pending['source']['image_id'], len(data),
                         pending['sha256'], time.monotonic() - started)
            self.hook('response_received')
            body = None
            if not r.oversized and not r.incomplete:
                try:
                    body = json.loads(r.body)
                except (ValueError, UnicodeError):
                    pass
            if r.status == 200:
                if (r.oversized or r.incomplete or r.headers.get('content-type', '').split(';')[0].strip().lower()
                        != 'application/json' or not isinstance(body, dict)
                        or body.get('committed') is not True
                        or body.get('sequence') != pending['sequence']):
                    return self.block(s, 'invalid_commit_acknowledgement')
                self.hook('before_ack')
                s['last_ack'] = {k: pending[k] for k in ('sequence', 'source', 'sha256')}
                s['pending'] = None
                s['failure_count'] = 0
                s['next_eligible_utc'] = self.clock() + 330
                self.store.save(s)
                self.hook('ack_durable')
                self.store.cleanup(s)
                return 'acknowledged'
            if r.status == 429:
                value = r.headers.get('retry-after', '')
                if re.fullmatch('[0-9]{1,10}', value) and int(value) > 0:
                    s['next_eligible_utc'] = max(s['next_eligible_utc'],
                                               self.clock() + int(value) + 5)
                    self.store.save(s)
                return 'rate_limited'
            if r.status == 503:
                return 'uncertain_commit'
            if r.status == 409:
                reason = body.get('error') if isinstance(body, dict) else None
                current = body.get('current_sequence') if isinstance(body, dict) else None
                if reason in ('obsolete_sequence', 'sequence_content_mismatch') and sequence(current):
                    LOG.warning('server_conflict reason=%s current_sequence=%s', reason, current)
                    return self.block(s, reason)  # never learn/reseed counter from server
                return self.block(s, 'invalid_conflict_response')
            reasons = {400: 'invalid_sequence', 401: 'authentication', 413: 'encoded_size',
                       415: 'content_type', 422: 'jpeg_validation'}
            return self.block(s, reasons.get(r.status, 'unexpected_http_status'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('run')
    init = sub.add_parser('initialise', help='offline, operator-confirmed sequence namespace only')
    init.add_argument('--next-sequence', required=True)
    init.add_argument('--confirm-server-namespace', action='store_true', required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        c = Config.load(args.config)
        store = StateStore(c.state_dir)
        if args.action == 'initialise':
            with store.lock():
                store.initialise(args.next_sequence)
            result = 'initialised_offline'
        else:
            result = Publisher(store, ImageSource(c), HTTPTransport()).run()
        LOG.info('%s', result)
        return 1 if result.startswith('blocked') else 0
    except Busy:
        LOG.info('already_running')
        return 0
    except Exception:
        # Never interpolate arbitrary exceptions: they may contain credentials.
        LOG.error('publisher_failed_requires_review')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
