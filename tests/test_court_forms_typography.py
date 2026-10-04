import unittest

import pymupdf

from apps.api import court_forms


class CourtTypographyTests(unittest.TestCase):
    def case(self):
        return {'id':'synthetic-typography','synthetic':True,'client_name':'가상검증',
                'court_id':'CT01','court_name':'서울회생법원','input_revision':1,
                'documents':[],'facts':[],'extraction_candidates':[],
                'consultation':{'notes':'확인된 자료에 따라 신청 경위를 작성합니다.'}}

    def test_value_uses_black_serif_and_the_original_row_baseline(self):
        case=self.case()
        preview=court_forms.preview(case,'D5100')
        self.assertEqual(preview['renderer_version'],court_forms.RENDERER_VERSION)
        self.assertEqual(len(preview['typography']['font_sha256']),64)
        self.assertIn('휴먼명조',preview['typography']['original_font_families'])
        field=next(item for item in preview['fields'] if item['key']=='client_name')
        with pymupdf.open(court_forms.original_path('D5100')) as original:
            baselines=[char['origin'][1] for char in court_forms._original_characters(original[0])
                       if field['rect'][1]<char['origin'][1]<field['rect'][3]]
        self.assertLess(min(abs(field['render_layout'][0]['baseline']-y) for y in baselines),0.01)
        with pymupdf.open(stream=court_forms.render_pdf('D5100',case),filetype='pdf') as rendered:
            spans=[span for block in rendered[0].get_text('dict')['blocks'] for line in block.get('lines',[]) for span in line['spans']]
            value=next(span for span in spans if span['text']=='가상검증')
            self.assertEqual(value['color'],0)
            self.assertNotIn('Gothic',value['font'])
            self.assertNotIn('Malgun',value['font'])
            self.assertGreaterEqual(value['size'],7)

    def test_every_registered_blank_excludes_original_nonspace_glyphs(self):
        for template_id,template in court_forms.TEMPLATES.items():
            with pymupdf.open(court_forms.original_path(template_id)) as original:
                chars={page:court_forms._original_characters(original[page]) for page in template['pages']}
                for field in template['fields']:
                    rect=pymupdf.Rect(field['rect'])
                    overlapping=[char['c'] for char in chars[field['page']]
                                 if (rect & pymupdf.Rect(char['bbox'])).get_area()>2]
                    self.assertFalse(overlapping,(template_id,field['key'],overlapping))

    def test_statement_starts_below_heading_and_long_unbroken_amount_is_not_clipped(self):
        case=self.case()
        preview=court_forms.preview(case,'D5105',{'statement':'확인된 자료에 따라 신청 경위를 작성합니다.'})
        field=next(item for item in preview['fields'] if item['key']=='statement')
        self.assertGreater(field['render_layout'][0]['baseline'],470)
        with pymupdf.open(court_forms.original_path('D5101')) as original:
            field=next(item for item in court_forms.TEMPLATES['D5101']['fields'] if item['key']=='bank_balance')
            self.assertIsNone(court_forms._field_layout(field,'123,456,789,012,345,678,901',court_forms._original_characters(original[0])))

    def test_case_number_fills_printed_year_and_sequence_blanks(self):
        case=self.case()
        preview=court_forms.preview(case,'D5110',{'case_number':'2026개회12345'})
        values={field['key']:field for field in preview['fields'] if field['page']==0}
        self.assertEqual(values['case_year_suffix']['value'],'26')
        self.assertEqual(values['case_sequence']['value'],'12345')
        with pymupdf.open(stream=court_forms.render_pdf('D5110',case,{'case_number':'2026개회12345'}),filetype='pdf') as rendered:
            for key,expected in [('case_year_suffix','26'),('case_sequence','12345')]:
                self.assertIn(expected,rendered[0].get_textbox(pymupdf.Rect(values[key]['rect'])))

    def test_multiline_annex_preserves_source_lines_without_baseline_collisions(self):
        rows=['QUOTE_ROW_'+str(i).zfill(3)+' '+('원문 근거 ' * 12) for i in range(75)]
        statement='\n'.join(rows)
        self.assertEqual(list(court_forms._annex_lines('첫줄\n\n다음줄\r\n끝')),['첫줄','','다음줄','끝'])
        with pymupdf.open(stream=court_forms.render_pdf('D5105',self.case(),{'statement':statement}),filetype='pdf') as rendered:
            original_pages=len(court_forms.TEMPLATES['D5105']['pages'])
            appendix=list(rendered)[original_pages:]
            self.assertGreater(len(appendix),2)
            text='\n'.join(page.get_text() for page in appendix)
            for i in range(75):self.assertIn('QUOTE_ROW_'+str(i).zfill(3),text)
            for page in appendix:
                spans=[span for block in page.get_text('dict')['blocks'] for line in block.get('lines',[]) for span in line['spans']]
                baselines=sorted({round(span['origin'][1],2) for span in spans})
                self.assertTrue(all(b-a>=14.5 for a,b in zip(baselines,baselines[1:])),baselines)
                self.assertTrue(all(span['bbox'][3]<page.rect.height-35 for span in spans))


if __name__=='__main__':
    unittest.main()
