"""Local fixtures and loopback HTTP only. No production DB/credentials/endpoints."""
import contextlib
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
import publisher as P

TOKEN = 'a' * 64  # synthetic test credential, never a production secret


def jpeg(size=(32, 24), color=20):
    b = io.BytesIO()
    Image.new('RGB', size, (color, 40, 60)).save(b, format='JPEG')
    return b.getvalue()


class Crash(BaseException):
    """Abrupt death analogue: bypass ordinary error handlers/cleanup policy."""


class Source:
    def __init__(self):
        self.ident, self.data, self.calls = 1, jpeg(), 0

    def newest(self, now):
        self.calls += 1
        return {'camera_id': 1, 'image_id': self.ident, 'created': '2026-11-16 10:00:00'}, self.data


class Wire:
    def __init__(self):
        self.calls = []
        self.reply = None

    def post(self, data, seq, token):
        self.calls.append((data, seq, token))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply or response(200, {'committed': True, 'sequence': seq})


def response(status, body=None, headers=None, oversized=False):
    return P.Response(status, headers or {'content-type': 'application/json'},
                      json.dumps(body).encode() if not isinstance(body, bytes) else body,
                      oversized)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = P.StateStore(self.directory)
        self.store.initialise('1')  # mock namespace only
        self.clock = [1800000000.]
        self.source, self.wire = Source(), Wire()

    def runner(self, **kwargs):
        return P.Publisher(self.store, self.source, self.wire,
                           credential=lambda: TOKEN, clock=lambda: self.clock[0], **kwargs)

    def tick(self):
        self.clock[0] = max(self.clock[0] + 360, self.store.load()['next_eligible_utc'])

    def pending(self):
        return self.store.load()['pending']

    def ambiguous(self):
        self.wire.reply = P.TransportFailure('transport_outcome_unknown')
        self.assertEqual(self.runner().run(), 'uncertain_transport')


