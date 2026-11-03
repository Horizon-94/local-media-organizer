"""Inference-only service statistics and remaining-work critical-path ETA."""
from collections import deque
import math


class ProjectCalibration:
    """Persistent, deliberately slow project-level ETA correction.

    Per-stage EWMA remains the source estimate.  This factor only corrects a
    repeated observed project bias after a meaningful terminal-source sample;
    a single short video can never rewrite a large project's forecast.
    """
    def __init__(self, alpha=.2, interval=50, factor=1.0, observations=None):
        self.alpha,self.interval,self.factor=alpha,interval,float(factor)
        self.observations=list(observations or [])[-256:]

    def observe(self, predicted_ms, actual_ms, completed_sources):
        if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in (predicted_ms,actual_ms)):
            return None
        row={'completed_sources':int(completed_sources),'predicted_elapsed_ms':float(predicted_ms),
             'actual_elapsed_ms':float(actual_ms),'ratio':float(actual_ms)/float(predicted_ms),
             'factor_before':self.factor,'applied':False}
        self.observations.append(row);self.observations=self.observations[-256:]
        if completed_sources < self.interval or completed_sources % self.interval:
            return row
        # Median of the last interval resists one long source poisoning ETA.
        ratios=sorted(x['ratio'] for x in self.observations[-self.interval:])
        ratio=ratios[len(ratios)//2]
        self.factor=(1-self.alpha)*self.factor+self.alpha*ratio
        row.update(calibration_ratio=ratio,factor_after=self.factor,applied=True)
        return row

    def display(self, raw_ms):
        return None if raw_ms is None else raw_ms*self.factor

    def confidence(self, completed_sources, service_samples):
        if completed_sources >= 100 and service_samples >= 100:return 'HIGH'
        if completed_sources >= 20 and service_samples >= 20:return 'MEDIUM'
        return 'LOW'

    def state(self):
        return {'factor':self.factor,'interval':self.interval,'observations':self.observations}


class ServiceStats:
    def __init__(self, alpha=.2):
        if not 0 < alpha <= 1:
            raise ValueError('ewma_alpha_invalid')
        self.alpha = alpha
        self.values = deque(maxlen=100)
        self.ewma = None
        self.completed_count = 0
        self.cold_start_ms = None

    def observe(self, elapsed_ms, items=1, *, diagnostic=False):
        if diagnostic:
            return  # footprint/vmmap diagnostics cannot calibrate throughput.
        if not math.isfinite(elapsed_ms) or elapsed_ms < 0 or items < 1:
            raise ValueError('invalid_service_observation')
        cost = elapsed_ms / items  # true batch throughput, not batch latency.
        self.ewma = cost if self.ewma is None else self.alpha*cost+(1-self.alpha)*self.ewma
        self.values.append(cost)
        self.completed_count += items

    def summary(self):
        values = list(self.values)
        ordered = sorted(values)
        def percentile(q):
            if not ordered:
                return None
            return ordered[min(len(ordered)-1, math.ceil(q*len(ordered))-1)]
        return dict(cold_start_ms=self.cold_start_ms, EWMA_ms_per_item=self.ewma,
            rolling_20_mean=sum(values[-20:])/len(values[-20:]) if values else None,
            rolling_100_mean=sum(values)/len(values) if values else None,
            P50=percentile(.5), P95=percentile(.95), completed_count=self.completed_count)


def remaining_ms(stats, remaining, workers):
    if remaining == 0:
        return 0
    if remaining < 0 or workers < 1 or stats.ewma is None:
        return None
    return remaining * stats.ewma / workers


def critical_path_eta(stage_times):
    """Independent visual evidence forks; visual/audio join at shared index.

    Returns UNKNOWN until all nonempty stage costs are calibrated. Bounds use
    measured service P50/P95 supplied separately by the caller, not fake ±%.
    This is a work estimate, not percent-complete linear extrapolation.
    """
    required = ('frame_basics', 'yoloe', 'openclip', 'insightface',
                'selection', 'qwen', 'ocr', 'person_local', 'vad', 'whisper',
                'embedding', 'index')
    if any(stage_times.get(key) is None for key in required):
        return None
    pre = max(stage_times[key] for key in ('frame_basics', 'yoloe', 'openclip', 'insightface'))
    visual = pre + stage_times['selection'] + max(stage_times['qwen'], stage_times['ocr'], stage_times['person_local'])
    audio = stage_times['vad'] + stage_times['whisper']
    # Embedding is shared (one worker): total queued embedding work counts once.
    return max(visual, audio) + stage_times['embedding'] + stage_times['index']


def source_priority(remaining_work_ms, waiting_seconds, aging_ms_per_second=1000):
    return remaining_work_ms - max(0, waiting_seconds)*aging_ms_per_second
