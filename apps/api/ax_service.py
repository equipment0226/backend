"""Event -> evidence/rules -> attributed candidates -> first drafts -> lawyer review."""
import asyncio
import contextlib
import copy
import json
import os
import traceback
from pathlib import Path
from . import domain, store, rulebook, drafting, intake_workflow

SYSTEM={'id':'automation','name':'자료 분석 자동화','role':'automation','org_id':'office-1'}
CURRENT_CASE_POLL_SECONDS = 1.0


def knowledge_signature(case=None):
    try:
        from .corpus import corpus_signature
        from .model_client import provider_config
        from .ax_engine import HARNESS_VERSION
        from .prompt_registry import signature
        from .automation import VERSION
        from . import legal_watch, court_forms, grounded_drafting, auto_documents, workflow_contract
        court_file = store.ROOT / 'data/court_request_rules.json'
        court_policy = json.loads(court_file.read_text(encoding='utf-8')) if court_file.exists() else {}
        court_id = (case or {}).get('court_id')
        if court_id and isinstance(court_policy.get('courts'), dict):
            court_policy['courts'] = {court_id: court_policy['courts'].get(court_id)}
            court_policy['sources'] = {key: value for key, value in court_policy.get('sources', {}).items()
                                       if value.get('scope') in (None, 'national', court_id)}
        return store.digest([corpus_signature(court_id),provider_config(),HARNESS_VERSION,signature(),VERSION,
                             court_policy, legal_watch.dependencies_signature(case or {}),
                             court_forms.RENDERER_VERSION, court_forms.SOURCES,
                             auto_documents.VERIFICATION_VERSION,
                             grounded_drafting.VERSION, grounded_drafting.writing_policy(),
                             workflow_contract.VERSION, workflow_contract.STAGES])
    except (ImportError,OSError,ValueError):return 'unavailable'


def fingerprint(case):
    return store.digest({k:case.get(k) for k in ('input_revision','client_name','region','court_id','court_name','case_type','documents','facts','issues','messages','requests','consultation','intake','corrections')} | {'accepted_candidates':[c for c in case.get('extraction_candidates',[]) if c.get('status') in ('accepted','rejected')], 'manual_calculations': [x for x in case.get('legal_calculations', []) if x.get('created_by') not in (None, 'automation')]})


def _save(table,run,**columns):
    with store.db() as con:
        fields={'status':run['status'],'body':store.dumps(run),**columns}
        con.execute(f"UPDATE {table} SET "+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),run['id']))


def present(row,case):
    run=json.loads(row['body'])
    if run['status'] not in ('queued','running','failed','cancelled') and (row['signature']!=fingerprint(case) or row['knowledge_signature']!=knowledge_signature(case)):
        run.update(status='stale',error='사건 자료 또는 수집 지식이 변경되었습니다. 현재 자료로 다시 분석하세요.')
    run.pop('input_text',None)
    return run


def case_runs(case,limit=5):
    with store.db() as con:
        # A burst of replaced queued jobs must not push the actual running job
        # out of the progress projection's recent-history window.
        rows=con.execute('SELECT * FROM ax_runs WHERE case_id=? AND (id IN '
                         '(SELECT id FROM ax_runs WHERE case_id=? ORDER BY rowid DESC LIMIT ?) '
                         "OR status IN ('running','queued')) ORDER BY rowid DESC",
                         (case['id'],case['id'],limit)).fetchall()
    return [present(r,case) for r in rows]


def project_active_pipeline(case, runs):
    """Keep current execution separate from the latest queued evidence update."""
    if intake_workflow.pending(case):
        return
    running = next((r for r in runs if r.get('status') == 'running'), None)
    queued = next((r for r in runs if r.get('status') == 'queued'), None)
    if not running and not queued:
        return
    if running and running.get('pipeline'):
        case['ax_pipeline'] = copy.deepcopy(running['pipeline'])
    pipeline = case.setdefault('ax_pipeline', {'stage': 'extraction', 'steps': []})
    pipeline.pop('stale', None)
    pipeline.pop('stale_reason', None)
    pipeline.pop('queued_update', None)
    pipeline.pop('active_run', None)
    pipeline['status'] = 'running' if running else 'queued'
    if running:
        pipeline['active_run'] = {k: running.get(k) for k in ('status', 'created_at', 'input_revision')}
    if queued:
        pipeline['queued_update'] = {k: queued.get(k) for k in ('status', 'created_at', 'input_revision', 'trigger')}
        if not running:
            pipeline['summary'] = '최신 제출·검토 결과를 반영해 다음 단계의 처리를 시작합니다.'