class StateAndPolicy(Fixture):
    def test_allocate_ack_and_skip_unchanged(self):
        self.assertEqual(self.runner().run(), 'acknowledged')
        s = self.store.load()
        self.assertEqual(s['next_sequence'], '2')
        self.assertIsNone(s['pending'])
        self.assertEqual(s['last_ack']['sequence'], '1')
        self.assertEqual(self.runner().run(), 'deferred')
        self.tick()
        self.assertEqual(self.runner().run(), 'unchanged')
        self.assertEqual(len(self.wire.calls), 1)

    def test_retry_same_sequence_exact_bytes_despite_new_source(self):
        self.ambiguous()
        original = self.pending()
        self.source.ident, self.source.data = 8, jpeg(color=80)
        self.tick()
        self.wire.reply = None
        self.assertEqual(self.runner().run(), 'acknowledged')
        self.assertEqual(self.wire.calls[0], self.wire.calls[1])
        self.assertEqual(self.store.load()['last_ack']['source']['image_id'], 1)
        self.assertEqual(self.source.calls, 1)
        self.assertEqual(original['sha256'], P.digest(self.wire.calls[1][0]))
        self.tick()
        self.runner().run()
        self.assertEqual(self.wire.calls[-1][1], '2')
        self.assertEqual(self.store.load()['last_ack']['source']['image_id'], 8)

    def test_stale_pending_superseded_newest_only(self):
        self.ambiguous()
        old = self.pending()
        self.source.ident, self.source.data = 100, jpeg(color=100)
        self.clock[0] += 1801
        self.wire.reply = None
        self.assertEqual(self.runner().run(), 'acknowledged')
        self.assertEqual([x[1] for x in self.wire.calls], ['1', '2'])
        self.assertEqual(self.store.load()['last_ack']['source']['image_id'], 100)
        self.assertFalse((self.directory / old['file']).exists())
        self.assertEqual(list(self.directory.glob('snapshot-*')), [])

    def test_stale_same_source_never_reposted(self):
        self.ambiguous()
        self.clock[0] += 1801
        self.assertEqual(self.runner().run(), 'no_fresh_replacement')
        self.assertEqual(len(self.wire.calls), 1)
        self.assertEqual(self.pending()['sequence'], '1')

    def test_retry_after_prevents_stale_supersession(self):
        self.wire.reply = response(429, {}, {'retry-after': '3600'})
        self.runner().run()
        self.source.ident = 2
        self.clock[0] += 1900
        self.assertEqual(self.runner().run(), 'deferred')
        self.assertEqual(self.source.calls, 1)

    def test_restart_preserves_backoff(self):
        self.ambiguous()
        self.assertEqual(self.runner().run(), 'deferred')
        delays = []
        for _ in range(4):
            self.source.ident += 1  # capture continues during outage
            self.tick()
            start = self.clock[0]
            self.runner().run()
            delays.append(self.store.load()['next_eligible_utc'] - start)
        self.assertEqual(delays, [720, 1440, 1800, 1800])
        self.assertEqual([call[1] for call in self.wire.calls], ['1', '1', '1', '2', '3'])

    def test_crash_before_network_leaves_provisional_delay(self):
        def hook(point):
            if point == 'attempt_durable':
                raise Crash
        with self.assertRaises(Crash):
            self.runner(hook=hook).run()
        self.assertEqual(self.wire.calls, [])
        self.assertEqual(self.runner().run(), 'deferred')
        self.tick()
        self.runner().run()
        self.assertEqual(self.wire.calls[0][1], '1')

    def test_crash_after_snapshot_before_allocation(self):
        def hook(point):
            if point == 'snapshot_durable':
                raise Crash
        with self.assertRaises(Crash):
            self.runner(hook=hook).run()
        self.assertEqual(self.store.load()['next_sequence'], '1')
        self.assertEqual(len(list(self.directory.glob('snapshot-*'))), 1)
        self.runner().run()
        self.assertEqual(len(self.wire.calls), 1)
        self.assertEqual(self.wire.calls[0][1], '1')
        self.assertEqual(list(self.directory.glob('snapshot-*')), [])

    def test_crash_after_allocation_never_reassigns_bytes(self):
        def hook(point):
            if point == 'allocation_durable':
                raise Crash
        with self.assertRaises(Crash):
            self.runner(hook=hook).run()
        original = self.pending()
        self.source.ident, self.source.data = 2, jpeg(color=100)
        self.runner().run()
        self.assertEqual(P.digest(self.wire.calls[0][0]), original['sha256'])
        self.assertEqual(self.wire.calls[0][1], '1')

    def test_crash_at_ack_boundaries(self):
        for boundary in ('response_received', 'before_ack', 'ack_durable'):
            with self.subTest(boundary=boundary):
                # Each subcase has its own mock sequence namespace.
                d = self.directory / boundary
                d.mkdir(mode=0o700)
                store = P.StateStore(d)
                store.initialise('1')
                wire = Wire()
                def hook(point):
                    if point == boundary:
                        raise Crash
                def run(h=lambda point: None):
                    return P.Publisher(store, self.source, wire, lambda: TOKEN,
                                       lambda: self.clock[0], h).run()
                with self.assertRaises(Crash):
                    run(hook)
                self.clock[0] += 360
                run()
                self.assertEqual(len(wire.calls), 1 if boundary == 'ack_durable' else 2)
                self.assertEqual({x[1] for x in wire.calls}, {'1'})

    def test_atomic_state_before_and_after_replace(self):
        original = self.store.load()
        for boundary in ('state_before_replace', 'state_after_replace'):
            with self.subTest(boundary=boundary):
                self.store.save(original)
                def hook(point):
                    if point == boundary:
                        raise Crash
                broken = P.StateStore(self.directory, hook)
                changed = dict(original, next_sequence='2')
                with self.assertRaises(Crash):
                    broken.save(changed)
                expected = '1' if boundary.endswith('before_replace') else '2'
                self.assertEqual(self.store.load()['next_sequence'], expected)

    def test_fsync_failure_prevents_network(self):
        with patch.object(P.os, 'fsync', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                self.runner().run()
        self.assertEqual(self.wire.calls, [])

    def test_corrupt_or_missing_state_no_reset(self):
        for data in (b'broken', b'{}'):
            self.store.path.write_bytes(data)
            with self.assertRaises(P.PublisherError):
                self.runner().run()
        self.store.path.unlink()
        with self.assertRaises(P.PublisherError):
            self.runner().run()
        self.assertEqual(self.wire.calls, [])

    def test_pending_corruption_blocks_before_supersession(self):
        self.ambiguous()
        p = self.pending()
        (self.directory / p['file']).write_bytes(jpeg(color=99))
        self.source.ident = 100
        self.clock[0] += 1900
        self.assertIn('pending_snapshot_integrity', self.runner().run())
        self.assertEqual(len(self.wire.calls), 1)
        self.assertIsNotNone(self.store.load()['blocked_reason'])

    def test_exhaustion_no_wrap_and_maximum_can_retry(self):
        self.store.path.unlink()
        self.store.initialise(str(P.MAX_SEQUENCE))
        self.ambiguous()
        self.tick()
        self.wire.reply = None
        self.assertEqual(self.runner().run(), 'acknowledged')
        self.tick()
        self.source.ident = 2
        self.assertIn('sequence_exhausted', self.runner().run())
        self.assertEqual({x[1] for x in self.wire.calls}, {str(P.MAX_SEQUENCE)})

    def test_sequence_syntax(self):
        for value in ('0', '01', '+1', '-1', ' 1', '1\n', '１', 1, True,
                      str(P.MAX_SEQUENCE + 1), '9' * 500):
            self.assertFalse(P.sequence(value), repr(value))
        self.assertTrue(P.sequence('1'))
        self.assertTrue(P.sequence(str(P.MAX_SEQUENCE)))

    def test_initialise_refuses_existing_state(self):
        with self.assertRaises(P.PublisherError):
            self.store.initialise('2')

    def test_concurrent_process_lock(self):
        with self.store.lock():
            receive, send = multiprocessing.Pipe(False)
            def child():
                try:
                    with P.StateStore(self.directory).lock():
                        send.send(False)
                except P.Busy:
                    send.send(True)
            process = multiprocessing.Process(target=child)
            process.start()
            self.assertTrue(receive.poll(3))
            self.assertTrue(receive.recv())
            process.join(3)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.runner().run(), 'acknowledged')

    def test_private_state_permissions(self):
        self.ambiguous()
        for path in (self.store.path, self.directory / self.pending()['file']):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.directory.chmod(0o755)
        with self.assertRaises(P.PublisherError):
            self.runner().run()
        self.directory.chmod(0o700)


