"""Admission-only recovery policy. Never preempts owned inference or commits."""
from dataclasses import dataclass, field
import math
import threading
import time


@dataclass
class ResourceGovernor:
    tier: int = 0
    healthy_samples: int = 0
    last_timestamp: float | None = None
    actions: list[dict] = field(default_factory=list)
    PAUSED = {0: set(), 1: {'embedding'}, 2: {'embedding', 'whisper'},
              3: {'embedding', 'whisper', 'qwen'}}

    def __post_init__(self):
        self.lock = threading.RLock()
        self.valid = False
        self.invalid_reason = 'RESOURCE_TELEMETRY_NOT_READY'
        self.windows = {}
        self.sample = None
        self.whisper_probe = False
        self.whisper_probe_active = False
        self.whisper_probe_finished = None
        self.critical_since = None

    def _reset(self):
        self.windows.clear()
        self.healthy_samples = 0
        self.whisper_probe = False

    def _window(self, name, eligible, timestamp, count, span):
        if not eligible:
            self.windows.pop(name, None)
            return False
        start, n = self.windows.get(name, (timestamp, 0))
        self.windows[name] = (start, n + 1)
        if name == 'normal':
            self.healthy_samples = n + 1
        return n + 1 >= count and timestamp - start >= span

    def update(self, sample: dict | None, now=None) -> int:
        with self.lock:
            return self._update(sample, time.time() if now is None else now)

    def _update(self, sample, now):
        finite = lambda v: type(v) in (int, float) and math.isfinite(v)
        timestamp = sample.get('timestamp') if isinstance(sample, dict) else None
        pressure = sample.get('memory_pressure_level') if isinstance(sample, dict) else None
        rate = sample.get('swap_growth_rate_bytes_per_second') if isinstance(sample, dict) else None
        sustained = sample.get('sustained_swap_growth') if isinstance(sample, dict) else None
        reason = None
        if not finite(timestamp) or not finite(pressure) or pressure not in (1, 2, 4) or not finite(rate) or rate < 0 or type(sustained) is not bool:
            reason = 'RESOURCE_TELEMETRY_UNAVAILABLE'
        elif not 0 <= now - timestamp <= 10:
            reason = 'RESOURCE_TELEMETRY_STALE_OR_FUTURE'
        elif self.last_timestamp is not None and timestamp < self.last_timestamp:
            reason = 'RESOURCE_TELEMETRY_NON_MONOTONIC'
        if reason:
            self.valid = False
            self.invalid_reason = reason
            self._reset()
            self.critical_since = None
            return self.tier
        if timestamp == self.last_timestamp:
            return self.tier  # cached sample cannot restore an invalid window
        if self.last_timestamp is not None and timestamp - self.last_timestamp > 10:
            self._reset()
            self.critical_since = None
            self.last_timestamp = timestamp
            self.valid = False
            self.invalid_reason = 'RESOURCE_TELEMETRY_INVALID_GAP'
            return self.tier
        self.valid = True
        self.invalid_reason = None
        self.last_timestamp = timestamp
        self.sample = dict(sample)
        self.critical_since = (self.critical_since if self.critical_since is not None else timestamp) if pressure == 4 else None
        desired = 3 if pressure == 4 or rate >= 64 * 1024**2 else 2 if pressure == 2 or rate >= 24 * 1024**2 else 1 if sustained else 0
        if desired > self.tier:
            self._set(desired, sample, 'pressure_escalation')
            self._reset()
        normal = self._window('normal', desired == 0, timestamp, 3, 10)
        warn = self._window('qwen_warn', pressure == 2 and rate < 24 * 1024**2 and not sustained, timestamp, 7, 30)
        self._window('reload', pressure == 1 and rate < 8 * 1024**2 and not sustained, timestamp, 7, 30)
        low = pressure == 2 and rate < 8 * 1024**2 and not sustained
        ready = self._window('whisper_warn', low, timestamp, 13, 60)
        post = self._window('whisper_post', low and self.whisper_probe_finished is not None and timestamp > self.whisper_probe_finished, timestamp, 7, 30)
        if not low:
            self.whisper_probe = False
        elif not self.whisper_probe_active and ready and (self.whisper_probe_finished is None or post):
            self.whisper_probe = True
        if self.tier and normal:
            self._set(self.tier - 1, sample, 'pressure_recovered')
            self.windows.pop('normal', None)
            self.healthy_samples = 0
        elif self.tier == 3 and warn:
            self._set(2, sample, 'stable_warn_qwen_recovery')
        elif desired != 0:
            self.healthy_samples = 0
        return self.tier

    def reload_ready(self):
        with self.lock:
            start, count = self.windows.get('reload', (0, 0))
            return self.valid and count >= 7 and self.last_timestamp - start >= 30

    def claimed(self, stage):
        with self.lock:
            if stage == 'whisper' and self.tier == 2:
                self.whisper_probe = False
                self.whisper_probe_active = True

    def completed(self, stage):
        with self.lock:
            if stage == 'whisper' and self.whisper_probe_active:
                self.whisper_probe_active = False
                self.whisper_probe_finished = self.last_timestamp
                self.windows.pop('whisper_post', None)

    def _set(self, tier, sample, reason):
        before = self.tier
        self.tier = tier
        self.actions.append({'timestamp': sample['timestamp'], 'decided_at': time.time(), 'from_tier': before, 'to_tier': tier,
                             'reason': reason, 'memory_pressure_level': sample['memory_pressure_level'],
                             'swap_growth_rate_bytes_per_second': sample['swap_growth_rate_bytes_per_second'],
                             'action': ('PAUSE_' if tier > before else 'RESUME_') + ','.join(sorted(self.PAUSED[tier] ^ self.PAUSED[before]))})
        self.actions = self.actions[-128:]

    def reason_for(self, stage, resident=False):
        with self.lock:
            if not self.valid:
                return self.invalid_reason
            if self.sample and self.sample['memory_pressure_level'] == 4:
                return 'MEMORY_PRESSURE_CRITICAL'
            if self.sample and self.sample.get('sustained_swap_growth'):
                return 'SUSTAINED_SWAP_GROWTH'
            if self.sample and self.sample.get('blocked_reason'):
                return self.sample['blocked_reason']
            if stage == 'whisper' and self.tier == 2 and resident and self.whisper_probe and not self.whisper_probe_active:
                return None
            return 'RESOURCE_GOVERNOR_PAUSE_' + stage.upper() if stage in self.PAUSED[self.tier] else None

    def snapshot(self):
        with self.lock:
            return {'tier': self.tier, 'paused_new_claims': sorted(self.PAUSED[self.tier]),
                    'healthy_samples': self.healthy_samples, 'actions': list(self.actions),
                    'telemetry_valid': self.valid, 'telemetry_reason': self.invalid_reason,
                    'recovery_windows': dict(self.windows), 'whisper_probe': self.whisper_probe,
                    'whisper_probe_active': self.whisper_probe_active}


# Frozen deferred contracts. No current Governor/claim/residency call path uses
# these declarations; integrating a future consumer must preserve this ABI.
from enum import Enum

DEFERRED_INTERFACE_VERSION = 1

class ModelResidencyAction(str, Enum):
    ALLOW = 'ALLOW'
    PAUSE_NEW_CLAIMS = 'PAUSE_NEW_CLAIMS'
    REQUEST_UNLOAD = 'REQUEST_UNLOAD'

@dataclass(frozen=True)
class GovernorDecisionSnapshot:
    observed_at: float
    generation: int
    memory_pressure_level: int | None
    swap_bytes: int | None
    swap_growth_rate: float | None
    swap_trend: str | None
    compressed_memory: int | None
    reclaim_state: str | None
    model_or_stage: str
    claim_allowed: bool
    current_reason: str | None
