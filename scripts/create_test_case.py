"""Create one fictional intake, leaving consultation and uploads for manual testing.

Only the established local fresh-profile accounts are supported. No document,
extraction answer, legal assumption or approval is preloaded into the case.
"""
import argparse
import json
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def create(apply=False):
    from apps.api import accounts, automation, domain, intake_workflow, store
    folder = ROOT / 'examples/court_ready_fixture'
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('synthetic') is not True:
        raise ValueError('Only the explicitly fictional fixture may be created.')
    notes = (folder / 'application.txt').read_text(encoding='utf-8')
    with store.db() as con:
        rows = con.execute("SELECT * FROM accounts WHERE id IN ('customer','staff','lawyer') AND active=1").fetchall()
    roster = {row['id']: row for row in rows}
    if set(roster) != {'customer', 'staff', 'lawyer'} or any(row['auth_profile'] != 'fresh' for row in rows):
        raise ValueError('Use the local fresh-profile customer, staff and lawyer accounts.')
    customer = accounts.public(roster['customer'])
    if len({row['org_id'] for row in rows}) != 1:
        raise ValueError('All three test accounts must belong to the same workspace.')
    report = {'applied': False, 'name': manifest['name'], 'stage': '상담 신청 접수',
              'documents_preloaded': 0, 'legal_assumptions_preloaded': False,
              'fixture': folder.relative_to(ROOT).as_posix()}
    if not apply:
        return report
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', 8000)) == 0:
            raise RuntimeError('Stop this project API before preparing the fictional intake.')
    case = domain.new_case(manifest['name'], manifest['court_id'], '서울회생법원', notes, True)
    case.update(org_id=customer['org_id'], client_user_id=customer['id'], members=['staff','lawyer'],
                assigned_to=roster['staff']['name'], case_type='personal_rehabilitation')
    case['intake'] = {'status': 'received', 'application_notes': notes, 'submitted_at': store.now()}
    case['consultation'] = {}
    case['consent'] = {'simulated': True, 'purpose': '사용자가 요청한 가상사례 기능 시험',
                       'at': store.now(), 'version': 'synthetic-fixture-intake-v1'}
    case['testing_fixture'] = {'name': folder.name, 'manifest_sha256': store.digest(manifest)}
    intake_workflow.waiting_state(case)
    case['audit'].append({'id': store.uid('ev'), 'action': 'synthetic.application.created',
        'actor': '가상사례 준비', 'role': 'maintenance', 'at': store.now(), 'from_version': 0, 'to_version': 1})
    automation.notify(case, 'application:' + case['id'], '새 가상사례 상담 신청',
        '새 연습자료의 상담 신청입니다. 세부 상담 요청부터 직접 진행해 주세요.', kind='application_received')
    saved, created = store.insert_application(case, customer,
        'fixture-court-ready-' + store.digest(manifest)[:20], store.digest({'name':manifest['name'],'notes':notes}))
    report.update(applied=True, created=created, case_id=saved['id'], matter_number=saved.get('matter_number'))
    (ROOT / 'reports/new-test-case.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(create(parser.parse_args().apply), ensure_ascii=False, indent=2))
