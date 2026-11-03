"""Pure MIXED drain proofs; no database, model, or runtime side effects.

An adapter must freeze ONE coherent read snapshot, including the entire admitted
identity/revision/kind set, before calling this module. No queue-derived seals.
False means a known open producer; None/missing facts invalidate the snapshot.
Counts deliberately add native and canonical mirrors: overcounting is safe.
No implicit exemptions for failed sources or absent media kinds: provide explicit
seals (including no-audio/no-selection decisions) for every admitted source.
"""
from dataclasses import dataclass

from .model_lifecycle import DrainProof

MODEL_STAGES = ('yoloe', 'openclip', 'insightface', 'qwen', 'ocr', 'whisper', 'embedding')
WORK_STAGES = MODEL_STAGES + ('frontend', 'selection', 'audio_extract', 'vad', 'final_projection')


@dataclass(frozen=True)
class SourceIdentity:
    source_id: str
    revision: str
    kind: str


@dataclass(frozen=True)
class Work:
    stage: str
    pending: int
    running: int
    retry_pending: int
    unpublished: int


@dataclass(frozen=True)
class SourceFacts:
    identity: SourceIdentity
    frontend_sealed: bool
    selection_sealed: bool
    audio_producer_sealed: bool
    final_projection_sealed: bool
    terminal_state: str
    native: tuple[Work, ...]
    canonical: tuple[Work, ...]
    unconsumed_document_events: int
    ready_documents: int


@dataclass(frozen=True)
class MixedFacts:
    admitted: tuple[SourceIdentity, ...]
    discovery_sealed: bool
    sources: tuple[SourceFacts, ...]


def _valid(facts):
    if type(facts) is not MixedFacts or type(facts.discovery_sealed) is not bool:
        return False
    if type(facts.admitted) is not tuple or type(facts.sources) is not tuple:
        return False
    for identity in facts.admitted:
        if (type(identity) is not SourceIdentity or
                type(identity.source_id) is not str or not identity.source_id or
                type(identity.revision) is not str or not identity.revision or
                type(identity.kind) is not str or identity.kind not in ('image', 'video', 'audio')):
            return False
    # A single source/revision cannot be registered under multiple kinds.
    keys = [(s.source_id, s.revision) for s in facts.admitted]
    if len(set(keys)) != len(keys):
        return False
    if any(type(s) is not SourceFacts for s in facts.sources):
        return False
    if any(type(s.identity) is not SourceIdentity for s in facts.sources):
        return False
    if any(any(type(getattr(s.identity, key)) is not str
               for key in ('source_id', 'revision', 'kind')) for s in facts.sources):
        return False
    if (len(facts.sources) != len(facts.admitted) or
            set(s.identity for s in facts.sources) != set(facts.admitted)):
        return False
    for source in facts.sources:
        if any(type(getattr(source, seal)) is not bool for seal in (
                'frontend_sealed', 'selection_sealed', 'audio_producer_sealed',
                'final_projection_sealed')):
            return False
        if source.terminal_state not in ('OPEN', 'SOURCE_COMPLETE', 'SOURCE_FAILED_FINAL'):
            return False
        if any(type(n) is not int or n < 0 for n in (
                source.unconsumed_document_events, source.ready_documents)):
            return False
        for plane in (source.native, source.canonical):
            if type(plane) is not tuple or len(plane) != len(WORK_STAGES):
                return False
            if any(type(w) is not Work or type(w.stage) is not str for w in plane):
                return False
            if {w.stage for w in plane} != set(WORK_STAGES):
                return False
            if any(type(n) is not int or n < 0 for w in plane for n in (
                    w.pending, w.running, w.retry_pending, w.unpublished)):
                return False
    return True


def mixed_drain_proofs(facts: MixedFacts) -> dict[str, DrainProof]:
    """Return existing Manager-compatible proofs; malformed facts fail closed.

    Source identities include revisions; native/canonical work must be attributed
    to registered identities by the snapshot reader. Unknown rows must never be
    filtered away: reject the snapshot or include them to invalidate coverage.
    Document event counts cover ALL unconsumed document-producing event types,
    including candidate/audio artifacts, evidence, and final projections.
    """
    if not _valid(facts):
        return {s: DrainProof(False, True, reason='invalid_or_missing_mixed_facts')
                for s in MODEL_STAGES}
    counts = {s: {k: 0 for k in ('pending', 'running', 'retry_pending', 'unpublished')}
              for s in WORK_STAGES}
    for source in facts.sources:
        for work in source.native + source.canonical:
            for key in counts[work.stage]:
                counts[work.stage][key] += getattr(work, key)

    def quiet(*stages):
        return all(not any(counts[s].values()) for s in stages)

    front = (facts.discovery_sealed and
             all(s.frontend_sealed for s in facts.sources) and quiet('frontend'))
    selection = (front and all(s.selection_sealed for s in facts.sources) and
                 quiet('selection', 'yoloe', 'openclip', 'insightface'))
    audio = (front and all(s.audio_producer_sealed for s in facts.sources) and
             quiet('audio_extract', 'vad'))
    documents = (selection and audio and quiet('qwen', 'ocr', 'whisper', 'final_projection') and
                 all(s.final_projection_sealed and
                     s.terminal_state in ('SOURCE_COMPLETE', 'SOURCE_FAILED_FINAL') and
                     s.unconsumed_document_events == s.ready_documents == 0
                     for s in facts.sources))
    sealed = dict.fromkeys(MODEL_STAGES[:3], front)
    sealed.update(qwen=selection, ocr=selection, whisper=audio, embedding=documents)
    return {s: DrainProof(sealed[s], not sealed[s], **counts[s],
                          downstream_safe=sealed[s],
                          reason=('all_legal_input_paths_sealed' if sealed[s]
                                  else 'legal_upstream_work_possible'))
            for s in MODEL_STAGES}
