from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import reset_fresh_start as resetter


class FreshResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / '.local'
        self.runtime.mkdir()
        (self.root / 'reports').mkdir()
        for path in ('uploads/old/source.txt', 'generated/old/document.pdf', 'ocr/jobs/old/result.json',
                     'corpus/text/law.txt', 'ocr/models/weights', 'backups/earlier.zip'):
            dest = self.runtime / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'original')
        with closing(sqlite3.connect(self.runtime / 'debtoff.sqlite3')) as con:
            con.executescript('CREATE TABLE cases(id TEXT); INSERT INTO cases VALUES ("old-case");')
            con.commit()
        for name, value in [('ROOT', self.root), ('RUNTIME', self.runtime)]:
            p = patch.object(resetter, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_dry_run_preserves_everything_and_does_not_create_profile(self):
        report = resetter.reset()
        self.assertFalse(report['applied'])
        self.assertTrue((self.runtime / 'uploads/old/source.txt').exists())
        self.assertFalse((self.runtime / 'start-profile.json').exists())

    def test_rejects_outside_runtime(self):
        with self.assertRaises(ValueError):
            resetter.checked_path('../outside')

    def test_running_server_prevents_deletion(self):
        with patch.object(resetter.socket, 'socket') as connection:
            connection.return_value.__enter__.return_value.connect_ex.return_value = 0
            with self.assertRaises(RuntimeError):
                resetter.reset(True)
        self.assertTrue((self.runtime / 'debtoff.sqlite3').exists())

    def test_verified_archive_contains_originals_before_clear_preserves_official_sources(self):
        with patch.object(resetter.socket, 'socket') as connection:
            connection.return_value.__enter__.return_value.connect_ex.return_value = 1
            report = resetter.reset(True)
        self.assertFalse((self.runtime / 'debtoff.sqlite3').exists())
        self.assertFalse((self.runtime / 'uploads').exists())
        self.assertFalse((self.runtime / 'ocr/jobs').exists())
        for path in ('corpus/text/law.txt', 'ocr/models/weights', 'backups/earlier.zip'):
            self.assertEqual((self.runtime / path).read_bytes(), b'original')
        self.assertEqual(json.loads((self.runtime / 'start-profile.json').read_text())['profile'], 'fresh')
        with zipfile.ZipFile(self.root / report['backup']) as archive:
            self.assertEqual(archive.read('uploads/old/source.txt'), b'original')
            recovered = self.root / 'recovered.sqlite3'
            recovered.write_bytes(archive.read('debtoff.sqlite3'))
        with closing(sqlite3.connect(recovered)) as con:
            self.assertEqual(con.execute('SELECT id FROM cases').fetchone()[0], 'old-case')
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    unittest.main()
