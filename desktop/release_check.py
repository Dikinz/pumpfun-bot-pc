"""Release check at window start: app/release_guard.verify(ROOT) unchanged, plus the same EXTRA_MODULE
rule for desktop/**/*.py (release_guard only covers app/). release_guard is stdlib-only and loaded from app/."""
import importlib.util,json,sys
from pathlib import Path
from . import constants as C

def release_guard(root=None):
    app=Path(root or C.ROOT).resolve()/'app';path=app/'release_guard.py'
    module=sys.modules.get('release_guard')
    if module is not None and Path(getattr(module,'__file__','')).resolve()==path.resolve():return module
    spec=importlib.util.spec_from_file_location('release_guard',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def extra_modules(root,files):
    """desktop/**/*.py files (outside __pycache__) that the manifest does not list."""
    root=Path(root).resolve();extra=[]
    for path in sorted((root/'desktop').rglob('*.py')):
        rel=path.relative_to(root)
        if '__pycache__' in rel.parts:continue
        if rel.as_posix() not in files:extra.append(rel.as_posix())
    return extra

def check_desktop(root,manifest=None):
    root=Path(root).resolve()
    if manifest is None:
        try:manifest=json.loads((root/'BUILD_MANIFEST.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):raise RuntimeError('CLEAN_RELEASE_MANIFEST_MISSING: extract the complete ZIP into a new folder')
    files=manifest.get('files') if isinstance(manifest,dict) else None
    if not isinstance(files,dict):raise RuntimeError('CLEAN_RELEASE_EMPTY_MANIFEST')
    extra=extra_modules(root,files)
    if extra:raise RuntimeError('DESKTOP_EXTRA_MODULE: '+extra[0])
    return manifest

def verify(root=None):
    """Full check the window runs before anything else; RuntimeError text is shown to the user."""
    root=Path(root or C.ROOT).resolve()
    manifest=release_guard(root).verify(root)
    return check_desktop(root,manifest)
