"""Automatic import must not mistake test oracles for customer evidence."""
from pathlib import Path
import unittest
import zipfile

from apps.api import testing_materials


class TestingMaterialBoundaryTests(unittest.TestCase):
    def test_known_synthetic_person_gets_their_own_materials_only(self):
        selected = testing_materials.current('이하늘')
        self.assertEqual(selected['name'], '이하늘')
        self.assertEqual(selected['bundle'].name, 'ocr_retest_submission.zip')
        with zipfile.ZipFile(selected['bundle']) as archive:
            self.assertTrue(archive.namelist())
            self.assertTrue(all(name.lower().endswith('.pdf') for name in archive.namelist()))
        with self.assertRaises(FileNotFoundError):
            testing_materials.current('등록하지 않은 가상 이름')

    def test_customer_pack_contains_all_original_pdfs_and_no_legal_assumptions(self):
        selected = testing_materials.current()
        originals = Path(__file__).resolve().parents[1] / 'examples/court_ready_fixture/documents'
        with zipfile.ZipFile(selected['bundle']) as archive:
            names = archive.namelist()
            self.assertEqual(set(names), {path.name for path in originals.glob('*.pdf')})
            self.assertEqual(len(names), 29)
            self.assertTrue(all(name.lower().endswith('.pdf') for name in names))
            for name in names:
                self.assertEqual(archive.read(name), (originals / name).read_bytes())


if __name__ == '__main__':
    unittest.main()
