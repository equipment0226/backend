"""A portal application is a request for a consultation, not a completed interview."""
from . import automation, domain, store


def pending(case):
    return bool(case.get('intake')) and case['intake'].get('status') != 'completed'


def require_completed(case):
    domain.require(not pending(case), 'CONSULTATION_REQUIRED',
                   '세부 상담을 요청하고 상담 기록을 저장한 뒤 추출을 시작하세요.')


def record_text(case):
    """Never substitute a portal application for a missing detailed interview."""
    record = case.get('consultation', {})
    if record.get('status') == 'quarantined' or pending(case):
        return ''
    if not case.get('intake'):
        return str(record.get('notes') or case.get('summary') or '')
    return '\n'.join(str(value).strip() for value in
                     [record.get('notes', ''), *record.get('answers', {}).values()] if str(value).strip())


def waiting_state(case):
    requested = case.get('intake', {}).get('status') == 'consultation_requested'
    case['stage'] = '세부 상담 대기' if requested else '상담 접수'
    detail = ('고객에게 세부 상담을 요청했습니다. 상담 후 확인한 내용을 기록하면 추출을 시작합니다.' if requested else
              '상담 신청이 접수되었습니다. 담당자가 신청 내용을 확인하고 세부 상담을 요청합니다.')
    steps = [
        {'id': 'application', 'title': '상담 신청 접수', 'status': 'completed'},
        {'id': 'consultation_request', 'title': '세부 상담 요청', 'status': 'completed' if requested else 'waiting'},
        {'id': 'consultation_record', 'title': '세부 상담 기록', 'status': 'waiting'},
        {'id': 'consultation_extraction', 'title': '상담 내용 추출', 'status': 'waiting'},
    ]
    next(step for step in steps if step['status'] == 'waiting')['detail'] = detail
    case['ax_pipeline'] = {'stage': 'consultation_waiting', 'status': 'waiting', 'label': case['stage'],
                           'summary': detail, 'steps': steps, 'reasons': [], 'strategies': [],
                           'updated_at': store.now(), 'input_revision': case['input_revision']}
    case['automation'] = {'stage': 'consultation_waiting', 'label': case['stage']}
    return case['ax_pipeline']


def request_consultation(case, user, message):
    domain.require(pending(case), 'CONSULTATION_REQUEST_STATE', '상담 접수·대기 중인 사건에서 세부 상담을 요청하세요.')
    message = message.strip()
    domain.require(len(message) >= 10, 'CONSULTATION_MESSAGE', '상담 방법과 확인할 내용을 10자 이상 적어 주세요.')
    domain.invalidate(case, '세부 상담 요청')
    timestamp, message_id = store.now(), store.uid('msg')
    case['intake'].update(status='consultation_requested', requested_at=timestamp,
                         requested_by=user['id'], request_message=message)
    case['messages'].append({'id': message_id, 'text': message, 'sender': user['name'],
                             'role': user['role'], 'kind': 'consultation_request', 'created_at': timestamp})
    for notice in case.get('notifications', []):
        if notice.get('kind') in {'application_received', 'consultation_request'} and not notice.get('resolved_at'):
            notice['resolved_at'] = timestamp
    automation.notify(case, 'consultation:' + message_id, '세부 상담 안내가 도착했습니다',
                      message, audience='client', kind='consultation_request')
    waiting_state(case)


def complete(case, user):
    if not case.get('intake'):
        return  # Staff-imported completed interviews keep their existing flow.
    domain.require(case['intake']['status'] in {'consultation_requested', 'completed'},
                   'CONSULTATION_REQUEST_REQUIRED', '고객에게 먼저 세부 상담을 요청하세요.')
    case['intake'].update(status='completed', completed_at=store.now(), completed_by=user['id'])
    for notice in case.get('notifications', []):
        if notice.get('kind') == 'consultation_request' and not notice.get('resolved_at'):
            notice['resolved_at'] = store.now()
    case['stage'] = '자료 수집'
    case.pop('ax_pipeline', None)
    case['automation'] = {'stage': 'extraction', 'label': '상담 내용 추출 준비'}


def migrate_legacy_application(case):
    """Preserve originals/uploads; retract only unsubmitted automatic requests.

    The immutable pre-migration case snapshot remains in store.history. Manual
    requests and case-specific court orders are outside this migration's scope.
    """
    if case.get('intake') or not any(e.get('action') == 'application.created' for e in case.get('audit', [])):
        return False
    recorded = any(e.get('action') == 'consultation.recorded' for e in case.get('audit', []))
    original = case.get('consultation', {}).get('notes') or case.get('summary', '')
    case['intake'] = {'status': 'completed' if recorded else 'received',
                      'application_notes': case.get('summary') or original,
                      'submitted_at': case.get('created_at'), 'migrated_at': store.now()}
    if recorded:
        return True
    domain.invalidate(case, '상담 신청과 세부 상담 기록 분리')
    case['intake']['original_application_record'] = case.pop('consultation', {})
    case['consultation'] = {}
    timestamp = store.now()
    for req in case.get('requests', []):
        managed = req.get('managed_by') == 'court_request_rules' or req.get('generated_by') == 'workflow_rulebook'
        if (not managed or req.get('manual_override') or req.get('court_order_id')
                or req.get('status') not in {'requested', 'needs_more'} or req.get('document_ids')
                or any(d.get('request_id') == req['id'] for d in case.get('documents', []))):
            continue
        previous = req['status']
        req.update(status='withdrawn', no_longer_required=True, withdrawn_at=timestamp,
                   withdrawal_reason='세부 상담 전 자동 요청을 철회했습니다. 상담 후 필요한 자료를 다시 안내합니다.',
                   version=req.get('version', 1) + 1)
        case.setdefault('request_history', []).append({'id': store.uid('reqevt'), 'request_id': req['id'],
            'action': 'withdrawn', 'previous_status': previous, 'reason': req['withdrawal_reason'], 'at': timestamp})
    # Candidate values extracted solely from the application are retained as
    # historical candidates, not reused as detailed consultation evidence.
    for candidate in case.get('extraction_candidates', []):
        if str(candidate.get('source_id', '')).startswith('consultation'):
            candidate.update(status='superseded', superseded_reason='세부 상담 기록 대기')
    automation.sync_request_notifications(case)
    waiting_state(case)
    return True


def migrate_stored_applications():
    import json
    with store.db() as con:
        cases = [json.loads(row['body']) for row in con.execute('SELECT body FROM cases')]
    for case in cases:
        if case.get('intake') or not any(e.get('action') == 'application.created' for e in case.get('audit', [])):
            continue
        store.mutate(case['id'], case['version'], {'name': '상담 흐름 업데이트', 'role': 'automation'},
                     'application.consultation_gate_migrated', migrate_legacy_application)
