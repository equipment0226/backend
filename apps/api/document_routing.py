"""Connect local document contents to one current request, never to a guess.

This is routing, not document approval. No model, network, file access, or case
mutation occurs here. Names and account numbers remain inside this process;
results contain request scope identifiers rather than copied personal data.
"""
from __future__ import annotations

import re
import unicodedata

from . import document_facts

VERSION = 'content-document-routing-v1'
INACTIVE = {'withdrawn', 'cancelled', 'superseded'}

# Document titles, not mentions of documents in consultations/checklists. Keep
# supporting receipts separate; accepted_catalog_ids belongs to the request.
TITLES = [
    ('D01', r'주민\s*등록(?:표)?\s*등본'),
    ('D02', r'주민\s*등록(?:표)?\s*초본'),
    ('D03', r'가족\s*관계\s*증명'), ('D04', r'혼인\s*관계\s*증명'),
    ('D05', r'소득\s*금액\s*증명'),
    ('D06', r'(?:근로\s*소득\s*)?원천\s*징수\s*영수증'),
    ('D07', r'(?:급여|급료)\s*명세'), ('D08', r'재직\s*증명'),
    ('D09', r'건강\s*보험\s*자격\s*득실'),
    ('D11', r'(?:국민\s*)?연금\s*(?:산정용\s*)?가입\s*내역'),
    ('D28', r'지적\s*전산\s*자료'), ('D29', r'지방세\s*세목별\s*과세'),
    ('D33', r'(?:주거용\s*|부동산\s*)?임대차\s*계약서'),
    ('D35', r'계좌\s*통합\s*조회'),
    ('D36', r'(?:입출금\s*|예금\s*|계좌\s*|통장\s*)?(?:거래\s*내역|거래\s*명세)'),
    ('D38', r'(?:부채\s*증명|금융\s*거래\s*확인서|채무\s*잔액\s*증명)'),
    ('D39', r'보험\s*(?:가입\s*(?:조회|확인)|(?:해약|해지)\s*환급금\s*확인)'),
    ('SUPPORT-HOUSING', r'보증금\s*(?:및\s*차임\s*)?납부\s*확인'),
    ('SUPPORT-RETIREMENT', r'(?:예상\s*퇴직금|퇴직\s*급여|퇴직\s*연금)\s*(?:확인|내역)'),
    ('SUPPORT-VEHICLE', r'자동차\s*(?:소유|보유)\s*(?:조회|확인)'),
    ('SUPPORT-TAX', r'(?:국세\s*납세|납세\s*증명|국세\s*체납\s*내역)'),
    ('SUPPORT-HISTORY', r'채무\s*(?:발생\s*)?(?:경위|발생).*진술'),
    ('SUPPORT-EXPENSE-RECEIPTS', r'생활\s*지출\s*납부.*(?:결제|내역)'),
    ('SUPPORT-EXPENSE', r'(?:월\s*)?생활\s*지출\s*명세'),
    ('SUPPORT-HISTORY-EVIDENCE', r'(?:과거\s*)?치료비\s*납입.*(?:사용|내역)'),
]
NEGATED_TITLE = re.compile(r'미제출|제출\s*(?:전|필요|예정)|요청|미확보|제출해|준비해')
IDENTIFIER = r'[A-Za-z0-9*＊●•-]{3,}'


def _normal(value):
    value = unicodedata.normalize('NFKC', str(value or '')).lower()
    value = re.sub(r'주식회사|\(주\)', '', value)
    return re.sub(r'[^가-힣a-z0-9]', '', value)


def _pages(entry):
    pages = entry.get('page_texts') or []
    texts = [str(page.get('text') or '') if isinstance(page, dict) else str(page or '') for page in pages]
    return texts if any(text.strip() for text in texts) else [str(entry.get('text') or '')]


def _catalogs(pages):
    found = set()
    for page in pages:
        # The first few title/issuer lines cannot be replaced by body references.
        for line in page[:650].splitlines()[:7]:
            title = re.sub(r'^\s*(?:[0-9]+[.)]\s*)?', '', line).strip()
            if NEGATED_TITLE.search(title):
                continue
            for catalog, pattern in TITLES:
                if re.match(pattern, title):
                    found.add(catalog)
                    break
    return found


def _label_values(text, labels):
    return {m.group(1).strip() for m in re.finditer(
        r'(?:^|[\n/;|])\s*(?:' + labels + r')\s*[:：]\s*([^\n/;|]+)', text)}


def _identifiers(text, labels):
    return {m.group(1) for m in re.finditer(
        r'(?:^|[\n/;|])\s*(?:' + labels + r')\s*[:：]\s*(' + IDENTIFIER + r')', text)}


