"""A document request is an evidence collection, not its last uploaded file."""
from datetime import date, timedelta

from . import extraction_readiness

INACTIVE_DOCUMENTS = {'rejected', 'superseded', 'quarantined'}


def active_documents(case, request):
    return [document for document in extraction_readiness.active_documents(case)
            if document.get('request_id') == request.get('id')
            and document.get('status') not in INACTIVE_DOCUMENTS
            and document.get('source_type') != 'meeting']


def sync_review_status(case, request):
    """One reviewed file cannot complete a request with other pending originals."""
    if request.get('status') in {'withdrawn', 'cancelled', 'superseded'} or request.get('no_longer_required'):
        return
    documents = active_documents(case, request)
    verified = [doc for doc in documents if doc.get('status') == 'verified'
                and extraction_readiness.document_state(case, doc)['can_review']]
    request['collection_progress'] = {'total': len(documents), 'verified': len(verified),
        'extraction_complete': sum(extraction_readiness.document_state(case, doc)['can_review'] for doc in documents)}
    if documents and len(verified) == len(documents):
        request['status'] = 'fulfilled'
        request.pop('public_review_note', None)
    elif any(doc.get('status') == 'needs_more' for doc in documents):
        request['status'] = 'needs_more'
    elif documents:
        request['status'] = 'received'


def combined_metadata(request, documents):
    """Combine explicit issuer coverage, preserving gaps and unknown metadata.

    A first/last transaction cannot prove a missing month's coverage. Adjacent
    certificate ranges may cover a single request together; a gap is retained.
    """
    rows = [doc.get('document_metadata', doc.get('metadata', {})) for doc in documents]
    result = {}
    for key in ('institution', 'account_key', 'certificate_type', 'address_history',
                'tax_scope', 'jurisdiction_scope', 'person_number_display'):
        choices = {row.get(key) for row in rows if row.get(key)}
        if len(choices) == 1 and all(row.get(key) for row in rows):
            result[key] = choices.pop()
        elif choices and request.get(key) and any(choice != request[key] for choice in choices):
            result[key] = next(choice for choice in choices if choice != request[key])
    if rows and all(row.get('issued_at') for row in rows):
        result['issued_at'] = min(row['issued_at'] for row in rows)
    intervals = []
    for row in rows:
        try:
            start, end = date.fromisoformat(row.get('period_start', '')), date.fromisoformat(row.get('period_end', ''))
            if start > end:
                return result
            intervals.append((start, end))
        except (TypeError, ValueError):
            return result
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    if merged:
        try:
            expected_start, expected_end = date.fromisoformat(request.get('period_start', '')), date.fromisoformat(request.get('period_end', ''))
            covered = next((pair for pair in merged if pair[0] <= expected_start and pair[1] >= expected_end), None)
        except (TypeError, ValueError):
            covered = None
        start, end = covered or max(merged, key=lambda pair: (pair[1] - pair[0]).days)
        result.update(period_start=start.isoformat(), period_end=end.isoformat())
        result['coverage_intervals'] = [{'start': start.isoformat(), 'end': end.isoformat()} for start, end in merged]
    return result
