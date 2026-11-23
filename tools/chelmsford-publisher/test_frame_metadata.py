"""Synthetic SQLite/JPEG fixtures and loopback networking only."""
import datetime as dt
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import publisher as P
import frame_metadata as M
from test_publisher import Fixture, Crash, TOKEN, response, jpeg


class MetadataFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.source.metadata = {'source': self.source.newest(0)[0], 'fields': fields()}
        self.meta = M.MetadataStore(self.directory)
        self.meta.directory.mkdir(mode=0o700)
        self.calls = []
        self.reply = None

    def companion(self, **kwargs):
        outer = self
        class Wire:
            def put(self, data, token):
                outer.calls.append(data)
                if isinstance(outer.reply, Exception):
                    raise outer.reply
                p = json.loads(data)
                return outer.reply or response(200, dict(committed=True,
                    sequence=p['sequence'], jpeg_sha256=p['jpeg_sha256']))
        return M.Companion(self.store, self.meta, Wire(), lambda: TOKEN,
                           lambda: self.clock[0], **kwargs)

    def test_only_after_durable_ack_no_second_selection(self):
        self.ambiguous()
        self.assertEqual(self.companion().run(), 'waiting_for_jpeg_ack')
        self.assertEqual(self.calls, [])
        frozen = self.meta.candidate()
        self.source.ident = 99
        self.source.metadata['fields']['stars'] = 999
        self.tick(); self.wire.reply = None
        self.runner().run()
        self.assertEqual(self.companion().run(), 'acknowledged')
        self.assertEqual(json.loads(self.calls[0])['stars'], 0)
        self.assertEqual(self.meta.candidate(), frozen)
        self.assertEqual(self.source.calls, 2)  # one setUp call, one JPEG selection
        self.assertEqual(set(self.store.load()['last_ack']['source']), {'camera_id','image_id','created'})

    def test_crash_boundaries_and_identical_delivery_retry(self):
        for point in ('allocation_durable', 'before_ack', 'ack_durable'):
            with self.subTest(point=point):
                def crash(p):
                    if p == point: raise Crash
                # Independent mock namespaces per boundary.
                with tempfile.TemporaryDirectory() as tmp:
                    store = P.StateStore(Path(tmp)); store.initialise('1')
                    (Path(tmp)/'metadata').mkdir(mode=0o700)
                    runner = P.Publisher(store,self.source,self.wire,lambda:TOKEN,
                                         lambda:self.clock[0],crash)
                    with self.assertRaises(Crash): runner.run()
                    meta = M.MetadataStore(Path(tmp))
                    self.assertIsNotNone(meta.candidate())
                    self.clock[0] += 360
                    P.Publisher(store,self.source,self.wire,lambda:TOKEN,lambda:self.clock[0]).run()
                    self.assertEqual(meta.candidate()['payload']['sequence'], store.load()['last_ack']['sequence'])
        self.runner().run()
        def crash(p):
            if p == 'metadata_response_received': raise Crash
        with self.assertRaises(Crash): self.companion(hook=crash).run()
        self.clock[0] += 400
        self.assertEqual(self.companion().run(), 'acknowledged')
        self.assertEqual(self.calls[0], self.calls[1])
        self.assertEqual(self.companion().run(), 'already_resolved')

    def test_every_metadata_failure_leaves_jpeg_state_byte_identical(self):
        self.runner().run()
        before = self.store.path.read_bytes()
        cases = [response(n, {'error': e}) for n,e in (
            (400,'invalid_json'),(401,'unauthorized'),(413,'metadata_too_large'),
            (415,'unsupported_media_type'),(422,'invalid_metadata'),
            (409,'jpeg_not_committed'),(409,'stale_sequence'),
            (409,'jpeg_digest_mismatch'),(409,'metadata_conflict'),(503,'commit_unconfirmed'))]
        cases += [P.TransportFailure('transport_outcome_unknown'),P.TransportFailure('tls_verification'),
                  response(302,{}),response(200,b'{broken'),response(200,{},oversized=True),
                  response(200,dict(committed=True,sequence='2',jpeg_sha256='0'*64)),
                  response(429,{}, {'retry-after':'1200'})]
        for reply in cases:
            with self.subTest(reply=type(reply).__name__):
                self.meta.ledger_path.unlink(missing_ok=True)
                self.reply = reply
                self.companion().run()
                self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn(TOKEN, self.meta.ledger_path.read_text())

    def test_stale_drop_conflict_review_backoff_and_new_candidate(self):
        self.runner().run()
        self.reply = response(409, {'error':'stale_sequence'})
        self.assertEqual(self.companion().run(), 'superseded')
        self.assertEqual(self.companion().run(), 'already_resolved')
        self.tick(); self.source.ident=2
        self.source.metadata['source']=self.source.newest(0)[0]
        self.runner().run()
        self.reply = response(409, {'error':'metadata_conflict'})
        self.assertEqual(self.companion().run(), 'review:metadata_conflict')
        self.assertEqual(self.companion().run(), 'already_resolved')
        self.tick(); self.source.ident=3
        self.source.metadata['source']=self.source.newest(0)[0]
        self.runner().run()
        self.reply = response(503, {'error':'commit_unconfirmed'})
        self.assertEqual(self.companion().run(),'retry_later')
        self.assertEqual(self.companion().run(),'deferred')
        original=self.calls[-1]; self.clock[0]+=400
        self.reply=None; self.companion().run()
        self.assertEqual(original,self.calls[-1])

    def test_legacy_missing_corrupt_and_capture_failure_do_not_reconstruct(self):
        self.source.metadata=None
        self.runner().run()
        self.assertEqual(self.companion().run(),'no_candidate')
        self.assertEqual(self.calls,[])
        self.tick(); self.source.ident=2
        self.source.metadata={'source':self.source.newest(0)[0], 'fields':fields()}
        with patch.object(M.MetadataStore,'freeze',side_effect=OSError('secret failure')):
            self.assertEqual(self.runner().run(),'acknowledged')
        self.assertEqual(self.companion().run(),'no_candidate')
        self.meta.candidate_path.write_text('corrupt')
        with self.assertRaises(P.PublisherError): self.companion().run()

    def test_independent_lock_and_root_cleanup(self):
        self.runner().run()
        with self.meta.lock():
            with self.assertRaises(P.Busy): self.companion().run()
        with self.store.lock():
            self.assertEqual(self.companion().run(),'acknowledged')
        marker=self.meta.directory/'.write-marker'; marker.touch()
        self.store.cleanup(self.store.load())
        self.assertTrue(marker.exists())

    def test_candidate_replaced_during_put_never_overwritten_by_consumer(self):
        self.runner().run()
        old=self.meta.candidate()
        def hook(p):
            if p=='metadata_response_received':
                newer=json.loads(json.dumps(old)); newer['payload']['sequence']='2'
                self.meta.write(self.meta.candidate_path,newer)
        self.companion(hook=hook).run()
        self.assertEqual(self.meta.candidate()['payload']['sequence'],'2')
        self.assertEqual(json.loads(self.meta.ledger_path.read_text())['identity']['sequence'],'1')

    def test_older_candidate_skipped_when_new_ack_exists(self):
        self.runner().run()
        state=self.store.load(); state['next_sequence']='3';state['last_ack']['sequence']='2';self.store.save(state)
        self.assertEqual(self.companion().run(),'superseded')
        self.assertEqual(self.calls,[])

    def test_retry_after_persisted_and_ack_validation(self):
        self.runner().run()
        self.reply=response(429,{}, {'retry-after':'1000'})
        self.companion().run()
        self.assertEqual(json.loads(self.meta.ledger_path.read_text())['next_eligible_utc'], self.clock[0]+1005)
        self.assertEqual(self.companion().run(),'deferred')


