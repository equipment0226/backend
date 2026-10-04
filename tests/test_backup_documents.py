"""Generated court PDFs must survive backup alongside their approval snapshots."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from apps.api import domain, store
from scripts.backup import create_backup, restore_backup


class GeneratedDocumentBackupTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'.work/tests'
        root.mkdir(parents=True,exist_ok=True)
        self.folder=Path(tempfile.mkdtemp(prefix='generated-backup-',dir=root))
        patch=mock.patch.object(store,'DATA_DIR',self.folder/'data')
        patch.start()
        self.addCleanup(patch.stop)
        store.initialize()
        case=domain.new_case('백업테스트','CT01','서울회생법원','생성파일 백업 회귀',True)
        self.content=b'%PDF-1.4\nsynthetic backup bytes\n%%EOF'
        case['court_documents']=[{'id':'court-doc-test','sha256':hashlib.sha256(self.content).hexdigest(),
                                  'status':'review_approved','approval':{'reason':'synthetic test only'}}]
        store.insert_case(case)
        self.relative=Path('generated')/case['id']/'court-doc-test.pdf'
        path=store.DATA_DIR/self.relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(self.content)

    def test_generated_pdf_is_restored_byte_for_byte(self):
        archive=self.folder/'backup.zip'
        create_backup(archive)
        restored=self.folder/'restored'
        result=restore_backup(archive,restored)
        self.assertEqual(result['integrity'],'ok')
        self.assertEqual((restored/self.relative).read_bytes(),self.content)

    def test_incomplete_archive_cannot_claim_successful_restore(self):
        archive=self.folder/'backup.zip'
        create_backup(archive)
        broken=self.folder/'incomplete.zip'
        with zipfile.ZipFile(archive) as source,zipfile.ZipFile(broken,'w') as target:
            manifest=json.loads(source.read('manifest.json'))
            manifest['files']=[r for r in manifest['files'] if not r['name'].startswith('generated/')]
            for row in manifest['files']:
                target.writestr(row['name'],source.read(row['name']))
            target.writestr('manifest.json',json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'PDF'):
            restore_backup(broken,self.folder/'incomplete-restore')
