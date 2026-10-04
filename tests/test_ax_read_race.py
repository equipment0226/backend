"""SQLite worker commits survive concurrent read receipts but reject changed case data."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.api import automation, ax_engine, ax_service, domain, notifications, store


class AnalysisReadRaceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(directory)))
        store.initialize()
        self.knowledge = 'known-source-version'
        self.stack.enter_context(patch.object(ax_service, 'knowledge_signature', side_effect=lambda case=None: self.knowledge))
        self.stack.enter_context(patch.object(ax_service, 'ensure_requests'))
        self.stack.enter_context(patch.object(ax_service.rulebook, 'evaluate_case', return_value={'required_documents': []}))
        self.stack.enter_context(patch.object(ax_engine, 'analyze_case', new=AsyncMock(return_value={
            'status': 'completed', 'document_checks': [], 'extracted_facts': [], 'findings': []})))
        self.case = domain.new_case('동시성 검증', 'CT01', '서울회생법원', '합성 상담', True)
        self.case['client_user_id'] = 'customer'
        self.case['documents'] = [{'id': 'source', 'text': '원래 제출 원문', 'sha256': 'old-hash',
                                   'status': 'received', 'version': 1}]
        automation.notify(self.case, 'initial-review', '자료 확인', '기존 검토 알림', kind='review_request')
        self.notice_id = self.case['notifications'][0]['id']
        store.insert_case(self.case)
        self.staff = {'id': 'staff', 'name': '담당 직원', 'role': 'staff', 'org_id': 'office-1'}
        self.lawyer = {'id': 'lawyer', 'name': '담당 변호사', 'role': 'lawyer', 'org_id': 'office-1'}
        self.run_id = ax_service.schedule(self.case, self.staff, Mock(), trigger='동시성 검증')['run_id']

    def read_notice(self, user):
        latest = store.get_case(self.case['id'])
        return store.mutate(latest['id'], latest['version'], user, 'notification.read',
            lambda case: notifications.mark_read(domain.item(case, 'notifications', self.notice_id), user))

    def run_worker(self, concurrent=None):
        async def advance(working, checks, progress=None):
            if concurrent:
                concurrent()
            notice = domain.item(working, 'notifications', self.notice_id)
            notice['resolved_at'] = 'worker-resolved-time'
            automation.notify(working, 'generated-notice', '자료 요청', '실제 분석 후 새 알림',
                              audience='client', kind='document_request')
            working['ax_pipeline'] = {'stage': 'collecting', 'status': 'waiting', 'steps': [],
                                       'input_revision': working['input_revision'], 'updated_at': store.now()}
            working['analysis_test_result'] = 'worker-result'
        with patch.object(automation, 'advance', side_effect=advance):
            ax_service.worker(self.run_id, copy.deepcopy(self.case))
        with store.db() as con:
            run = json.loads(con.execute('SELECT body FROM ax_runs WHERE id=?', (self.run_id,)).fetchone()['body'])
        return store.get_case(self.case['id']), run

    def test_reads_during_advance_keep_both_readers_audit_versions_and_generated_alerts(self):
        read_states = []
        def read_both():
            read_states.append(self.read_notice(self.staff))
            read_states.append(self.read_notice(self.lawyer))
        saved, run = self.run_worker(read_both)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(saved['version'], 4)
        self.assertEqual(saved['analysis_test_result'], 'worker-result')
        notice = domain.item(saved, 'notifications', self.notice_id)
        expected = domain.item(read_states[-1], 'notifications', self.notice_id)
        self.assertEqual(notice['read_by'], expected['read_by'])
        self.assertEqual(set(notice['read_by']), {'staff', 'lawyer'})
        self.assertEqual(notice['resolved_at'], 'worker-resolved-time')
        self.assertEqual(len(saved['notifications']), 2)
        self.assertEqual(saved['notifications'][-1]['key'], 'generated-notice')
        self.assertEqual([event['action'] for event in saved['audit']],
                         ['notification.read', 'notification.read', 'ax.analysis_and_draft'])
        self.assertEqual([event['to_version'] for event in saved['audit']], [2, 3, 4])
        self.assertEqual(saved['audit'][-1]['previous_hash'], store.digest(read_states[-1]))
        with store.db() as con:
            versions = [row[0] for row in con.execute('SELECT version FROM history WHERE case_id=? ORDER BY version', (saved['id'],))]
        self.assertEqual(versions, [1, 2, 3, 4])

    def test_actual_document_change_during_advance_is_stale_and_never_overwritten(self):
        def change_document():
            def change(case):
                case['documents'][0].update(text='실제 변경 원문', sha256='new-hash', version=2)
                domain.invalidate(case, '새 원문')
            latest = store.get_case(self.case['id'])
            store.mutate(latest['id'], latest['version'], self.staff, 'document.received', change)
        saved, run = self.run_worker(change_document)
        self.assertEqual(run['status'], 'stale')
        self.assertEqual(saved['documents'][0]['text'], '실제 변경 원문')
        self.assertEqual(saved['version'], 2)
        self.assertNotIn('analysis_test_result', saved)
        self.assertIsNone(saved['notifications'][0]['resolved_at'])
        self.assertEqual(len(saved['notifications']), 1)

    def test_decision_outside_evidence_fingerprint_is_stale_and_preserved(self):
        def add_decision():
            latest = store.get_case(self.case['id'])
            changed = store.mutate(latest['id'], latest['version'], self.lawyer, 'workflow.changed',
                lambda case: case.update(stage='검토 보류', stage_reason='담당자 추가 판단'))
            self.assertEqual(ax_service.fingerprint(changed), ax_service.fingerprint(self.case))
        saved, run = self.run_worker(add_decision)
        self.assertEqual(run['status'], 'stale')
        self.assertEqual(saved['stage'], '검토 보류')
        self.assertEqual(saved['stage_reason'], '담당자 추가 판단')
        self.assertNotIn('analysis_test_result', saved)

    def test_law_change_during_advance_is_stale_even_without_case_version_change(self):
        def change_law():
            self.knowledge = 'new-effective-source-version'
        saved, run = self.run_worker(change_law)
        self.assertEqual(run['status'], 'stale')
        self.assertEqual(saved['version'], 1)
        self.assertNotIn('analysis_test_result', saved)

    def test_read_between_latest_fetch_and_commit_retries_and_preserves_receipt(self):
        original_mutate = store.mutate
        interrupted = []
        def mutate(*args, **kwargs):
            if args[3] == 'ax.analysis_and_draft' and not interrupted:
                interrupted.append(True)
                latest = store.get_case(self.case['id'])
                original_mutate(latest['id'], latest['version'], self.staff, 'notification.read',
                    lambda case: notifications.mark_read(domain.item(case, 'notifications', self.notice_id), self.staff))
            return original_mutate(*args, **kwargs)
        with patch.object(store, 'mutate', side_effect=mutate):
            saved, run = self.run_worker()
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(saved['version'], 3)
        self.assertTrue(saved['notifications'][0]['read_by']['staff'])
        self.assertEqual([event['action'] for event in saved['audit']], ['notification.read', 'ax.analysis_and_draft'])

    def test_legacy_read_timestamp_is_preserved_without_reopening_worker_resolution(self):
        def legacy_read():
            latest = store.get_case(self.case['id'])
            store.mutate(latest['id'], latest['version'], self.staff, 'notification.read',
                lambda case: domain.item(case, 'notifications', self.notice_id).update(read_at='legacy-read-time'))
        saved, run = self.run_worker(legacy_read)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(saved['notifications'][0]['read_at'], 'legacy-read-time')
        self.assertEqual(saved['notifications'][0]['resolved_at'], 'worker-resolved-time')


if __name__ == '__main__':
    unittest.main()
