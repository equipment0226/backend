"""Local test downloads only; never a source for extraction or legal decisions."""
import json

from . import store


def current(case_name=None):
    options = [store.ROOT / 'examples' / name for name in ('court_ready_fixture', 'ocr_retest', 'fresh_start')]
    selected = []
    for folder in options:
        manifest = folder / 'manifest.json'
        if manifest.is_file():
            metadata = json.loads(manifest.read_text(encoding='utf-8'))
            if not case_name or metadata['name'] == case_name:
                selected.append((folder, metadata))
    if not selected:
        raise FileNotFoundError('No matching synthetic materials for this case.')
    folder, metadata = selected[0]
    submission = folder.parent / (folder.name + '_submission.zip')
    return {'name': metadata['name'], 'court_id': metadata['court_id'],
            'consultation': (folder / 'application.txt').read_text(encoding='utf-8'),
            'bundle': submission if submission.is_file() else folder.parent / (folder.name + '_bundle.zip'), 'synthetic': True}
