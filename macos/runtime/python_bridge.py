import os,sys
from pathlib import Path
contents=Path(__file__).resolve().parents[1]
pipeline=contents/'Resources'/'Pipeline'
os.environ.setdefault('MEDIA_ARCHIVE_APP_STATE_ROOT',str(Path.home()/'Library/Application Support/素材大整理/1.3.2-build151'))
os.environ.setdefault('MEDIA_ARCHIVE_APP_CONTENTS',str(contents))
os.environ.setdefault('MEDIA_ARCHIVE_PROJECT_ROOT',str(pipeline))
os.environ.setdefault('MEDIA_ARCHIVE_ENV_ROOT',str(contents/'Resources'/'RuntimeEnvs'))
os.environ.setdefault('PYTHONDONTWRITEBYTECODE','1')
sys.path.insert(0,str(pipeline))
import importlib.abc
class _NoLegacyProcessing(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.rsplit('.',1)[-1] in {'product_execution','v2_product_driver','scheduler_switching'}:
            raise RuntimeError('BUILD149_PROCESSING_DISABLED:'+fullname)
        return None
sys.meta_path.insert(0,_NoLegacyProcessing())
from apps.media_archive_image_video_ui.native_bridge import main
raise SystemExit(main(sys.argv[1:]))
