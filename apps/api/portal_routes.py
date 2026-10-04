"""Portal HTTP boundaries; reuse the application's actual case authorization."""
import json
from typing import Literal

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from . import accounts, store, portal_service as service


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=2, max_length=80)
    content: str = Field(min_length=10, max_length=2000)
    alias: str | None = Field(default=None, max_length=20)
    public_consent: StrictBool
    show_outcome: StrictBool = False


class ReviewModeration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['published', 'hidden']
    reason: str = Field(min_length=5, max_length=500)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)


class HandoffRequest(ChatRequest):
    expected_version: StrictInt = Field(ge=1)


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=2, max_length=80)
    court_id: str | None = Field(default=None, max_length=80)
    consultation: str = Field(min_length=10, max_length=10000)
    consent: StrictBool
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


def register(app, current_user, staff, authorize, change, visible, on_application=None):
    def client(user):
        if user['role'] != 'client':
            raise HTTPException(403, '고객 계정에서 이용해주세요.')

    def owned_review(review_id, user):
        row = service.review_record(review_id)
        if not row or row['status'] == 'deleted' or row['org_id'] != user['org_id']:
            raise HTTPException(404, '후기를 찾을 수 없습니다.')
        if user['role'] == 'client' and row['author_id'] != user['id']:
            raise HTTPException(404, '후기를 찾을 수 없습니다.')
        authorize(row['case_id'], user)
        return row

    @app.get('/api/portal/public')
    def public():
        return {**service.guide(), 'reviews': service.reviews(), 'courts': service.court_choices()}

    @app.post('/api/portal/applications')
    def application(data: ApplicationCreate, user=Depends(current_user)):
        client(user)
        case, created = service.application(user, data.model_dump())
        if created and on_application:
            on_application(case, user)
        return visible(store.get_case(case['id']), user)

    def fresh_testing(user):
        client(user)
        if accounts.config()['start_profile'] != 'fresh':
            raise HTTPException(404, '테스트 자료를 제공하지 않는 환경입니다.')

    @app.get('/api/portal/testing-materials')
    def testing_materials(case_id: str = '', user=Depends(current_user)):
        fresh_testing(user)
        from .testing_materials import current
        case = authorize(case_id, user) if case_id else None
        try:
            path = current(case.get('client_name') if case else None)['bundle']
        except FileNotFoundError:
            raise HTTPException(404, '이 사건 이름에 맞는 연습자료가 없습니다. 직접 준비한 같은 사람의 자료를 제출해 주세요.')
        if not path.is_file():
            raise HTTPException(404, '가상 자료 묶음이 준비되지 않았습니다.')
        return FileResponse(path, filename='새출발_연습자료.zip', media_type='application/zip')

    @app.get('/api/portal/testing-application')
    def testing_application(user=Depends(current_user)):
        fresh_testing(user)
        from .testing_materials import current
        try:
            sample = current()
        except FileNotFoundError:
            raise HTTPException(404, '가상 상담 내용이 준비되지 않았습니다.')
        return {key: sample[key] for key in ('name', 'court_id', 'consultation', 'synthetic')}

    @app.get('/api/portal/reviews')
    def public_reviews():
        return {'reviews': service.reviews()}

    @app.get('/api/portal/reviews/manage')
    def manage_reviews(user=Depends(staff)):
        rows = []
        for record in service.manage_records(user['org_id']):
            try:
                authorize(record['case_id'], user)
            except HTTPException:
                continue
            rows.append({**json.loads(record['body']), 'case_id': record['case_id']})
        return {'reviews': rows}

    @app.get('/api/portal/reviews/mine')
    def my_reviews(user=Depends(current_user)):
        client(user)
        rows = []
        for record in service.own_reviews(user):
            try:
                case = authorize(record['case_id'], user)
            except HTTPException:
                continue
            rows.append(service.public_review(json.loads(record['body']), case))
        return {'reviews': rows}

    @app.post('/api/portal/reviews')
    def create_review(data: ReviewCreate, user=Depends(current_user)):
        client(user)
        case = authorize(data.case_id, user)
        return service.create_review(case, user, data.model_dump())

    @app.delete('/api/portal/reviews/{review_id}')
    def delete_review(review_id: str, user=Depends(current_user)):
        row = owned_review(review_id, user)
        return service.update_review(row, user, 'deleted', '작성자 공개 동의 철회' if user['role'] == 'client' else '담당자 삭제')

    @app.patch('/api/portal/reviews/{review_id}')
    def moderate_review(review_id: str, data: ReviewModeration, user=Depends(staff)):
        row = owned_review(review_id, user)
        return service.update_review(row, user, data.status, data.reason)

    @app.post('/api/portal/chat')
    async def chat(data: ChatRequest, user=Depends(current_user)):
        client(user)
        case = authorize(data.case_id, user)
        return await service.chat(case, user, data.message)

    @app.post('/api/portal/handoff')
    def handoff(data: HandoffRequest, user=Depends(current_user)):
        client(user)
        return change(data.case_id, user, data.expected_version, 'portal.handoff',
                      lambda case: service.handoff(case, user, data.message))
