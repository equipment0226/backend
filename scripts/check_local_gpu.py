"""Check local Ollama JSON extraction and GPU residency using synthetic data only.

Example: python scripts/check_local_gpu.py --output reports/local-gpu-check.json
Never reads case files, environment secrets or credentials, and never forces GPU
offload. A passing result requires both extractions and matching /api/ps VRAM.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

OPTIONS = {'num_ctx':8192, 'use_mmap':True, 'num_batch':128, 'num_predict':128, 'temperature':0}
SCHEMA = {'type':'object', 'properties':{
    'monthly_income':{'type':['number','null']},
    'total_debt':{'type':['number','null']}},
    'required':['monthly_income','total_debt'], 'additionalProperties':False}
CHECKS = (
    ('explicit_amounts', '합성 확인자료입니다. 월 실수령 급여는 2,800,000원입니다. '
     '대출 원금 잔액 합계는 74,000,000원입니다.',
     {'monthly_income':2800000, 'total_debt':74000000}),
    ('unknown_amounts', '합성 확인자료입니다. 급여 금액과 대출 원금 잔액은 기재하지 않았습니다. '
     '두 금액 모두 확인되지 않았습니다.',
     {'monthly_income':None, 'total_debt':None}),
)
METRICS = ('total_duration','load_duration','prompt_eval_count','prompt_eval_duration','eval_count','eval_duration')


class ProbeError(Exception):
    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def loopback_url(value):
    """Allow literal loopback addresses or localhost, with no URL credentials."""
    try:
        parts=urlsplit(value)
        if parts.scheme not in {'http','https'} or not parts.hostname:
            raise ValueError
        if parts.username is not None or parts.password is not None or parts.query or parts.fragment or parts.path not in {'','/'}:
            raise ValueError
        host=parts.hostname.lower()
        if host!='localhost' and not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        # Accessing .port also rejects invalid and out-of-range port values.
        port=parts.port
        if port==0:
            raise ValueError
        netloc=f'[{host}]' if ':' in host else host
        if port is not None:
            netloc+=f':{port}'
        return urlunsplit((parts.scheme,netloc,'','',''))
    except ValueError:
        raise argparse.ArgumentTypeError('Use a loopback http(s) URL without a path, credentials, query or fragment.') from None


def positive_timeout(value):
    try:
        result=float(value)
        if not math.isfinite(result) or result<=0:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError('Timeout must be a positive number of seconds.') from None


def model_name(value):
    if not value or len(value)>160 or any(not (c.isascii() and (c.isalnum() or c in '._:/-')) for c in value) or '://' in value:
        raise argparse.ArgumentTypeError('Provide a local Ollama model name, for example llama3:latest.')
    return value


def canonical_model(value):
    if not isinstance(value,str):
        return ''
    return value if ':' in value.rsplit('/',1)[-1] else value+':latest'


def strict_json(value):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON number')
    return json.loads(value,parse_constant=invalid_constant)


def request_json(opener, base, path, timeout, payload=None):
    data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode('utf-8')
    request=Request(base+path,data=data,headers={'Accept':'application/json',**({'Content-Type':'application/json'} if data else {})})
    try:
        with opener.open(request,timeout=timeout) as response:
            body=response.read(1024*1024+1)
            if len(body)>1024*1024:
                raise ProbeError('response_too_large')
            result=strict_json(body)
            if not isinstance(result,dict):
                raise ProbeError('invalid_response_shape')
            return result
    except HTTPError as error:
        # Do not record an HTTP error body: a local service may include secrets.
        raise ProbeError('http_error',error.code) from None
    except (TimeoutError,socket.timeout):
        raise ProbeError('timeout') from None
    except URLError as error:
        raise ProbeError('timeout' if isinstance(error.reason,(TimeoutError,socket.timeout)) else 'connection_error') from None
    except (ValueError,UnicodeError):
        raise ProbeError('invalid_json_response') from None
    except OSError:
        raise ProbeError('connection_error') from None


def nonnegative_number(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


def extraction_check(opener, args, name, content, expected):
    row={'name':name,'passed':False,'expected':expected}
    started=time.monotonic()
    payload={'model':args.model,'stream':False,'keep_alive':'5m','format':SCHEMA,'options':OPTIONS,
        'messages':[
            {'role':'system','content':'자료에 직접 기재된 월 실수령 급여(monthly_income)와 대출 원금 잔액 합계(total_debt)를 원 단위 숫자로 추출하세요. '
             '기재하지 않은 금액은 추측하거나 계산하지 말고 null로 표시하세요. 두 항목만 포함한 JSON 객체로 답하세요.'},
            {'role':'user','content':content}]}
    try:
        response=request_json(opener,args.base_url,'/api/chat',args.timeout,payload)
        row['model_matched']=canonical_model(response.get('model'))==canonical_model(args.model)
        row['done']=response.get('done') is True
        row['metrics']={key:response[key] for key in METRICS if nonnegative_number(response.get(key))}
        duration=row['metrics'].get('eval_duration',0)
        tokens=row['metrics'].get('eval_count')
        row['tokens_per_second']=round(tokens*1_000_000_000/duration,3) if duration>0 and tokens is not None else None
        try:
            actual=strict_json(response.get('message',{}).get('content',''))
        except (ValueError,TypeError,AttributeError):
            actual=None
        valid=isinstance(actual,dict) and set(actual)==set(expected) and all(value is None or nonnegative_number(value) for value in actual.values())
        # Keep only the expected numeric fields; never retain arbitrary model prose.
        row['actual']=actual if valid else None
        row['values_matched']=bool(valid and actual==expected)
        row['passed']=row['values_matched'] and row['model_matched'] and row['done']
        if not row['passed']:
            row['error']='extraction_mismatch' if not row['values_matched'] else 'model_mismatch' if not row['model_matched'] else 'incomplete_response'
    except ProbeError as error:
        row['error']=error.code
        if error.status is not None:
            row['http_status']=error.status
    row['wall_seconds']=round(time.monotonic()-started,3)
    return row


def gpu_check(opener,args):
    row={'passed':False,'model_matched':False,'size_vram_bytes':0,'context_tokens_requested':OPTIONS['num_ctx']}
    try:
        response=request_json(opener,args.base_url,'/api/ps',args.timeout)
        models=response.get('models',[])
        if not isinstance(models,list):
            raise ProbeError('invalid_model_list')
        match=next((item for item in models if isinstance(item,dict) and
                    any(canonical_model(item.get(key))==canonical_model(args.model) for key in ('model','name'))),None)
        if match is None:
            row['error']='requested_model_not_loaded'
            return row
        row['model_matched']=True
        row['size_vram_bytes']=match.get('size_vram') if nonnegative_number(match.get('size_vram')) else 0
        row['size_bytes']=match.get('size') if nonnegative_number(match.get('size')) else None
        context=match.get('context_length')
        row['context_tokens_reported']=context if nonnegative_number(context) else None
        row['processor']='gpu_assisted' if row['size_vram_bytes']>0 else 'cpu_only'
        row['passed']=row['size_vram_bytes']>0 and (row['context_tokens_reported'] is None or context==OPTIONS['num_ctx'])
        if not row['passed']:
            row['error']='cpu_only' if row['size_vram_bytes']==0 else 'context_mismatch'
    except ProbeError as error:
        row['error']=error.code
        if error.status is not None:
            row['http_status']=error.status
    return row


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',type=loopback_url,default='http://127.0.0.1:11434')
    parser.add_argument('--model',type=model_name,default='llama3:latest')
    parser.add_argument('--timeout',type=positive_timeout,default=240,help='Timeout per local HTTP request in seconds (default: 240).')
    parser.add_argument('--output',type=Path,help='Optional JSON report path; never stores document text or model prose.')
    args=parser.parse_args(argv)
    if args.output and args.output.suffix.lower()!='.json':
        parser.error('--output must be a .json file')
    # Disabling proxies and redirects keeps every request inside the local boundary.
    opener=build_opener(ProxyHandler({}),NoRedirect())
    report={'passed':False,'checked_at':datetime.now(timezone.utc).isoformat(),
        'base_url':args.base_url,'model':args.model,'synthetic_only':True,'options':OPTIONS,
        'timeout_seconds_per_request':args.timeout,'duration_unit':'nanoseconds',
        'checks':[extraction_check(opener,args,*check) for check in CHECKS]}
    report['gpu']=gpu_check(opener,args)
    report['passed']=all(check['passed'] for check in report['checks']) and report['gpu']['passed']
    output=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        try:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(output,encoding='utf-8')
        except OSError:
            print('Could not write the JSON report.',file=sys.stderr)
            return 2
    print(output,end='')
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
