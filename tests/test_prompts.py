import unittest
from apps.api import prompt_registry


class PromptRegistryTests(unittest.TestCase):
    def test_registered_prompts_resolve_with_stable_source_hash(self):
        import hashlib
        entries=prompt_registry.catalog(True)['prompts']
        self.assertEqual({e['id'] for e in entries},{'intake','case_evidence','legacy_review'})
        for entry in entries:
            self.assertEqual(prompt_registry.instruction(entry['id']),entry['text'])
            self.assertEqual(entry['sha256'],hashlib.sha256(entry['text'].encode()).hexdigest())

    def test_unknown_prompt_and_path_traversal_are_not_read(self):
        for key in ('../../.env','unknown'):
            with self.assertRaises(ValueError):prompt_registry.instruction(key)

    def test_hash_changes_when_instruction_changes(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import json
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            (folder/'manifest.json').write_text(json.dumps({'version':'test','prompts':[{'id':'x','file':'x.txt','version':'1'}]}),encoding='utf-8')
            (folder/'x.txt').write_text('first instructions',encoding='utf-8')
            with patch.object(prompt_registry,'PROMPT_DIR',folder):
                before=prompt_registry.signature()
                (folder/'x.txt').write_text('changed instructions',encoding='utf-8')
                self.assertNotEqual(before,prompt_registry.signature())
