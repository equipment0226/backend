"""Current evidence preempts obsolete I/O; progress cannot be hidden by a queue."""
import asyncio
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.api import automation, ax_engine, ax_service, domain, store


class ProgressProjectionTests(unittest.TestCase):
    def test_queued_update_does_not_hide_running_ocr(self):
        case = {'ax_pipeline': {'stage': 'collecting', 'status': 'waiting', 'stale': True}}
        runs = [{'id': 'latest', 'status': 'queued', 'input_revision': 8, 'trigger': '서류 검토 완료'},
                {'id': 'running', 'status': 'running', 'input_revision': 7,
                 'pipeline': {'stage': 'ocr_verification', 'status': 'running',
                              'progress': {'completed': 3, 'total': 11}, 'steps': []}}]
        ax_service.project_active_pipeline(case, runs)
        self.assertEqual(case['ax_pipeline']['stage'], 'ocr_verification')
        self.assertEqual(case['ax_pipeline']['progress']['completed'], 3)
        self.assertEqual(case['ax_pipeline']['queued_update']['input_revision'], 8)
        self.assertEqual(case['ax_pipeline']['active_run']['input_revision'], 7)
        self.assertNotIn('stale', case['ax_pipeline'])
        self.assertNotIn('queued_update', runs[1]['pipeline'])

    def test_queued_only_has_explicit_waiting_status_and_intake_remains_gated(self):
        case = {'ax_pipeline': {'stage': 'collecting', 'status': 'waiting', 'stale': True}}
        ax_service.project_active_pipeline(case, [{'status': 'queued'}])
        self.assertEqual(case['ax_pipeline']['status'], 'queued')
        pending = {'intake': {'status': 'received'}, 'ax_pipeline': {'stage': 'consultation_waiting'}}
        ax_service.project_active_pipeline(pending, [{'status': 'running', 'pipeline': {'stage': 'extraction'}}])
        self.assertEqual(pending['ax_pipeline']['stage'], 'consultation_waiting')


class ObsoleteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DATA_DIR', Path(directory)))
        self.stack.enter_context(patch.object(ax_service, 'CURRENT_CASE_POLL_SECONDS', .005))
        self.stack.enter_context(patch.object(ax_service, 'knowledge_signature', return_value='same-law'))
        store.initialize()
        self.case = domain.new_case('합성 고객', 'CT01', '서울회생법원', '급여소득 상담', True)
        self.user = {'id': 'staff', 'role': 'staff', 'name': '직원'}
        store.insert_case(self.case)

    def test_changed_evidence_cancels_inflight_io_without_overwriting_review(self):
        cancelled = []
        async def operation():
            current = store.get_case(self.case['id'])
            def edit(c):
                domain.invalidate(c, '마지막 서류 확인')
                c['last_manual_review'] = '보존할 검토 결과'
            store.mutate(current['id'], current['version'], self.user, 'document.verified', edit)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        with self.assertRaises(store.VersionConflict):
            asyncio.run(ax_service._while_current(operation(), self.case['id'], ax_service.fingerprint(self.case), 'same-law'))
        self.assertEqual(cancelled, [True])
        self.assertEqual(store.get_case(self.case['id'])['last_manual_review'], '보존할 검토 결과')

    def test_obsolete_worker_releases_queue_and_latest_job_advances(self):
        first = ax_service.schedule(self.case, self.user, Mock())['run_id']
        next_job = []
        cancelled = []
        async def obsolete_advance(working, checks, progress=None):
            current = store.get_case(self.case['id'])
            latest = store.mutate(current['id'], current['version'], self.user, 'document.verified',
                                  lambda c: domain.invalidate(c, '마지막 서류 검토 완료'))
            next_job.append((ax_service.schedule(latest, self.user, Mock())['run_id'], latest))
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        analysis = {'status': 'completed', 'document_checks': [], 'extracted_facts': [], 'findings': []}
        with patch.object(ax_engine, 'analyze_case', new=AsyncMock(return_value=analysis)), \
             patch.object(ax_service, 'ensure_requests'), \
             patch.object(automation, 'advance', side_effect=obsolete_advance):
            ax_service.worker(first, copy.deepcopy(self.case))
        self.assertEqual(cancelled, [True])
        async def current_advance(working, checks, progress=None):
            working['ax_pipeline'] = {'stage': 'legal_analysis', 'status': 'running', 'steps': []}
        with patch.object(ax_engine, 'analyze_case', new=AsyncMock(return_value=analysis)), \
             patch.object(ax_service, 'ensure_requests'), \
             patch.object(automation, 'advance', side_effect=current_advance):
            ax_service.worker(*next_job[0])
        self.assertEqual(store.get_case(self.case['id'])['ax_pipeline']['stage'], 'legal_analysis')
        with store.db() as con:
            rows = {row['id']: row['status'] for row in con.execute('SELECT id,status FROM ax_runs')}
        self.assertEqual(rows[first], 'stale')
        self.assertEqual(rows[next_job[0][0]], 'completed')

    def test_read_receipts_do_not_cancel_slow_work(self):
        async def operation():
            latest = store.get_case(self.case['id'])
            store.mutate(latest['id'], latest['version'], self.user, 'notification.read',
                         lambda c: c.update(notifications=[{'id': 'notice', 'read_at': store.now()}]))
            await asyncio.sleep(.015)
            return 'finished'
        result = asyncio.run(ax_service._while_current(operation(), self.case['id'], ax_service.fingerprint(self.case), 'same-law'))
        self.assertEqual(result, 'finished')

    def test_running_job_survives_history_window_after_many_cancelled_updates(self):
        with store.db() as con:
            for i in range(9):
                status = 'running' if i == 0 else 'queued' if i == 8 else 'cancelled'
                run = {'id': str(i), 'status': status}
                con.execute('INSERT INTO ax_runs VALUES (?,?,?,?,?,?)',
                            (str(i), self.case['id'], ax_service.fingerprint(self.case), 'same-law', status, store.dumps(run)))
        runs = ax_service.case_runs(self.case, limit=5)
        self.assertIn('0', [run['id'] for run in runs])
        self.assertIn('8', [run['id'] for run in runs])
        self.assertEqual(len(runs), 6)


if __name__ == '__main__':
    unittest.main()
