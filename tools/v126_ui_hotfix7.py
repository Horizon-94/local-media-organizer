"""Small reversible installed-App hotfix; no full bundle copy, media or DB writes."""
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'test-output/v126-hotfix-ui7b-20260828'
APP = Path('/Applications/本地数据库 1.2.6.app')
MODULE = 'Contents/Resources/media_archive_image_video_ui/'
BIN = 'Contents/MacOS/本地数据库 1.2.6'
INFO = 'Contents/Info.plist'
IDENTITY = 'Contents/Resources/release_source_identity.json'
SEAL = 'Contents/_CodeSignature/CodeResources'
SOURCES = {MODULE + name: ROOT / 'apps/media_archive_image_video_ui_v125_candidate' / name
           for name in ('native_frontend.swift',)}
FILES = [BIN, INFO, IDENTITY, SEAL, *SOURCES]
HOTFIX = 'v126-ui-hotfix-20260828-07b'
HOTFIX_LABEL = '搜索界面热修复7'
IDENTITY_SCOPE = 'base_release_identity_plus_ui_hotfix7'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args, timeout=30):
    print(json.dumps([str(a) for a in args], ensure_ascii=False), flush=True)
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    print(result.stdout + result.stderr, flush=True)
    result.check_returncode()
    return result.stdout.strip()


def check_app(closed=False):
    info = plistlib.loads((APP / INFO).read_bytes())
    assert info['CFBundleShortVersionString'] == '1.2.6'
    assert info['CFBundleIdentifier'] == 'local.horizon.local-database.release'
    for name in FILES:
        target = APP / name
        assert target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(APP.resolve())
    if closed:
        processes = subprocess.check_output(['ps', '-axo', 'comm='], text=True)
        assert str(APP) + '/' not in processes, 'Close the App normally before install'
    return info


def atomic_copy(source, destination):
    with tempfile.NamedTemporaryFile(prefix='.hotfix7-', dir=destination.parent, delete=False) as f:
        temporary = Path(f.name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)  # This function's temporary file only.


def prepare():
    check_app()
    assert not (WORK / 'prepared.json').exists(), 'Never overwrite rollback baseline'
    run(['codesign', '--verify', '--deep', '--strict', str(APP)])
    before = {}
    for name in FILES:
        dest = WORK / 'rollback' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # A failed compile may be retried, never replace its rollback copy.
            assert sha(dest) == sha(APP / name), 'Rollback/App baseline changed'
        else:
            shutil.copy2(APP / name, dest)
        before[name] = sha(dest)
    for name, source in SOURCES.items():
        target = WORK / 'staged' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    compiler = run(['xcrun', '--find', 'swiftc'])
    sdk = run(['xcrun', '--show-sdk-path'])
    arches = run(['lipo', '-archs', str(APP / BIN)]).split()
    assert set(arches) == {'arm64', 'x86_64'}
    binary = WORK / 'staged' / BIN
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='v126-hotfix7-') as folder:
        parts = []
        for arch in arches:
            target = Path(folder) / arch
            run([compiler, '-swift-version', '5', '-O', '-target', f'{arch}-apple-macosx12.0',
                 '-sdk', sdk, '-module-cache-path', str(ROOT / '.tmp-swift-cache'),
                 '-framework', 'SwiftUI', '-framework', 'AppKit', '-framework', 'AVFoundation',
                 '-framework', 'AVKit', '-framework', 'PDFKit', '-framework', 'ImageIO',
                 str(WORK / 'staged' / (MODULE + 'native_frontend.swift')), '-o', str(target)], timeout=180)
            parts.append(str(target))
        run(['lipo', '-create', *parts, '-output', str(binary)])
    binary.chmod(0o755)
    payload = dict(hotfix=HOTFIX, before=before, staged={name: sha(WORK / 'staged' / name) for name in [BIN, *SOURCES]})
    (WORK / 'prepared.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def restore(prepared):
    assert all(sha(WORK / 'rollback' / name) == digest for name, digest in prepared['before'].items())
    for name in FILES:
        atomic_copy(WORK / 'rollback' / name, APP / name)
    run(['codesign', '--verify', '--deep', '--strict', str(APP)])
    assert all(sha(APP / name) == digest for name, digest in prepared['before'].items())


def refresh_sources():
    """Refresh tested Python changes without recompiling or replacing rollback baselines."""
    check_app()
    assert not (WORK/'installed.json').exists()
    prepared = json.loads((WORK/'prepared.json').read_text())
    assert all(sha(APP/name) == digest for name,digest in prepared['before'].items())
    swift = MODULE+'native_frontend.swift'
    assert sha(SOURCES[swift]) == prepared['staged'][swift], 'Swift changed; needs a fresh build'
    for name,source in SOURCES.items():
        if name == swift:
            continue
        assert name.endswith('.py')
        if name not in prepared['before']:
            baseline = WORK/'rollback'/name
            assert not baseline.exists()
            baseline.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(APP/name,baseline)
            prepared['before'][name] = sha(baseline)
        dest = WORK/'staged'/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,dest)
        prepared['staged'][name] = sha(dest)
    (WORK/'prepared.json').write_text(json.dumps(prepared,ensure_ascii=False,indent=2))