class StatusHandling(Fixture):
    def test_permanent_statuses(self):
        for status, reason in ((400, 'invalid_sequence'), (401, 'authentication'),
                               (413, 'encoded_size'), (415, 'content_type'),
                               (422, 'jpeg_validation'), (302, 'unexpected_http_status'),
                               (500, 'unexpected_http_status'), (204, 'unexpected_http_status')):
            with self.subTest(status=status):
                self.source.ident += 1
                s = self.store.load()
                s.update(blocked_reason=None, next_eligible_utc=0)
                self.store.save(s)
                self.wire.reply = response(status, b'not-json')
                self.assertEqual(self.runner().run(), 'blocked:' + reason)
                count = len(self.wire.calls)
                self.clock[0] += 3600
                self.assertEqual(self.runner().run(), 'blocked')
                self.assertEqual(len(self.wire.calls), count)
                self.assertIsNotNone(self.pending())

    def test_conflict_variants_and_no_counter_learning(self):
        for reason in ('obsolete_sequence', 'sequence_content_mismatch'):
            self.wire.reply = response(409, {'error': reason, 'current_sequence': '1000'})
            self.assertEqual(self.runner().run(), 'blocked:' + reason)
            self.assertEqual(self.store.load()['next_sequence'], '2')
            s = self.store.load()
            s.update(blocked_reason=None, next_eligible_utc=0)
            self.store.save(s)
        self.wire.reply = response(409, {'error': 'obsolete_sequence', 'current_sequence': 1000})
        self.assertIn('invalid_conflict_response', self.runner().run())

    def test_malformed_acknowledgements(self):
        cases = [response(200, {}), response(200, b''), response(200, b'<html>ok</html>'),
                 response(200, {'committed': 1, 'sequence': '1'}),
                 response(200, {'committed': True, 'sequence': 1}),
                 response(200, {'committed': True, 'sequence': '2'}),
                 response(200, {'committed': True, 'sequence': '01'}),
                 response(200, {'committed': True, 'sequence': '1'}, {'content-type': 'text/html'}),
                 response(200, b'x' * P.MAX_RESPONSE, oversized=True)]
        for reply in cases:
            with self.subTest(reply=reply.status):
                s = self.store.load()
                s.update(blocked_reason=None, next_eligible_utc=0)
                self.store.save(s)
                self.wire.reply = reply
                self.assertIn('invalid_commit_acknowledgement', self.runner().run())
                self.assertIsNone(self.store.load()['last_ack'])

    def test_503_retains_pending_exact_retry(self):
        self.wire.reply = response(503, b'')
        self.assertEqual(self.runner().run(), 'uncertain_commit')
        self.tick()
        self.wire.reply = None
        self.runner().run()
        self.assertEqual(self.wire.calls[0], self.wire.calls[1])

    def test_retry_after_valid_invalid_and_oversized_error(self):
        self.wire.reply = response(429, b'x' * P.MAX_RESPONSE,
                                   {'retry-after': '1000'}, oversized=True)
        self.assertEqual(self.runner().run(), 'rate_limited')
        self.assertEqual(self.store.load()['next_eligible_utc'], self.clock[0] + 1005)
        for bad in ('-1', 'abc', '0', '999999999999999999999999999999999999'):
            self.source.ident += 1
            self.tick()
            self.wire.reply = response(429, {}, {'retry-after': bad})
            self.assertEqual(self.runner().run(), 'rate_limited')
            self.assertLessEqual(self.store.load()['next_eligible_utc'] - self.clock[0], 1800)

    def test_tls_verification_blocks(self):
        self.wire.reply = P.TransportFailure('tls_verification')
        self.assertEqual(self.runner().run(), 'blocked:tls_verification')

    def test_missing_credential_blocks_without_post(self):
        def absent():
            raise P.PublisherError('credential missing')
        runner = P.Publisher(self.store, self.source, self.wire, absent, lambda: self.clock[0])
        self.assertIn('credential', runner.run())
        self.assertEqual(self.wire.calls, [])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'fixtures.sqlite'
        self.img = self.root / 'images'
        self.img.mkdir()
        self.now = 1800000000.
        self.c = P.Config(self.db, self.img, 1, self.root)
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE image (id INTEGER,createDate TEXT,filename TEXT,fileSize INTEGER,camera_id INTEGER)')
        self.add(1, jpeg())

    def add(self, ident, data, age=0, filename=None, camera=1):
        name = filename or str(ident) + '.jpg'
        if not filename:
            (self.img / name).write_bytes(data)
        created = datetime.datetime.fromtimestamp(self.now - age, datetime.timezone.utc).replace(tzinfo=None).isoformat(' ')
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO image VALUES (?,?,?,?,?)', (ident, created, name, len(data), camera))

    def test_newest_correct_camera_and_database_readonly(self):
        self.add(2, jpeg(color=40))
        self.add(3, jpeg(color=50), camera=2)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        src, data = P.ImageSource(self.c).newest(self.now)
        self.assertEqual(src['image_id'], 2)
        self.assertEqual(data, jpeg(color=40))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_no_fallback_missing_newest(self):
        self.add(2, jpeg())
        (self.img / '2.jpg').unlink()
        with self.assertRaises(P.SourceUnavailable):
            P.ImageSource(self.c).newest(self.now)

    def test_stale_or_future_source(self):
        for age in (601, -61):
            with self.subTest(age=age):
                self.add(100 - age, jpeg(), age=age)
                with self.assertRaises(P.SourceUnavailable):
                    P.ImageSource(self.c).newest(self.now + (1000 if age > 0 else 0))

    def test_truncated_and_size_mismatch(self):
        self.add(2, jpeg()[:-10])
        with self.assertRaises(P.SourceUnavailable):
            P.ImageSource(self.c).newest(self.now)
        (self.img / '2.jpg').write_bytes(jpeg())
        with self.assertRaises(P.SourceUnavailable):
            P.ImageSource(self.c).newest(self.now)

    def test_path_escape(self):
        self.add(2, jpeg(), filename='../outside.jpg')
        with self.assertRaises(P.SourceUnavailable):
            P.ImageSource(self.c).newest(self.now)

    def test_file_changed_during_read(self):
        real = P.os.fstat
        counter = [0]
        def changed(fd):
            s = real(fd)
            counter[0] += 1
            if counter[0] % 2 == 0:
                # Keep a stat-like value but change identity/size signature.
                from types import SimpleNamespace
                return SimpleNamespace(st_dev=s.st_dev, st_ino=s.st_ino,
                    st_size=s.st_size+1, st_mtime_ns=s.st_mtime_ns, st_ctime_ns=s.st_ctime_ns)
            return s
        with patch.object(P.os, 'fstat', side_effect=changed):
            with self.assertRaises(P.SourceUnavailable):
                P.ImageSource(self.c).newest(self.now)

    def test_limits_inclusive_and_no_partial_markers_only(self):
        P.validate_jpeg(jpeg(size=(2000, 2000)))
        with self.assertRaises(P.SourceUnavailable):
            P.validate_jpeg(jpeg(size=(2001, 2000)))
        base = jpeg()
        # JPEG decoder permits bytes after EOI; preserve final EOI and inclusive length.
        exact = base + b'\0' * (P.MAX_BYTES - len(base) - 2) + b'\xff\xd9'
        self.assertEqual(len(exact), P.MAX_BYTES)
        P.validate_jpeg(exact)
        with self.assertRaises(P.SourceUnavailable):
            P.validate_jpeg(exact + b'\0')
        with self.assertRaises(P.SourceUnavailable):
            P.validate_jpeg(b'\xff\xd8not jpeg\xff\xd9')

    def test_database_missing_not_created(self):
        self.db.unlink()
        with self.assertRaises(P.SourceUnavailable):
            P.ImageSource(self.c).newest(self.now)
        self.assertFalse(self.db.exists())


