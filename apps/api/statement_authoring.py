"""Local, evidence-grounded authorship for the manual official-statement action."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from . import automation, grounded_drafting, intake_workflow, legal_watch, store
from .verification import run_local_verification_batched


def _consultation(case):
    consultation=case.get('consultation',{})
    return {'text':intake_workflow.record_text(case),
            'source_id':'consultation:notes'} if consultation.get('status')!='quarantined' else None


def source_signature(case):
    """Bind a saved statement to current evidence, law and the writing contract."""
    try:policy=grounded_drafting.writing_policy()
    except (OSError,ValueError,KeyError,TypeError):policy={'status':'unavailable'}
    return store.digest({
        'input_revision':case.get('input_revision'),
        'court_id':case.get('court_id'),'org_id':case.get('org_id'),
        'documents':automation._sources(case,verified_only=True),
        'candidates':case.get('extraction_candidates',[]),'facts':case.get('facts',[]),
        'consultation':case.get('consultation',{}),'summary':case.get('summary'),'intake':case.get('intake'),
        'calculations':[{key:calculation.get(key) for key in ('id','input_revision','stale','input_hash','policy_hash','result_hash','inputs','summary')}
                        for calculation in case.get('legal_calculations',[])],
        'legal_dependencies':legal_watch.dependencies_signature({'court_id':case.get('court_id')}),
        'writer':grounded_drafting.VERSION,'writer_code':hashlib.sha256(Path(grounded_drafting.__file__).read_bytes()).hexdigest(),
        'writing_policy':policy,
        'local_model':grounded_drafting.model_client.provider_config('grounded_drafting')['model'],
        'approved_structures':automation.approved_examples(case),
    })


def current_run(case):
    candidates=[run for run in reversed(case.get('statement_runs',[]))
        if run.get('status')=='completed' and not run.get('stale')
        and run.get('input_revision')==case.get('input_revision')
        and run.get('verification',{}).get('passed') and run.get('statement')]
    if not candidates:return None
    expected=source_signature(case)
    return next((run for run in candidates if run.get('source_signature')==expected),None)


def current_verified_draft(case, *, require_semantic=True):
    """A past short-form draft must not bypass the current writing contract."""
    for draft in reversed(case.get('drafts',[])):
        if draft.get('stale') or draft.get('input_revision')!=case.get('input_revision') or (require_semantic and not draft.get('ai_review',{}).get('passed')):
            continue
        narrative=next((run for run in case.get('narrative_runs',[]) if run.get('id')==draft.get('narrative_id')),None)
        source_summary=bool(narrative and narrative.get('authoring_method')=='source_summary'
            and narrative.get('verification',{}).get('source_binding_passed') and not require_semantic)
        if not narrative or narrative.get('version')!=grounded_drafting.VERSION or not (narrative.get('verification',{}).get('passed') or source_summary):
            continue
        calculation=next((row for row in case.get('legal_calculations',[]) if row.get('id')==draft.get('calculation_id')),None)
        if draft.get('calculation_id') and (not calculation or calculation.get('stale') or calculation.get('input_revision')!=case.get('input_revision')):
            continue
        sources=automation._sources(case,verified_only=True)
        items=automation._verification_items(case,sources)
        if draft.get('narrative_source_signature'):
            from . import evidence_mapping
            if draft['narrative_source_signature']!=evidence_mapping.build(case)['source_signature']:
                continue
            selected=set(draft.get('narrative_fact_ids',[]))
            items=[item for item in items if item['id'] in selected]
            if {item['id'] for item in items}!=selected:
                continue
        signature=grounded_drafting.signature(items,automation._retrieve(case),
            automation.approved_examples(case),calculation or {},court_id=case.get('court_id'),org_id=case.get('org_id'),
            consultation=_consultation(case),section_ids=draft.get('narrative_section_ids'))
        if signature==narrative.get('input_signature'):
            return draft
    return None


def _failed(case,code,message,narrative=None,review=None):
    return {'status':'needs_review','code':code,'message':message,'statement':None,
            'input_revision':case.get('input_revision'),'source_signature':source_signature(case),
            'narrative':narrative or {},'verification':review or {'passed':False},
            'external_processing':False}


async def prepare(case,calculation=None,*,_feedback=None,_attempt=0):
    """Never copy consultation into output or let generated text prove itself."""
    signature=source_signature(case)
    cached=current_run(case)
    if cached and cached.get('calculation_id')==(calculation or {}).get('id'):
        return {**copy.deepcopy(cached),'reused':True}
    sources=automation._sources(case,verified_only=True)
    source_map={source['id']:source for source in sources}
    items=[item for item in automation._verification_items(case,sources)
           if isinstance(item.get('quote'),str) and item['quote'].strip()
           and all(ref in source_map and item['quote'] in source_map[ref]['text'] for ref in item['source_ids'])]
    if not items:
        return _failed(case,'STATEMENT_FACT_EVIDENCE_REQUIRED',
            '진술서 작성에 필요한 검증 서류와 원문에 연결된 추출값이 없습니다. 자료 검증을 완료한 뒤 다시 작성하세요.')
    consultation=_consultation(case)
    notes=consultation['text'] if consultation else None
    narrative=await grounded_drafting.compose(items,automation._retrieve(case),automation.approved_examples(case),
        calculation or {},court_id=case.get('court_id'),org_id=case.get('org_id'),
        consultation=consultation,
        revision_feedback=_feedback,section_ids=['statement'])
    section=next((section for section in narrative.get('sections',[]) if section.get('id')=='statement'),None)
    paragraphs=(section or {}).get('paragraphs',[])
    if narrative.get('status')!='completed' or not narrative.get('verification',{}).get('passed') or not paragraphs:
        return _failed(case,'STATEMENT_AUTHORING_REQUIRED',
            '진술서의 구체적인 경위와 근거를 확인하지 못해 작성을 보류했습니다. 작성 근거와 누락 자료를 검토해주세요.',narrative)
    # Compose's references are backed by original case quotes/public snapshots.
    # Keep originals too; never add the newly generated statement as a source.
    for source in narrative.get('source_refs',[]):
        source_map.setdefault(source['id'],source)
    if notes:source_map['consultation:notes']={'id':'consultation:notes','kind':'party_statement','text':notes}
    review_items=[{'id':f'statement:{index}','key':'grounded_narrative','value':paragraph['text'],
                   'source_ids':paragraph.get('source_ids',[])} for index,paragraph in enumerate(paragraphs)]
    review_items.extend({'id':'fact:'+item['id'],'key':item['key'],'value':item['value'],
                         'source_ids':item['source_ids'],'quote':item['quote']} for item in items)
    if any(not item['source_ids'] or any(ref not in source_map for ref in item['source_ids']) for item in review_items):
        return _failed(case,'STATEMENT_SOURCE_BINDING_REQUIRED','진술서 문장의 근거 연결을 확인하지 못했습니다.',narrative)
    review=await run_local_verification_batched('document',{'sources':list(source_map.values()),'items':review_items,
        'context':{'scope':'진술서 문장별 사실·금액·인과관계·법적 주장과 현재 사건 원문을 독립 대조한다. '
            '상담 진술은 당사자 주장으로만 취급하고 객관적 증빙으로 단정하지 않는다. '
            '가상 사례, 테스트 설명, 예시 문구가 제출 본문에 섞이거나 미확인 사유를 지어내면 통과시키지 않는다.'}})
    if not review.get('passed'):
        if _attempt==0 and review.get('status')=='needs_review' and review.get('findings') and not review.get('error'):
            revised=await prepare(case,calculation,_feedback=review['findings'],_attempt=1)
            revised.update(revision_attempts=1,prior_verification=review)
            return revised
        return _failed(case,'STATEMENT_VERIFICATION_REQUIRED',
            '작성한 진술서의 원문·금액·사유 대조가 완료되지 않았습니다. 검토 알림에서 부족한 근거를 확인해주세요.',narrative,review)
    return {'status':'completed','statement':'\n\n'.join(paragraph['text'] for paragraph in paragraphs),
            'paragraphs':copy.deepcopy(paragraphs),'narrative':narrative,'verification':review,
            'input_revision':case.get('input_revision'),'source_signature':signature,
            'calculation_id':(calculation or {}).get('id'),'external_processing':False,'reused':False,
            'revision_attempts':_attempt}


def save(case,result):
    if result.get('reused'):return result
    result=copy.deepcopy(result)
    result.update(id=store.uid('statement'),created_at=store.now())
    case.setdefault('statement_runs',[]).append(result)
    if result.get('status')!='completed':
        automation.notify(case,'statement:'+store.digest([case['input_revision'],result.get('code')]),
            '진술서 작성 검토 요청',result['message'])
    else:
        for notice in case.get('notifications',[]):
            if str(notice.get('key','')).startswith('statement:') and not notice.get('resolved_at'):
                notice['resolved_at']=store.now()
    return result