def install():
    info = check_app(closed=True)
    prepared = json.loads((WORK / 'prepared.json').read_text())
    assert not (WORK / 'installed.json').exists()
    assert all(sha(APP / name) == digest for name, digest in prepared['before'].items()), 'App baseline changed'
    assert all(sha(WORK / 'staged' / name) == digest for name, digest in prepared['staged'].items())
    assert all(sha(source) == prepared['staged'][name] for name, source in SOURCES.items())
    now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    identity = json.loads((APP / IDENTITY).read_text())
    identity['previous_hotfix'] = identity.get('hotfix')
    identity['base_content_sha256'] = identity['content_sha256']
    identity['hotfix'] = dict(id=HOTFIX, installed_at=now, staged=prepared['staged'])
    identity['identity_scope'] = IDENTITY_SCOPE
    identity['content_sha256'] = hashlib.sha256(json.dumps([identity['base_content_sha256'], identity['hotfix']], sort_keys=True).encode()).hexdigest()
    info.update(HorizonHotfixID=HOTFIX, HorizonHotfixLabel=HOTFIX_LABEL, HorizonBuildDate=now,
                HorizonSourceContentSHA256=identity['content_sha256'])
    (WORK / 'staged' / INFO).write_bytes(plistlib.dumps(info))
    (WORK / 'staged' / IDENTITY).write_text(json.dumps(identity, ensure_ascii=False, indent=2))
    try:
        for name in FILES:
            if name != SEAL:
                atomic_copy(WORK / 'staged' / name, APP / name)
        run(['codesign', '--force', '--sign', '-', str(APP)])
        run(['codesign', '--verify', '--deep', '--strict', str(APP)])
        assert all(sha(APP / name) == sha(source) for name, source in SOURCES.items())
    except BaseException:
        restore(prepared)
        raise
    (WORK / 'installed.json').write_text(json.dumps(dict(status='PASS', hotfix=HOTFIX,
        version='1.2.6', files={name: sha(APP / name) for name in FILES}, database_written=False,
        original_media_written=False, models_run=False), ensure_ascii=False, indent=2))


def main():
    mode = sys.argv[1]
    assert mode in {'prepare', 'install', 'rollback', 'refresh-sources'}
    WORK.mkdir(parents=True, exist_ok=True)
    log = ROOT / 'logs' / (HOTFIX + '-' + mode + '.log')
    if log.exists():
        log = log.with_name(log.stem + '-' + datetime.datetime.now().strftime('%H%M%S') + '.log')
    log.with_suffix('.pid').write_text(str(os.getpid()))
    code = 0
    with log.open('x') as handle, contextlib.redirect_stdout(handle), contextlib.redirect_stderr(handle):
        try:
            if mode == 'prepare': prepare()
            elif mode == 'install': install()
            elif mode == 'refresh-sources': refresh_sources()
            else:
                check_app(closed=True)
                installed = json.loads((WORK / 'installed.json').read_text())
                assert all(sha(APP / name) == digest for name, digest in installed['files'].items())
                restore(json.loads((WORK / 'prepared.json').read_text()))
            print('PASS')
        except BaseException:
            traceback.print_exc()
            code = 1
    log.with_suffix('.exit').write_text(str(code))
    print(json.dumps(dict(mode=mode, exit_code=code, log=str(log), pid=str(log.with_suffix('.pid')))))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
