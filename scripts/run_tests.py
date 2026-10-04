import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


class RecordedResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.records=[]

    def addSuccess(self,test):
        super().addSuccess(test)
        self.records.append({'test':test.id(),'status':'PASS'})

    def addFailure(self,test,err):
        super().addFailure(test,err)
        self.records.append({'test':test.id(),'status':'FAIL'})

    def addError(self,test,err):
        super().addError(test,err)
        self.records.append({'test':test.id(),'status':'ERROR'})


if __name__=='__main__':
    # Set the storage boundary BEFORE unittest imports any application module.
    # Never inherit a development/live DATA_DIR when running the whole suite.
    test_root = ROOT / '.work' / 'tests'
    test_root.mkdir(parents=True, exist_ok=True)
    suite_data_dir = Path(tempfile.mkdtemp(prefix='suite-', dir=test_root)).resolve()
    if not suite_data_dir.is_relative_to(test_root.resolve()) or suite_data_dir == (ROOT / '.local').resolve():
        raise RuntimeError('Test storage must be isolated from the development data directory.')
    os.environ['DEBTOFF_DATA_DIR'] = str(suite_data_dir)
    os.environ['DEBTOFF_CORPUS_DIR'] = str(suite_data_dir / 'corpus')
    os.environ['DEBTOFF_DEMO_MODE'] = '1'
    os.environ['DEBTOFF_START_PROFILE'] = 'demo'
    os.environ['DEBTOFF_AUTO_AX'] = '0'
    os.environ['DEBTOFF_LEGAL_WATCH'] = '0'
    started=time.perf_counter()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    result=unittest.TextTestRunner(verbosity=2,resultclass=RecordedResult).run(suite)
    report={'suite':'actual API + SQLite + evidence harness integration','passed':sum(x['status']=='PASS' for x in result.records),'failed':len(result.failures)+len(result.errors),'skipped':len(result.skipped),'seconds':round(time.perf_counter()-started,3),'tests':result.records,'limitations':['합성 데이터 검증이며 법률 정답·현장 수용·전체 요구 인수 아님','모델 API 단위시험은 대역 사용; 실제 llama3 결과는 llama-benchmark.json에 분리']}
    report['storage_isolation'] = {'suite_data_dir': str(suite_data_dir.relative_to(ROOT)),
                                   'development_data_dir_used': False,
                                   'environment_set_before_discovery': True}
    (ROOT/'reports').mkdir(exist_ok=True)
    (ROOT/'reports/test-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)