def fields():
    return dict(recorded_utc='2026-11-16T10:00:00Z',recorded_time_kind='indi_blob_receipt',
                exposure_seconds=1.25,gain=14.,night=True,moon_mode=False,stars=0,
                jsqm=0.,camera_model='Synthetic Camera',exposure_start_utc=None)


class ExtractionTests(unittest.TestCase):
    def test_same_row_frozen_before_database_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); dbpath=root/'image.db'
            data=jpeg(); (root/'1.jpg').write_bytes(data)
            with sqlite3.connect(dbpath) as db:
                db.execute('CREATE TABLE image(id,createDate,filename,fileSize,camera_id,exposure,gain,night,moonmode,stars,sqm,data)')
                db.execute('INSERT INTO image VALUES(1,?,?,?,?,?,?,?,?,?,?,?)',
                    ('2026-11-16 10:00:00','1.jpg',len(data),1,1.25,14,1,0,0,0,
                     json.dumps({'qhy_native':{'fits_date_obs':'2026-11-16T09:59:58.750'}})))
            c=P.Config(dbpath,root,1,root)
            source=P.ImageSource(c)
            src, selected=source.newest(dt.datetime(2026,11,16,10,tzinfo=dt.timezone.utc).timestamp())
            with sqlite3.connect(dbpath) as db: db.execute('UPDATE image SET stars=999')
            self.assertEqual(source.metadata['fields']['stars'],0)
            self.assertEqual(selected,data)
            self.assertEqual(source.metadata['fields']['exposure_start_utc'],'2026-11-16T09:59:58.750000Z')
            self.assertEqual(source.metadata['source'],src)

    def test_nullable_invalid_values_exif_and_bounded_allowlist(self):
        row=dict(createDate='2026-11-16 10:00:00',exposure=1.,gain=0,night=0,
                 moonmode=None,stars=0,sqm=0,data='{}')
        b=io.BytesIO(); im=Image.new('RGB',(4,4)); ex=Image.Exif();ex[272]='Exact JPEG model'
        im.save(b,format='JPEG',exif=ex)
        result=M.extract(row,b.getvalue())
        self.assertEqual(result['camera_model'],'Exact JPEG model')
        self.assertEqual(result['jsqm'],0)
        self.assertIsNone(result['moon_mode'])
        self.assertEqual(set(result),set(fields()))
        for key,value in [('exposure',0),('gain',float('nan')),('night',2)]:
            with self.subTest(key=key), self.assertRaises(ValueError): M.extract(dict(row,**{key:value}),jpeg())
        result=M.extract(dict(row,stars=-1,sqm=float('inf'),data='bad'),jpeg())
        self.assertIsNone(result['stars']);self.assertIsNone(result['jsqm']);self.assertIsNone(result['exposure_start_utc'])

    def test_dst_fold_gap_invalid_and_known_utc(self):
        self.assertEqual(M.recorded_utc('2026-11-16 10:00:00'),'2026-11-16T10:00:00Z')
        self.assertEqual(M.recorded_utc('2026-07-01 10:00:00'),'2026-07-01T09:00:00Z')
        self.assertIsNone(M.recorded_utc('2026-10-25 01:30:00'))
        self.assertIsNone(M.recorded_utc('2026-03-29 01:30:00'))
        self.assertIsNone(M.recorded_utc('nonsense'))
        self.assertEqual(M.native_utc('2026-11-16T09:00:00.125'),'2026-11-16T09:00:00.125000Z')
        self.assertIsNone(M.native_utc('bad'))



