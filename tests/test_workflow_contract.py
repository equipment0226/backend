import unittest
from apps.api import workflow_contract as workflow


class WorkflowContractTests(unittest.TestCase):
    def test_blocked_stage_never_marks_later_checks_completed(self):
        steps = workflow.project_steps(3, 'blocked')
        self.assertEqual([step['status'] for step in steps],
                         ['completed', 'completed', 'completed', 'blocked', 'waiting', 'waiting', 'waiting', 'waiting'])
        self.assertTrue(all(step['inputs'] and step['outputs'] and step['gate'] for step in steps))
        steps[0]['inputs'].append('caller mutation')
        self.assertNotIn('caller mutation', workflow.STAGES[0]['inputs'])

    def test_checkpoint_preserves_version_and_gate_without_private_reason(self):
        pipeline = {'stage': 'lawyer_review', 'status': 'blocked', 'input_revision': 7,
                    'steps': workflow.project_steps(3, 'blocked'), 'updated_at': '2026-10-03',
                    'legal_update': {'version': 'law-v2'},
                    'reasons': [{'code': 'EVIDENCE_REQUIRED', 'reason': 'PRIVATE_CUSTOMER_TEXT'}]}
        checkpoint = workflow.checkpoint(pipeline)
        self.assertEqual(checkpoint['current_step'], 'analysis')
        self.assertEqual(checkpoint['reason_codes'], ['EVIDENCE_REQUIRED'])
        self.assertEqual(checkpoint['legal_version'], 'law-v2')
        self.assertNotIn('PRIVATE_CUSTOMER_TEXT', str(checkpoint))

    def test_invalid_transition_coordinates_rejected(self):
        for index, status in [(len(workflow.STAGES), 'running'), (-1, 'waiting'), (2, 'assumed_pass')]:
            with self.assertRaises(ValueError):
                workflow.project_steps(index, status)

    def test_first_draft_completion_still_waits_for_human_and_submission(self):
        steps = workflow.project_steps(6, 'waiting')
        self.assertEqual([step['id'] for step in steps[-2:]], ['human_review', 'submission'])
        self.assertTrue(all(step['status'] == 'waiting' for step in steps[-2:]))
        steps[2]['status'] = 'review_required'
        pipeline = {'stage': 'human_review', 'status': 'waiting', 'steps': steps}
        checkpoint = workflow.checkpoint(pipeline)
        self.assertEqual(checkpoint['current_step'], 'human_review')
        self.assertNotIn('ocr', checkpoint['completed_steps'])
