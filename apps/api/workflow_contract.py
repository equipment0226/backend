"""Shared stage contracts for execution, progress and persisted checkpoints.

Structural reference, not copied implementation:
https://github.com/equipment0226/triz_backend/blob/main/docs/STAGES.md
Checkpoints record progress. Resume re-enters the evidence-gated orchestrator
and reuses only artifacts whose evidence and legal versions are still current.
"""
VERSION = 'case-workflow-v2-draft-then-human-review'
STAGES = (
    {'id': 'collection', 'title': '필요서류 선정·제출 확인',
     'inputs': ['현재 상담', '관할 법원 기준'], 'outputs': ['기관·계좌·기간별 요청', '제출 이력'],
     'gate': '현재 적용 법원 기준과 모든 유효 요청의 제출 범위 확인', 'on_failure': '자료 보완 요청'},
    {'id': 'validation', 'title': '서류 범위·내용 검증',
     'inputs': ['제출 원문', '요청 범위'], 'outputs': ['인물·종류·기간·발급옵션 검증'],
     'gate': '제출 원문과 요청의 일치', 'on_failure': '고객에게 잘못되거나 부족한 자료 재요청'},
    {'id': 'ocr', 'title': '추출값 원문 대조',
     'inputs': ['검증된 서류', '기존 추출 후보'], 'outputs': ['근거가 연결된 구조화 자료'],
     'gate': '추출값별 원문 대조 결과와 미확정 항목 기록', 'on_failure': '미확정 값을 확인 항목으로 남겨 1차 문서 작성'},
    {'id': 'analysis', 'title': '쟁점·전략·법률계산',
     'inputs': ['구조화 자료', '현행 근거', '검증된 법원 결과'], 'outputs': ['법률계산', '근거별 쟁점과 보완 전략'],
     'gate': '확인된 입력의 계산·전략 분석과 미확정 판단 구분', 'on_failure': '계산·판단 보완 사유를 1차 문서와 함께 제시'},
    {'id': 'draft', 'title': '1차 문서 자동 작성',
     'inputs': ['현재 사건 사실', '관련 법령·판례', '검증 계산'], 'outputs': ['근거별 진술 문단', '버전이 고정된 문서'],
     'gate': '확인된 사실·출처로 작성하고 누락·상충 내용은 확인 항목으로 구분', 'on_failure': '작성 가능한 부분과 작성 실패·누락 항목을 함께 보존'},
    {'id': 'review', 'title': 'AI 문서 검토',
     'inputs': ['작성 문서', '원문 근거', '계산과 공식 서식'], 'outputs': ['검증 범위가 명시된 작성본', '검토 이력'],
     'gate': '본문·출력값·원본 선택 조항 검토 결과 기록', 'on_failure': '문제 항목을 문서와 함께 담당자 보완으로 연결'},
    {'id': 'human_review', 'title': '담당자 보완·승인',
     'inputs': ['1차 작성 문서', 'AI 검토 결과', '미확정 판단·보완 항목'],
     'outputs': ['수정·재검증 문서', '변호사 최종 승인'],
     'gate': '필수 보완 및 문서 검증 완료 후 현재 문서 버전의 변호사 승인',
     'on_failure': '내용 수정·추가 자료 반영 후 AI 재검토'},
    {'id': 'submission', 'title': '제출 처리',
     'inputs': ['최종 승인된 제출 문서', '접수 근거'], 'outputs': ['제출 이력·접수 확인'],
     'gate': '승인된 문서 버전과 접수 근거 일치', 'on_failure': '변경 내용 재승인 또는 접수 근거 확인'},
)


def project_steps(index, status, detail=''):
    if not 0 <= index <= len(STAGES):
        raise ValueError('Unknown workflow stage index')
    if status not in {'running', 'waiting', 'blocked', 'completed'}:
        raise ValueError('Unknown workflow status')
    if index == len(STAGES) and status != 'completed':
        raise ValueError('Only a completed workflow may pass the last stage')
    return [{**stage, 'inputs': list(stage['inputs']), 'outputs': list(stage['outputs']),
             'status': 'completed' if status == 'completed' or i < index else status if i == index else 'waiting',
             'detail': detail if i == index else ''} for i, stage in enumerate(STAGES)]


def checkpoint(pipeline):
    """Metadata only; never duplicate source text or customer identity."""
    # A completed attempt can retain unresolved evidence while execution has
    # already reached the human review stage. Do not display the earlier issue
    # as the currently executing step.
    active = next((step for step in pipeline['steps'] if step['status'] in {'running', 'waiting', 'blocked'}), None)
    return {'contract_version': VERSION, 'stage': pipeline['stage'], 'status': pipeline['status'],
            'at': pipeline.get('updated_at'), 'input_revision': pipeline.get('input_revision'),
            'current_step': (active or {}).get('id'), 'gate': (active or {}).get('gate'),
            'completed_steps': [step['id'] for step in pipeline['steps'] if step['status'] == 'completed'],
            'reason_codes': [reason.get('code') for reason in pipeline.get('reasons', []) if isinstance(reason, dict)],
            'legal_version': pipeline.get('legal_update', {}).get('version')}
