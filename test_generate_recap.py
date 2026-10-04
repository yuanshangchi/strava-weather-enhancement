import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import generate_recap as recap


class RecapTests(unittest.TestCase):
    def test_parse_completed_json(self):
        expected = {"recap": "A 5 km run.", "caveats": ["Estimated start weather only."]}
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(expected)}]}}]}
        self.assertEqual(recap.parse_response(response), expected)

    def test_reject_incomplete_or_malformed(self):
        for response in ({}, {"candidates": [{"finishReason": "MAX_TOKENS"}]},
                         {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "bad json"}]}}]}):
            with self.assertRaises(RuntimeError):
                recap.parse_response(response)
        for result in ({"recap": "", "caveats": ["x"]}, {"recap": "x", "caveats": "x"}, {"recap": "x", "caveats": []}):
            with self.assertRaises(RuntimeError):
                recap.validate_result(result)

    def test_quota_error_is_actionable_and_not_retried(self):
        error = HTTPError('https://example.invalid', 429, 'hidden-secret', {}, None)
        with patch.object(recap, 'urlopen', side_effect=error) as call:
            with self.assertRaisesRegex(RuntimeError, 'quota') as caught:
                recap.generate_recap({}, 'test-secret', 'gemini-2.5-flash-lite')
            self.assertNotIn('hidden-secret', str(caught.exception))
            call.assert_called_once()


if __name__ == '__main__':
    unittest.main()
