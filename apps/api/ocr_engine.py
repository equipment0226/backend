"""Local Korean OCR with bounded, killable workers and page-level provenance.

No document leaves this machine. Model downloads happen only in setup_ocr.py.
The API process never imports ONNX/OpenCV, so a slow or malformed scan cannot
hold the model worker or indefinitely occupy an upload request.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = ROOT / '.local' / 'ocr'
MODEL_NAMES = ('ch_PP-OCRv5_det_mobile.onnx', 'ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx', 'korean_PP-OCRv5_rec_mobile.onnx')
_LOCK = threading.Lock()


def _settings():
    return {
        'max_scan_pages': max(1, min(50, int(os.getenv('DEBTOFF_OCR_MAX_PAGES', '20')))),
        'timeout_seconds': max(10, min(300, int(os.getenv('DEBTOFF_OCR_TIMEOUT', '180')))),
        'dpi': max(100, min(250, int(os.getenv('DEBTOFF_OCR_DPI', '200')))),
        'max_pixels': 40_000_000,
        'min_line_confidence': 0.70,
    }


def status():
    deps = OCR_ROOT / 'python'
    models = OCR_ROOT / 'models'
    missing = [name for name in MODEL_NAMES if not (models / name).is_file()]
    installed = (deps / 'rapidocr').is_dir() and (deps / 'onnxruntime').is_dir()
    if not installed:
        installed = bool(importlib.util.find_spec('rapidocr') and importlib.util.find_spec('onnxruntime'))
    return {
        'available': installed and not missing,
        'engine': 'rapidocr', 'language': 'ko', 'model': 'PP-OCRv5 Korean mobile',
        'device': 'CPU', 'offline': True, 'missing_models': missing,
        'dependency_ready': installed, 'settings': _settings(),
        'setup_command': 'python scripts/setup_ocr.py',
    }


def _unreadable(number, reason, native_text=''):
    return {'page': number, 'text': native_text, 'extraction_method': 'native_text' if native_text else 'ocr',
            'status': 'manual_review', 'ocr_status': reason, 'confidence': None,
            'lines': [], 'warnings': [reason], 'requires_review': True}


def _native(number, text, page=None):
    lines=[]
    if page is not None:
        import pymupdf
        # Exclude embedded image bytes while retaining actual PDF span geometry.
        blocks=page.get_text('dict',flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES).get('blocks',[])
        for block in blocks:
            for line in block.get('lines',[]):
                value=''.join(span.get('text','') for span in line.get('spans',[]))
                if not value.strip():continue
                x0,y0,x1,y1=line['bbox']
                lines.append({'line':len(lines)+1,'text':value,'confidence':None,
                    'bbox':[[x0,y0],[x1,y0],[x1,y1],[x0,y1]],'source':'native_text',
                    'coordinate_space':'pdf_points','requires_review':False})
    return {'page': number, 'text': text, 'raw_text':text, 'extraction_method': 'native_text',
            'status': 'extracted', 'confidence': None, 'lines': lines, 'warnings': [], 'requires_review': False,
            'text_truncated':False,'coordinate_space':'pdf_points',
            **({'page_width':page.rect.width,'page_height':page.rect.height} if page is not None else {})}


def _raster_union_area(rectangles):
    """Image tiles may each be small; count their union, never overlaps twice."""
    xs=sorted({x for rect in rectangles for x in (rect[0],rect[2])});area=0.0
    for left,right in zip(xs,xs[1:]):
        intervals=sorted((y0,y1) for x0,y0,x1,y1 in rectangles if x0<right and x1>left)
        length=0.0;start=end=None
        for y0,y1 in intervals:
            if start is None:start,end=y0,y1
            elif y0<=end:end=max(end,y1)
            else:length+=end-start;start,end=y0,y1
        if start is not None:length+=end-start
        area+=(right-left)*length
    return area


def _has_scan(page):
    # A substantial raster image can contain the body below a native letterhead.
    area = max(1.0, page.rect.width * page.rect.height)
    rectangles=[]
    for image in page.get_image_info():
        x0, y0, x1, y1 = image['bbox']
        x0,y0=max(page.rect.x0,x0),max(page.rect.y0,y0)
        x1,y1=min(page.rect.x1,x1),min(page.rect.y1,y1)
        if x1>x0 and y1>y0:rectangles.append((x0,y0,x1,y1))
    return _raster_union_area(rectangles)/area >= 0.25


def _pending_native(native, reason):
    if not native.get('text'):return _unreadable(native['page'],reason)
    return {**native,'status':'manual_review','ocr_status':reason,
            'warnings':list(dict.fromkeys([*[w for w in native.get('warnings',[]) if w!='pending'],reason])),
            'requires_review':True,'contains_scan':True}


def _normalized(text):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',text))


def _rect(line):
    points=line['bbox'];xs=[p[0] for p in points];ys=[p[1] for p in points]
    return min(xs),min(ys),max(xs),max(ys)


def _overlap(first, second):
    a,b=_rect(first),_rect(second)
    intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return intersection/max(1,min((a[2]-a[0])*(a[3]-a[1]),(b[2]-b[0])*(b[3]-b[1])))


def _merge_native_ocr(native, ocr):
    """Keep native spans and OCR observations separately, including disagreement.

    Geometry is mapped to PDF points for reading order. OCR boxes in their
    original rendered pixel space remain in ``ocr_lines`` unchanged. A native
    span wins only over the same overlapping OCR text, not unrelated scan text.
    """
    if not native.get('text'):return ocr
    result={**ocr,'native_text':native['text'],'native_lines':native.get('lines',[]),
            'ocr_text':ocr.get('text',''),'ocr_raw_text':ocr.get('raw_text',ocr.get('text','')),
            'ocr_lines':ocr.get('lines',[]),'contains_native_text':True,'contains_scan':True}
    width,height=ocr.get('image_width',0),ocr.get('image_height',0)
    if not native.get('lines') or not width or not height:
        # Failed/older workers have no boxes. Preserve both bodies explicitly;
        # never replace the native letterhead with an empty failed OCR result.
        result['text']='\n'.join(dict.fromkeys(t for t in (native['text'],ocr.get('text','')) if t))
        result['raw_text']='\n'.join(dict.fromkeys(t for t in (native['text'],ocr.get('raw_text',ocr.get('text',''))) if t))
        result['lines']=native.get('lines',[])+ocr.get('lines',[])
        result['warnings']=list(dict.fromkeys([*ocr.get('warnings',[]),'mixed_layout_unresolved']))
        result.update(status='manual_review',requires_review=True,reading_order='native_then_ocr',text_truncated=False)
        return result
    scale_x,scale_y=native['page_width']/width,native['page_height']/height
    lines=[{**line,'source_line':line['line']} for line in native['lines']]
    conflicts=[]
    for item in ocr.get('lines',[]):
        line={**item,'source':'ocr','source_line':item['line'],'coordinate_space':'pdf_points',
              'bbox':[[round(x*scale_x,3),round(y*scale_y,3)] for x,y in item['bbox']]}
        overlaps=[n for n in native['lines'] if _overlap(n,line)>.55]
        if any(_normalized(n['text'])==_normalized(line['text']) for n in overlaps):continue
        if overlaps:
            line['requires_review']=True
            conflicts.append({'native_lines':[n['line'] for n in overlaps],'ocr_line':item['line']})
        lines.append(line)
    for index,line in enumerate(lines,1):line['line']=index
    rows=_reading_rows(lines)
    result.update(text='\n'.join(' | '.join(line['text'] for line in row['lines'] if not line.get('requires_review')) for row in rows).strip(),
        raw_text='\n'.join(' | '.join(line['text'] for line in row['lines']) for row in rows),lines=lines,
        rows=[{'source_lines':[line['line'] for line in row['lines']]} for row in rows],coordinate_space='pdf_points',
        page_width=native['page_width'],page_height=native['page_height'],reading_order='rows_left_to_right',text_truncated=False,
        native_ocr_conflicts=conflicts)
    if conflicts:
        result['warnings']=list(dict.fromkeys([*result.get('warnings',[]),'native_ocr_disagreement']))
        result.update(status='manual_review',requires_review=True)
    return result


def extract_pages(content: bytes, extension: str):
    """Extract PDF/image pages, preserving native text and OCR failure reasons.

    Raises ValueError for invalid/encrypted/oversize input; OCR unavailable,
    queue limits, timeouts, and low confidence remain explicit page statuses.
    """
    extension = extension.lower()
    if extension not in ('.pdf', '.png', '.jpg', '.jpeg'):
        raise ValueError('unsupported OCR input')
    if not content or len(content) > 10_000_000:
        raise ValueError('OCR input must be between 1 byte and 10 MB')
    settings = _settings()
    pages, scan_indices = [], []
    if extension == '.pdf':
        import pymupdf as fitz
        with fitz.open(stream=content, filetype='pdf') as pdf:
            if pdf.is_encrypted:
                raise ValueError('encrypted PDF')
            if len(pdf) > 100:
                raise ValueError('PDF exceeds 100 pages')
            for index, page in enumerate(pdf):
                text = page.get_text(sort=True).strip()
                native = _native(index+1,text,page)
                if text and not _has_scan(page):
                    pages.append(native)
                else:
                    pages.append(_pending_native(native,'pending'))
                    scan_indices.append(index)
    else:
        pages = [_unreadable(1, 'pending')]
        scan_indices = [0]
    if not scan_indices:
        return pages
    allowed = scan_indices[:settings['max_scan_pages']]
    for index in scan_indices[settings['max_scan_pages']:]:
        pages[index] = _pending_native(pages[index],'page_limit')
    if not status()['available']:
        for index in allowed:
            pages[index] = _pending_native(pages[index],'engine_unavailable')
        return pages
    if not _LOCK.acquire(timeout=10):
        for index in allowed:
            pages[index] = _pending_native(pages[index],'queue_busy')
        return pages
    started = time.perf_counter()
    outcome = 'worker_failed'
    try:
        job_root = OCR_ROOT / 'jobs'
        job_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='ocr-', dir=job_root) as work:
            folder = Path(work)
            source, output, job = folder / ('input' + extension), folder / 'result.json', folder / 'job.json'
            source.write_bytes(content)
            job.write_text(json.dumps({'source': str(source), 'output': str(output), 'extension': extension,
                                       'indices': allowed, 'settings': settings}), encoding='utf-8')
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            env = dict(os.environ, PYTHONUTF8='1', OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
            try:
                proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(job)],
                                      cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                      timeout=settings['timeout_seconds'], creationflags=flags)
                outcome = 'worker_failed' if proc.returncode else 'no_text'
            except subprocess.TimeoutExpired:
                outcome = 'timeout'
            if output.is_file():
                results = json.loads(output.read_text(encoding='utf-8'))
                for page in results:
                    index = page['page'] - 1
                    if index in allowed:
                        pages[index] = _merge_native_ocr(pages[index],page)
            for index in allowed:
                if pages[index].get('ocr_status') == 'pending':
                    pages[index] = _pending_native(pages[index],outcome)
    finally:
        _LOCK.release()
    duration = round(time.perf_counter() - started, 3)
    for page in pages:
        if page.get('extraction_method') == 'ocr':
            page['job_seconds'] = duration
            page['input_sha256'] = hashlib.sha256(content).hexdigest()
    return pages


def _reading_rows(lines):
    """Preserve table label/value relationships using actual detected geometry."""
    rows = []
    for line in sorted(lines, key=lambda item: sum(point[1] for point in item['bbox']) / 4):
        ys = [point[1] for point in line['bbox']]
        center = sum(ys) / 4
        tolerance = max(7, (max(ys)-min(ys)) * 0.45)
        if rows and abs(center - rows[-1]['center_y']) <= tolerance:
            rows[-1]['lines'].append(line)
        else:
            rows.append({'center_y': center, 'lines': [line]})
    for row in rows:
        row['lines'].sort(key=lambda line: min(point[0] for point in line['bbox']))
    return rows


def _retry_line_readings(engine, pixels, lines, settings):
    """Retry weak horizontal readings without the fallible direction classifier.

    Only observed pixels and boxes are used. Near-identical identifier readings
    are retained as alternatives, never corrected from an expected dictionary.
    Work is bounded to six crops per page and the existing process timeout.
    """
    recognize=getattr(engine,'recognize_txt',None)
    if not callable(recognize):return []
    candidates=[]
    for line in lines:
        x0,y0,x1,y1=_rect(line);ratio=(x1-x0)/max(1,y1-y0)
        sparse=len(re.sub(r'\s','',line['text']))<=2 and ratio>=8
        ambiguous=bool(re.search(r'[0-9][OoIl]|[OoIl][0-9]',line['text']))
        if ratio>=2 and (line['requires_review'] or sparse or ambiguous):
            candidates.append((line,sparse,ambiguous))
    warnings=[]
    if len(candidates)>6:warnings.append('line_retry_limit')
    height,width=pixels.shape[:2]
    for line,sparse,ambiguous in candidates[:6]:
        x0,y0,x1,y1=_rect(line)
        crop=pixels[max(0,int(y0)-2):min(height,int(y1)+3),max(0,int(x0)-2):min(width,int(x1)+3)].copy()
        if not crop.size:continue
        try:
            retry=recognize([crop])
            if not retry.txts:continue
            text=str(retry.txts[0]);score=float(retry.scores[0])
            original={'text':line['text'],'confidence':line['confidence'],'method':'direction_classified'}
            alternative={'text':text,'confidence':round(score,4),'method':'upright_crop'}
            line['recognition_candidates']=[original,alternative]
            if _normalized(text)==_normalized(line['text']):continue
            if (line['requires_review'] or sparse) and score>=.90 and score-line['confidence']>=.06:
                # A clear reading can recover an incorrectly flipped line. If
                # both observations contain different numbers, retain review.
                old_numbers=re.findall(r'\d[\d,.]*',line['text'])
                new_numbers=re.findall(r'\d[\d,.]*',text)
                conflict=bool(old_numbers and old_numbers!=new_numbers)
                line.update(text=text,confidence=round(score,4),recognition_method='upright_crop',
                            requires_review=conflict)
                if conflict:warnings.append('recognition_disagreement')
            elif ambiguous and score>=.70:
                line['requires_review']=True
                line['recognition_disagreement']=True
                warnings.append('recognition_disagreement')
        except Exception:
            line['retry_status']='failed'
            warnings.append('line_retry_failed')
    return list(dict.fromkeys(warnings))


def _ocr_page(engine, image, page_number, settings):
    import numpy as np
    started = time.perf_counter()
    # RapidOCR arrays use BGR while PIL images use RGB.
    pixels=np.asarray(image.convert('RGB'))[:, :, ::-1].copy()
    result = engine(pixels)
    lines = []
    if result.txts:
        for index, (text, confidence, box) in enumerate(zip(result.txts, result.scores, result.boxes)):
            lines.append({'line': index + 1, 'text': text, 'confidence': round(float(confidence), 4),
                          'bbox': [[round(float(v), 1) for v in point] for point in box],
                          'requires_review': float(confidence) < settings['min_line_confidence']})
    warnings=_retry_line_readings(engine,pixels,lines,settings)
    accepted = [line for line in lines if not line['requires_review']]
    rows = _reading_rows(lines)
    text = '\n'.join(' | '.join(line['text'] for line in row['lines'] if not line['requires_review'])
                     for row in rows).strip()
    confidence = sum(line['confidence'] * len(line['text']) for line in lines) / max(1, sum(len(line['text']) for line in lines))
    if len(accepted) < len(lines):
        warnings.append('uncertain_lines_excluded')
    if any(line['confidence']<settings['min_line_confidence'] for line in lines):
        warnings.append('low_confidence_lines_excluded')
    if not text:
        warnings.append('no_reliable_text')
    return {'page': page_number, 'text': text,
            'raw_text': '\n'.join(' | '.join(line['text'] for line in row['lines']) for row in rows),
            'extraction_method': 'ocr', 'engine': 'rapidocr-3.9.2', 'model': 'korean_PP-OCRv5_rec_mobile',
            'language': 'ko', 'status': 'extracted' if text and not warnings else 'manual_review',
            'ocr_status': 'completed' if text else 'no_text', 'confidence': round(confidence, 4),
            'lines': lines, 'warnings': warnings, 'requires_review': True,
            'reading_order': 'rows_left_to_right',
            'rows': [{'source_lines': [line['line'] for line in row['lines']]} for row in rows],
            'image_width': image.width, 'image_height': image.height,
            'coordinate_space':'rendered_pixels','text_truncated':False,
            'recognition_retry_count':sum(bool(line.get('recognition_candidates')) for line in lines),
            'duration_seconds': round(time.perf_counter()-started, 3)}


def _engine():
    from rapidocr import RapidOCR, LangRec, OCRVersion, ModelType
    models = OCR_ROOT / 'models'
    return RapidOCR(params={
        'Global.log_level': 'error', 'Global.text_score': 0.0,
        'Global.model_root_dir': str(models), 'Global.max_side_len': 2500,
        'Det.model_path': str(models / MODEL_NAMES[0]),
        'Det.ocr_version': OCRVersion.PPOCRV5, 'Det.model_type': ModelType.MOBILE,
        'Cls.model_path': str(models / MODEL_NAMES[1]),
        'Cls.ocr_version': OCRVersion.PPOCRV5,
        'Rec.model_path': str(models / MODEL_NAMES[2]),
        'Rec.lang_type': LangRec.KOREAN, 'Rec.ocr_version': OCRVersion.PPOCRV5,
        'Rec.model_type': ModelType.MOBILE,
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
    })


def _worker(job_path):
    local_deps = OCR_ROOT / 'python'
    if local_deps.is_dir():
        sys.path.insert(0, str(local_deps))
    from PIL import Image, ImageOps
    import pymupdf as fitz
    job = json.loads(Path(job_path).read_text(encoding='utf-8'))
    settings = job['settings']
    Image.MAX_IMAGE_PIXELS = settings['max_pixels']
    engine = _engine()
    result = []
    pdf = fitz.open(job['source']) if job['extension'] == '.pdf' else None
    try:
        for index in job['indices']:
            try:
                if pdf is not None:
                    page = pdf[index]
                    scale = min(settings['dpi']/72, 2500 / max(page.rect.width, page.rect.height))
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
                    image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                else:
                    with Image.open(job['source']) as original:
                        if original.width * original.height > settings['max_pixels']:
                            raise ValueError('pixel_limit')
                        image = ImageOps.exif_transpose(original).convert('RGB')
                    image.thumbnail((2500, 2500))
                result.append(_ocr_page(engine, image, index + 1, settings))
            except Exception as exc:
                # Never echo document bytes, paths, or native-library error dumps.
                failed = _unreadable(index + 1, 'page_error')
                failed['error_type'] = type(exc).__name__
                result.append(failed)
            temporary = Path(job['output']).with_suffix('.tmp')
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            temporary.replace(job['output'])
    finally:
        if pdf is not None:
            pdf.close()


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--worker':
    _worker(sys.argv[2])
