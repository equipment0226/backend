import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from PIL import Image

from apps.api import ocr_engine


def pdf_bytes(scans=1, native=False):
    image = Image.new('RGB', (200, 300), 'white')
    image_bytes = io.BytesIO()
    image.save(image_bytes, format='PNG')
    with fitz.open() as doc:
        if native:
            page = doc.new_page()
            page.insert_text((30, 50), 'native text must remain available')
        for _ in range(scans):
            page = doc.new_page()
            page.insert_image(page.rect, stream=image_bytes.getvalue())
        return doc.tobytes()


class OCRTests(unittest.TestCase):
    def test_native_pdf_never_starts_ocr(self):
        with patch.object(ocr_engine.subprocess, 'run') as run:
            pages = ocr_engine.extract_pages(pdf_bytes(scans=0, native=True), '.pdf')
        self.assertEqual(pages[0]['extraction_method'], 'native_text')
        self.assertIn('native text', pages[0]['text'])
        run.assert_not_called()

    def test_missing_engine_preserves_native_page_and_scan_position(self):
        with patch.object(ocr_engine, 'status', return_value={'available': False}):
            pages = ocr_engine.extract_pages(pdf_bytes(scans=2, native=True), '.pdf')
        self.assertEqual([p['page'] for p in pages], [1, 2, 3])
        self.assertEqual(pages[1]['ocr_status'], 'engine_unavailable')
        self.assertEqual(pages[1]['text'], '')
        self.assertIn('native text', pages[0]['text'])

    def test_scan_page_limit_is_explicit(self):
        with patch.dict('os.environ', {'DEBTOFF_OCR_MAX_PAGES': '1'}), patch.object(ocr_engine, 'status', return_value={'available': False}):
            pages = ocr_engine.extract_pages(pdf_bytes(scans=2), '.pdf')
        self.assertEqual(pages[0]['ocr_status'], 'engine_unavailable')
        self.assertEqual(pages[1]['ocr_status'], 'page_limit')

    def test_timeout_returns_review_status_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ocr_engine, 'OCR_ROOT', Path(directory)), \
             patch.object(ocr_engine, 'status', return_value={'available': True}), \
             patch.object(ocr_engine.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ocr', 10)):
            pages = ocr_engine.extract_pages(pdf_bytes(), '.pdf')
        self.assertEqual(pages[0]['ocr_status'], 'timeout')
        self.assertFalse(ocr_engine._LOCK.locked())

    def test_partial_result_survives_timeout(self):
        def worker(args, **kwargs):
            job = json.loads(Path(args[-1]).read_text(encoding='utf-8'))
            Path(job['output']).write_text(json.dumps([{'page': 1, 'text': 'read first page',
                                                       'extraction_method': 'ocr', 'status': 'extracted', 'ocr_status': 'completed'}]), encoding='utf-8')
            raise subprocess.TimeoutExpired('ocr', 10)
        with tempfile.TemporaryDirectory() as directory, patch.object(ocr_engine, 'OCR_ROOT', Path(directory)), \
             patch.object(ocr_engine, 'status', return_value={'available': True}), \
             patch.object(ocr_engine.subprocess, 'run', side_effect=worker):
            pages = ocr_engine.extract_pages(pdf_bytes(scans=2), '.pdf')
        self.assertEqual(pages[0]['text'], 'read first page')
        self.assertEqual(pages[1]['ocr_status'], 'timeout')

    def test_rejects_invalid_format_and_size(self):
        for content, extension in [(b'', '.pdf'), (b'x', '.docx'), (b'x' * 10_000_001, '.png')]:
            with self.assertRaises(ValueError):
                ocr_engine.extract_pages(content, extension)

    def test_table_rows_keep_labels_next_to_amounts(self):
        lines = [{'line': 1, 'text': '월소득', 'bbox': [[10,10],[70,10],[70,30],[10,30]]},
                 {'line': 2, 'text': '2,800,000', 'bbox': [[300,12],[420,12],[420,30],[300,30]]},
                 {'line': 3, 'text': '다음 행', 'bbox': [[10,80],[70,80],[70,100],[10,100]]}]
        rows = ocr_engine._reading_rows(lines)
        self.assertEqual(len(rows), 2)
        self.assertEqual([line['line'] for line in rows[0]['lines']], [1, 2])

    def test_low_confidence_line_not_used_as_fact_source(self):
        import numpy as np
        class FakeResult:
            txts = ['월소득: 2,800,000원', '월소득: 9,900,000원']
            scores = [0.99, 0.30]
            boxes = [np.zeros((4, 2)), np.ones((4, 2))]
        result = ocr_engine._ocr_page(lambda image: FakeResult(), Image.new('RGB', (100, 100)), 1, ocr_engine._settings())
        self.assertIn('2,800,000', result['text'])
        self.assertNotIn('9,900,000', result['text'])
        self.assertIn('9,900,000', result['raw_text'])
        self.assertEqual(len(result['lines']), 2)
        self.assertEqual(result['status'], 'manual_review')

    def test_native_text_beyond_old_cutoff_and_geometry_are_preserved(self):
        with fitz.open() as doc:
            page=doc.new_page(width=1000,height=2200)
            for i in range(150):
                page.insert_text((10,15+i*13),'row '+str(i)+' '+('sample '*20),fontsize=6)
            page.insert_text((10,2100),'TAIL AMOUNT 7,654,321',fontsize=10)
            raw=doc.tobytes()
        result=ocr_engine.extract_pages(raw,'.pdf')[0]
        self.assertGreater(len(result['text']),15000)
        self.assertIn('TAIL AMOUNT 7,654,321',result['text'])
        self.assertFalse(result['text_truncated'])
        self.assertEqual(result['lines'][-1]['coordinate_space'],'pdf_points')
        self.assertEqual(result['lines'][-1]['text'],'TAIL AMOUNT 7,654,321')

    def test_ocr_full_text_and_low_confidence_raw_text_are_not_truncated(self):
        import numpy as np
        words=['page row '+str(i)+' '+('read '*25) for i in range(140)]
        words+=['TAIL AMOUNT 8,765,432','UNCERTAIN TAIL 9,999,999']
        fake=SimpleNamespace(txts=words,scores=[.99]*(len(words)-1)+[.2],
            boxes=[np.array([[0,i*20],[900,i*20],[900,i*20+10],[0,i*20+10]]) for i in range(len(words))])
        result=ocr_engine._ocr_page(lambda image:fake,Image.new('RGB',(100,100)),1,ocr_engine._settings())
        self.assertGreater(len(result['text']),15000)
        self.assertIn('TAIL AMOUNT 8,765,432',result['text'])
        self.assertNotIn('UNCERTAIN TAIL',result['text'])
        self.assertIn('UNCERTAIN TAIL 9,999,999',result['raw_text'])
        self.assertEqual(len(result['lines']),len(words))

    def test_small_raster_tiles_trigger_ocr_by_union_not_duplicate_area(self):
        rect=SimpleNamespace(width=100,height=100,x0=0,y0=0,x1=100,y1=100)
        page=SimpleNamespace(rect=rect,get_image_info=lambda:[{'bbox':(0,0,40,40)},{'bbox':(40,40,80,80)}])
        self.assertTrue(ocr_engine._has_scan(page))
        page.get_image_info=lambda:[{'bbox':(0,0,40,40)}]*3
        self.assertFalse(ocr_engine._has_scan(page))

    def test_successful_ocr_retains_native_header_and_both_source_boxes(self):
        def worker(args,**kwargs):
            job=json.loads(Path(args[-1]).read_text(encoding='utf-8'))
            line={'line':1,'text':'scanned amount 1,234,567','confidence':.98,'requires_review':False,
                  'bbox':[[10,200],[500,200],[500,230],[10,230]]}
            result=[{'page':1,'text':line['text'],'raw_text':line['text'],'lines':[line],
                'image_width':595,'image_height':842,'extraction_method':'ocr','status':'extracted','ocr_status':'completed','warnings':[]}]
            Path(job['output']).write_text(json.dumps(result),encoding='utf-8')
            return SimpleNamespace(returncode=0)
        image=io.BytesIO();Image.new('RGB',(200,300),'white').save(image,format='PNG')
        with fitz.open() as doc:
            page=doc.new_page();page.insert_image((0,100,595,842),stream=image.getvalue())
            page.insert_text((20,35),'Native header and person')
            raw=doc.tobytes()
        with tempfile.TemporaryDirectory() as directory, patch.object(ocr_engine,'OCR_ROOT',Path(directory)),patch.object(ocr_engine,'status',return_value={'available':True}),patch.object(ocr_engine.subprocess,'run',side_effect=worker):
            result=ocr_engine.extract_pages(raw,'.pdf')[0]
        self.assertIn('Native header and person',result['text'])
        self.assertIn('scanned amount 1,234,567',result['text'])
        self.assertTrue(result['native_lines'])
        self.assertTrue(result['ocr_lines'])
        self.assertEqual(result['coordinate_space'],'pdf_points')

    def test_native_ocr_disagreement_is_retained_without_overriding_native_amount(self):
        bbox=[[10,10],[200,10],[200,30],[10,30]]
        native={**ocr_engine._native(1,'Amount 1,234'),'page_width':400,'page_height':600,
                'lines':[{'line':1,'text':'Amount 1,234','bbox':bbox,'requires_review':False}]}
        ocr={'page':1,'text':'Amount 7,234','raw_text':'Amount 7,234','status':'extracted','warnings':[],
             'image_width':400,'image_height':600,'lines':[{'line':1,'text':'Amount 7,234','bbox':bbox,'confidence':.99,'requires_review':False}]}
        result=ocr_engine._merge_native_ocr(native,ocr)
        self.assertIn('Amount 1,234',result['text'])
        self.assertNotIn('Amount 7,234',result['text'])
        self.assertIn('Amount 7,234',result['raw_text'])
        self.assertIn('native_ocr_disagreement',result['warnings'])
        self.assertEqual(result['status'],'manual_review')

    def test_failed_ocr_preserves_native_provenance(self):
        native={**ocr_engine._native(1,'Native header'),'lines':[{'line':1,'text':'Native header','bbox':[[0,0],[90,0],[90,20],[0,20]]}]}
        result=ocr_engine._merge_native_ocr(native,ocr_engine._unreadable(1,'timeout'))
        self.assertEqual(result['text'],'Native header')
        self.assertEqual(result['native_lines'][0]['text'],'Native header')
        self.assertIn('timeout',result['warnings'])
        self.assertTrue(result['requires_review'])

    def test_direction_retry_recovers_sparse_line_with_original_reading_retained(self):
        import numpy as np
        class Engine:
            def __call__(self,image):
                return SimpleNamespace(txts=['/'],scores=[.80],boxes=[np.array([[2,2],[180,2],[180,15],[2,15]])])
            def recognize_txt(self,crops):
                return SimpleNamespace(txts=['발급 담당자 확인'],scores=[.98])
        result=ocr_engine._ocr_page(Engine(),Image.new('RGB',(200,30)),1,ocr_engine._settings())
        self.assertIn('발급 담당자 확인',result['text'])
        line=result['lines'][0]
        self.assertEqual(line['recognition_candidates'][0]['text'],'/')
        self.assertEqual(line['recognition_candidates'][1]['method'],'upright_crop')
        self.assertEqual(line['bbox'][0],[2.0,2.0])

    def test_conflicting_identifier_retry_is_not_silently_corrected(self):
        import numpy as np
        class Engine:
            def __call__(self,image):
                return SimpleNamespace(txts=['REF-0O1'],scores=[.94],boxes=[np.array([[2,2],[180,2],[180,15],[2,15]])])
            def recognize_txt(self,crops):
                return SimpleNamespace(txts=['REF-001'],scores=[.941])
        result=ocr_engine._ocr_page(Engine(),Image.new('RGB',(200,30)),1,ocr_engine._settings())
        self.assertEqual(result['text'],'')
        self.assertIn('REF-0O1',result['raw_text'])
        self.assertIn('recognition_disagreement',result['warnings'])
        self.assertEqual(result['lines'][0]['recognition_candidates'][1]['text'],'REF-001')
        self.assertTrue(result['lines'][0]['requires_review'])

    def test_retry_is_bounded_and_numeric_disagreement_stays_reviewable(self):
        import numpy as np
        class Engine:
            calls=0
            def __call__(self,image):
                return SimpleNamespace(txts=['9,900']*9,scores=[.4]*9,
                    boxes=[np.array([[2,y],[180,y],[180,y+10],[2,y+10]]) for y in range(0,180,20)])
            def recognize_txt(self,crops):
                self.calls+=1
                return SimpleNamespace(txts=['1,100'],scores=[.99])
        engine=Engine()
        result=ocr_engine._ocr_page(engine,Image.new('RGB',(200,200)),1,ocr_engine._settings())
        self.assertEqual(engine.calls,6)
        self.assertEqual(result['text'],'')
        self.assertIn('line_retry_limit',result['warnings'])
        self.assertEqual(len(result['lines']),9)


if __name__ == '__main__':
    unittest.main()
