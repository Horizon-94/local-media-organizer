"""Read-only SHA stream hints; no admission, digest or model policy change."""
from contextlib import contextmanager
from pathlib import Path
import ctypes,fcntl,os,sys

@contextmanager
def verification_input(path):
    info={'role':'content_identity_verification','full_sha_required':True,
          'io_priority':'unchanged','cache_hint':'unchanged'}
    with Path(path).open('rb') as stream:
        lib=None;previous=None
        try:
            if sys.platform=='darwin':
                # This is a one-pass scan, not a model input cache. It must not
                # evict model/DB pages while prefetching the rest of a large HDD.
                fcntl.fcntl(stream.fileno(),fcntl.F_NOCACHE,1)
                info['cache_hint']='F_NOCACHE'
                lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
                lib.getiopolicy_np.argtypes=[ctypes.c_int,ctypes.c_int]
                lib.setiopolicy_np.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_int]
                previous=lib.getiopolicy_np(0,1) # DISK, THREAD; never process-wide.
                if previous<0:raise OSError(ctypes.get_errno(),'getiopolicy_np')
                if lib.setiopolicy_np(0,1,4)!=0:raise OSError(ctypes.get_errno(),'setiopolicy_np utility')
                info.update(io_priority='IOPOL_UTILITY',previous_io_policy=previous,scope='THREAD')
            yield stream,info
        finally:
            if lib is not None and previous is not None and previous>=0:
                if lib.setiopolicy_np(0,1,previous)!=0:
                    raise OSError(ctypes.get_errno(),'restore verification I/O policy')
