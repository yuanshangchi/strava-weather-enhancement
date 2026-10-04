import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import generate_recap as recap


class RecapTests(unittest.TestCase):
    def test_legacy_duration_conversion_preserves_input(self):
        original = {"activity": {"moving_time_seconds": 75, "elapsed_time_seconds": 4507}, "weather": {}}
        result = recap.prepare_facts(original)
        self.assertEqual(result['activity']['moving_time'], '1 minute and 15 seconds')
        self.assertEqual(result['activity']['elapsed_time'], '75 minutes and 7 seconds')
        self.assertIn('moving_time_seconds', original['activity'])
        self.assertNotIn('moving_time_seconds', result['activity'])
        self.assertEqual(recap.prepare_facts(result), result)

    def test_duration_boundaries(self):
        for seconds, expected in [(0, '0 minutes and 0 seconds'), (1, '0 minutes and 1 second'),
                                  (60, '1 minute and 0 seconds'), (61, '1 minute and 1 second'),
                                  (3600, '60 minutes and 0 seconds'), (None, None), (-1, None)]:
            with self.subTest(seconds=seconds):
                self.assertEqual(recap.format_duration(seconds), expected)

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