async def _while_current(operation, case_id, signature, knowledge):
    """Cancel obsolete I/O promptly instead of finishing every costly batch.

    Read receipts are deliberately absent from fingerprint; a real evidence or
    legal input change still prevents publication at the final commit boundary.
    """
    task = asyncio.create_task(operation)
    try:
        while True:
            latest = store.get_case(case_id)
            if not latest or fingerprint(latest) != signature:
                raise store.VersionConflict('새 검토 결과가 도착해 이전 자료의 분석을 중단했습니다.')
            done, _ = await asyncio.wait({task}, timeout=CURRENT_CASE_POLL_SECONDS)
            if done:
                if knowledge_signature(latest) != knowledge:
                    raise store.VersionConflict('분석 중 적용 근거가 변경되었습니다.')
                return await task
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def schedule(case,user,pool,kind='case_review',trigger='직원 분석 요청'):
    intake_workflow.require_completed(case)
    signature=fingerprint(case);ks=knowledge_signature(case)
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        previous=con.execute('SELECT * FROM ax_runs WHERE case_id=? AND signature=? AND knowledge_signature=? ORDER BY rowid DESC',(case['id'],signature,ks)).fetchall()
        for row in previous:
            old=json.loads(row['body'])
            retryable_model_failure=bool(old.get('error') and old.get('metrics',{}).get('model_status')=='failed') or old.get('pipeline',{}).get('stage')=='verification_waiting'
            if old.get('kind')==kind and row['status'] not in ('failed','stale','cancelled') and not retryable_model_failure:
                return {'run_id':row['id'],'status':row['status'],'reused':True}
        domain.require(len(previous)<3,'AX_BUDGET','동일한 자료의 재분석 상한입니다. 실패 또는 근거 변경을 확인하세요.')
        # A newer arrival supersedes queued work; an already-running result is checked on completion.
        for row in con.execute("SELECT * FROM ax_runs WHERE case_id=? AND status='queued'",(case['id'],)).fetchall():
            old=json.loads(row['body']);old.update(status='cancelled',error='새 자료가 도착하여 최신 분석으로 대체했습니다.',finished_at=store.now())
            con.execute('UPDATE ax_runs SET status=?,body=? WHERE id=?',('cancelled',store.dumps(old),row['id']))
        count=con.execute("SELECT count(*) FROM ax_runs WHERE status IN ('queued','running')").fetchone()[0]
        domain.require(count<8,'AX_QUEUE_FULL','분석 대기열이 가득 찼습니다. 저장된 자료는 유지되며 잠시 후 다시 분석할 수 있습니다.')
        run={'id':store.uid('ax'),'case_id':case['id'],'case_version':case['version'],'input_revision':case['input_revision'],'kind':kind,'trigger':trigger,'status':'queued','created_at':store.now(),'requested_by':user['name'],'findings':[],'steps':[{'key':'received','label':trigger,'status':'completed'},{'key':'analysis','label':'상황·규칙·공식 근거 분석','status':'queued'},{'key':'draft','label':'자료 완비 후 문서 자동 작성','status':'queued'}]}
        con.execute('INSERT INTO ax_runs VALUES (?,?,?,?,?,?)',(run['id'],case['id'],signature,ks,'queued',store.dumps(run)))
    pool.submit(worker,run['id'],copy.deepcopy(case))
    return {'run_id':run['id'],'status':'queued'}