def _observations(pages, catalog):
    text = '\n'.join(pages)
    if catalog in {'D07', 'D08', 'SUPPORT-RETIREMENT'}:
        institutions = _label_values(text, r'근무처|회사명|직장명|사업장명|사업장')
    elif catalog == 'D38':
        institutions = _label_values(text, r'채권자(?:명)?|금융기관|은행명')
    elif catalog == 'D39':
        institutions = _label_values(text, r'보험회사|보험사|보험회사명')
    else:
        institutions = _label_values(text, r'금융기관|은행명')
    accounts = _identifiers(text, r'계좌번호|계좌\s*식별|계좌')
    loans = _identifiers(text, r'대출번호|채권번호|약정번호|계약번호')
    policies = _identifiers(text, r'증권번호|보험증권번호|보험계약번호')
    names = set()
    for value in _label_values(text, r'(?:본인\s*)?성명|이름|채무자(?:\s*성명)?|예금주|납세자(?:\s*성명)?|임차인'):
        match = re.match(r'([가-힣]{2,20})(?=\s|[.(]|$)', value)
        if match:
            names.add(match.group(1))
    periods = []
    for page in pages:
        context = document_facts._context(page)
        start, end = context.get('period_start'), context.get('period_end')
        if start and end and start <= end:
            periods.append((start, end))
    return {'institutions': institutions, 'accounts': accounts, 'loans': loans,
            'policies': policies, 'names': names, 'periods': periods,
            'headers': '\n'.join('\n'.join(page.splitlines()[:3]) for page in pages), 'text': text}


def _entity(case, request):
    catalog = request.get('catalog_id')
    collection = ('financial_accounts' if catalog in {'D36', 'D37'} else 'creditors' if catalog == 'D38'
                  else 'insurance_policies' if catalog == 'D39' else 'employers')
    rows = case.get(collection, case.get('accounts', []) if collection == 'financial_accounts' else [])
    key = str(request.get('account_key') or '')
    return next((row for row in rows if isinstance(row, dict)
                 and str(row.get('id') or row.get('account_key') or '') == key
                 and row.get('status') not in {'excluded', 'rejected'}), {})


def _exact_identifier(observed, expected):
    # A masked suffix cannot uniquely identify an account, even if only one
    # currently requested account has that suffix.
    return bool(expected and observed and not re.search(r'[*＊●•]', str(expected))
                and any(not re.search(r'[*＊●•]', value) and _normal(value) == _normal(expected) for value in observed))


def _institution_in_header(institution, headers):
    cleaned = unicodedata.normalize('NFKC', headers).lower()
    cleaned = re.sub(r'주식회사|\(주\)', ' ', cleaned)
    # "검증은행" must not match "다른검증은행". Tolerate OCR spacing,
    # without letting an issuer become a substring of another issuer's name.
    pattern = r'(?<![가-힣a-z0-9])' + r'\s*'.join(re.escape(char) for char in _normal(institution)) + r'(?![가-힣a-z0-9])'
    return bool(re.search(pattern, cleaned))


def _scope_match(case, request, observed):
    catalog = request.get('catalog_id')
    if request.get('scope_unresolved'):
        return False
    institution = request.get('institution')
    if institution:
        normalized = _normal(institution)
        declared = {_normal(value) for value in observed['institutions']}
        # Issuer headings are useful when a body has no institution label. Body
        # transaction counterparties are deliberately not issuer evidence.
        if declared:
            if declared != {normalized}:
                return False
        elif not _institution_in_header(institution, observed['headers']):
            # An insurance inventory lists issuers in a table. Exact whole-line
            # names are accepted only with a separately resolved policy number.
            if catalog != 'D39' or normalized not in {_normal(line) for line in observed['text'].splitlines()}:
                return False
    elif catalog in {'D36', 'D38', 'D39'}:
        return False
    entity = _entity(case, request)
    if catalog == 'D36':
        number = entity.get('account_number', entity.get('number')) or request.get('account_number')
        if not number:
            # Legacy scopes can themselves store a real account number. Opaque
            # IDs do not count as numbers and suffix-only labels never count.
            key = str(request.get('account_key') or '')
            number = key if re.fullmatch(r'\d[\d -]{5,}', key) else None
        if not _exact_identifier(observed['accounts'], number):
            return False
    elif catalog == 'D38':
        number = entity.get('loan_number') or entity.get('loan_key') or request.get('loan_key')
        if number and not _exact_identifier(observed['loans'], number):
            return False
    elif catalog == 'D39':
        number = entity.get('policy_number') or entity.get('insurance_policy') or entity.get('policy_key')
        if number and not _exact_identifier(observed['policies'], number):
            # Table cells in an inventory keep the full policy as one line.
            if not _exact_identifier({line.strip() for line in observed['text'].splitlines()}, number):
                return False
    start, end = request.get('period_start'), request.get('period_end')
    if start and end:
        if not observed['periods'] or not all(begin <= str(end) and finish >= str(start)
                                            for begin, finish in observed['periods']):
            return False
    return True


