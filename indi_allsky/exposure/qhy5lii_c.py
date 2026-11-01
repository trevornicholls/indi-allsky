"""QHY5L-II-C integer feedback. Policy lives ONLY in ImageWorker.
Transport tags one frame and waits for image completion; never computes policy.
"""
from collections import deque
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import logging
import math
from queue import Empty
from threading import RLock
import time
import uuid
LOG = logging.getLogger('indi_allsky')
STRATEGY = 'exposure_qhy5lii_c'
LADDER = (1, 3, 5, 7, *range(8, 23))

class ControlError(ValueError):
    pass

def check(ok, reason):
    if not ok:
        raise ControlError(reason)

def enabled(config):
    return config.get('CCD_CONFIG', {}).get('EXPOSURE_CLASSNAME') == STRATEGY

def integer(value):
    check(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and (int(value) == value), 'fractional/nonfinite gain or binning')
    return int(value)

def mode_name(night, moon=False):
    return 'moon' if night and moon else 'night' if night else 'day'

def limits(config, mode, hardware_min=1e-06, hardware_max=15.0):
    c = config['CCD_CONFIG']
    floor = integer(max(1, min(22, c['DAY']['GAIN'])))
    key = {'day': 'DAY', 'night': 'NIGHT', 'moon': 'MOONMODE'}[mode]
    cap = integer(max(1, min(22, c[key]['GAIN'])))
    check(floor <= cap, 'gain ceiling below floor')
    check(integer(c[key]['BINNING']) == 1 and config.get('IMAGE_STACK_COUNT', 1) == 1, 'QHY C feedback requires 1x1 and unstacked images')
    minimum = config.get('CCD_EXPOSURE_MIN_DAY' if mode == 'day' else 'CCD_EXPOSURE_MIN', 0) or hardware_min
    lo = math.ceil(max(hardware_min, minimum) * 1000000.0) / 1000000.0
    hi = math.floor(min(hardware_max, config.get('CCD_EXPOSURE_MAX', 15), 15) * 1000000.0) / 1000000.0
    target = float(config['TARGET_ADU_DAY' if mode == 'day' else 'TARGET_ADU'])
    deviation = max(1.0, float(config.get('TARGET_ADU_DEV_DAY' if mode == 'day' else 'TARGET_ADU_DEV', 20)))
    check(0 < lo <= hi <= 15 and 0 < target - deviation < target + deviation < 255, 'invalid exposure/ADU bounds')
    return {'lo': lo, 'hi': hi, 'floor': floor, 'cap': cap, 'codes': [floor, *[g for g in LADDER if floor < g <= cap]], 'target': target, 'deviation': deviation}

def validate(settings, bounds):
    e, g, b = settings
    g, b = (integer(g), integer(b))
    check(isinstance(e, (int, float)) and math.isfinite(e) and (bounds['lo'] <= e <= bounds['hi']) and (1 <= g <= 22) and (bounds['floor'] <= g <= bounds['cap']) and (b == 1), 'QHY settings outside mode limits')
    return (float(e), g, b)

def clamp(settings, bounds):
    e, g, _ = settings
    codes = bounds['codes']
    gain = max([v for v in codes if v <= g] or [codes[0]])
    return validate((round(max(bounds['lo'], min(bounds['hi'], e)), 6), gain, 1), bounds)

