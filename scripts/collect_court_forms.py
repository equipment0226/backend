"""Fetch public court blank forms; save hashes and discovery URLs, never claim currency."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
import concurrent.futures
import re
import httpx
from lxml import html

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'data'/'court_forms'/'originals'
DEST.mkdir(parents=True,exist_ok=True)
SOURCES=[(f'national-{n}',f'https://www.scourt.go.kr/nm/minwon/doc/DocListAction.work?min_gubun=l&pageIndex={n}') for n in range(1,7)]+[(f'seoul-{n}',f'https://slb.scourt.go.kr/rel/information/min/MinListAction.work?gubun=25&pageIndex={n}') for n in range(1,7)]

def index(item):
    label,url=item
    r=httpx.get(url,timeout=40,follow_redirects=True);r.raise_for_status()
    (DEST.parent/(label+'-index.html')).write_text(r.text,encoding='utf-8')
    root=html.fromstring(r.text)
    links=[]
    for a in root.xpath('//a[@href]'):
        href=a.get('href','')
        if 'downdoc(' in href:
            parts=re.findall("'([^']*)'",href)
            if len(parts)>=2:
                links.append({'index':label,'index_url':url,'title':parts[1], 'url':'https://file.scourt.go.kr/AttachDownload?'+urlencode({'path':'004','file':parts[0],'downFile':parts[1],'seqnum':''})})
            continue
        if 'AttachDownload' not in href:continue
        href=httpx.URL(url).join(href).__str__()
        title=' '.join(a.itertext()).strip()
        links.append({'index':label,'index_url':url,'title':title,'url':href})
    return links

if __name__=='__main__':
    all_links=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for label,result in zip([s[0] for s in SOURCES],pool.map(index,SOURCES)):
            print(label,len(result));all_links.extend(result)
    (DEST.parent/'discovered.json').write_text(json.dumps(all_links,ensure_ascii=False,indent=2),encoding='utf-8')
    # Select only workflow forms. Both PDF originals and matching HWP are retained.
    codes=('D5100','D5106','D5101','D5103','D5105','D5110','D5115')
    selected=[];seen=set()
    for entry in all_links:
        title=entry['title']
        if not entry['index'].startswith('national'):continue
        if any(code in title for code in codes) or '부산회생법원 자료제출목록' in title or '(서울회생법원)개인회생사건관련 제증명' in title:
            code=next((c for c in codes if c in title), 'BUSAN-ATTACHMENTS' if '부산' in title else 'SEOUL-CERTIFICATE')
            key=(code,Path(title).suffix)
            if key not in seen:selected.append(entry);seen.add(key)
    def download(entry):
        suffix=Path(entry['title']).suffix
        code=next((c for c in codes if c in entry['title']), 'BUSAN-ATTACHMENTS' if '부산' in entry['title'] else 'SEOUL-CERTIFICATE')
        path=DEST/(code+suffix)
        if path.exists():data=path.read_bytes()
        else:
            r=httpx.get(entry['url'],timeout=50,follow_redirects=True);r.raise_for_status();data=r.content
            if suffix=='.pdf' and not data.startswith(b'%PDF'):raise ValueError('not PDF')
            if suffix=='.hwp' and not data.startswith(bytes.fromhex('d0cf11e0')):raise ValueError('not HWP')
            path.write_bytes(data)
        return dict(entry,id=code+suffix.replace('.','-'),path=str(path.relative_to(ROOT)).replace('\\','/'),sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),retrieved_at='2026-10-03',currency_status='officially_listed_snapshot_requires_filing_review')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: collected=list(pool.map(download,selected))
    unique={x['id']:x for x in collected}
    (DEST.parent/'sources.json').write_text(json.dumps(list(unique.values()),ensure_ascii=False,indent=2),encoding='utf-8')
    for entry in unique.values():print(entry['id'],entry['bytes'])
