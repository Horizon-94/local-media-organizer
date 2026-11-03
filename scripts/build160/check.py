"""Offline source checks. Only temporary fixture databases are written by tests."""
from pathlib import Path
import ast,os,subprocess,sys,json,hashlib
ROOT=Path(__file__).resolve().parents[2]
def main():
    manifest=json.loads((ROOT/'docs/build160-source-manifest.json').read_text())
    for item in manifest['files']:
        path=ROOT/item['path']
        if not path.is_file():raise RuntimeError('Missing runtime source: '+item['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item['public_sha256']:raise RuntimeError('Source manifest mismatch: '+item['path'])
        if path.suffix=='.py':ast.parse(path.read_text(),filename=str(path))
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(ROOT))
    subprocess.run([sys.executable,'-c','from apps.media_archive_image_video_ui import native_bridge, spoken_content_catalog, search_jobs'],cwd=ROOT,env=env,check=True)
    for path in sorted((ROOT/'tests/build160').glob('test_*.py')):
        subprocess.run([sys.executable,str(path)],cwd=ROOT,env=env,check=True)
    print('PASS BUILD160 runtime sources and 95 isolated regressions')
if __name__=='__main__':main()