def route_documents(case, entries):
    """Return one decision per parsed entry; never approve or complete a request.

    Entries are {id, filename, text, page_texts:[{page,text}]}. Filename is not
    consulted. Partial but overlapping periods can join a request; completeness
    and gaps remain the existing per-document/collection review's responsibility.
    """
    requests = [request for request in case.get('requests', []) if isinstance(request, dict)
                and request.get('id') and request.get('status') not in INACTIVE
                and not request.get('no_longer_required')]
    results = []
    for entry in entries:
        pages = _pages(entry)
        catalogs = _catalogs(pages)
        result = {'entry_id': entry.get('id'), 'request_id': None, 'status': 'unmatched',
                  'confidence': 'none', 'reason': '본문에서 서류 종류를 확인하지 못했습니다. 원본을 확인해 연결해주세요.',
                  'matched_scope': {}, 'candidate_request_ids': []}
        results.append(result)
        if len(catalogs) != 1:
            if catalogs:
                result.update(status='ambiguous', reason='여러 종류의 서류가 한 파일에 있습니다. 원본별 요청 항목을 확인해주세요.')
            continue
        catalog = next(iter(catalogs))
        observed = _observations(pages, catalog)
        result['matched_scope'] = {'catalog_id': catalog}
        client_name = _normal(case.get('client_name'))
        if client_name and observed['names'] and any(_normal(name) != client_name for name in observed['names']):
            result.update(status='identity_review', identity_status='conflict',
                          reason='사건 대상자와 다른 명의가 기재되어 있습니다. 제출 대상자를 확인해주세요.')
            continue
        if client_name and not observed['names']:
            result.update(status='identity_review', identity_status='unresolved',
                          reason='서류의 대상자 성명을 확인하지 못했습니다. 원본의 명의를 확인해주세요.')
            continue
        candidates = [request for request in requests
                      if catalog in {request.get('catalog_id'), *request.get('accepted_catalog_ids', [])}]
        result['candidate_request_ids'] = [request['id'] for request in candidates]
        if not candidates:
            result['reason'] = '이 서류에 해당하는 현재 요청이 없습니다. 별도 제출자료로 보존합니다.'
            continue
        if ((catalog == 'D36' and len(observed['accounts']) > 1)
                or (catalog == 'D38' and len(observed['loans']) > 1)
                or (catalog == 'D39' and len(observed['policies']) > 1)
                or (catalog in {'D36', 'D38', 'D39'} and len(observed['institutions']) > 1)):
            result.update(status='ambiguous', reason='여러 기관·계좌·계약의 자료가 한 파일에 있습니다. 각각의 요청 범위를 확인해주세요.')
            continue
        matches = [request for request in candidates if _scope_match(case, request, observed)]
        # A single unsplit request is a collection inbox. Linking evidence by
        # kind is safe here, but neither fills unknown scope nor approves it.
        generic = (len(candidates) == 1 and candidates[0].get('scope_unresolved')
                   and not candidates[0].get('institution') and not candidates[0].get('account_key'))
        if not matches and generic:
            request = candidates[0]
            result.update(request_id=request['id'], status='matched', confidence='high', scope_pending=True,
                          reason='서류 종류에 맞는 요청에 모았습니다. 기관·계좌·기간은 추출 후 확인이 필요합니다.')
            result['matched_scope'].update(scope_pending=True)
            continue
        if len(matches) != 1:
            result.update(status='ambiguous' if len(matches) > 1 or len(candidates) > 1 else 'unmatched',
                          reason='기관·계좌·기간을 하나의 요청과 확실히 연결하지 못했습니다. 요청 범위를 확인해주세요.')
            continue
        request = matches[0]
        scope = result['matched_scope']
        scope.update(institution=request.get('institution', ''), account_key=request.get('account_key', ''),
                     period_start=min((start for start, _ in observed['periods']), default=None),
                     period_end=max((end for _, end in observed['periods']), default=None))
        result.update(request_id=request['id'], status='matched', confidence='high',
                      reason='본문의 서류 종류와 요청 범위가 일치합니다. 추출 및 서류 검토를 이어갑니다.',
                      candidate_request_ids=[request['id']])
    return results