class MetadataHTTPTests(unittest.TestCase):
    def test_loopback_put_headers_limits_redirect_timeout_and_retry(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import time
        received=[]; replies=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_PUT(self):
                raw=self.rfile.read(int(self.headers['Content-Length']))
                received.append((self.path,dict(self.headers),raw))
                status,body,delay=replies.pop(0)
                if delay: time.sleep(delay)
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                if status==302: self.send_header('Location','http://127.0.0.1:1/not-followed')
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            transport=M.MetadataTransport(f'127.0.0.1:{server.server_port}',secure=False,timeout=.05)
            body=M.encoded(dict(fields(),schema_version=1,sequence='1',jpeg_sha256='0'*64,image_id='1',camera_id='1'))
            for status,raw in ((200,b'{"committed":true}'),(302,b'{}'),(503,b'{}'),(200,b'x'*5000)):
                replies.append((status,raw,0))
                r=transport.put(body,TOKEN)
                self.assertEqual(r.status,status)
                self.assertEqual(r.oversized,len(raw)>P.MAX_RESPONSE)
            replies.append((200,b'{}',.15))
            with self.assertRaises(P.TransportFailure): transport.put(body,TOKEN)
            replies.append((200,b'{}',0))
            transport.put(body,TOKEN)
            self.assertEqual(len(received),6)
            for path,headers,raw in received:
                self.assertEqual(path,M.PATH)
                self.assertEqual(headers['Content-Type'],'application/json')
                self.assertEqual(headers['Authorization'],'Bearer '+TOKEN)
                self.assertEqual(int(headers['Content-Length']),len(body))
                self.assertEqual(raw,body)
        finally: server.shutdown();server.server_close()

    def test_cli_logs_never_interpolate_arbitrary_exceptions(self):
        with patch.object(P.Config,'load',side_effect=Exception(TOKEN)):
            with self.assertLogs(M.LOG,level='ERROR') as log:
                self.assertEqual(M.main(['--config','/mock']),1)
        self.assertNotIn(TOKEN,str(log.output))


class BaselineComparisonTests(unittest.TestCase):
    def test_jpeg_source_identity_state_timing_and_wire_unchanged(self):
        # Load the protected deployment as a distinct module: it has no capture addition.
        import importlib.util
        import os
        baseline=Path(os.environ.get('BASELINE_PUBLISHER_PATH','/nonexistent-baseline'))
        # Test fixture is exported from the committed parent, never production secrets.
        if not baseline.exists(): self.skipTest('run with exported baseline fixture')
        spec=importlib.util.spec_from_file_location('jpeg_baseline',baseline)
        old=importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name]=old;spec.loader.exec_module(old)
        from test_publisher import Source,Wire
        for status in (200,401,409,429,503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                observations=[]
                for module,directory in ((old,a),(P,b)):
                    store=module.StateStore(Path(directory));store.initialise('1')
                    (Path(directory)/'metadata').mkdir(mode=0o700)
                    src=Source();src.metadata={'source':src.newest(0)[0],'fields':fields()}
                    wire=Wire()
                    if status!=200: wire.reply=response(status,{'error':'obsolete_sequence','current_sequence':'9'})
                    runner=module.Publisher(store,src,wire,lambda:TOKEN,lambda:1800000000.)
                    result=runner.run(); state=store.load()
                    if state['pending']: state['pending']['file']='random-snapshot-name'
                    observations.append((result,state,wire.calls))
                self.assertEqual(observations[0],observations[1])

if __name__=='__main__': unittest.main()
