import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import preview_weather as preview


class WeatherTests(unittest.TestCase):
    def test_recap_calculations_and_allowlisted_fields(self):
        start = datetime(2026, 9, 20, 0, 12, tzinfo=timezone.utc)
        activity = {"sport_type": "Run", "distance": 14660.3, "moving_time": 5227,
                    "elapsed_time": 6684, "name": "Private title", "description": "Private note",
                    "start_latlng": [37, -122], "access_token": "must-not-appear"}
        data = {"hourly": {"time": [int(start.timestamp()) - 720], "temperature_2m": [15.4]}}
        result = preview.build_recap_input(activity, data, start, weather_source="recent_forecast")
        self.assertEqual(result['activity']['distance_km'], 14.66)
        self.assertEqual(result['activity']['average_moving_pace_seconds_per_km'], 357)
        self.assertEqual(result['activity']['elapsed_time_seconds'], 6684)
        self.assertEqual(result['weather']['sample_time_utc'], '2026-09-20T00:00:00Z')
        self.assertEqual(result['weather']['temperature_c'], 15.4)
        self.assertIsNone(result['weather']['uv_index'])
        import json
        encoded = json.dumps(result, allow_nan=False)
        for excluded in ('Private title', 'Private note', 'start_latlng', 'must-not-appear'):
            self.assertNotIn(excluded, encoded)

    def test_recap_zero_distance_and_invalid_measurements(self):
        start = datetime(2026, 9, 20, tzinfo=timezone.utc)
        data = {"hourly": {"time": [int(start.timestamp())], "temperature_2m": [float('nan')], "uv_index": [0]}}
        result = preview.build_recap_input({'distance': 0, 'moving_time': 0}, data, start, weather_source='recent_forecast')
        self.assertIsNone(result['activity']['average_moving_pace_seconds_per_km'])
        self.assertEqual(result['activity']['distance_km'], 0)
        self.assertIsNone(result['weather']['temperature_c'])
        self.assertEqual(result['weather']['uv_index'], 0)

    def test_skip_indoor_and_missing_gps(self):
        activity = {"start_latlng": [0, 0], "start_date": "2026-09-20T00:10:00Z"}
        self.assertTrue(preview.usable_activity(activity))
        self.assertFalse(preview.usable_activity(activity | {"trainer": True}))
        self.assertFalse(preview.usable_activity(activity | {"start_latlng": []}))
        self.assertFalse(preview.usable_activity(activity | {"sport_type": "VirtualRide"}))

    def test_recent_and_archive_routing(self):
        activity = {"start_latlng": [0, 0], "start_date": "2026-09-20T00:10:00Z"}
        now = datetime(2026, 9, 20, 2, tzinfo=timezone.utc)
        url, _ = preview.weather_request(activity, now)
        self.assertIn('/v1/forecast?', url)
        self.assertIn('uv_index', url)
        url, _ = preview.weather_request(activity | {"start_date": "2026-08-01T00:10:00Z"}, now)
        self.assertIn('/v1/archive?', url)
        self.assertNotIn('uv_index', url)

    def test_hour_matching_and_missing_values(self):
        start = datetime(2026, 9, 20, 0, 59, tzinfo=timezone.utc)
        target = int(start.timestamp()) // 3600 * 3600
        data = {"hourly": {"time": [target], "temperature_2m": [0], "relative_humidity_2m": [None]}}
        result = preview.format_weather(data, start)
        self.assertIn('Temperature: 0°C', result)
        self.assertIn('Humidity: unavailable', result)
        self.assertIn('00:00 UTC', result)
        with self.assertRaises(RuntimeError):
            preview.format_weather({"hourly": {"time": []}}, start)

    def test_refresh_persists_rotated_token(self):
        import tempfile
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tokens.json').write_text(json.dumps({"expires_at": 1, "refresh_token": "old", "scope": ["activity:read_all"]}))
            rotated = {"expires_at": 9999999999, "refresh_token": "new", "access_token": "access"}
            with patch.object(preview, 'ROOT', root), patch.object(preview, 'load_credentials', return_value=('id', 'secret')), patch.object(preview, 'fetch_json', return_value=rotated), patch.object(preview, 'save_tokens') as save:
                self.assertEqual(preview.access_token(), 'access')
                self.assertEqual(save.call_args.args[0]['refresh_token'], 'new')
                self.assertEqual(save.call_args.args[0]['scope'], ['activity:read_all'])


if __name__ == '__main__':
    unittest.main()
