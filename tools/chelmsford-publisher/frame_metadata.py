#!/usr/bin/env python3
"""Exact-frame observation sidecar. Never writes the JPEG publisher state.

The producer owns candidate.json; the separate companion owns delivery.json.
Only a matching durable JPEG ACK authorizes a PUT. No database access after ACK.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import http.client
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import re
import ssl
import time
from zoneinfo import ZoneInfo

import publisher as P

PATH = '/api/camera/latest/metadata'
MAX_BODY = 8192
LOG = logging.getLogger('chelmsford-metadata')
FIELDS = {'schema_version','sequence','jpeg_sha256','image_id','camera_id',
          'recorded_utc','recorded_time_kind','exposure_seconds','gain','night',
          'moon_mode','stars','jsqm','camera_model','exposure_start_utc'}


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def utc_string(stamp):
    stamp = stamp.astimezone(dt.timezone.utc)
    return stamp.isoformat(timespec='microseconds' if stamp.microsecond else 'seconds').replace('+00:00','Z')


def recorded_utc(value):
    """Host-local DB receipt time, with ambiguous/nonexistent times left null.

    /etc/localtime is the same source timezone used by datetime.fromtimestamp
    in the application. This does not change the protected JPEG age policy.
    """
    try:
        stamp=dt.datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            return utc_string(stamp)
        with open('/etc/localtime','rb') as f:
            zone=ZoneInfo.from_file(f)
        candidates=set()
        for fold in (0,1):
            aware=stamp.replace(tzinfo=zone,fold=fold)
            back=aware.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None)
            if back==stamp:
                candidates.add(utc_string(aware))
        return candidates.pop() if len(candidates)==1 else None
    except (ValueError,TypeError,OSError,OverflowError):
        return None


def native_utc(value):
    # INDI's FITS DATE-OBS is specified as UTC even without an explicit suffix.
    try:
        if not isinstance(value,str): return None
        stamp=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=dt.timezone.utc)
        return utc_string(stamp)
    except (ValueError,TypeError,OverflowError):
        return None


def extract(row,jpeg):
    """Normalize only the allowlist from the SAME SQL row and JPEG buffer."""
    row=dict(row)
    exposure,gain,night=row.get('exposure'),row.get('gain'),row.get('night')
    if not finite(exposure) or exposure<=0 or not finite(gain) or type(night) is not int or night not in (0,1):
        raise ValueError('metadata_required_fields')
    moon,stars,jsqm=row.get('moonmode'),row.get('stars'),row.get('sqm')
    model=None
    try:
        from PIL import Image
        with Image.open(BytesIO(jpeg)) as image:
            value=image.getexif().get(272)
        if (isinstance(value,str) and value.strip() and
                len(value.encode('utf-16-le'))//2<=128 and
                not re.search(r'[\x00-\x1f\x7f]',value)):
            model=value
    except Exception:
        pass  # optional EXIF must not change JPEG eligibility
    start=None
    try:
        raw=row.get('data')
        if isinstance(raw,str) and len(raw)<=65536:
            data=json.loads(raw)
            native=data.get('qhy_native') if isinstance(data,dict) else None
            if isinstance(native,dict): start=native_utc(native.get('fits_date_obs'))
    except (ValueError,TypeError):
        pass
    return dict(recorded_utc=recorded_utc(row.get('createDate')),
                recorded_time_kind='indi_blob_receipt',exposure_seconds=exposure,gain=gain,
                night=bool(night),moon_mode=bool(moon) if type(moon) is int and moon in (0,1) else None,
                stars=stars if type(stars) is int and 0<=stars<=P.MAX_SEQUENCE else None,
                jsqm=jsqm if finite(jsqm) else None,camera_model=model,exposure_start_utc=start)


def valid_payload(p):
    try:
        if set(p)!=FIELDS or type(p['schema_version']) is not int or p['schema_version']!=1: return False
        if not P.sequence(p['sequence']) or not re.fullmatch('[0-9a-f]{64}',p['jpeg_sha256']): return False
        for key in ('image_id','camera_id'):
            if not isinstance(p[key],str) or not re.fullmatch('[1-9][0-9]{0,18}',p[key]) or int(p[key])>9223372036854775807: return False
        if p['recorded_time_kind']!='indi_blob_receipt': return False
        for key in ('recorded_utc','exposure_start_utc'):
            value=p[key]
            if value is not None:
                if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.[0-9]{1,9})?Z',value): return False
                dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if not finite(p['exposure_seconds']) or p['exposure_seconds']<=0 or not finite(p['gain']): return False
        if type(p['night']) is not bool or (p['moon_mode'] is not None and type(p['moon_mode']) is not bool): return False
        if p['stars'] is not None and (type(p['stars']) is not int or not 0<=p['stars']<=P.MAX_SEQUENCE): return False
        if p['jsqm'] is not None and not finite(p['jsqm']): return False
        value=p['camera_model']
        if value is not None and (not isinstance(value,str) or not value.strip() or len(value.encode('utf-16-le'))//2>128 or re.search(r'[\x00-\x1f\x7f]',value)): return False
        return len(encoded(p))<=MAX_BODY
    except (ValueError,TypeError,KeyError,OverflowError):
        return False


class MetadataStore:
    def __init__(self,jpeg_directory):
        self.directory=jpeg_directory/'metadata'
        self.candidate_path=self.directory/'candidate.json'
        self.ledger_path=self.directory/'delivery.json'

    def check(self):
        if (self.directory.is_symlink() or not self.directory.is_dir() or
                self.directory.stat().st_mode & 0o077):
            raise P.PublisherError('metadata_directory_permissions')

    def write(self,path,value):
        self.check()
        P.durable_file(path,encoded(value)+b'\n')

    def read(self,path):
        self.check()
        if not path.exists(): return None
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise P.PublisherError('metadata_file_permissions')
        with path.open('rb') as f: raw=f.read(16385)
        if len(raw)>16384: raise P.PublisherError('metadata_file_size')
        return json.loads(raw)

    def freeze(self,pending,capture):
        if not capture or capture['source']!=pending['source']: return
        p=dict(capture['fields'],schema_version=1,sequence=pending['sequence'],
               jpeg_sha256=pending['sha256'],image_id=str(pending['source']['image_id']),
               camera_id=str(pending['source']['camera_id']))
        if not valid_payload(p): raise ValueError('metadata_payload_invalid')
        self.write(self.candidate_path,dict(source=pending['source'],payload=p))

    def candidate(self):
        try:
            c=self.read(self.candidate_path)
            if c is None: return None
            if set(c)!= {'source','payload'} or not valid_payload(c['payload']): raise ValueError
            s=c['source'];p=c['payload']
            if (set(s)!={'camera_id','image_id','created'} or type(s['camera_id']) is not int
                    or type(s['image_id']) is not int or not isinstance(s['created'],str)
                    or str(s['camera_id'])!=p['camera_id'] or str(s['image_id'])!=p['image_id']): raise ValueError
            return c
        except Exception:
            raise P.PublisherError('metadata_candidate_invalid') from None

    @contextmanager
    def lock(self):
        self.check()
        fd=os.open(self.directory/'companion.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise P.Busy('metadata_running') from None
            yield
        finally: os.close(fd)


class MetadataTransport:
    def __init__(self,host=P.HOST,*,secure=True,timeout=10):
        self.host,self.secure,self.timeout=host,secure,timeout

    def put(self,data,token):
        conn=(http.client.HTTPSConnection if self.secure else http.client.HTTPConnection)(self.host,timeout=self.timeout)
        try:
            conn.request('PUT',PATH,body=data,headers={'Authorization':'Bearer '+token,
                         'Content-Type':'application/json','Accept':'application/json','Content-Length':str(len(data))})
            r=conn.getresponse()
            headers={k.lower():v for k,v in r.getheaders()}
            try: body=r.read(P.MAX_RESPONSE+1)
            except (OSError,http.client.HTTPException): return P.Response(r.status,headers,b'',incomplete=True)
            return P.Response(r.status,headers,body[:P.MAX_RESPONSE],len(body)>P.MAX_RESPONSE)
        except ssl.SSLCertVerificationError:
            raise P.TransportFailure('tls_verification') from None
        except (OSError,http.client.HTTPException):
            raise P.TransportFailure('transport_outcome_unknown') from None
        finally: conn.close()


class Companion:
    def __init__(self,jpeg_store,metadata_store,transport,credential=P.load_token,
                 clock=time.time,hook=lambda point:None):
        self.jpeg,self.meta,self.transport=jpeg_store,metadata_store,transport
        self.credential,self.clock,self.hook=credential,clock,hook

    def run(self):
        with self.meta.lock():
            c=self.meta.candidate()
            if c is None: return 'no_candidate'
            p=c['payload']; body=encoded(p)
            ident=dict(sequence=p['sequence'],jpeg_sha256=p['jpeg_sha256'],metadata_sha256=P.digest(body))
            # Atomic read only: do not acquire JPEG lock or touch JPEG cleanup/state.
            ack=self.jpeg.load()['last_ack']
            if ack and int(ack['sequence'])>int(p['sequence']): return 'superseded'
            if not ack or ack['sequence']!=p['sequence']: return 'waiting_for_jpeg_ack'
            if ack['sha256']!=p['jpeg_sha256'] or ack['source']!=c['source']:
                raise P.PublisherError('metadata_local_ack_mismatch')
            try:
                ledger=self.meta.read(self.meta.ledger_path)
                if ledger is not None:
                    if (set(ledger)!={'identity','outcome','failure_count','next_eligible_utc'}
                            or type(ledger['failure_count']) is not int or ledger['failure_count']<0
                            or not finite(ledger['next_eligible_utc']) or ledger['next_eligible_utc']<0
                            or not isinstance(ledger['identity'],dict)
                            or set(ledger['identity'])!={'sequence','jpeg_sha256','metadata_sha256'}
                            or not P.sequence(ledger['identity']['sequence'])
                            or any(not isinstance(ledger['identity'][k],str) or not re.fullmatch('[0-9a-f]{64}',ledger['identity'][k]) for k in ('jpeg_sha256','metadata_sha256'))
                            or ledger['outcome'] not in {'pending','acknowledged','superseded',
                                'review:credential','review:tls','review:invalid_ack',
                                'review:jpeg_digest_mismatch','review:metadata_conflict',
                                'review:invalid_conflict','review:http_status'}):
                        raise ValueError
            except Exception: raise P.PublisherError('metadata_delivery_state_invalid') from None
            if ledger is None or ledger['identity']!=ident:
                ledger=dict(identity=ident,outcome='pending',failure_count=0,next_eligible_utc=0)
            if ledger['outcome']!='pending': return 'already_resolved'
            if self.clock()<ledger['next_eligible_utc']: return 'deferred'
            # Persistent metadata-only backoff BEFORE networking, one PUT per run.
            ledger['failure_count']+=1
            ledger['next_eligible_utc']=self.clock()+min(1800,60*2**min(ledger['failure_count']-1,5))
            self.meta.write(self.meta.ledger_path,ledger)
            def finish(outcome):
                ledger['outcome']=outcome
                self.meta.write(self.meta.ledger_path,ledger)
                return outcome
            try:
                token=self.credential()
            except Exception: return finish('review:credential')
            try:
                r=self.transport.put(body,token)
            except P.TransportFailure as exc:
                return finish('review:tls') if exc.category=='tls_verification' else 'retry_later'
            self.hook('metadata_response_received')
            obj=None
            if not r.oversized and not r.incomplete and r.headers.get('content-type','').split(';')[0].strip().lower()=='application/json':
                try: obj=json.loads(r.body)
                except (ValueError,UnicodeError): pass
            if r.status==200:
                if (not isinstance(obj,dict) or obj.get('committed') is not True
                        or obj.get('sequence')!=p['sequence'] or obj.get('jpeg_sha256')!=p['jpeg_sha256']):
                    return finish('review:invalid_ack')
                result=finish('acknowledged')
                LOG.info('metadata_ack sequence=%s image=%s jpeg_sha256=%s metadata_sha256=%s',
                         p['sequence'],p['image_id'],p['jpeg_sha256'],ident['metadata_sha256'])
                return result
            if r.status==409:
                error=obj.get('error') if isinstance(obj,dict) else None
                if error=='stale_sequence': return finish('superseded')
                if error=='jpeg_not_committed': return 'retry_later'
                if error in ('jpeg_digest_mismatch','metadata_conflict'): return finish('review:'+error)
                return finish('review:invalid_conflict')
            if r.status==503: return 'retry_later'
            if r.status==429:
                value=r.headers.get('retry-after','')
                if re.fullmatch('[0-9]{1,10}',value) and int(value)>0:
                    ledger['next_eligible_utc']=max(ledger['next_eligible_utc'],self.clock()+int(value)+5)
                    self.meta.write(self.meta.ledger_path,ledger)
                return 'retry_later'
            return finish('review:http_status')  # includes redirects; never follow


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    try:
        config=P.Config.load(args.config)
        result=Companion(P.StateStore(config.state_dir),MetadataStore(config.state_dir),MetadataTransport()).run()
        LOG.info('%s',result)
        return 1 if result.startswith('review:') else 0
    except P.Busy:
        LOG.info('metadata_running');return 0
    except Exception:
        LOG.error('metadata_failed_requires_review');return 1


if __name__=='__main__': raise SystemExit(main())
