"""Local test downloads only; never a source for extraction or legal decisions."""
import json

from . import store


def current():
    folder = store.ROOT / 'examples/court_ready_fixture'
    if not (folder / 'manifest.json').is_file():
        folder = store.ROOT / 'examples/ocr_retest'
    if not (folder / 'manifest.json').is_file():
        folder = store.ROOT / 'examples/fresh_start'
    metadata = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    return {'name': metadata['name'], 'court_id': metadata['court_id'],
            'consultation': (folder / 'application.txt').read_text(encoding='utf-8'),
            'bundle': folder.parent / (folder.name + '_bundle.zip'), 'synthetic': True}