def maybe_schedule(case,user,pool,trigger):
    if intake_workflow.pending(case):return None
    if os.getenv('DEBTOFF_AUTO_AX','1')!='1':return None
    try:
        return schedule(case,user,pool,trigger=trigger)
    except domain.DomainError as exc:
        # Already stored business data must not appear to fail because the model queue is busy.
        run={'id':store.uid('ax'),'case_id':case['id'],'status':'failed','trigger':trigger,'error':exc.message,'created_at':store.now(),'findings':[]}
        with store.db() as con:
            con.execute('INSERT INTO ax_runs VALUES (?,?,?,?,?,?)',(run['id'],case['id'],fingerprint(case),knowledge_signature(case),'failed',store.dumps(run)))
        return {'run_id':run['id'],'status':'failed'}


def enrich_candidates(case,candidates,default_doc=None):
    existing=case.setdefault('extraction_candidates',[])
    for candidate in candidates:
        if candidate.get('value') is None or not candidate.get('key'):continue
        quote=str(candidate.get('quote',''))
        document_id=candidate.get('document_id') or default_doc
        source_id=candidate.get('source_id') or ('doc:'+document_id if document_id else '')
        doc=next((d for d in case['documents'] if d['id']==document_id),None)
        valid=bool(doc and doc.get('status') not in ('rejected','quarantined','superseded') and quote and quote in doc.get('text',''))
        if not valid and (source_id=='consultation' or source_id.startswith('consultation:')):
            consultation=case.get('consultation',{})
            contents=consultation.get('notes','')+'\n'+'\n'.join(consultation.get('answers',{}).values())
            valid=bool(consultation.get('status')!='quarantined' and quote and quote in contents)
        if not valid and source_id.startswith('message:'):
            valid=any((source_id=='message:'+m['id'] or source_id.startswith('message:'+m['id']+':')) and m.get('role')=='client' and m.get('status')!='quarantined' and quote and quote in m['text'] for m in case.get('messages',[]))
        if not valid:continue
        signature=store.digest([candidate['key'],candidate['value'],source_id,quote])
        duplicate=next((c for c in existing if c.get('signature')==signature),None)
        if duplicate:
            if duplicate.get('status')=='superseded':
                duplicate.update(**{k:v for k,v in candidate.items() if k not in ('id','status')},status='candidate')
            if duplicate.get('status')=='quarantined' and doc and doc.get('status')=='verified':
                duplicate.update(status='candidate',quarantine_reason=None)
            continue
        existing.append({**candidate,'id':store.uid('candidate'),'signature':signature,'document_id':document_id,'source_id':source_id,'status':'candidate','created_at':store.now(),'rule_ids':[r['id'] for r in rulebook.definition()['rules'] if candidate['key'] in r['required_factors']]})


def ensure_requests(case,needs):
    if intake_workflow.pending(case):return
    from . import court_rules, automation, legal_watch
    policy_state = legal_watch.case_policy_status(case)
    case['legal_update'] = policy_state
    if policy_state.get('pending_changes'):
        case.setdefault('court_request_plan', {}).update(coverage='update_review_required',
            pending_changes=policy_state['pending_changes'])
        return
    plan = court_rules.reconcile(case, needs)
    case['court_request_plan'] = plan
    automation.sync_request_notifications(case)


def _case_without_read_state(case):
    """Only notification reads may commute with detached model work."""
    state = copy.deepcopy(case)
    for key in ('version', 'updated_at', 'audit'):
        state.pop(key, None)
    for notice in state.get('notifications', []):
        notice.pop('read_by', None)
        notice.pop('read_at', None)
    return state


