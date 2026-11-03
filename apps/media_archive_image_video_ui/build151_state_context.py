"""P0 state boundary. No product imports or filesystem writes."""
import os
from pathlib import Path
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar

TEST_ROOT = Path(os.environ.get('MEDIA_ARCHIVE_TEST_ROOT', str(Path.home() / 'Library/Caches/LocalMediaOrganizer/tests'))).expanduser().resolve()
PRODUCTION_ROOT = Path.home() / 'Library/Application Support/素材大整理/1.3.2-build151'
KEYS = dict(kind='MEDIA_ARCHIVE_CONTEXT_KIND', state_root='MEDIA_ARCHIVE_APP_STATE_ROOT', artifact_root='MEDIA_ARCHIVE_ARTIFACT_ROOT', run_id='MEDIA_ARCHIVE_RUN_ID')
class ContextError(RuntimeError):
    def __init__(self, code):
        self.reason_code = code
        super().__init__(code)

def inside(path, parent):
    return path == parent or parent in path.parents

@dataclass(frozen=True)
class StateContext:
    kind: str
    state_root: Path
    artifact_root: Path
    run_id: str

    def guard(self, path):
        if self.kind == 'PRODUCTION':
            return Path(path)
        p = Path(path).resolve()
        if self.kind == 'TEST':
            if not inside(p, self.artifact_root) or not inside(p, TEST_ROOT.resolve()):
                raise ContextError('TEST_PATH_OUTSIDE_ROOT')
        return p

    def env(self):
        return {KEYS[k]: str(getattr(self, k)) for k in KEYS}

    def identity(self):
        return {'origin': self.kind, 'context': self.kind, 'run_id': self.run_id}

def resolve_context(*, kind=None, state_root=None, artifact_root=None, run_id=None, env=None, validation=False):
    env = os.environ if env is None else env
    args = dict(kind=kind, state_root=state_root, artifact_root=artifact_root, run_id=run_id)
    for key, value in args.items():
        other = env.get(KEYS[key])
        if value is not None and other is not None and str(value) != other:
            raise ContextError('CONTEXT_ENV_CONFLICT')
        args[key] = value if value is not None else other
    kind = args['kind']
    if validation and kind != 'TEST':
        raise ContextError('EXPLICIT_TEST_CONTEXT_REQUIRED')
    if kind is None:
        if args['artifact_root'] or args['run_id']:
            raise ContextError('CONTEXT_KIND_REQUIRED')
        kind = 'PRODUCTION'
    if kind not in ('TEST', 'PRODUCTION'):
        raise ContextError('INVALID_CONTEXT_KIND')
    if kind == 'TEST':
        if not all(args.values()):
            raise ContextError('TEST_CONTEXT_INCOMPLETE')
        rid = str(args['run_id'])
        if not rid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in rid):
            raise ContextError('INVALID_RUN_ID')
        artifact = Path(args['artifact_root']).resolve()
        state = Path(args['state_root']).resolve()
        # Each run owns exactly one subtree; state and artifacts cannot alias other runs.
        expected = TEST_ROOT / rid
        if TEST_ROOT.resolve() != TEST_ROOT or expected.resolve() != expected or artifact != expected or state != artifact / 'state':
            raise ContextError('INVALID_TEST_ROOT')
        return StateContext(kind, state, artifact, rid)
    return StateContext(kind, Path(args['state_root'] or PRODUCTION_ROOT).resolve(), Path(args['artifact_root'] or PRODUCTION_ROOT).resolve(), str(args['run_id'] or 'production'))

_current = ContextVar('build151_state_context', default=None)
def current():
    ctx = _current.get()
    if ctx is None:
        return resolve_context()
    # Reject environment changes and mixed child contexts instead of silently selecting one.
    resolve_context(**{k: getattr(ctx, k) for k in KEYS})
    return ctx

@contextmanager
def use_context(ctx):
    verified = resolve_context(**{k: getattr(ctx, k) for k in KEYS}, env={})
    token = _current.set(verified)
    try:
        yield verified
    finally:
        _current.reset(token)
