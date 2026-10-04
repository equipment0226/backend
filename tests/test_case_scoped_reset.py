import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import reset_test_case


class CaseScopedResetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.local = root / '.local'
        self.local.mkdir()
        for attribute, value in [('ROOT', root), ('RUNTIME', self.local)]:
            patcher = patch.object(reset_test_case, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.case_id = 'case-111111111111'
        self.other_id = 'case-222222222222'
        with closing(sqlite3.connect(self.local / 'debtoff.sqlite3')) as con, con:
            con.executescript('CREATE TABLE cases(id TEXT,body TEXT); CREATE TABLE accounts(id TEXT,password_hash TEXT);'
                             'CREATE TABLE history(case_id TEXT,body TEXT); CREATE TABLE court_outcomes(case_id TEXT,body TEXT);')
            con.execute('INSERT INTO accounts VALUES (?,?)', ('customer', 'preserve-password-hash'))
            for case_id in (self.case_id, self.other_id):
                con.execute('INSERT INTO cases VALUES (?,?)', (case_id, json.dumps({'id': case_id, 'synthetic': True})))
                con.execute('INSERT INTO history VALUES (?,?)', (case_id, 'original-history'))
                con.execute('INSERT INTO court_outcomes VALUES (?,?)', (case_id, 'fictional-learning'))
                for kind in ('uploads', 'generated'):
                    folder = self.local / kind / case_id
                    folder.mkdir(parents=True)
                    (folder / 'evidence.txt').write_text(case_id)
        (self.local / 'legal-corpus.txt').write_text('keep official sources')

    def test_scoped_reset_archives_evidence_and_preserves_other_case_accounts(self):
        with patch('scripts.reset_test_case.socket.socket') as socket:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 1
            result = reset_test_case.reset(self.case_id, apply=True)
        self.assertEqual(result['remaining_cases'], 1)
        self.assertTrue(result['accounts_unchanged'])
        with closing(sqlite3.connect(self.local / 'debtoff.sqlite3')) as con, con:
            self.assertEqual(con.execute('SELECT id FROM cases').fetchall(), [(self.other_id,)])
            self.assertEqual(con.execute('SELECT * FROM accounts').fetchall(), [('customer', 'preserve-password-hash')])
            for table in ('history', 'court_outcomes'):
                self.assertEqual(con.execute(f'SELECT case_id FROM {table}').fetchall(), [(self.other_id,)])
        self.assertFalse((self.local / 'uploads' / self.case_id).exists())
        self.assertTrue((self.local / 'uploads' / self.other_id / 'evidence.txt').exists())
        self.assertEqual((self.local / 'legal-corpus.txt').read_text(), 'keep official sources')
        with zipfile.ZipFile(self.local.parent / result['backup']) as archive:
            self.assertIn('debtoff.sqlite3', archive.namelist())
            self.assertEqual(archive.read('uploads/' + self.case_id + '/evidence.txt').decode(), self.case_id)

    def test_real_case_is_never_deleted_by_fictional_reset(self):
        with closing(sqlite3.connect(self.local / 'debtoff.sqlite3')) as con, con:
            con.execute('UPDATE cases SET body=? WHERE id=?', (json.dumps({'synthetic': False}), self.case_id))
        with self.assertRaises(ValueError):
            reset_test_case.reset(self.case_id, apply=True)
        self.assertTrue((self.local / 'uploads' / self.case_id).exists())

    def test_archive_import_jobs_are_backed_up_and_removed_with_their_case(self):
        folder = self.local / 'document_imports' / self.case_id
        folder.mkdir(parents=True)
        (folder / 'job.json').write_text('{"status":"completed"}', encoding='utf-8')
        with patch('scripts.reset_test_case.socket.socket') as socket:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 1
            result = reset_test_case.reset(self.case_id, apply=True)
        self.assertFalse(folder.exists())
        with zipfile.ZipFile(self.local.parent / result['backup']) as archive:
            self.assertEqual(json.loads(archive.read('document_imports/' + self.case_id + '/job.json')),
                             {'status': 'completed'})

    def test_invalid_or_escaping_case_id_is_rejected(self):
        with self.assertRaises(ValueError):
            reset_test_case.reset('../accounts', apply=True)
        with self.assertRaises(ValueError):
            reset_test_case.checked(self.local.parent / 'outside')
