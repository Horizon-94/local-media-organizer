"""Explicit opt-in video experiment; never changes the image pipeline."""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class VideoProfile:
    frame_basics: int = 2
    yoloe: int = 1
    openclip: int = 1
    openclip_batch: int = 8
    insightface: int = 1
    ocr: int = 1
    qwen: int = 1
    vad: int = 1
    whisper: int = 1
    embedding: int = 1
    source_reader: int = 1
    person_local: int = 1
    spool_hard_bytes: int = 8*1024**3

    def validate(self):
        if not 2 <= self.frame_basics <= 4 or not 1 <= self.yoloe <= 2 or not 1 <= self.ocr <= 2:
            raise ValueError('worker_profile_out_of_declared_bounds')
        for stage in ('openclip','insightface','qwen','vad','whisper','embedding','source_reader','person_local'):
            if getattr(self,stage) != 1:
                raise ValueError('single_worker_required:'+stage)
        if not 1 <= self.openclip_batch <= 8 or self.spool_hard_bytes <= 0:
            raise ValueError('bounded_batch_and_spool_required')
        return self


def enabled(environment=None):
    environment=os.environ if environment is None else environment
    return environment.get('VIDEO_PIPELINE_V2_STREAMING')=='1'