class Policy:
    """Memory-only brightness policy; no hardware, database or worker imports."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.epoch = None
        self.status = 'starting'
        self.target = 0
        self.stable = False
        self.halted = None
        self.last_settings = None
        self.history = []
        self.direction = self.qualified = self.stable_count = self.settle = 0
        self.after_gain = False
        self.gain_directions = deque(maxlen=6)
        self.hold_until = 0
        self.bracket = None
        self.up_reference = None
        self.no_response = 0
        self.invalid = 0
        self.clip_limit = self.clip_reference = self.clip_safe = None
        self.clip_clear = 0

    def decide(self, command, adu, native):
        b = command['bounds']
        current = validate((command['exposure'], command['gain'], command['binning']), b)
        e, g, binning = current
        self.target = b['target']
        self.stable = False
        epoch = (command['session'], command['epoch'])
        if self.epoch != epoch:
            self.__init__(self.clock)
            self.epoch, self.target = (epoch, b['target'])
            self.status = 'mode changed'
        if self.halted:
            raise ControlError(self.halted)
        if command['kind'] == 'sqm':
            self.history = []
            self.settle, self.after_gain = (1, True)
            self.clip_clear = 0
            self.status = 'SQM complete; ordinary settling required'
            return None
        if self.last_settings is not None and self.last_settings != current:
            self.history = []
            self.direction = self.qualified = self.stable_count = 0
            if self.last_settings[1:] != current[1:]:
                self.settle, self.after_gain = (1, True)
                self.clip_limit = self.clip_reference = self.clip_safe = None
                self.clip_clear = 0
        self.last_settings = current
        if not native.get('valid') or adu is None or (not isinstance(adu, (int, float))) or (not math.isfinite(adu)) or (not 0 <= adu <= 255):
            self.invalid += 1
            self.history = []
            self.qualified = self.stable_count = 0
            self.clip_clear = 0
            self.status = 'invalid brightness; hold'
            if self.invalid >= 2:
                self.halted = 'repeated invalid brightness'
                raise ControlError(self.halted)
            return current
        self.invalid = 0
        if self.settle:
            self.settle -= 1
            self.status = 'settling gain/SQM'
            return current
        self.history = (self.history + [float(adu)])[-2:]
        if self.after_gain and len(self.history) < 2:
            self.status = 'qualifying post-gain feedback'
            return current
        if self.after_gain:
            self.after_gain = False
            adu = sum(self.history) / len(self.history)
        T, D = (b['target'], b['deviation'])
        direction = 1 if adu < T - D else -1 if adu > T + D else 0
        if self.hold_until and self.clock() >= self.hold_until:
            self.hold_until = 0
            self.qualified = 0
        self.qualified = self.qualified + 1 if direction == self.direction else 1
        self.direction = direction
        if native.get('clipped'):
            self.clip_clear = 0
            if self.up_reference is not None and self.up_reference['gain'] == g:
                self.bracket = {'lower': self.up_reference['lower'], 'upper': g, 'reference': None}
                self.up_reference = None
            self.stable_count = 0
            if e > b['lo']:
                # Reuse the last measured safe setting rather than cut again
                # independently of the ADU loop. If none is lower, halve once
                # and retain that bound until new measurements justify release.
                safe = self.clip_safe if self.clip_safe and self.clip_safe[0] < e else None
                reduced = max(b['lo'], round(max(e * .5, safe[0]) if safe else e * .5, 6))
                self.clip_limit = min(self.clip_limit or b['hi'], reduced)
                self.clip_reference = safe[1] if safe and safe[0] == self.clip_limit else None
                self.clip_clear = 0
                self.status = 'clipped: constrain exposure'
                return validate((self.clip_limit, g, 1), b)
            self.clip_limit, self.clip_reference = (e, None)
            if g > b['floor']:
                return self.step(command, -1, adu)
            self.status = 'overexposed limit'
            return current
        self.clip_safe = (e, adu)
        if self.clip_limit is not None and abs(e - self.clip_limit) <= .0000011:
            # Shared exposure storage can truncate a rounded request by 1us.
            # Bind the ceiling/reference to the actual immutable command.
            self.clip_limit = e
            if self.clip_reference is None:
                self.clip_reference = adu
            # Hysteresis: same-gain, same-exposure dimming AND two frames below
            # half the unchanged 1% clipping threshold. Time alone cannot release
            # the ceiling and recreate the static-scene limit cycle.
            clearing = (direction > 0 and native.get('clip_fraction', 0.) <= .005
                        and adu <= self.clip_reference - max(2., D / 4))
            self.clip_clear = self.clip_clear + 1 if clearing else 0
            if self.clip_clear >= 2:
                raised = min(b['hi'], max(round(e * 1.25, 6), round(e + .000001, 6)))
                self.clip_limit = raised if raised < b['hi'] else None
                self.clip_reference = None
                self.clip_clear = 0
            elif direction > 0:
                self.qualified = self.stable_count = 0
                self.status = 'clipping-limited: ADU target unattainable'
                return current
        if self.up_reference is not None and self.up_reference['gain'] == g:
            previous = self.up_reference
            if direction < 0:
                self.bracket = {'lower': previous['lower'], 'upper': g, 'reference': None}
            if e == previous['exposure'] and abs(adu - previous['adu']) <= max(2.0, D / 4):
                self.no_response += 1
            else:
                self.no_response = 0
            self.up_reference = None
            if self.no_response >= 2:
                self.hold_until = self.clock() + 120
                self.status = 'gain-no-response; temporary gain hold'
        if not direction:
            self.stable_count += 1
            self.stable = self.stable_count >= 2
            self.status = 'stable' if self.stable else 'qualifying stable'
            if self.bracket and self.bracket['upper'] == g and (self.bracket['reference'] is None) and self.stable:
                self.bracket['reference'] = e
            if self.stable_count >= 6:
                self.gain_directions.clear()
            if self.stable and self.bracket and (self.bracket['upper'] == g) and (self.bracket['reference'] is not None) and (e <= self.bracket['reference'] * 0.5) and (self.clock() >= self.hold_until):
                return self.step(command, -1, adu)
            return current
        self.stable_count = 0
        index = b['codes'].index(g)
        gain_allowed = self.clock() >= self.hold_until
        if direction > 0 and e >= b['hi'] and (index < len(b['codes']) - 1):
            if gain_allowed and self.qualified >= 2:
                return self.step(command, 1, adu)
            self.status = 'gain hold' if not gain_allowed else 'qualifying upward gain'
            return current
        blocked_lower = self.bracket and self.bracket['upper'] == g and (e > b['lo'])
        if direction < 0 and index > 0 and (not blocked_lower) and gain_allowed:
            if self.qualified >= 2:
                return self.step(command, -1, adu)
            self.status = 'qualifying downward gain'
            return current
        factor = min(2.0, max(0.5, 1 + 0.5 * (T / max(adu, 1.0) - 1)))
        desired = min(e * factor, self.clip_limit) if self.clip_limit is not None else e * factor
        proposed = clamp((desired, g, 1), b)
        self.status = ('underexposed limit' if direction > 0 else 'overexposed limit') if proposed == current else 'bounded exposure correction'
        return proposed

    def step(self, command, direction, adu):
        b = command['bounds']
        e, g = (command['exposure'], command['gain'])
        index = b['codes'].index(g)
        new = b['codes'][max(0, min(len(b['codes']) - 1, index + direction))]
        if new == g:
            return (e, g, 1)
        self.gain_directions.append(direction)
        if sum((a != b for a, b in zip(self.gain_directions, list(self.gain_directions)[1:]))) >= 3:
            self.hold_until = self.clock() + 120
            self.gain_directions.clear()
        if direction > 0:
            self.up_reference = {'lower': g, 'gain': new, 'adu': adu, 'exposure': e}
        if self.bracket and direction > 0:
            self.bracket = {'lower': g, 'upper': new, 'reference': None}
        self.qualified = 0
        self.stable = False
        self.status = 'one integer gain step' if self.clock() >= self.hold_until else 'one gain step; reversal cooldown'
        return validate((e, new, 1), b)

@lru_cache(maxsize=4)
def mask_file(path):
    import cv2
    result = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    check(result is not None, 'cannot read existing ADU/detection mask')
    return result > 0

def native_statistics(data, config):
    import numpy as np
    check(data is not None and data.shape == (960, 1280) and (data.dtype == np.uint8), 'QHY native format must be 1280x960 uint8')
    if config.get('DETECT_MASK'):
        mask = mask_file(config['DETECT_MASK'])
        check(mask.shape == data.shape, 'native/ADU mask dimensions differ')
    else:
        h, w = data.shape
        mask = np.zeros((h, w), dtype=bool)
        roi = config.get('ADU_ROI', [])
        if roi:
            check(len(roi) == 4, 'invalid ADU rectangle')
            x1, y1, x2, y2 = map(int, roi)
        else:
            div = float(config.get('ADU_FOV_DIV', 4))
            check(div >= 2, 'invalid ADU divisor')
            x1, y1, x2, y2 = (int(w / 2 - w / div), int(h / 2 - h / div), int(w / 2 + w / div), int(h / 2 + h / div))
        mask[max(0, y1):min(h, y2 + 1), max(0, x1):min(w, x2 + 1)] = True
    check(mask.any(), 'empty ADU mask')
    clipping = [float(np.mean(data[mask] == 255))]
    for y in (0, 1):
        for x in (0, 1):
            m = mask[y::2, x::2]
            if m.any():
                clipping.append(float(np.mean(data[y::2, x::2][m] == 255)))
    uniform = bool(data.min() == data.max() and 0 < int(data.flat[0]) < 255)
    return {'valid': not uniform, 'uniform': bool(data.min() == data.max() and 0 < int(data.flat[0]) < 255), 'clipped': max(clipping) >= 0.01, 'raw_mean': float(data[mask].mean()), 'raw_std': float(data.std()), 'clip_fraction': max(clipping), 'pixel_sha': hashlib.sha256(data.tobytes()).hexdigest()}

class FrameGate:
    """Capture-owned association/gate only. Image worker owns all adaptation."""

    def __init__(self, config, feedback, shared, hardware_min=1e-06, hardware_max=15.0):
        check(feedback is not None and shared is not None, 'missing frame coordination')
        check(shared[0] == 0, 'worker replaced with unresolved QHY frame/fault')
        self.config, self.feedback, self.shared = (config, feedback, shared)
        self.hardware_min, self.hardware_max = (hardware_min, hardware_max)
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.epoch = 0
        self.mode = None
        self.pending = None
        self.completed = None
        self.blob_time = None
        self.sent_time = None
        self.fault = None
        self.lock = RLock()
        self.last_date = None
        self.last_pixel = None
        self.uniform_count = 0
        self.next = None
        self.ordinary = None

    def set_mode(self, mode):
        if self.mode != mode:
            self.mode = mode
            self.epoch += 1
            self.shared[1] = self.epoch

    def bounds(self):
        return limits(self.config, self.mode, self.hardware_min, self.hardware_max)

    def fail(self, error):
        if self.fault is None:
            self.fault = str(error)
            LOG.error('QHY C halted: %s', error)
        self.shared[0] = 2

    def start(self, exposure, gain, binning, kind):
        with self.lock:
            check(not self.fault and self.pending is None, 'QHY pending/fault blocks command')
            b = self.bounds()
            e, g, n = validate((exposure, gain, binning), b)
            self.sequence += 1
            command = {'session': self.session, 'sequence': self.sequence, 'epoch': self.epoch, 'mode': self.mode, 'kind': kind, 'exposure': e, 'gain': g, 'binning': n, 'bounds': b}
            self.exposure_submitted = False
            self.pending = command
            self.sent_time = time.monotonic()
            self.sent_wall = time.time()
            self.blob_time = None
            self.shared[0] = 1
            if kind == 'normal':
                self.ordinary = (e, g, n)
            LOG.info('QHY C request %s', command)
            return command

    def property(self, name, values):
        if name not in ('CCD_GAIN', 'CCD_EXPOSURE', 'CCD_BINNING'):
            return
        c = self.pending
        check(c is not None and (not self.fault), 'QHY property write outside pending command')
        expected = {'CCD_GAIN': {'GAIN': c['gain']}, 'CCD_EXPOSURE': {'CCD_EXPOSURE_VALUE': c['exposure']}, 'CCD_BINNING': {'HOR_BIN': c['binning'], 'VER_BIN': c['binning']}}
        check(values == expected[name], 'QHY property contradicts pending settings')
        if name == 'CCD_EXPOSURE':
            check(not self.exposure_submitted, 'duplicate exposure write')
            self.exposure_submitted = True

    def frame(self, hdulist):
        with self.lock:
            c = self.pending
            check(c is not None and self.exposure_submitted and (self.blob_time is None) and (not self.fault), 'unsolicited/duplicate QHY BLOB')
            h = hdulist[0].header
            check(abs(float(h['EXPTIME']) - c['exposure']) <= 2.1e-06 and float(h['GAIN']) == c['gain'] and (h.get('XBINNING') == 1) and (h.get('YBINNING') == 1) and (h.get('BAYERPAT') == 'GRBG'), 'QHY FITS/settings mismatch')
            date = datetime.fromisoformat(h['DATE-OBS'].replace('Z', '+00:00'))
            date = date if date.tzinfo else date.replace(tzinfo=timezone.utc)
            stamp = date.timestamp()
            check(self.sent_wall - 1 <= stamp <= time.time() + 1 and (self.last_date is None or stamp > self.last_date), 'stale/repeated QHY timestamp')
            raw = native_statistics(hdulist[0].data, self.config)
            check(raw['pixel_sha'] != self.last_pixel or raw['raw_std'] == 0 or raw['clipped'], 'replayed nonuniform native image')
            self.uniform_count = self.uniform_count + 1 if raw['uniform'] else 0
            check(self.uniform_count < 2, 'repeated midscale-uniform native image')
            self.last_date, self.last_pixel = (stamp, raw['pixel_sha'])
            self.blob_time = time.monotonic()
            LOG.info('QHY C native seq=%d raw=%s', c['sequence'], raw)
            return (dict(c), raw)

    def poll(self, idle):
        with self.lock:
            if self.fault:
                return None
            if self.pending:
                age = time.monotonic() - (self.blob_time or self.sent_time)
                if age > (60 if self.blob_time else self.config.get('CCD_EXPOSURE_TIMEOUT', 330)):
                    self.fail('processing acknowledgement timeout' if self.blob_time else 'camera/BLOB timeout')
                    return None
            while True:
                try:
                    msg = self.feedback.get_nowait()
                except Empty:
                    break
                c = msg.get('command', {})
                if c.get('session') != self.session or c.get('sequence', 0) < self.sequence:
                    continue
                if self.pending is None and c == self.completed:
                    continue
                if self.pending is None or c != self.pending:
                    self.fail('frame acknowledgement identity/settings mismatch')
                    break
                if not idle:
                    self.feedback.put(msg)
                    break
                if self.blob_time is None or not msg.get('ok'):
                    self.fail(msg.get('error', 'invalid processing completion'))
                    break
                try:
                    if c['epoch'] != self.epoch:
                        self.next = clamp(self.ordinary if c['kind'] == 'sqm' else (c['exposure'], c['gain'], 1), self.bounds())
                    elif c['kind'] == 'sqm':
                        self.next = clamp(self.ordinary or self.next, self.bounds())
                    else:
                        self.next = validate(msg['proposal'], self.bounds())
                except (ControlError, KeyError, TypeError) as e:
                    self.fail('invalid feedback proposal: ' + str(e))
                    break
                LOG.info('QHY C feedback seq=%d epoch=%d status=%s target=%s stable=%s next=%s', c['sequence'], c['epoch'], msg.get('status'), msg.get('target'), msg.get('stable'), self.next)
                self.completed = self.pending
                self.pending = None
                self.blob_time = None
                self.shared[0] = 0
                return self.next
            return None

class Exposure:
    """Existing image-worker interface; the ONLY feedback policy owner."""

    def __init__(self, config, *arrays):
        self.config = config
        self.policy = Policy()
        self.context = None
        self.reply = None
        self.target_adu_found = False
        self.current_adu_target = 0.0
        self.hist_adu = []

    def prepare(self, job, shared):
        check('c_command' in job and 'c_native' in job, 'missing QHY frame context')
        self.context = job
        self.reply = None
        self.target_adu_found = False
        if job['c_command']['epoch'] != shared[1]:
            job['c_stale_mode'] = True

    def compare_exposure(self, adu, exposure, gain):
        job = self.context
        c = job['c_command']
        check(exposure == c['exposure'] and gain == c['gain'], 'image worker settings/context mismatch')
        if job.get('c_stale_mode'):
            proposal = (exposure, integer(gain), 1)
            status = 'old mode completed; no adaptation'
            stable = False
        else:
            proposal = self.policy.decide(c, float(adu), job['c_native'])
            status = self.policy.status
            stable = self.policy.stable
        self.current_adu_target = c['bounds']['target']
        self.target_adu_found = stable
        self.reply = {'command': c, 'ok': True, 'proposal': proposal, 'status': status, 'target': self.current_adu_target, 'stable': stable, 'clip_limit': self.policy.clip_limit}
        LOG.info('QHY C decision seq=%d ADU=%.3f status=%s target=%.3f stable=%s proposal=%s', c['sequence'], adu, status, self.current_adu_target, stable, proposal)
        return (adu, adu if stable else 0.0)

    def sqm_complete(self):
        c = self.context['c_command']
        self.policy.decide(c, 0.0, self.context['c_native'])
        self.reply = {'command': c, 'ok': True, 'proposal': None, 'status': self.policy.status, 'target': c['bounds']['target'], 'stable': False}
