"""Track complete page parsing independently from semantic or legal approval."""
from __future__ import annotations

from . import store


def active_documents(case):
    """Select originals used by the current workflow, without parsing or mutation.

    Retired request originals remain in history but cannot re-enter a current
    calculation through an old verified flag. An explicit attachment list is
    authoritative, including an empty list. Imported standalone evidence with
    no known request remains eligible for its own extraction/review gates.
    """
    requests = {row['id']: row for row in case.get('requests', []) if row.get('id')}
    result = []
    for document in case.get('documents', []):
        if (document.get('status') in {'superseded', 'rejected', 'quarantined'}
                or document.get('source_type') == 'meeting'):
            continue
        request = requests.get(document.get('request_id'))
        if request is not None:
            if request.get('status') in {'withdrawn', 'cancelled', 'superseded'} or request.get('no_longer_required'):
                continue
            ids = request.get('document_ids')
            if isinstance(ids, list) and document.get('id') not in ids:
                continue
        result.append(document)
    return result


def source_signature(document):
    from .ax_engine import HARNESS_VERSION
    from .document_facts import VERSION as FACT_VERSION
    return store.digest({key:document.get(key) for key in
        ('id','sha256','version','text','page_texts')} | {'parser_version':HARNESS_VERSION,'fact_version':FACT_VERSION})


def candidates(document):
    from .ax_engine import case_sources, extract_factor_candidates
    return extract_factor_candidates(case_sources({'documents':[document]}))


def _identity(candidate):
    return store.digest([candidate['key'],candidate.get('value'),candidate.get('source_id') or
                         ('doc:'+candidate.get('document_id','')),str(candidate.get('quote',''))])


def _pages(document):
    pages=document.get('page_texts') or [{'page':1,'text':document.get('text','')}]
    total=max(len(pages),int(document.get('ocr_summary',{}).get('page_count') or 0))
    parsed=sum(bool((page.get('text','') if isinstance(page,dict) else str(page)).strip()) for page in pages)
    return parsed,total


def _page_gaps(document):
    return any(isinstance(page, dict) and (
        page.get('status') in {'manual_review', 'failed', 'partial'}
        or page.get('text_truncated') is True
        or any(line.get('requires_review') for line in page.get('lines', [])))
        for page in document.get('page_texts', []))


def capture(case, document, extracted=None):
    """Persist the parser's completion receipt after its candidates were stored."""
    expected=candidates(document) if extracted is None else extracted
    identities={_identity(row) for row in expected if row.get('document_id')==document['id']}
    stored={row.get('signature') or _identity(row) for row in case.get('extraction_candidates',[])
            if row.get('document_id')==document['id'] and row.get('status')!='quarantined'}
    parsed,total=_pages(document)
    complete=(parsed==total and total>0 and not _page_gaps(document) and not document.get('ocr_summary',{}).get('unreadable_pages')
              and identities <= stored)
    document['extraction_manifest']={'source_signature':source_signature(document),
        'status':'completed' if complete else 'failed','completed_at':store.now(),
        'expected_signatures':sorted(identities),
        'expected_count':len(identities),'stored_count':len(identities & stored),
        'parsed_pages':parsed,'total_pages':total}
    return document['extraction_manifest']


def document_state(case, document):
    parsed,total=_pages(document)
    current=[row for row in case.get('extraction_candidates',[]) if row.get('document_id')==document['id']
             and row.get('status') not in {'superseded','quarantined','rejected'}]
    result={'status':'pending','parsed_pages':parsed,'total_pages':total,'extracted_count':len(current),
            'can_review':False,'message':'추출값 정리를 기다리고 있습니다.'}
    if document.get('status') in {'quarantined','rejected','superseded'}:
        return {**result,'status':'failed','message':'현재 사건의 서류인지 먼저 확인해야 합니다.'}
    if (parsed!=total or not parsed or _page_gaps(document) or document.get('ocr_summary',{}).get('unreadable_pages')
            or document.get('extraction_status') in {'partial','manual_review','failed'}):
        return {**result,'status':'failed','message':'읽지 못한 페이지나 행이 있어 추출을 완료하지 못했습니다. 읽을 수 있는 원문을 보완해 주세요.'}
    if document.get('extraction_status') in {'pending','running','processing'}:
        return {**result,'status':'running' if document['extraction_status']!='pending' else 'pending',
                'message':'원문 페이지와 추출값을 읽고 있습니다.'}
    receipt=document.get('extraction_manifest') or {}
    if receipt:
        stored={row.get('signature') or _identity(row) for row in case.get('extraction_candidates',[])
                if row.get('document_id')==document['id'] and row.get('status')!='quarantined'}
        ready=(receipt.get('source_signature')==source_signature(document) and receipt.get('status')=='completed'
               and receipt.get('expected_count')==receipt.get('stored_count')
               and len(receipt.get('expected_signatures',[]))==receipt.get('expected_count')
               and set(receipt.get('expected_signatures',[]))<=stored)
    else:
        # Legacy documents are checked against a fresh deterministic parse; a
        # nonempty text string or one saved candidate is not proof of completion.
        expected={_identity(row) for row in candidates(document) if row.get('document_id')==document['id']}
        stored={row.get('signature') or _identity(row) for row in case.get('extraction_candidates',[])
                if row.get('document_id')==document['id'] and row.get('status')!='quarantined'}
        ready=expected <= stored
    if ready:
        return {**result,'status':'completed','can_review':True,
                'message':'모든 페이지의 추출이 끝났습니다. 원문과 추출값을 함께 검토해 주세요.' if current
                          else '원문 읽기가 끝났습니다. 정형 추출 항목이 없어 원문 내용을 직접 확인해 주세요.'}
    runs=case.get('ax_runs',[])
    processing=any(run.get('status')=='running' for run in runs)
    return {**result,'status':'running' if processing else 'pending',
            'message':'추출값을 정리하고 있습니다.' if processing else result['message']}


def project(case):
    for document in case.get('documents',[]):
        if document.get('source_type')!='meeting':
            document['extraction_state']=document_state(case,document)
    return case
