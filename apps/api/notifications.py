"""Audience-safe, per-user read projections for persisted case events."""
import copy

from . import automation, store


def projected(case, user):
    audience = 'client' if user['role'] == 'client' else 'staff'
    rows = []
    for notice in case.get('notifications', []):
        if notice.get('audience') != audience:
            continue
        row = copy.deepcopy(notice)
        reads = row.pop('read_by', {})
        # Legacy client notices have one recipient. An old shared staff timestamp
        # cannot establish that this particular lawyer/staff member read it.
        read_at = reads.get(user['id']) or (row.get('read_at') if audience == 'client' and not reads else None)
        row.update(read_at=read_at, read_by_current_user=bool(read_at),
                   unread=not bool(read_at) and not bool(row.get('resolved_at')))
        rows.append(row)
    return rows


def mark_read(notice, user):
    notice.setdefault('read_by', {})[user['id']] = store.now()


def record_event(case, user, action):
    if action == 'message.created' and case.get('messages'):
        message = case['messages'][-1]
        is_client = user['role'] == 'client'
        automation.notify(case, 'message:' + message['id'],
                          '고객 메시지 도착' if is_client else '담당자 메시지 도착',
                          '새 메시지가 도착했습니다. 사건 대화에서 확인해주세요.',
                          audience='staff' if is_client else 'client',
                          kind='client_message' if is_client else 'staff_message')
    elif action == 'document.received' and user['role'] == 'client' and case.get('documents'):
        document = case['documents'][-1]
        automation.notify(case, 'upload:' + document['id'], '고객 서류 제출',
                          '고객이 서류를 제출했습니다. 자동 검증 진행 상태를 확인할 수 있습니다.',
                          kind='document_received')
