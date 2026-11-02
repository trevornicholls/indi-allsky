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

# This is a binary-derived/readout-specific model, not a universal camera minimum.
# SDK SetChipUSBTraffic: reg300c = 1650 + 50*traffic (full width).
# SetChipExposeTime: RowTime = reg300c/(12 MHz * PLLratio=1), speed 0.
# Only the measured RAW8/full-frame/speed0/traffic30 profile is enabled.
SDK_QUANT_SHA = 'dbee1afda782c80f3a90d0c4a5a8de38430d592d56b97ce0052c95872248c078'
SHORT_REGION = .001


def exposure_quantum(readout):
    expected = {'sdk': '26.7.21', 'sdk_sha256': SDK_QUANT_SHA,
                'format': 'INDI_RAW', 'bits': 8., 'speed': 0., 'traffic': 30.,
                'width': 1280., 'height': 960., 'x': 0., 'y': 0.,
                'bin_x': 1., 'bin_y': 1.}
    if not readout or any(readout.get(k) != v for k, v in expected.items()):
        return None
    return (1650. + 50. * readout['traffic']) / 12. / 1e6


def exposure_rows(exposure, quantum):
    return max(1, int(int(exposure * 1e6) / (quantum * 1e6)))


@lru_cache(maxsize=4)
def _sdk_digest(path, size, modified):
    from pathlib import Path
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def readout_identity(device):
    """Read cached INDI properties, never write/query SDK or request a frame."""
    from pathlib import Path
    def number(prop, element):
        return next(v.getValue() for v in device.getNumber(prop) if v.getName() == element)
    try:
        sdk = Path('/usr/local/lib/libqhyccd.so').resolve()
        stat = sdk.stat()
        return {'sdk': next(v.getText() for v in device.getText('SDK_VERSION') if v.getName() == 'VERSION'),
                'sdk_sha256': _sdk_digest(str(sdk), stat.st_size, stat.st_mtime_ns),
                'format': next(v.getName() for v in device.getSwitch('CCD_CAPTURE_FORMAT') if int(v.getState()) == 1),
                'bits': number('CCD_INFO', 'CCD_BITSPERPIXEL'),
                'speed': number('USB_SPEED', 'SPEED'), 'traffic': number('USB_TRAFFIC', 'TRAFFIC'),
                **{key: number('CCD_FRAME', name) for key, name in [('x','X'),('y','Y'),('width','WIDTH'),('height','HEIGHT')]},
                'bin_x': number('CCD_BINNING', 'HOR_BIN'), 'bin_y': number('CCD_BINNING', 'VER_BIN')}
    except (OSError, AttributeError, TypeError, ValueError, StopIteration):
        return {'unverified': True}  # no invented mapping for missing properties


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
        self.clip_bad = self.clip_lower = None
        self.clip_retested = False
        self.resolution_scope = None
        self.resolution_samples = []
        self.resolution_observed = {}
        self.resolution_lock = None
        self.resolution_changed = 0
        self.resolution_telemetry = {}

    def decide(self, command, adu, native):
        if command['mode'] == 'day' and command['kind'] == 'normal':
            # Highlights are telemetry, not a Day veto. Use one private view
            # for both ordinary policy and every row/adjacent-row decision.
            native = dict(native, clipped=False, clip_fraction=0.)
        # Night/Moon retain f445f2c recovery. The narrow final arbiter
        # translates short requests into distinct rows in every mode.
        if command['kind'] == 'sqm':
            proposal = self._decide(command, adu, native)
            self.resolution_samples = []
            self.resolution_changed = 0
            return proposal  # SQM settings must not replace ordinary scope
        readout = command.get('readout', {})
        b = command['bounds']
        scope = (command['session'], command['epoch'], command['gain'],
                 tuple(sorted(readout.items())), tuple(sorted((k, b[k]) for k in ('lo','hi','target','deviation'))))
        previous_scope = self.resolution_scope
        if previous_scope is not None and scope != previous_scope:
            # Readout/limits changes invalidate physical clipping knowledge too.
            self.clip_limit = self.clip_reference = self.clip_safe = None
            self.clip_bad = self.clip_lower = None
            self.clip_clear = 0
            self.clip_retested = False
        proposal = self._decide(command, adu, native)
        if scope != self.resolution_scope:
            self.resolution_scope = scope
            self.resolution_samples = []
            self.resolution_observed = {}
            self.resolution_lock = None
            self.resolution_changed = 0
        quantum = exposure_quantum(readout)
        self.resolution_telemetry = {'model': 'SDK-row-profile' if quantum else 'unmapped',
                                     'row_seconds_estimate': quantum, 'rows': None, 'limited': False}
        if command['mode'] == 'day' and command['kind'] == 'normal':
            self.resolution_telemetry['clipping_policy'] = 'day-adu-primary'
        if (quantum is None or proposal is None or command['kind'] != 'normal'
                or self.invalid or self.status in ('settling gain/SQM', 'qualifying post-gain feedback')):
            self.resolution_samples = []
            self.resolution_changed = 0
            return proposal
        if command['exposure'] > SHORT_REGION:
            # Keep at most the neighbouring row at the region edge.
            # Policy and proposals outside the short region remain unchanged.
            edge = int(exposure_rows(SHORT_REGION, quantum)) + 1
            if exposure_rows(command['exposure'], quantum) == edge:
                self.resolution_observed[edge] = {'adu': adu, 'clip': native.get('clip_fraction', 0.), 'clipped': native.get('clipped', False)}
            else:
                self.resolution_observed = {}
            self.resolution_samples = []
            self.resolution_lock = None
            return proposal
        result = self._resolve_exposure(command, adu, native, proposal, quantum)
        self.resolution_telemetry['next_rows'] = exposure_rows(result[0], quantum)
        return result

    def _resolve_exposure(self, command, adu, native, proposal, quantum):
        b = command['bounds']
        e, g = command['exposure'], command['gain']
        row = int(exposure_rows(e, quantum))
        self.resolution_telemetry.update(rows=row, effective_seconds_estimate=row*quantum)
        # A gain decision remains the existing policy's responsibility.
        if proposal[1] != g:
            return proposal
        samples = self.resolution_samples
        if not samples or samples[-1][0] != row:
            samples = []
        samples = (samples + [(row, float(adu), float(native.get('clip_fraction', 0.)))])[-2:]
        self.resolution_samples = samples
        clipped = native.get('clipped', False)
        value = {'adu': sum(v[1] for v in samples)/len(samples),
                 'clip': max(v[2] for v in samples), 'clipped': clipped}
        if clipped or len(samples) == 2:
            self.resolution_observed[row] = value
        # Bound memory to this row and its immediate neighbours.
        self.resolution_observed = {r: v for r,v in self.resolution_observed.items() if abs(r-row) <= 1}
        T, D = b['target'], b['deviation']
        lo_row, hi_row = int(exposure_rows(b['lo'], quantum)), int(exposure_rows(min(b['hi'], SHORT_REGION), quantum))

        def setting(number):
            # Centre of a sensor-row interval avoids both SDK integer-us and
            # driver float truncation at exact boundaries. Respect user bounds.
            request = round((number + .5) * quantum, 6)
            request = round(max(b['lo'], min(b['hi'], request)), 6)
            return validate((request, g, 1), b)

        def hold(reason, limited=False):
            self.status = reason
            self.resolution_telemetry['limited'] = limited
            if limited:
                self.stable = False
            # Keep the existing clipping ceiling consistent with what will
            # actually be commanded, not an ineffective fractional-row probe.
            if self.clip_limit is not None:
                self.clip_limit = e
            return (e, g, 1)

        if clipped:
            self.resolution_lock = None
            if row <= lo_row:
                if g > b['floor']:
                    return self.step(command, -1, adu)
                return hold('exposure-resolution-limited: minimum permitted row clipped', True)
            result = setting(row-1)
            self.clip_limit = result[0]
            self.stable = False
            self.status = 'clipped: reduce one effective exposure row'
            return result
        lock = self.resolution_lock
        if lock and lock['selected'] == row:
            changed = (abs(value['adu']-lock['adu']) >= max(2., D/4)
                       or abs(value['clip']-lock['clip']) >= .0025)
            self.resolution_changed = self.resolution_changed+1 if changed else 0
            if self.resolution_changed < 2:
                return hold('exposure-resolution-limited: adjacent rows straddle target', True)
            self.resolution_lock = None
            self.resolution_changed = 0
            self.resolution_observed = {row: value}  # discard stale other-row data
        if len(samples) < 2:
            return hold('qualifying effective exposure row')
        observed = self.resolution_observed
        for lower in (row-1, row):
            low, high = observed.get(lower), observed.get(lower+1)
            if (low and high and not low['clipped'] and low['adu'] < T-D
                    and (high['clipped'] or high['adu'] > T+D)):
                selected = lower if high['clipped'] or T-D-low['adu'] <= high['adu']-(T+D) else lower+1
                ref = observed[selected]
                self.resolution_lock = {'selected': selected, 'adu': ref['adu'], 'clip': ref['clip']}
                self.resolution_changed = 0
                if selected == row:
                    return hold('exposure-resolution-limited: adjacent rows straddle target', True)
                self.stable = False
                self.status = 'exposure-resolution-limited: select nearer safe row'
                self.resolution_telemetry['limited'] = True
                return setting(selected)
        direction = 1 if adu < T-D else -1 if adu > T+D else 0
        if not direction:
            return proposal if proposal[0] == e else hold('stable effective exposure row')
        next_row = row + direction
        if next_row < lo_row or (next_row > hi_row and b['hi'] <= SHORT_REGION):
            return hold('exposure-resolution-limited: configured row boundary', True)
        # At the top of the measured short region, return to the existing
        # continuous policy. Until then an adaptation must change a row.
        if next_row > hi_row and exposure_rows(proposal[0], quantum) != row:
            return proposal
        result = setting(next_row)
        if exposure_rows(result[0], quantum) == row:
            return hold('exposure-resolution-limited: configured row boundary', True)
        self.stable = False
        self.status = 'bounded effective exposure row probe'
        if self.clip_limit is not None:
            self.clip_limit = result[0]
        return result

    def _decide(self, command, adu, native):
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
            self.clip_clear = 0
            if self.last_settings[1:] != current[1:]:
                self.settle, self.after_gain = (1, True)
                self.clip_limit = self.clip_reference = self.clip_safe = None
                self.clip_bad = self.clip_lower = None
                self.clip_retested = False
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
            return self._clip_reduce(command, current, adu)
        recovery = self._clip_recover(command, current, adu, native, direction)
        if recovery is not None:
            return recovery
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

    def _clip_reduce(self, command, current, adu):
        """Immediate protection; failed probes return to qualified safe data."""
        e, g, _ = current
        b = command['bounds']
        self.clip_retested = self.clip_bad is not None and e >= self.clip_bad - 1e-9
        self.clip_bad = min(self.clip_bad or e, e)
        self.clip_reference = None
        self.clip_clear = 0
        if self.clip_safe and self.clip_safe[0] >= e:
            self.clip_safe = self.clip_lower if self.clip_lower and self.clip_lower[0] < e else None
            self.clip_lower = None  # retain at most one earlier qualified fallback
        if e > b['lo']:
            safe_e = self.clip_safe[0] if self.clip_safe else 0.
            reduced = max(b['lo'], round(max(e * .5, safe_e), 6))
            self.clip_limit = reduced
            self.status = 'clipped: constrain exposure'
            return validate((reduced, g, 1), b)
        self.clip_limit = e
        if g > b['floor']:
            return self.step(command, -1, adu)
        self.status = 'overexposed limit'
        return current

    def _clip_recover(self, command, current, adu, native, direction):
        """Qualified bounded search; no ADU-decline prerequisite for recovery."""
        if command['mode'] == 'day' and command['kind'] == 'normal':
            return None  # No Day clipping boundary or "safe" history to retain.
        e, g, _ = current
        b = command['bounds']
        self.clip_clear = min(2, self.clip_clear + 1)
        if self.clip_clear >= 2:
            if self.clip_safe and self.clip_safe[0] < e:
                self.clip_lower = self.clip_safe
            self.clip_safe = (e, adu)
            if self.clip_bad is not None and e >= self.clip_bad - 1e-9:
                # The formerly clipped setting is now demonstrably safe.
                self.clip_limit = self.clip_bad = self.clip_reference = None
                self.clip_retested = False
                self.clip_clear = 0
        if self.clip_limit is None or direction <= 0:
            return None
        self.qualified = self.stable_count = 0
        if self.clip_clear < 2:
            self.status = 'clipping recovery: qualifying safe feedback'
            return current
        if abs(e - self.clip_limit) <= .0000011:
            self.clip_limit = e  # actual command after shared-storage rounding
        step_max = max(e * 1.25, e + .000001)
        if e < self.clip_limit - .0000011:
            # A previously permitted probe may not have been applied yet.
            probe = min(self.clip_limit, step_max)
        else:
            gap = self.clip_bad - e
            closed = gap <= max(e * .05, .000001)
            if self.clip_reference is None or abs(self.clip_reference['exposure'] - e) > .0000011:
                self.clip_reference = {'exposure': e, 'adu': adu,
                                       'clip': native.get('clip_fraction', 0.)}
            ref = self.clip_reference
            changed = (adu <= ref['adu'] - max(2., b['deviation'] / 4)
                       or ref['clip'] - native.get('clip_fraction', 0.) >= .0025)
            clear = native.get('clip_fraction', 0.) <= .005
            if closed:
                if self.clip_retested and not (clear and changed):
                    self.status = 'clipping-limited: qualified safe boundary'
                    return current
                if not clear:
                    self.status = 'clipping-limited: qualified safe boundary'
                    return current
                # Once per closed bracket, recheck a possibly transient/stale
                # upper bound. A failed check stays closed until conditions change.
                self.clip_retested = True
                probe = min(self.clip_bad + .000001, step_max)
            else:
                probe = min((e + self.clip_bad) * .5, step_max)
        proposal = clamp((min(b['hi'], probe), g, 1), b)
        if proposal == current:
            self.status = 'clipping-limited: exposure grid boundary'
            return current
        self.clip_limit = proposal[0]
        self.clip_clear = 0
        self.clip_reference = None
        self.status = 'clipping recovery: bounded exposure probe'
        return proposal

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

    def __init__(self, config, feedback, shared, hardware_min=1e-06, hardware_max=15.0, readout=None):
        check(feedback is not None and shared is not None, 'missing frame coordination')
        check(shared[0] == 0, 'worker replaced with unresolved QHY frame/fault')
        self.config, self.feedback, self.shared = (config, feedback, shared)
        self.hardware_min, self.hardware_max = (hardware_min, hardware_max)
        self.readout = readout
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

    def drain_shutdown(self, timeout=60.0):
        """Consume only this session's correlated ACK before graceful replacement.

        Camera must already be idle. No proposal is applied during shutdown;
        poll retains all identity checks and is the only successful gate release.
        An unresolved/faulted operation remains latched for the next worker.
        """
        deadline = time.monotonic() + timeout
        if self.pending:
            LOG.info('QHY C shutdown draining sequence=%d', self.sequence)
        while self.pending and not self.fault:
            self.poll(True)
            if not self.pending or self.fault:
                break
            if time.monotonic() >= deadline:
                self.fail('shutdown processing acknowledgement timeout')
                break
            time.sleep(0.05)
        clean = self.pending is None and not self.fault
        LOG.info('QHY C shutdown drain complete=%s shared=%s', clean, self.shared[0])
        return clean

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
            if self.readout:
                command['readout'] = self.readout()
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
            check(not self.readout or c.get('readout') == self.readout(), 'QHY readout changed during pending frame')
            h = hdulist[0].header
            # INDI emits EXPTIME through fits_update_key_dbl(..., 6): six
            # decimals in scientific notation, not six absolute decimal places.
            # Retain the existing 2.1us floor; at 10..15s half a printed unit
            # is 5us. This compares labels, not physical integration accuracy.
            reported_exposure = float(h['EXPTIME'])
            printed_unit = 10. ** (math.floor(math.log10(c['exposure'])) - 6)
            tolerance = max(2.1e-6, printed_unit * .5 + 1e-12)
            check(math.isfinite(reported_exposure) and abs(reported_exposure - c['exposure']) <= tolerance,
                  f"QHY FITS exposure mismatch: requested={c['exposure']!r} reported={reported_exposure!r} tolerance={tolerance!r}")
            check(float(h['GAIN']) == c['gain'] and h.get('XBINNING') == 1
                  and h.get('YBINNING') == 1 and h.get('BAYERPAT') == 'GRBG',
                  f"QHY FITS gain/binning/CFA mismatch: expected gain={c['gain']} bin=1 CFA=GRBG; "
                  f"reported gain={h.get('GAIN')} bin={h.get('XBINNING')}/{h.get('YBINNING')} CFA={h.get('BAYERPAT')}")
            date = datetime.fromisoformat(h['DATE-OBS'].replace('Z', '+00:00'))
            date = date if date.tzinfo else date.replace(tzinfo=timezone.utc)
            stamp = date.timestamp()
            check(self.sent_wall - 1 <= stamp <= time.time() + 1 and (self.last_date is None or stamp > self.last_date), 'stale/repeated QHY timestamp')
            raw = native_statistics(hdulist[0].data, self.config)
            raw.update(fits_exposure=reported_exposure, fits_gain=float(h['GAIN']),
                       fits_date_obs=h['DATE-OBS'], exposure_tolerance=tolerance)
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
        self.reply = {'command': c, 'ok': True, 'proposal': proposal, 'status': status, 'target': self.current_adu_target, 'stable': stable, 'clip_limit': self.policy.clip_limit, 'clip_bad': self.policy.clip_bad,
                      'clip_safe': self.policy.clip_safe[0] if self.policy.clip_safe else None,
                      'exposure_resolution': ({'model': 'not applied', 'reason': 'stale mode'}
                                              if job.get('c_stale_mode') else self.policy.resolution_telemetry)}
        LOG.info('QHY C decision seq=%d ADU=%.3f status=%s target=%.3f stable=%s proposal=%s resolution=%s', c['sequence'], adu, status, self.current_adu_target, stable, proposal, self.reply['exposure_resolution'])
        return (adu, adu if stable else 0.0)

    def sqm_complete(self):
        c = self.context['c_command']
        self.policy.decide(c, 0.0, self.context['c_native'])
        self.reply = {'command': c, 'ok': True, 'proposal': None, 'status': self.policy.status, 'target': c['bounds']['target'], 'stable': False}