class CredentialTests(unittest.TestCase):
    @contextlib.contextmanager
    def credential_store(self, file_mode=0o440, directory_mode=0o550,
                         read_only=True, owner=0):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'fixture.service'
            directory.mkdir()
            token = directory / 'upload-token'
            token.write_text(TOKEN + '\n')
            token.chmod(file_mode)
            directory.chmod(directory_mode)
            real_fstat = os.fstat
            def metadata(fd):
                t = real_fstat(fd)
                return SimpleNamespace(st_uid=owner, st_gid=0, st_mode=t.st_mode,
                                       st_nlink=t.st_nlink, st_size=t.st_size)
            with patch.object(P, '_CREDENTIAL_ROOT', root), patch.object(P.os, 'fstat', side_effect=metadata), patch.object(P.os, 'fstatvfs', return_value=SimpleNamespace(f_flag=os.ST_RDONLY if read_only else 0)), patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(directory)}):
                try:
                    yield root, directory, token
                finally:
                    directory.chmod(0o700)

    def test_loadcredential_0400_and_acl_0440(self):
        for file_mode in (0o400, 0o440):
            for directory_mode in (0o500, 0o550):
                with self.subTest(file_mode=file_mode, directory_mode=directory_mode):
                    with self.credential_store(file_mode, directory_mode):
                        self.assertEqual(P.load_token(), TOKEN)

    def test_unsafe_delivered_file_modes_rejected(self):
        for mode in (0o444, 0o404, 0o640, 0o600, 0o460, 0o441):
            with self.subTest(mode=mode), self.credential_store(mode):
                with self.assertRaises(P.PublisherError) as caught:
                    P.load_token()
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_unsafe_credential_directories_rejected(self):
        for mode in (0o555, 0o505, 0o570, 0o700, 0o777):
            with self.subTest(mode=mode), self.credential_store(directory_mode=mode):
                with self.assertRaises(P.PublisherError):
                    P.load_token()

    def test_writable_mount_or_untrusted_owner_rejected(self):
        with self.credential_store(read_only=False):
            with self.assertRaises(P.PublisherError):
                P.load_token()
        with self.credential_store(owner=987654):
            with self.assertRaises(P.PublisherError):
                P.load_token()

    def test_unsafe_root_directory_rejected(self):
        with self.credential_store() as (root, directory, token):
            root.chmod(0o770)
            with self.assertRaises(P.PublisherError):
                P.load_token()
            root.chmod(0o700)

    def test_arbitrary_directory_and_symlinks_rejected(self):
        with self.credential_store() as (root, directory, token):
            for path in (root, root / '..' / directory.name):
                with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(path)}):
                    with self.assertRaises(P.PublisherError):
                        P.load_token()
            link = root / 'linked.service'
            link.symlink_to(directory, target_is_directory=True)
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(link)}):
                with self.assertRaises(P.PublisherError):
                    P.load_token()
            directory.chmod(0o700)
            token.rename(directory / 'real-token')
            token.symlink_to('real-token')
            directory.chmod(0o550)
            with self.assertRaises(P.PublisherError):
                P.load_token()

    def check_credential_text(self, text, accepted):
        with self.credential_store() as (root, directory, token):
            token.chmod(0o600)
            token.write_bytes(text.encode())
            token.chmod(0o440)
            before = token.read_bytes()
            if accepted:
                self.assertEqual(P.load_token(), TOKEN)
            else:
                with self.assertRaises(P.PublisherError) as caught:
                    P.load_token()
                self.assertNotIn(TOKEN, str(caught.exception))
            self.assertEqual(token.read_bytes(), before)  # input is never rewritten

    def test_terminal_cr_lf_normalisation(self):
        for ending in ('', '\n', '\r', '\r\n', '\n\n', '\r\n' * 3, '\r\r\n\n'):
            with self.subTest(ending_codepoints=[ord(c) for c in ending]):
                self.check_credential_text(TOKEN + ending, True)

    def test_invalid_token_length_characters_and_whitespace(self):
        for text in (TOKEN+' ', TOKEN+'\t', ' '+TOKEN, '\t'+TOKEN, '\n'+TOKEN,
                     TOKEN[:32]+'\n'+TOKEN[32:], TOKEN[:32]+'\r'+TOKEN[32:],
                     TOKEN+' \r\n', TOKEN+'\t\n', TOKEN+'\n ', TOKEN[:-1], TOKEN+'a',
                     'g'*64, 'A'*64, TOKEN[:-1]+'%', TOKEN[:-1]+'\x00', TOKEN[:-1]+'é',
                     '', '\r\n'):
            with self.subTest(case_length=len(text)):
                self.check_credential_text(text, False)

    def test_lineending_normalisation_keeps_size_bound(self):
        self.check_credential_text(TOKEN + '\n' * (256-len(TOKEN)), True)
        self.check_credential_text(TOKEN + '\n' * (257-len(TOKEN)), False)

    def test_cli_failure_does_not_print_secret(self):
        with patch.object(P.Config, 'load', side_effect=Exception(TOKEN)):
            with self.assertLogs(P.LOG, level='ERROR') as captured:
                self.assertEqual(P.main(['--config', '/nonexistent', 'run']), 1)
        self.assertNotIn(TOKEN, str(captured.output))


