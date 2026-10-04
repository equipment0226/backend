"""Retrieval must send statutory text, not a title standing in for its body."""
import copy
import unittest
from unittest.mock import patch

from apps.api import automation, corpus, model_client, verification


ARTICLES = {'LW579': 579, 'LW614': 614, 'LW611': 611}


def legal_source(source_id, article):
    source = {'id': source_id, 'title': f'합성 법률 제{article}조',
              'url': 'https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=290631',
              'source_type': 'statute', 'status': 'collected', 'court_id': None,
              'effective_date': '2020-01-01', 'applicability_status': 'baseline_unchanged',
              'sha256': 'synthetic-public-snapshot'}
    common = {'source_id': source_id, 'source_type': 'statute', 'court_id': None,
              'sha256': source['sha256'], 'url': source['url']}
    source['chunks'] = [
        {**common, 'id': source_id + ':header', 'locator': '본문 단락 1',
         'text': '합성 법률\n[시행 2020. 1. 1.]'},
        {**common, 'id': source_id + ':body', 'locator': f'제{article}조(시험 조문) · 단락 2',
         'text': f'제{article}조(시험 조문) ①개인회생의 소득과 재산을 확인한다.\n②추가 요건도 함께 확인한다.'},
    ]
    return source


class StrategyLegalRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.sources = {source_id: legal_source(source_id, article)
                        for source_id, article in ARTICLES.items()}
        self.detail = patch.object(corpus, 'source_detail', side_effect=lambda source_id: self.sources.get(source_id))
        self.detail.start()
        self.addCleanup(self.detail.stop)

    def retrieve(self, found):
        with patch.object(corpus, 'search', return_value=found):
            return automation._retrieve({'court_id': 'CT01'})

    def test_current_article_bodies_precede_search_results_without_headers_or_duplicates(self):
        before = copy.deepcopy(self.sources)
        found = [chunk for source in self.sources.values() for chunk in source['chunks']]
        found += [{'id': f'past:{i}', 'source_id': 'PAST', 'text': '과거 판단의 참고 근거'}
                  for i in range(10)]
        selected = self.retrieve(found)
        self.assertEqual([chunk['id'] for chunk in selected[:3]],
                         [source_id + ':body' for source_id in ARTICLES])
        self.assertEqual(len(selected), 8)
        self.assertEqual(len({chunk['id'] for chunk in selected}), 8)
        self.assertFalse(any(chunk['id'].endswith(':header') for chunk in selected))
        self.assertEqual(self.sources, before)

    def test_wrong_article_cross_reference_or_heading_alone_is_not_a_body(self):
        self.sources = {'LW579': legal_source('LW579', 579)}
        source = self.sources['LW579']
        body = source['chunks'][-1]
        source['chunks'][1:1] = [
            {**body, 'id': 'heading-only', 'text': '제579조(시험 조문)\n'},
            {**body, 'id': 'different-article', 'locator': '제579조의2(다른 조문)',
             'text': '제579조의2(다른 조문) 별개의 내용이다.'},
            {**body, 'id': 'cross-reference', 'locator': '제614조(시험 조문)',
             'text': '제614조(시험 조문) 제579조를 참조한다.'},
        ]
        self.assertEqual([item['id'] for item in self.retrieve(source['chunks'])], ['LW579:body'])

    def test_source_with_only_header_or_empty_article_is_not_sent_as_statutory_evidence(self):
        for source_id, source in self.sources.items():
            source['chunks'][-1]['text'] = f'제{ARTICLES[source_id]}조(시험 조문)'
        found = [chunk for source in self.sources.values() for chunk in source['chunks']]
        self.assertEqual(self.retrieve(found), [])

    def test_future_unassessed_or_failed_source_is_not_promoted_to_current_context(self):
        for change in ({'effective_date': '2099-01-01'}, {'applicability_status': 'review_required'},
                       {'applicability_status': 'awaiting_rule_assessment'}, {'status': 'stale'}):
            with self.subTest(change=change):
                for source in self.sources.values():
                    source.update(status='collected', effective_date='2020-01-01',
                                  applicability_status='baseline_unchanged')
                    source.update(change)
                self.assertEqual(self.retrieve([]), [])

    def test_article_continuation_keeps_its_real_chunk_locator_and_text(self):
        source = self.sources['LW611']
        source['chunks'][-1].update(text='⑤계속되는 조문 본문을 확인한다.')
        selected = self.retrieve([])
        self.assertEqual(selected[-1], source['chunks'][-1])

    def test_external_strategy_rehydrates_all_three_bodies_and_rejects_text_tampering(self):
        selected = self.retrieve([])
        selected[0] = {**selected[0], 'text': 'PRIVATE_CUSTOMER_DO_NOT_SEND'}
        payload = verification.build_safe_strategy_payload({'court_id': 'CT01'}, {}, selected)
        refs = [ref for ref in payload['legal_references'] if ref.get('public_chunk_id')]
        self.assertEqual([ref['public_source_id'] for ref in refs], list(ARTICLES))
        for ref in refs:
            self.assertEqual(ref['excerpt'], self.sources[ref['public_source_id']]['chunks'][-1]['text'])
            self.assertIn('②추가 요건', ref['excerpt'])
            self.assertNotIn('PRIVATE_CUSTOMER_DO_NOT_SEND', ref['excerpt'])
        verification.safe_strategy_messages(payload)
        refs[0]['excerpt'] += '임의로 추가한 문구'
        with self.assertRaises(model_client.ModelClientError):
            verification.safe_strategy_messages(payload)


if __name__ == '__main__':
    unittest.main()