def _commit_detached_analysis(base, working, signature, knowledge):
    """Rebase read receipts, never evidence, decisions, workflow edits or new alerts.

    Model/network work has already finished. The short optimistic retry handles a
    second reader arriving between our last read and SQLite's write transaction.
    """
    expected_state = _case_without_read_state(base)
    for attempt in range(8):
        latest = store.get_case(base['id'])
        if (not latest or fingerprint(latest) != signature
                or knowledge_signature(latest) != knowledge
                or _case_without_read_state(latest) != expected_state):
            raise store.VersionConflict('분석 중 자료·판단 또는 적용 근거가 변경되었습니다.')

        def merge(current):
            # A law/source refresh can occur independently of case.version.
            if knowledge_signature(current) != knowledge:
                raise store.VersionConflict('분석 중 법원 규칙 또는 근거가 변경되었습니다.')
            combined = copy.deepcopy(working)
            combined['version'] = current['version']
            combined['audit'] = copy.deepcopy(current['audit'])
            live_notices = {notice['id']: notice for notice in current.get('notifications', [])}
            for notice in combined.get('notifications', []):
                live = live_notices.get(notice['id'])
                if live is None:
                    continue  # A newly generated alert belongs to the worker.
                for key in ('read_by', 'read_at'):
                    if key in live:
                        notice[key] = copy.deepcopy(live[key])
                    else:
                        notice.pop(key, None)
                # Keep the worker's resolved_at; copying the old receipt would
                # otherwise reopen a request fulfilled during this analysis.
            current.update(combined)

        try:
            return store.mutate(base['id'], latest['version'], SYSTEM, 'ax.analysis_and_draft', merge)
        except store.VersionConflict:
            if attempt == 7:
                raise


def worker(run_id,case):
    with store.db() as con:
        row=con.execute('SELECT * FROM ax_runs WHERE id=?',(run_id,)).fetchone()
    if not row or row['status']!='queued':return
    current = store.get_case(case['id'])
    if intake_workflow.pending(case) or (current and intake_workflow.pending(current)):
        run = json.loads(row['body'])
        run.update(status='cancelled', error='세부 상담 기록을 기다리고 있습니다.', finished_at=store.now())
        _save('ax_runs', run)
        return
    run=json.loads(row['body']);run['status']='running';run['steps'][1]['status']='running'
    from . import automation, workflow_contract
    run['pipeline'] = {'version': automation.VERSION, 'stage': 'extraction', 'status': 'running',
        'contract_version': workflow_contract.VERSION, 'input_revision': case['input_revision'],
        'updated_at': store.now(), 'reasons': [], 'steps': workflow_contract.project_steps(0, 'running',
            '현재 상담·제출자료를 정리하고 법원별 필요서류를 확인하고 있습니다.')}
    run['checkpoints'] = [workflow_contract.checkpoint(run['pipeline'])]
    _save('ax_runs',run)
    try:
        from .ax_engine import analyze_case
        result=asyncio.run(_while_current(analyze_case(case,kind=run['kind']), case['id'], row['signature'], row['knowledge_signature']))
        current=store.get_case(case['id'])
        if fingerprint(current)!=row['signature'] or knowledge_signature(current)!=row['knowledge_signature']:
            run.update(result,status='stale',error='분석 중 자료 또는 지식이 변경되어 결과를 업무에 적용하지 않았습니다.')
        else:
            def apply(c):
                unsafe={x.get('document_id') for x in result.get('document_checks',[]) if x.get('coverage_status')=='identity_conflict'}
                for candidate in c.get('extraction_candidates',[]):
                    if candidate.get('status')=='candidate':
                        candidate.update(status='superseded',superseded_by_run=run_id)
                    if candidate.get('document_id') in unsafe:
                        candidate.update(status='quarantined',quarantine_reason='서류 인물 불일치')
                enrich_candidates(c,[x for x in result.get('extracted_facts',[]) if x.get('document_id') not in unsafe])
                for check in result.get('document_checks',[]):
                    doc=next((d for d in c['documents'] if d['id']==check.get('document_id')),None)
                    if not doc:continue
                    doc['automated_check']=check
                    if check.get('coverage_status')=='identity_conflict':doc['status']='quarantined'
                    if (check.get('coverage_status') in ('needs_more','unreadable','identity_conflict') and doc.get('request_id')
                            and not automation.manual_document_review_current(c, doc)):
                        req=domain.item(c,'requests',doc['request_id']);req['status']='needs_more'
                        req['public_review_note']='제출자료의 인물·종류·기간 또는 판독 상태를 담당자가 확인 중입니다.'
                c['rule_evaluation']=rulebook.evaluate_case(c)
                if c.get('case_type','personal_rehabilitation') in ('personal_rehabilitation','bankruptcy_review','unknown'):
                    ensure_requests(c,c['rule_evaluation']['required_documents'])
            # Model I/O runs outside SQLite's write transaction. Commit the complete
            # detached result only if the original evidence/version is still current.
            working = copy.deepcopy(current)
            apply(working)
            from . import automation
            def progress(pipeline):
                run['pipeline'] = pipeline
                checkpoint = workflow_contract.checkpoint(pipeline)
                run.setdefault('checkpoints', []).append(checkpoint)
                working.setdefault('workflow_checkpoints', []).append(checkpoint)
                run['steps'] = [{'key':s['id'],'label':s['title'],'status':s['status']} for s in pipeline['steps']]
                _save('ax_runs', run)
            asyncio.run(_while_current(automation.advance(working, result.get('document_checks', []), progress=progress),
                                       case['id'], row['signature'], row['knowledge_signature']))
            draft = next((d for d in reversed(working.get('drafts', [])) if not d.get('stale')), {})
            working.setdefault('automation_history', []).append({'run_id':run_id,'trigger':run['trigger'],
                'at':store.now(),'candidate_count':len(result.get('extracted_facts',[])),
                'draft_id':draft.get('id'),'stage':working.get('ax_pipeline',{}).get('stage')})
            working['legal_change_pending'] = False
            saved = _commit_detached_analysis(current, working, row['signature'], row['knowledge_signature'])
            run.update(result)
            run['pipeline'] = saved['ax_pipeline']
            run.setdefault('checkpoints', []).append(workflow_contract.checkpoint(saved['ax_pipeline']))
            run.update(case_id=case['id'],case_version=saved['version'],draft_id=draft.get('id'),
                       steps=[{'key':s['id'],'label':s['title'],'status':s['status']} for s in saved['ax_pipeline']['steps']])
            _save('ax_runs',run,signature=fingerprint(saved))
    except store.VersionConflict:
        run.update(status='stale',error='저장 시점에 사건이 변경되어 분석을 적용하지 않았습니다.')
    except Exception as exc:
        run.update(status='failed',error=f'자료 분석 처리 실패 ({type(exc).__name__}). 원문은 저장되어 있으며 재분석할 수 있습니다.')
    run['finished_at']=store.now();_save('ax_runs',run)


