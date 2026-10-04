"""Application text must not silently re-enter detailed interview drafting."""
import asyncio
import unittest
from unittest.mock import patch

from apps.api import automation, domain, intake_workflow, statement_authoring


class IntakeSourcesTests(unittest.TestCase):
    def test_answer_only_interview_uses_answers_not_original_application(self):
        case = {'intake': {'status': 'completed'}, 'summary': '신청 시 잘못 적은 정보',
                'consultation': {'notes': '', 'answers': {'income': '통화로 확인한 급여소득', 'assets': '재산 설명'}}}
        text = statement_authoring._consultation(case)['text']
        self.assertIn('통화로 확인한 급여소득', text)
        self.assertIn('재산 설명', text)
        self.assertNotIn(case['summary'], text)
        case['consultation']['answers'] = {}
        self.assertEqual(intake_workflow.record_text(case), '')

    def test_pending_or_quarantined_interview_does_not_become_draft_source(self):
        case = {'intake': {'status': 'received'}, 'summary': '접수 원문',
                'consultation': {'notes': '아직 완료되지 않은 상담'}}
        self.assertEqual(intake_workflow.record_text(case), '')
        case['intake']['status'] = 'completed'
        case['consultation']['status'] = 'quarantined'
        self.assertEqual(intake_workflow.record_text(case), '')

    def test_pending_automation_never_calls_document_validation(self):
        case = domain.new_case('합성 고객', 'CT01', '서울회생법원', '접수 원문')
        case['intake'] = {'status': 'consultation_requested'}
        with patch.object(automation, '_validate_requests') as validate:
            asyncio.run(automation.advance(case))
            validate.assert_not_called()
        self.assertEqual(case['stage'], '세부 상담 대기')
        self.assertEqual(case['requests'], [])

    def test_legacy_staff_interview_keeps_existing_source(self):
        self.assertEqual(intake_workflow.record_text({'summary': '직원 회의록'}), '직원 회의록')


if __name__ == '__main__':
    unittest.main()
