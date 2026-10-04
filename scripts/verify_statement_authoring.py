"""Live, local-only narrative check using synthetic facts; never opens a case DB."""
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.api import grounded_drafting, model_client
from apps.api.automation import _retrieve
from apps.api.verification import run_local_verification_batched


async def main():
    notes = ('가상 사례: 급여소득자로 생활비 부족으로 차용을 반복하였습니다. '
             '금융채무 7800만원과 상환의무 있는 가족 차용금 200만원이 있습니다. '
             '월 급여는 280만원입니다. 이 테스트 계산에 반영합니다. 실제사건이 아닙니다.')
    facts = [{'id': 'salary', 'key': 'monthly_income', 'value': 2800000,
              'quote': '월 급여는 280만원입니다.', 'source_ids': ['synthetic-payroll']},
             {'id': 'financial-debt', 'key': 'unsecured_debt', 'value': 78000000,
              'quote': '금융채무 7800만원', 'source_ids': ['synthetic-balance']},
             {'id': 'family-debt', 'key': 'family_debt', 'value': 2000000,
              'quote': '상환의무 있는 가족 차용금 200만원', 'source_ids': ['synthetic-loan']}]
    # Synthetic precomputed figures exercise provenance, not legal correctness.
    calculations = {'summary': {'monthly_creditor_capacity': 1000000, 'months': 36}}
    timings = []
    outputs = []
    original = model_client.generate
    async def measured(*args, **kwargs):
        if kwargs.get('task_role') not in model_client.LOCAL_ROLES:
            raise RuntimeError('This check permits local processing only')
        start = time.perf_counter()
        result = await original(*args, **kwargs)
        timings.append({'role': kwargs.get('task_role'), 'seconds': round(time.perf_counter()-start, 2),
                        'output_tokens': result.get('eval_count')})
        outputs.append(result.get('message', {}).get('content'))
        (ROOT/'reports/statement-authoring-synthetic-output.json').write_text(
            json.dumps({'synthetic_only': True, 'outputs': outputs}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(timings[-1]), flush=True)
        return result
    model_client.generate = measured
    previous = ROOT/'reports/statement-authoring-live.json'
    if '--review-saved' in sys.argv:
        saved = json.loads(previous.read_text(encoding='utf-8'))
        if not saved.get('synthetic_only') or saved['writer'].get('status') != 'completed':
            raise RuntimeError('A completed synthetic writer result is required')
        result = saved['writer']
    else:
        result = await grounded_drafting.compose(facts, _retrieve({'court_id': 'CT01'}), [], calculations,
            court_id='CT01', org_id='synthetic-office', consultation={'text': notes, 'source_id': 'consultation:notes'}, section_ids=['statement'])
    paragraphs = [p for s in result.get('sections', []) if s['id'] == 'statement' for p in s['paragraphs']]
    review = None
    if paragraphs:
        review = await run_local_verification_batched('document', {'sources': result['source_refs'],
            'items': [{'id': 'statement:' + str(i), 'key': 'grounded_narrative', 'value': p['text'],
                       'source_ids': p['source_ids']} for i, p in enumerate(paragraphs)],
            'context': {'scope': 'Synthetic sample. Check every claim, causal link and number. No unsupported efforts or promises.'}})
    report = {'synthetic_only': True, 'external_processing': False, 'version': grounded_drafting.VERSION,
              'writer': result, 'independent_review': review, 'timings': timings}
    (ROOT/'reports/statement-authoring-live.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'writer_status': result['status'], 'paragraph_count': len(paragraphs),
                      'review_passed': bool(review and review.get('passed'))}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