def start_intake(text,user,pool,filename='상담 회의록'):
    domain.require(20<=len(text)<=50000,'INTAKE_SIZE','상담 회의록은 20~50,000자로 입력하세요.')
    run={'id':store.uid('intake'),'status':'queued','created_at':store.now(),'requested_by':user['name'],'org_id':user['org_id'],'input_text':text,'filename':filename,'steps':[{'key':'intake','label':'회의록 접수','status':'completed'},{'key':'classify','label':'인물·지역·사건 유형 분석','status':'queued'},{'key':'rules','label':'상황별 규칙·필요서류 결정','status':'queued'},{'key':'draft','label':'서류 요청 알림 준비','status':'queued'}]}
    with store.db() as con:
        pending=con.execute("SELECT count(*) FROM intake_runs WHERE status IN ('queued','running')").fetchone()[0]
        domain.require(pending<4,'INTAKE_QUEUE_FULL','상담 분석 대기열이 가득 찼습니다. 잠시 후 다시 접수하세요.')
        con.execute('INSERT INTO intake_runs VALUES (?,?,?,?)',(run['id'],user['org_id'],'queued',store.dumps(run)))
    pool.submit(intake_worker,run['id'],copy.deepcopy(user))
    return {'run_id':run['id'],'status':'queued'}


