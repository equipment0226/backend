"""Candidate review preserves authored versions until the queued pipeline finishes.

Real HTTP/SQLite scheduling in a temporary store; the executor never runs and no
model, live case, or external API is used.
"""
import asyncio
import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from apps.api import automation, ax_service, domain, drafting, main, store, workflow_contract


class CandidateReviewPreservationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(folder)))
        self.stack.enter_context(patch.dict(os.environ, {'DEBTOFF_AUTO_AX': '1'}))
        self.stack.enter_context(patch.object(ax_service, 'knowledge_signature', return_value='fixed-test-law'))
        self.pool = self.stack.enter_context(patch.object(main, 'POOL', Mock()))
        self.generate = self.stack.enter_context(patch.object(drafting, 'generate', side_effect=AssertionError('Generic draft generation is not allowed during evidence review')))
        store.initialize()
        self.user = {'id': 'staff', 'name': '담당 직원', 'role': 'staff', 'org_id': 'office-1'}
        self.stack.enter_context(patch.dict(main.app.dependency_overrides, {main.staff: lambda: self.user}))
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.case = domain.new_case('합성 보존 고객', 'CT01', '서울회생법원', '작성본 보존 시험', True)
        self.case['intake'] = {'status': 'completed'}
        self.case['extraction_candidates'] = [
            {'id': 'income-candidate', 'key': 'monthly_income', 'value': 3200000, 'status': 'candidate',
             'document_id': 'source', 'quote': '월 실수령액 3,200,000원'}]
        self.case['documents'] = [{'id': 'source', 'filename': '합성 소득.pdf', 'text': '월 실수령액 3,200,000원',
                                   'status': 'verified', 'sha256': 'unchanged-original-hash', 'version': 1}]
        self.case['drafts'] = [{'id': 'authored-draft', 'input_revision': 1, 'stale': False,
            'created_at': store.now(), 'content_hash': 'authored-content-hash',
            'sections': [{'id': 'statement', 'content': '원문 근거와 관련 법령을 연결한 기존 진술 본문입니다.'}],
            'narrative_id': 'verified-narrative', 'strategy_id': 'existing-strategy',
            'court_document_ids': ['authored-form'], 'ai_review': {'status': 'needs_review', 'passed': False}}]
        self.case['court_documents'] = [{'id': 'authored-form', 'template_id': 'D5105',
            'created_at': store.now(), 'input_revision': 1, 'stale': False,
            'fields': {}, 'preview': {'fields': [{'key': 'statement', 'value': '기존 근거 작성 본문'}]},
            'ai_review': {'status': 'needs_review', 'passed': False}}]
        self.case['ax_pipeline'] = {'stage': 'human_review', 'status': 'waiting', 'input_revision': 1,
            'steps': workflow_contract.project_steps(6, 'waiting'), 'draft_id': 'authored-draft',
            'human_review_required': True, 'submission_ready': False}
        self.case['automation'] = {'stage': 'human_review', 'draft_id': 'authored-draft'}
        self.pdf = store.DATA_DIR / 'generated' / self.case['id'] / 'authored-form.pdf'
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b'%PDF-synthetic-authored-content-preservation')
        self.pdf_hash = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.case['court_documents'][0]['sha256'] = self.pdf_hash
        store.insert_case(self.case)
        self.path = '/api/cases/' + self.case['id'] + '/extraction-candidates/income-candidate/review'

    def review(self, decision, **extra):
        case = store.get_case(self.case['id'])
        return self.client.post(self.path, json={'expected_version': case['version'],
            'decision': decision, 'reason': '원문을 대조한 담당자 검토 결과입니다.', **extra})

    def preserved(self, body, reason='추출값 검토 반영', documents_unchanged=True):
        self.assertEqual(len(body['drafts']), 1)
        self.assertEqual(len(body['court_documents']), 1)
        for key in ('drafts', 'court_documents'):
            row, original = body[key][0], self.case[key][0]
            self.assertTrue(row['stale'])
            self.assertEqual(row['stale_reason'], reason)
            self.assertEqual({key: value for key, value in row.items() if key not in ('stale', 'stale_reason')},
                             {key: value for key, value in original.items() if key != 'stale'})
        if documents_unchanged:
            self.assertEqual([{key: value for key, value in document.items() if key != 'extraction_state'}
                              for document in body['documents']], self.case['documents'])
            self.assertEqual(store.get_case(self.case['id'])['documents'], self.case['documents'])
        self.assertEqual(hashlib.sha256(self.pdf.read_bytes()).hexdigest(), self.pdf_hash)
        self.assertEqual(body['ax_pipeline']['status'], 'queued')
        self.assertEqual(body['ax_pipeline']['queued_update']['input_revision'], body['input_revision'])
        self.assertFalse(body['ax_pipeline']['submission_ready'])
        self.generate.assert_not_called()

    def test_accept_preserves_authored_text_and_queues_pipeline(self):
        response = self.review('accept')
        self.assertEqual(response.status_code, 200, response.text)
        self.preserved(response.json())
        self.assertEqual(response.json()['extraction_candidates'][0]['status'], 'accepted')
        self.pool.submit.assert_called_once()

    def test_correct_preserves_existing_output_without_relabeling_it_as_current(self):
        response = self.review('correct', value=3300000)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.preserved(body)
        self.assertEqual(body['extraction_candidates'][0]['value'], 3300000)
        self.assertEqual(body['extraction_candidates'][0]['original_value'], 3200000)
        self.assertEqual(body['input_revision'], 2)

    def test_reject_preserves_reviewed_document_history(self):
        response = self.review('reject')
        self.assertEqual(response.status_code, 200, response.text)
        self.preserved(response.json())
        self.assertEqual(response.json()['extraction_candidates'][0]['status'], 'rejected')

    def test_repeated_reviews_replace_queued_work_without_adding_generic_drafts(self):
        first = self.review('accept')
        self.assertEqual(first.status_code, 200, first.text)
        second = self.review('correct', value=3300000)
        self.assertEqual(second.status_code, 200, second.text)
        body = second.json()
        self.preserved(body)
        runs = body['ax_runs']
        self.assertEqual(sum(row['status'] == 'queued' for row in runs), 1)
        self.assertEqual(sum(row['status'] == 'cancelled' for row in runs), 1)
        self.assertEqual(body['input_revision'], 3)
        self.assertEqual([entry['action'] for entry in body['audit']], ['candidate.reviewed', 'candidate.reviewed'])

    def test_stale_review_does_not_change_outputs_or_schedule_a_new_run(self):
        response = self.review('accept')
        self.assertEqual(response.status_code, 200, response.text)
        before = copy.deepcopy(store.get_case(self.case['id']))
        self.pool.submit.reset_mock()
        conflict = self.client.post(self.path, json={'expected_version': 1, 'decision': 'correct',
            'value': 3400000, 'reason': '다른 화면에서 시작한 이전 버전 검토입니다.'})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(store.get_case(self.case['id']), before)
        self.pool.submit.assert_not_called()

    def finding(self, kind='document_request'):
        case = store.get_case(self.case['id'])
        catalog_id = main.registry()['documents'][0]['id']
        action = {'type': kind, 'catalog_id': catalog_id, 'period': '최근 자료 확인'}
        run = {'id': 'prior-analysis', 'case_id': case['id'], 'kind': 'case_review',
               'status': 'needs_review', 'created_at': store.now(), 'findings': [
                   {'id': 'finding', 'title': '원문 보완 확인', 'observation': '추가 원문을 확인해야 합니다.',
                    'origin': 'rule', 'action': action}]}
        signature = ax_service.fingerprint(case)
        with store.db() as con:
            con.execute('INSERT INTO ax_runs VALUES (?,?,?,?,?,?)',
                        (run['id'], case['id'], signature, 'fixed-test-law', run['status'], store.dumps(run)))
        def request(c, needs):
            c['requests'].append({'id': 'additional-request', 'catalog_id': catalog_id,
                'title': '추가 확인 서류', 'status': 'requested', 'document_ids': [], 'period': needs[0]['period']})
        self.stack.enter_context(patch.object(ax_service, 'ensure_requests', side_effect=request))
        return '/api/cases/' + case['id'] + '/ax-runs/prior-analysis/findings/finding/', signature

    def test_new_document_request_preserves_authored_outputs_and_schedules_after_commit(self):
        path, signature = self.finding()
        def committed_before_submit(*args):
            saved = store.get_case(self.case['id'])
            self.assertEqual(saved['requests'][-1]['status'], 'requested')
            self.assertEqual(saved['audit'][-1]['action'], 'ax.finding.applied')
        self.pool.submit.side_effect = committed_before_submit
        response = self.client.post(path + 'apply', json={'expected_version': 1, 'reason': '원문 보완이 필요한 제안을 반영합니다.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.preserved(response.json(), '분석 제안 업무 반영')
        self.pool.submit.assert_called_once()
        with store.db() as con:
            source_run = con.execute('SELECT signature,body FROM ax_runs WHERE id=?', ('prior-analysis',)).fetchone()
        self.assertEqual(source_run['signature'], signature)
        self.assertNotEqual(source_run['signature'], ax_service.fingerprint(store.get_case(self.case['id'])))
        self.assertEqual(json.loads(source_run['body'])['findings'][0]['review_status'], 'applied')
        # Exercise the real next-step gate without running a model or the worker.
        working = store.get_case(self.case['id'])
        with patch('apps.api.verification.run_local_verification_batched', side_effect=AssertionError('No model before receipt')):
            asyncio.run(automation.advance(working))
        self.assertEqual(working['ax_pipeline']['stage'], 'collecting')
        self.assertEqual(len(working['drafts']), 1)
        self.assertEqual(len(working['court_documents']), 1)
        self.generate.assert_not_called()

    def test_review_task_proposal_queues_instead_of_replacing_authored_draft(self):
        path, _ = self.finding('review_task')
        response = self.client.post(path + 'apply', json={'expected_version': 1, 'reason': '별도 확인 업무를 담당자에게 전달합니다.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.preserved(response.json(), '분석 제안 업무 반영')
        self.assertEqual(response.json()['tasks'][-1]['title'], '원문 보완 확인')
        self.pool.submit.assert_called_once()

    def test_dismissing_proposal_preserves_current_outputs_without_scheduling(self):
        path, signature = self.finding()
        response = self.client.post(path + 'dismiss', json={'expected_version': 1, 'reason': '이미 확인된 자료이므로 제안을 반려합니다.'})
        self.assertEqual(response.status_code, 200, response.text)
        saved = store.get_case(self.case['id'])
        self.assertEqual(saved['drafts'], self.case['drafts'])
        self.assertEqual(saved['court_documents'], self.case['court_documents'])
        self.assertEqual(saved['input_revision'], self.case['input_revision'])
        self.assertEqual(ax_service.fingerprint(saved), signature)
        self.assertEqual(hashlib.sha256(self.pdf.read_bytes()).hexdigest(), self.pdf_hash)
        self.pool.submit.assert_not_called()
        self.generate.assert_not_called()

    def test_classification_review_preserves_outputs_and_uses_existing_queue(self):
        self.stack.enter_context(patch.object(ax_service, 'ensure_requests'))
        response = self.client.post('/api/cases/' + self.case['id'] + '/intake-review', json={
            'expected_version': 1, 'client_name': self.case['client_name'], 'court_id': 'CT01',
            'case_type': 'personal_rehabilitation', 'reason': '담당자가 사건 관할과 유형을 확인했습니다.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.preserved(response.json(), '담당자 사건 분류 검토')
        self.pool.submit.assert_called_once()

    def test_identity_change_quarantines_sources_without_generating_a_replacement(self):
        self.stack.enter_context(patch.object(ax_service, 'ensure_requests'))
        response = self.client.post('/api/cases/' + self.case['id'] + '/intake-review', json={
            'expected_version': 1, 'client_name': '다른 합성 고객', 'court_id': 'CT01',
            'case_type': 'personal_rehabilitation', 'reason': '명의가 달라 자료 귀속을 다시 확인합니다.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.preserved(response.json(), '담당자 사건 분류 검토', documents_unchanged=False)
        source = store.get_case(self.case['id'])['documents'][0]
        self.assertEqual(source['status'], 'quarantined')
        self.assertEqual(source['text'], self.case['documents'][0]['text'])
        self.assertEqual(source['sha256'], self.case['documents'][0]['sha256'])
        self.pool.submit.assert_called_once()

    def test_stale_proposal_does_not_schedule_or_touch_documents(self):
        path, _ = self.finding()
        before = copy.deepcopy(store.get_case(self.case['id']))
        response = self.client.post(path + 'apply', json={'expected_version': 99, 'reason': '오래된 화면에서 검토한 제안입니다.'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(store.get_case(self.case['id']), before)
        self.pool.submit.assert_not_called()
        self.generate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
