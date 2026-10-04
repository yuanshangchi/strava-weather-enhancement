import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync_description as sync
from connect_strava import validate_callback


class SyncTests(unittest.TestCase):
    def test_preserve_text_and_replace_block(self):
        recap = {"recap": "A run.", "caveats": ["Start weather only."]}
        original = "My notes 🏃\n "
        first = sync.merge_description(original, recap)
        self.assertTrue(first.startswith(original + '\n\n'))
        self.assertEqual(sync.merge_description(first, recap), first)
        updated = sync.merge_description(first + '\nAfterward', recap | {'recap': 'Updated run.'})
        self.assertTrue(updated.endswith('\nAfterward'))
        self.assertEqual(updated.count(sync.BEGIN), 1)
        for malformed in (sync.BEGIN, sync.END, sync.END + sync.BEGIN, sync.BEGIN * 2 + sync.END):
            with self.assertRaises(RuntimeError):
                sync.merge_description(malformed, recap)

    def test_write_scope_is_required_during_reconnect(self):
        query = {'state': ['s'], 'code': ['c'], 'scope': ['activity:read_all']}
        with self.assertRaises(ValueError):
            validate_callback(query, 's', {'activity:read_all', 'activity:write'})

    def test_apply_checks_edits_and_only_writes_description(self):
        draft = {'activity_id': 123, 'athlete_id': 7, 'original_description': 'old', 'proposed_description': 'new'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'tokens.json').write_text(json.dumps({'scope': ['activity:write']}))
            with patch.object(sync, 'ROOT', root), patch.object(sync, 'access_token', return_value='dummy'), patch.object(sync, 'fetch_json') as fetch, patch.object(sync, 'urlopen') as send:
                fetch.return_value = {'athlete': {'id': 7}, 'description': 'edited'}
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    sync.apply_draft(draft)
                send.assert_not_called()
                fetch.return_value['description'] = 'new'
                self.assertIn('already saved', sync.apply_draft(draft))
                send.assert_not_called()
                fetch.return_value['description'] = 'old'
                send.return_value.__enter__.return_value.read.return_value = '{"description": "new"}'
                self.assertIn('verified', sync.apply_draft(draft))
                request = send.call_args.args[0]
                self.assertEqual(request.method, 'PUT')
                self.assertEqual(json.loads(request.data), {'description': 'new'})

    def test_missing_write_scope_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'tokens.json').write_text('{"scope": ["activity:read_all"]}')
            with patch.object(sync, 'ROOT', root), patch.object(sync, 'access_token', return_value='dummy'), patch.object(sync, 'urlopen') as send:
                with self.assertRaisesRegex(RuntimeError, 'Reconnect'):
                    sync.apply_draft({'activity_id': 123, 'original_description': '', 'proposed_description': 'new'})
                send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