def intake_worker(run_id,user):
    with store.db() as con:row=con.execute('SELECT body FROM intake_runs WHERE id=?',(run_id,)).fetchone()
    run=json.loads(row['body']);run['status']='running';run['steps'][1]['status']='running';_save('intake_runs',run)
    try:
        from .ax_engine import analyze_intake
        analysis=asyncio.run(analyze_intake(run['input_text']))
        courts=json.loads((store.ROOT/'data/registry.json').read_text(encoding='utf-8'))['courts']
        court=next((c for c in courts if c['id']==analysis.get('court_id')),None)
        case=domain.new_case(analysis.get('client_name') or '성명 미확인 내담자',court['id'] if court else 'unknown',court['name'] if court else '관할 후보 확인 필요',analysis.get('summary') or run['input_text'][:400],False)
        case.update(org_id=user['org_id'],region=analysis.get('region'),case_type=analysis.get('case_type','unknown'),case_type_label={'personal_rehabilitation':'개인회생','bankruptcy_review':'파산 검토','other':'다른 사건유형','unknown':'사건유형 확인 필요'}.get(analysis.get('case_type'),'유형 검토'),intake_analysis=analysis)
        case['title']=case['client_name']+' · '+case['case_type_label']
        case['consultation']={'notes':run['input_text'],'answers':{},'consent':False,'consent_basis':'직원 분석 요청; 내담자 동의 여부 별도 확인','status':'party_statement','recorded_by':user['name'],'updated_at':store.now(),'version':1}
        doc_id=store.uid('doc');folder=store.DATA_DIR/'uploads'/case['id'];folder.mkdir(parents=True,exist_ok=True)
        raw=run['input_text'].encode('utf-8');(folder/(doc_id+'.txt')).write_bytes(raw)
        import hashlib
        case['documents'].append({'id':doc_id,'filename':run['filename']+'.txt','storage_name':doc_id+'.txt','sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'version':1,'request_id':'','status':'received','public_status':'상담기록 수신','source_type':'meeting','created_at':store.now(),'uploader':user['id'],'text':run['input_text'],'page_texts':[{'page':1,'text':run['input_text']}],'extraction_status':'extracted'})
        enrich_candidates(case,analysis.get('extracted_facts',[]),doc_id)
        for key in ('client_name',):
            value=analysis.get(key)
            if value and value in run['input_text']:
                enrich_candidates(case,[{'key':key,'label':'내담자 성명','value':value,'quote':value,'source_type':'meeting','origin':'grounded_identity'}],doc_id)
        evaluation=rulebook.evaluate_case(case);case['rule_evaluation']=evaluation
        # Keep the diagnosed procedure; document generation begins only after evidence completion.
        if case['case_type'] in ('personal_rehabilitation','bankruptcy_review','unknown'):
            ensure_requests(case,evaluation['required_documents'])
        for n,missing in enumerate(analysis.get('missing_information',[])):
            case.setdefault('tasks',[]).append({'id':store.uid('task'),'title':str(missing),'status':'open','origin':'intake','assigned_to':'담당 변호사'})
        from . import automation
        automation._state(case, 'collecting', 0)
        draft = {'id': None}
        case['automation_history']=[{'at':store.now(),'trigger':'회의록에서 사건·법원별 필요서류 자동 선정','intake_run_id':run_id,'draft_id':None}]
        store.insert_case(case)
        run.update(status='completed',result={'case_id':case['id'],'client_name':case['client_name'],'court_name':case['court_name'],'case_type':case['case_type'],'documents_requested':len(case['requests']),'matched_rules':len(evaluation['matched_rules']),'draft_id':draft['id'],'metrics':analysis.get('metrics',{}),'model_status':analysis.get('status'),'missing_information':analysis.get('missing_information',[])},steps=[{'key':'intake','label':'회의록 접수','status':'completed'},{'key':'classify','label':'인물·지역·사건유형 분석','status':'completed'},{'key':'rules','label':str(len(evaluation['matched_rules']))+'개 업무 규칙·'+str(len(case['requests']))+'종 필요서류','status':'completed'},{'key':'draft','label':'서류 요청 알림·자동 검증 준비','status':'completed'}])
    except Exception as exc:
        run.update(status='failed',error=f'상담 분석 실패 ({type(exc).__name__}). 회의록은 보존되었습니다.')
    run['finished_at']=store.now();_save('intake_runs',run)


def apply_finding(case_id,run_id,finding_id,user,expected_version,reason,period=None,dismiss=False):
    domain.require(len(reason.strip())>=5,'REASON_REQUIRED','반영 또는 반려 이유를 5자 이상 기록하세요.')
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        case_row=con.execute('SELECT body FROM cases WHERE id=?',(case_id,)).fetchone()
        case=json.loads(case_row['body'])
        domain.require(case['org_id']==user['org_id'] and user['id'] in case['members'],'CASE_ACCESS','사건 권한을 확인하세요.')
        if case['version']!=expected_version:raise store.VersionConflict('현재 사건을 새로고침하고 다시 적용하세요.')
        row=con.execute('SELECT * FROM ax_runs WHERE id=? AND case_id=?',(run_id,case_id)).fetchone()
        domain.require(row is not None,'RUN_NOT_FOUND','이 사건의 분석 결과가 아닙니다.')
        run=json.loads(row['body']);finding=next((f for f in run.get('findings',[]) if f['id']==finding_id),None)
        domain.require(finding is not None,'FINDING_NOT_FOUND','제안을 찾을 수 없습니다.')
        domain.require(row['signature']==fingerprint(case) and row['knowledge_signature']==knowledge_signature(case),'STALE_PROPOSAL','근거가 변경되어 과거 제안을 적용할 수 없습니다.')
        domain.require(finding.get('review_status','pending')=='pending','ALREADY_REVIEWED','이미 반영 또는 반려한 제안입니다.')
        before=copy.deepcopy(case)
        if not dismiss:
            domain.invalidate(case,'분석 제안 업무 반영')
            action=finding.get('action',{});kind=action.get('type');target_id=None
            if kind=='document_request':
                need={'catalog_id':action.get('catalog_id'),'period':period or action.get('period') or '담당자 확인 필요','reason':action.get('reason') or finding['observation']}
                catalog_ids={d['id'] for d in json.loads((store.ROOT/'data/registry.json').read_text(encoding='utf-8'))['documents']}
                domain.require(need['catalog_id'] in catalog_ids,'UNKNOWN_DOCUMENT','등록된 서류 종류만 요청할 수 있습니다.')
                ensure_requests(case,[need]);target_id=next(r['id'] for r in case['requests'] if r['catalog_id']==need['catalog_id'])
            elif kind in ('review_task','correction_draft'):
                task={'id':store.uid('task'),'title':finding['title'],'description':finding['observation'],'status':'open','origin':finding['origin'],'source_run_id':run_id,'source_finding_id':finding_id,'evidence_refs':finding.get('evidence_refs',[]),'assigned_to':'담당 변호사'}
                case.setdefault('tasks',[]).append(task);target_id=task['id']
                if kind=='correction_draft':
                    corr=domain.item(case,'corrections',action.get('correction_id'))
                    corr.update(answer=action.get('answer') or finding['observation'],status='draft',ai_source=run_id)
            else:raise domain.DomainError('ACTION_NOT_ALLOWED','이 제안은 자동 업무 반영을 지원하지 않습니다.')
            finding.update(review_status='applied',applied_target_id=target_id)
            drafting.generate(case,'검토 제안을 업무에 반영')
        else:finding['review_status']='dismissed'
        finding.update(reviewed_by=user['name'],review_reason=reason,reviewed_at=store.now())
        case['version']+=1;case['updated_at']=store.now()
        case.setdefault('automation_history',[]).append({'at':store.now(),'trigger':'제안 반려' if dismiss else '제안 업무 반영','finding_id':finding_id,'run_id':run_id,'actor':user['name'],'reason':reason})
        case['audit'].append({'id':store.uid('ev'),'action':'ax.finding.dismissed' if dismiss else 'ax.finding.applied','actor':user['name'],'role':user['role'],'at':store.now(),'from_version':before['version'],'to_version':case['version'],'previous_hash':store.digest(before)})
        con.execute('UPDATE cases SET version=?,body=? WHERE id=?',(case['version'],store.dumps(case),case_id))
        con.execute('INSERT INTO history VALUES (?,?,?,?)',(case_id,case['version'],store.dumps(case),store.now()))
        con.execute('UPDATE ax_runs SET signature=?,body=? WHERE id=?',(fingerprint(case),store.dumps(run),run_id))
    return case
