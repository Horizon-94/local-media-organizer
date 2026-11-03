"""Shared heavy-model budget; no model loading, task partition or preemption."""
from collections import deque
import math
import time


DEFAULT_HEAVY_BUDGET = {
    'headroom_mb': 3072,
    'ceilings': {'qwen': 3, 'whisper': 2},
    # Single/dual/triple real-model evidence permits Qwen scaling. Whisper's
    # repeated semantic mismatch keeps its production selection at one.
    'validated_limits': {'qwen': 3, 'whisper': 1},
    # Conservative activation envelopes, not sums of resident Python RSS.
    'activation_mb': {'qwen': 6144, 'whisper': 3072},
    'warm_job_mb': {'qwen': 1024, 'whisper': 1024},
    'scale_interval_seconds': 15,
    'fair_wait_seconds': 15,
    'swap_growth_mb_per_minute': 256,
}


class HeavyAdmissionController:
    """Decision state is owned by one scheduler; leases remain its authority.

    Active memory reservations are provisional envelopes. System available
    memory already reflects resident models, so warm requests reserve only
    incremental working memory instead of charging model weights again.
    """
    def __init__(self, policy=None, *, clock=time.monotonic):
        import copy
        self.policy = copy.deepcopy(DEFAULT_HEAVY_BUDGET)
        for key, value in (policy or {}).items():
            if key not in self.policy:raise ValueError('heavy_admission_policy_unknown:'+key)
            if isinstance(value, dict):
                if not isinstance(self.policy[key],dict) or set(value)-set(self.policy[key]):
                    raise ValueError('heavy_admission_policy_invalid:'+key)
                self.policy[key].update(value)
            else:self.policy[key] = value
        for stage, maximum in DEFAULT_HEAVY_BUDGET['ceilings'].items():
            ceiling=self.policy['ceilings'][stage]
            validated=self.policy['validated_limits'][stage]
            if (type(ceiling) is not int or not 1<=ceiling<=maximum or
                    type(validated) is not int or not 1<=validated<=ceiling or
                    validated>DEFAULT_HEAVY_BUDGET['validated_limits'][stage]):
                raise ValueError('heavy_admission_worker_limit_invalid:'+stage)
            for key in ('activation_mb','warm_job_mb'):
                v=self.policy[key][stage]
                if type(v) not in (int,float) or not math.isfinite(v) or v<=0:
                    raise ValueError('heavy_admission_memory_invalid:'+stage)
        for key in ('headroom_mb','scale_interval_seconds','fair_wait_seconds','swap_growth_mb_per_minute'):
            v=self.policy[key]
            if type(v) not in (int,float) or not math.isfinite(v) or v<=0:
                raise ValueError('heavy_admission_budget_invalid:'+key)
        if self.policy['headroom_mb']<DEFAULT_HEAVY_BUDGET['headroom_mb']:
            raise ValueError('heavy_admission_headroom_below_safety_reserve')
        self.clock = clock
        self.selected = {stage: 1 for stage in self.policy['ceilings']}
        self.last_scale = {stage: clock() for stage in self.selected}
        self.completed = {stage: 0 for stage in self.selected}
        self.waiting_since = {}
        self.wait_times = {stage: [] for stage in self.selected}
        self.swap_samples = deque(maxlen=128)
        self.last_decisions = {}

    def completion(self, stage):
        if stage in self.completed:self.completed[stage] += 1

    def clear_wait(self, stage):
        self.waiting_since.pop(stage, None)

    def granted(self, stage):
        if stage in self.selected:
            self.wait_times[stage].append(self.clock()-self.waiting_since.pop(stage,self.clock()))

    def observe_swap(self,now,swap):
        if not self.swap_samples or now > self.swap_samples[-1][0]:
            self.swap_samples.append((now,swap))
        while len(self.swap_samples)>1 and now-self.swap_samples[0][0]>60:
            self.swap_samples.popleft()
        span=now-self.swap_samples[0][0]
        return max(0.,swap-self.swap_samples[0][1])*60/span if span>=10 else 0.

    def decide(self, stage, pressure, active, *, resident_ready=False):
        if stage not in self.selected:return None
        now = self.clock()
        self.waiting_since.setdefault(stage, now)
        required = float(self.policy['warm_job_mb' if resident_ready else 'activation_mb'][stage])
        reserve = float(self.policy['headroom_mb'])
        try:
            available = float(pressure['available_mb'])
            level = int(pressure['pressure_level'])
            swap = float(pressure['swap_used_mb'])
            if not all(math.isfinite(v) and v >= 0 for v in (available, swap, required, reserve)):
                raise ValueError('invalid_memory_sample')
            if not pressure.get('healthy'):raise ValueError('memory_probe_failed')
        except (KeyError, TypeError, ValueError):
            return {'reason': 'heavy_memory_probe_unavailable', 'selected_limit': self.selected[stage]}
        growth=self.observe_swap(now,swap)
        active_count = sum(item['stage']==stage for item in active)
        reserved = sum(float(item['memory_mb']) for item in active)
        unsafe = level >= 4 or growth > float(self.policy['swap_growth_mb_per_minute'])
        fits = available-reserved-required >= reserve
        activation_fits=available-reserved-float(self.policy['activation_mb'][stage])>=reserve
        limit = min(self.policy['ceilings'][stage], self.policy['validated_limits'][stage])
        grow_due=(self.completed[stage]>=3 and self.selected[stage]<limit and
                  now-self.last_scale[stage]>=float(self.policy['scale_interval_seconds']))
        scale_up_blocked_reason='cold_activation_headroom' if grow_due and not activation_fits else None
        reason = None
        # A busy pool at its selected limit needs no additional activation.
        # Do not confuse a hypothetical cold worker with insufficient memory
        # for current work, reset its growth clock, or unload unrelated models.
        if unsafe or available<reserve:
            self.selected[stage] = max(1, self.selected[stage]-1)
            self.last_scale[stage] = now
            reason = 'heavy_memory_pressure' if unsafe else 'heavy_memory_headroom'
        else:
            if grow_due and activation_fits:
                self.selected[stage] += 1
                self.last_scale[stage] = now
                self.completed[stage] = 0
            if active_count>=self.selected[stage]:reason = 'heavy_selected_capacity'
            elif not fits:
                self.selected[stage]=max(1,self.selected[stage]-1)
                self.last_scale[stage]=now
                reason='heavy_memory_headroom'
            oldest=min(self.waiting_since,key=self.waiting_since.get)
            if (reason is None and oldest!=stage and
                    now-self.waiting_since[oldest]>=float(self.policy['fair_wait_seconds']) and
                    not any(item['stage']==oldest for item in active)):
                self.selected[stage]=max(1,self.selected[stage]-1)
                reason='heavy_memory_fair_turn'
        diagnostics = {'reason': reason, 'stage': stage, 'selected_limit': self.selected[stage],
            'maximum_limit': self.policy['ceilings'][stage], 'validated_limit': self.policy['validated_limits'][stage],
            'active_worker_count': active_count, 'available_mb': available, 'required_mb': required,
            'reserved_mb': reserved, 'headroom_target_mb': reserve, 'pressure_level': level,
            'scale_up_blocked_reason':scale_up_blocked_reason,
            'swap_used_mb': swap, 'swap_growth_mb_per_minute': growth, 'resident_ready': resident_ready}
        self.last_decisions[stage]=diagnostics
        return diagnostics