class LoopbackTests(Fixture):
    def setUp(self):
        super().setUp()
        outer = self
        self.received = []
        self.responses = []
        self.server_current = None
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                outer.received.append((self.path, dict(self.headers), body))
                if outer.responses:
                    status, payload, content_type, delay = outer.responses.pop(0)
                else:
                    seq = self.headers['X-Frame-Sequence']
                    old = outer.server_current
                    if old and old[0] == seq and old[1] != body:
                        status, payload = 409, {'error': 'sequence_content_mismatch', 'current_sequence': seq}
                    else:
                        outer.server_current = seq, body
                        status, payload = 200, {'committed': True, 'sequence': seq}
                    content_type, delay = 'application/json', 0
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                if status == 302:
                    self.send_header('Location', 'http://127.0.0.1:1/never-follow')
                data = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.wire = P.HTTPTransport('127.0.0.1:' + str(self.server.server_port), secure=False, timeout=.1)

    def test_real_http_headers_body_ack_and_same_retry(self):
        def crash(point):
            if point == 'before_ack':
                raise Crash
        with self.assertRaises(Crash):
            self.runner(hook=crash).run()
        self.tick()
        self.assertEqual(self.runner().run(), 'acknowledged')
        self.assertEqual(len(self.received), 2)
        path, headers, data = self.received[0]
        self.assertEqual(path, '/api/camera/latest')
        self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertEqual(headers['Content-Length'], str(len(data)))
        self.assertEqual(headers['X-Frame-Sequence'], '1')
        self.assertEqual(self.received[0], self.received[1])
        self.assertNotIn(TOKEN, self.store.path.read_text())
        for f in self.directory.glob('snapshot-*'):
            self.assertNotIn(TOKEN.encode(), f.read_bytes())

    def test_redirect_not_followed(self):
        self.responses.append((302, {}, 'application/json', 0))
        self.assertEqual(self.runner().run(), 'blocked:unexpected_http_status')
        self.assertEqual(len(self.received), 1)

    def test_response_bound(self):
        self.responses.append((200, b'x' * (P.MAX_RESPONSE + 1000), 'application/json', 0))
        self.assertEqual(self.runner().run(), 'blocked:invalid_commit_acknowledgement')

    def test_timeout_after_full_send_preserves_pending(self):
        self.responses.append((200, {'committed': True, 'sequence': '1'}, 'application/json', .25))
        self.assertEqual(self.runner().run(), 'uncertain_transport')
        self.assertEqual(self.pending()['sequence'], '1')
        self.assertEqual(len(self.received), 1)
        self.tick()
        self.assertEqual(self.runner().run(), 'acknowledged')
        self.assertEqual(self.received[0][2], self.received[1][2])

    def test_connection_refused(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.wire = P.HTTPTransport(f'127.0.0.1:{port}', secure=False, timeout=.1)
        self.assertEqual(self.runner().run(), 'uncertain_transport')
        self.assertIsNotNone(self.pending())

    def test_status_matrix_over_actual_loopback_http(self):
        cases = [(400, {}, 'blocked:invalid_sequence'),
                 (401, {}, 'blocked:authentication'),
                 (409, {'error': 'obsolete_sequence', 'current_sequence': '9'}, 'blocked:obsolete_sequence'),
                 (409, {'error': 'sequence_content_mismatch', 'current_sequence': '1'}, 'blocked:sequence_content_mismatch'),
                 (413, {}, 'blocked:encoded_size'), (415, {}, 'blocked:content_type'),
                 (422, {}, 'blocked:jpeg_validation'), (429, {}, 'rate_limited'),
                 (503, {}, 'uncertain_commit')]
        for status, body, expected in cases:
            with self.subTest(status=status):
                # Independent response fixtures, with operator blocks reset only in mock state.
                state = self.store.load()
                state.update(blocked_reason=None, next_eligible_utc=0)
                self.store.save(state)
                self.responses.append((status, body, 'application/json', 0))
                self.assertEqual(self.runner().run(), expected)
        self.assertEqual(len(self.received), len(cases))

    def test_malformed_json_over_loopback(self):
        self.responses.append((200, b'{broken json', 'application/json', 0))
        self.assertEqual(self.runner().run(), 'blocked:invalid_commit_acknowledgement')


class AdditionalBoundaries(Fixture):
    def test_invalid_local_frame_allocates_no_sequence(self):
        self.source.data = b'not a JPEG'
        with self.assertRaises(P.SourceUnavailable):
            self.runner().run()
        self.assertEqual(self.store.load()['next_sequence'], '1')
        self.assertEqual(self.wire.calls, [])

    def test_supersession_rejects_older_source(self):
        self.ambiguous()
        self.clock[0] += 1801
        self.source.ident = 0
        self.assertEqual(self.runner().run(), 'no_fresh_replacement')
        self.assertEqual(self.pending()['sequence'], '1')
        self.assertEqual(len(self.wire.calls), 1)

    def test_source_unavailable_retains_pending_without_allocating(self):
        self.ambiguous()
        self.clock[0] += 1801
        with patch.object(self.source, 'newest', side_effect=P.SourceUnavailable('missing')):
            self.assertEqual(self.runner().run(), 'no_fresh_source')
        self.assertEqual(self.pending()['sequence'], '1')
        self.assertEqual(self.store.load()['next_sequence'], '2')

    def test_ack_persistence_failure_retries_identical(self):
        real_save = self.store.save
        def fail_ack(state):
            if state['last_ack']:
                raise OSError('simulated filesystem failure')
            real_save(state)
        with patch.object(self.store, 'save', side_effect=fail_ack):
            with self.assertRaises(OSError):
                self.runner().run()
        self.assertEqual(self.pending()['sequence'], '1')
        self.tick()
        self.assertEqual(self.runner().run(), 'acknowledged')
        self.assertEqual(self.wire.calls[0], self.wire.calls[1])

    def test_partial_error_body_retains_known_status(self):
        self.wire.reply = P.Response(401, {}, b'', incomplete=True)
        self.assertEqual(self.runner().run(), 'blocked:authentication')

    def test_safe_failure_logs_and_no_secret_in_state(self):
        self.ambiguous()
        self.assertNotIn(TOKEN, self.store.path.read_text())
        self.tick()
        self.wire.reply = response(400, ('echoed-secret:' + TOKEN).encode())
        with patch.object(P.Config, 'load', return_value=P.Config(
                Path('/mock'), Path('/mock'), 1, self.directory)):
            with patch.object(P, 'ImageSource', return_value=self.source), patch.object(P, 'HTTPTransport', return_value=self.wire), patch.object(P, 'Publisher', return_value=self.runner()):
                # Exercise CLI's sanitised error boundary without real source/transport.
                with self.assertLogs(P.LOG, level='INFO') as captured:
                    P.main(['--config', '/mock', 'run'])
        self.assertNotIn(TOKEN, str(captured.output))
        self.assertNotIn(TOKEN, self.store.path.read_text())

    def test_token_rotation_does_not_change_pending_namespace(self):
        self.ambiguous()
        self.tick()
        self.wire.reply = None
        other = 'b' * 64
        runner = P.Publisher(self.store, self.source, self.wire,
                             lambda: other, lambda: self.clock[0])
        self.assertEqual(runner.run(), 'acknowledged')
        self.assertEqual(self.wire.calls[0][:2], self.wire.calls[1][:2])
        self.assertEqual(self.wire.calls[1][2], other)
        self.assertNotIn(other, self.store.path.read_text())

    def test_snapshot_before_replace_crash_leaves_no_allocation(self):
        def hook(point):
            if point == 'snapshot_before_replace':
                raise Crash
        store = P.StateStore(self.directory, hook)
        runner = P.Publisher(store, self.source, self.wire,
                             lambda: TOKEN, lambda: self.clock[0])
        with self.assertRaises(Crash):
            runner.run()
        self.assertEqual(self.store.load()['next_sequence'], '1')
        self.assertEqual(self.wire.calls, [])
        self.assertEqual(list(self.directory.glob('.write-*')), [])


class TransportUnitTests(unittest.TestCase):
    def test_dns_timeout_partial_send_and_tls_categories(self):
        from unittest.mock import Mock
        import ssl
        for error, expected in ((socket.gaierror('fake dns'), 'transport_outcome_unknown'),
                                (TimeoutError('before send'), 'transport_outcome_unknown'),
                                (ConnectionResetError('partial send'), 'transport_outcome_unknown'),
                                (ssl.SSLCertVerificationError('bad certificate'), 'tls_verification')):
            with self.subTest(kind=type(error).__name__):
                conn = Mock()
                conn.request.side_effect = error
                with patch.object(P.http.client, 'HTTPSConnection', return_value=conn):
                    with self.assertRaises(P.TransportFailure) as caught:
                        P.HTTPTransport().post(jpeg(), '1', TOKEN)
                self.assertEqual(caught.exception.category, expected)
                conn.close.assert_called_once()

    def test_incomplete_body_keeps_http_status(self):
        from unittest.mock import Mock
        conn, reply = Mock(), Mock()
        reply.status = 429
        reply.getheaders.return_value = [('Retry-After', '900')]
        reply.read.side_effect = P.http.client.IncompleteRead(b'part')
        conn.getresponse.return_value = reply
        with patch.object(P.http.client, 'HTTPSConnection', return_value=conn):
            result = P.HTTPTransport().post(jpeg(), '1', TOKEN)
        self.assertEqual(result.status, 429)
        self.assertTrue(result.incomplete)
        self.assertEqual(result.headers['retry-after'], '900')

    def test_production_destination_and_tls_default(self):
        from unittest.mock import Mock
        conn, reply = Mock(), Mock()
        reply.status = 200
        reply.getheaders.return_value = [('Content-Type', 'application/json')]
        reply.read.return_value = b'{"committed":true,"sequence":"1"}'
        conn.getresponse.return_value = reply
        with patch.object(P.http.client, 'HTTPSConnection', return_value=conn) as factory:
            P.HTTPTransport().post(jpeg(), '1', TOKEN)
        factory.assert_called_once_with('chelmsfordskies.co.uk', timeout=20)
        self.assertEqual(conn.request.call_args.args[0:2], ('POST', '/api/camera/latest'))


if __name__ == '__main__':
    unittest.main()
