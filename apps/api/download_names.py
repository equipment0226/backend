"""Human-readable export names, independent from internal artifact storage keys."""
import re
import unicodedata
from urllib.parse import quote


def _part(value, fallback):
    text = unicodedata.normalize('NFC', str(value or fallback))
    text = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', text)
    return re.sub(r'\s+', ' ', text).strip(' ._')[:100] or fallback


def filename(case, title, extension='pdf'):
    reference = (case or {}).get('matter_number') or (case or {}).get('engagement_number') or (case or {}).get('id')
    prefix = _part(reference, '법원서식')
    return f'{prefix}_{_part(title, "문서")}.{extension}'


def disposition(name):
    return "attachment; filename*=UTF-8''" + quote(name, safe='')
