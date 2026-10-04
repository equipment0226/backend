"""Local OCR/form/calculation workflow shared by both fronts and future n8n nodes."""
import copy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import domain, store, legal_calculator, download_names


class Versioned(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: StrictInt = Field(ge=1)


class CalculationCreate(Versioned):
    inputs: dict


class Review(Versioned):
    reason: str = Field(min_length=5, max_length=4000)


class CourtDocumentCreate(Versioned):
    template_id: str = Field(max_length=80)
    fields: dict = Field(default_factory=dict)
    calculation_id: str | None = None


def _lawyer(user):
    if user['role'] != 'lawyer':
        raise HTTPException(403, '최종 검토 승인은 변호사 권한이 필요합니다.')


def _current_calculation(case, calculation):
    domain.require(not calculation.get('stale') and calculation['input_revision'] == case.get('input_revision'),
                   'CALCULATION_STALE', '입력 자료가 변경되었습니다. 현재 근거로 다시 계산하세요.')
    fresh = legal_calculator.calculate_legal(case, calculation['inputs'])
    domain.require(fresh['status'] == 'ready_for_review', 'CALCULATION_BLOCKED',
                   '증빙 또는 계산 요건을 충족하지 못했습니다. 다시 계산하여 확인하세요.')
    for key in ('input_hash', 'policy_hash', 'result_hash'):
        domain.require(calculation.get(key) == fresh.get(key), 'CALCULATION_CHANGED',
                       '입력값·계산 기준·결과가 변경되었습니다. 다시 계산하고 검토하세요.')
    # Compare persisted numerical content too, not just a saved hash field.
    for key in ('summary', 'schedule', 'creditor_allocations'):
        domain.require(calculation.get(key) == fresh.get(key), 'CALCULATION_CHANGED', '저장 결과가 재계산 결과와 일치하지 않습니다.')
    return fresh


def _approved_calculation(case, calculation_id):
    if not calculation_id:
        return None
    calc = domain.item(case, 'legal_calculations', calculation_id)
    _current_calculation(case, calc)
    authority = calc.get('approval') if calc.get('status') == 'approved' else calc.get('auto_preparation')
    domain.require(bool(authority) and (calc.get('status') == 'approved' or authority.get('passed') is True),
                   'CALCULATION_NOT_APPROVED', '법원 서식에는 검증을 완료한 현재 계산을 연결하세요.')
    for key in ('input_hash', 'policy_hash', 'result_hash'):
        domain.require(authority.get(key) == calc.get(key), 'APPROVAL_MISMATCH', '검증 당시의 계산과 다릅니다.')
    return calc


def _form_preview(case, template_id, fields=None, calculation=None):
    from . import court_forms
    try:
        return court_forms.preview(case, template_id, fields, calculation)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _artifact_path(case_id, document_id):
    # IDs originate from the store, never from a submitted file path.
    path = store.DATA_DIR / 'generated' / case_id / (document_id + '.pdf')
    domain.require(path.resolve().is_relative_to((store.DATA_DIR / 'generated').resolve()),
                   'ARTIFACT_PATH', '잘못된 문서 경로입니다.')
    return path


def attach(app, staff, authorize, change):
    @app.get('/api/legal-calculation/schema')
    def calculation_schema(user=Depends(staff)):
        return legal_calculator.schema()

    @app.post('/api/cases/{case_id}/legal-calculations')
    def calculate(case_id: str, data: CalculationCreate, user=Depends(staff)):
        try:
            encoded = json.dumps(data.inputs, ensure_ascii=False, allow_nan=False)
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, '유한한 숫자와 유효한 JSON 계산 입력이 필요합니다.') from exc
        domain.require(len(encoded) <= 250000,
                       'INPUT_SIZE', '계산 입력이 너무 큽니다.')
        def apply(case):
            result = legal_calculator.calculate_legal(case, data.inputs, user)
            result.update(id=store.uid('legal'), created_at=store.now(), created_by=user['id'])
            case.setdefault('legal_calculations', []).append(result)
        return change(case_id, user, data.expected_version, 'legal_calculation.created', apply)

    @app.post('/api/cases/{case_id}/legal-calculations/{calculation_id}/approve')
    def approve_calculation(case_id: str, calculation_id: str, data: Review, user=Depends(staff)):
        _lawyer(user)
        def apply(case):
            calc = domain.item(case, 'legal_calculations', calculation_id)
            _current_calculation(case, calc)
            domain.require(calc['status'] == 'ready_for_review', 'ALREADY_REVIEWED', '검토 대기 계산만 승인할 수 있습니다.')
            calc['approval'] = {'actor': user['id'], 'actor_name': user['name'], 'reason': data.reason,
                                'at': store.now(), 'input_revision': case.get('input_revision'),
                                **{key: calc[key] for key in ('input_hash', 'policy_hash', 'result_hash')}}
            calc['status'] = 'approved'
        return change(case_id, user, data.expected_version, 'legal_calculation.approved', apply)

    @app.get('/api/cases/{case_id}/legal-calculations/{calculation_id}/download')
    def calculation_download(case_id: str, calculation_id: str, user=Depends(staff)):
        case = authorize(case_id, user)
        calc = domain.item(case, 'legal_calculations', calculation_id)
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        # User-supplied text never becomes an Excel formula.
        safe = lambda s: "'" + s if isinstance(s, str) and s.lstrip().startswith(('=', '+', '-', '@')) else s
        writer.writerow(['개인회생 계산·변제 일정', safe(case['client_name']), '가상자료' if case.get('synthetic') else ''])
        writer.writerow(['검토 상태', '재검토 필요' if calc.get('stale') else calc['status']])
        writer.writerow(['입력 해시', calc['input_hash'], '기준 버전', calc['policy_version']])
        writer.writerow(['원 단위 정수 계산 / 인가 결과와 별개'])
        writer.writerow(['회차', '채권자 ID', '채권자', '원금 변제액', '이자 변제액', '합계', '회차 총 납입액'])
        names = {row['creditor_id']: row['name'] for row in calc.get('creditor_allocations', [])}
        for month in calc.get('schedule', []):
            for row in month['allocations']:
                writer.writerow([month['month'], safe(row['creditor_id']), safe(names.get(row['creditor_id'], '')),
                                 row['principal'], row['interest'], row['total'], month['deposit']])
        store.access(user, case_id, 'legal_calculation.download:' + calculation_id)
        return Response(out.getvalue().encode('utf-8-sig'), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{calc["id"]}.csv"'})

    @app.get('/api/court-forms')
    def forms_catalog(court_id: str | None = None, user=Depends(staff)):
        from .court_forms import catalog
        return catalog(court_id)

    @app.get('/api/court-forms/{template_id}/original')
    def form_original(template_id: str, format: Literal['pdf', 'hwp'] = 'pdf', case_id: str | None = None, user=Depends(staff)):
        from .court_forms import original_path, SOURCES, TEMPLATES
        case = authorize(case_id, user) if case_id else None
        try:
            path = original_path(template_id)
            if format == 'hwp':
                source = next((s for s in SOURCES if s['id'] == template_id + '-hwp'), None)
                if not source:
                    raise ValueError('HWP original unavailable')
                path = store.ROOT / source['path']
                if hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
                    raise ValueError('HWP original hash mismatch')
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, '등록된 공식 원본이 없습니다.') from exc
        return FileResponse(path, media_type='application/pdf' if format == 'pdf' else 'application/octet-stream',
                            filename=download_names.filename(case, TEMPLATES[template_id]['title'] + '_원본', format))

    @app.get('/api/court-forms/{template_id}/preview')
    def form_preview(template_id: str, case_id: str, calculation_id: str | None = None, user=Depends(staff)):
        case = authorize(case_id, user)
        return _form_preview(case, template_id, calculation=_approved_calculation(case, calculation_id))

    @app.post('/api/cases/{case_id}/court-documents')
    async def create_form(case_id: str, data: CourtDocumentCreate, user=Depends(staff)):
        from .court_forms import render_pdf
        domain.require(len(data.fields) <= 300 and all(isinstance(k, str) and len(k) <= 100 and
                       (v is None or type(v) in (str, int, float)) and
                       (not isinstance(v, float) or math.isfinite(v)) and len(str(v)) <= 12000
                       for k, v in data.fields.items()), 'FORM_FIELDS', '서식 입력값의 종류와 길이를 확인하세요.')
        fields=copy.deepcopy(data.fields)
        calculation_id=data.calculation_id
        statement_result=None
        if data.template_id=='D5105':
            from . import statement_authoring
            domain.require(fields.get('statement') is None or isinstance(fields.get('statement'),str),
                'STATEMENT_TEXT','진술서 본문은 문장으로 입력해주세요.')
            explicit_statement=bool(str(fields.get('statement') or '').strip())
            if not explicit_statement:fields.pop('statement',None)
            snapshot=authorize(case_id,user)
            domain.require(snapshot['version']==data.expected_version,'VERSION_CONFLICT',
                '사건 자료가 변경되었습니다. 새로고침 후 다시 작성하세요.')
            calc=_approved_calculation(snapshot,calculation_id)
            if calc is None:
                for candidate in reversed(snapshot.get('legal_calculations',[])):
                    if candidate.get('stale') or candidate.get('input_revision')!=snapshot.get('input_revision'):continue
                    try:calc=_approved_calculation(snapshot,candidate['id'])
                    except domain.DomainError:continue
                    if calc:calculation_id=calc['id'];break
            existing=_form_preview(snapshot,data.template_id,fields,calc)
            statement=next((field for field in existing['fields'] if field['key']=='statement'),{})
            if not explicit_statement and not (statement.get('value') and
                    statement.get('source',{}).get('type')=='grounded_narrative' and statement.get('status')=='automatically_verified'):
                statement_result=await statement_authoring.prepare(copy.deepcopy(snapshot),calc)
                if statement_result.get('status')!='completed':
                    saved=change(case_id,user,data.expected_version,'statement.authoring_blocked',
                                 lambda case:statement_authoring.save(case,statement_result))
                    raise HTTPException(409,{'code':statement_result.get('code','STATEMENT_AUTHORING_REQUIRED'),
                        'message':statement_result['message'],'case_version':saved['version']})
        def apply(case):
            calc = _approved_calculation(case, calculation_id)
            statement_run=None
            if statement_result is not None:
                from . import statement_authoring
                domain.require(statement_authoring.source_signature(case)==statement_result['source_signature'],
                    'STATEMENT_SOURCE_CHANGED','진술서 작성 중 원문 또는 적용 근거가 변경되었습니다. 현재 자료로 다시 작성하세요.')
                statement_run=statement_authoring.save(case,statement_result)
            preview = _form_preview(case, data.template_id, fields, calc)
            document_id = store.uid('court-doc')
            try:
                pdf = render_pdf(data.template_id, case, fields, calc)
            except (KeyError, ValueError) as exc:
                raise HTTPException(422, str(exc)) from exc
            document = {'id': document_id, 'template_id': data.template_id,
                        'created_at': store.now(), 'created_by': user['id'], 'input_revision': case.get('input_revision'),
                        'status': 'draft', 'stale': False, 'fields': copy.deepcopy(fields),
                        'calculation_id': calculation_id, 'preview': preview,
                        'original_sha256': preview['original_sha256'],
                        'sha256': hashlib.sha256(pdf).hexdigest(), 'approval': None,
                        'ai_review': {'passed': False, 'status': 'pending', 'scope': 'rendered_pdf'}}
            if data.template_id=='D5105':
                statement_field=next((field for field in preview['fields'] if field['key']=='statement'),{})
                document['statement_source']=copy.deepcopy(statement_field.get('source',{}))
                if statement_run:
                    document['statement_authoring_id']=statement_run['id']
                    document['statement_review']=copy.deepcopy(statement_run['verification'])
                    document['statement_review']['scope']='statement_text_and_source_values_only'
                # A passed narrative review does not approve the rendered PDF.
                document['ai_review']={'passed':False,'status':'pending','scope':'rendered_pdf'}
            from .automation import _features
            document['generation_features'] = _features(case, calc, document)
            document['content_hash'] = store.digest({'fields': document['fields'], 'preview': preview,
                'input_revision': document['input_revision'], 'calculation_id': calculation_id})
            path = _artifact_path(case['id'], document_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pdf)
            case.setdefault('court_documents', []).append(document)
        return change(case_id, user, data.expected_version, 'court_document.created', apply)

    @app.post('/api/cases/{case_id}/court-documents/{document_id}/approve')
    def approve_form(case_id: str, document_id: str, data: Review, user=Depends(staff)):
        _lawyer(user)
        def apply(case):
            doc = domain.item(case, 'court_documents', document_id)
            domain.require(not doc.get('stale') and doc['input_revision'] == case.get('input_revision'),
                           'FORM_STALE', '사건 자료가 바뀌었습니다. 서식을 다시 생성하세요.')
            domain.require(doc['status'] == 'draft', 'ALREADY_REVIEWED', '검토 대기 서식만 승인할 수 있습니다.')
            calc = _approved_calculation(case, doc.get('calculation_id'))
            fresh = _form_preview(case, doc['template_id'], doc['fields'], calc)
            domain.require(fresh['ready_for_review'], 'FORM_INCOMPLETE', '서식의 필수 항목과 계산 검토를 먼저 완료하세요.')
            domain.require(fresh['original_sha256'] == doc['original_sha256'], 'TEMPLATE_CHANGED', '공식 원본이 변경되었습니다. 다시 생성하세요.')
            content_hash = store.digest({'fields': doc['fields'], 'preview': fresh,
                'input_revision': doc['input_revision'], 'calculation_id': doc.get('calculation_id')})
            domain.require(content_hash == doc['content_hash'], 'FORM_CHANGED', '서식 내용이 생성 당시와 다릅니다.')
            path = _artifact_path(case['id'], doc['id'])
            domain.require(path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == doc['sha256'],
                           'ARTIFACT_CHANGED', '저장된 PDF를 검증할 수 없습니다. 다시 생성하세요.')
            doc['status'] = 'review_approved'
            doc['approval'] = {'actor': user['id'], 'actor_name': user['name'], 'at': store.now(),
                               'reason': data.reason, 'sha256': doc['sha256'], 'content_hash': content_hash,
                               'input_revision': case.get('input_revision')}
        return change(case_id, user, data.expected_version, 'court_document.approved', apply)

    @app.get('/api/cases/{case_id}/court-documents/{document_id}/download')
    def download_form(case_id: str, document_id: str, user=Depends(staff)):
        from .court_forms import TEMPLATES
        case = authorize(case_id, user)
        doc = domain.item(case, 'court_documents', document_id)
        path = _artifact_path(case['id'], doc['id'])
        domain.require(path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == doc['sha256'],
                       'ARTIFACT_CHANGED', '저장된 PDF를 검증할 수 없습니다.')
        store.access(user, case_id, 'court_document.download:' + document_id)
        title = TEMPLATES.get(doc['template_id'], {}).get('title') or doc.get('title') or '법원서식'
        return FileResponse(path, media_type='application/pdf', filename=download_names.filename(case, title))

    @app.get('/api/testing/sample-bundle')
    def sample_bundle(case_id: str = '', user=Depends(staff)):
        import os
        fresh = os.getenv('DEBTOFF_START_PROFILE') == 'fresh'
        from .testing_materials import current
        case = authorize(case_id, user) if case_id else None
        try:
            path = current(case.get('client_name') if case else None)['bundle'] if fresh else store.ROOT / 'examples/synthetic_case_bundle.zip'
        except FileNotFoundError:
            raise HTTPException(404, '이 사건 이름에 맞는 연습자료가 없습니다. 같은 사람의 자료를 직접 준비해 주세요.')
        if not path.exists():
            raise HTTPException(404, '가상자료 묶음이 아직 생성되지 않았습니다.')
        return FileResponse(path, media_type='application/zip', filename='새출발_연습자료.zip' if fresh else 'debtoff-synthetic-case.zip')
