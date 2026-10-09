from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import route_matches as routes
import sync_description as sync


def encode(points):
    result, last = '', (0, 0)
    for point in points:
        integers = tuple(round(x * 1e5) for x in point)
        for value, old in zip(integers, last):
            difference = value - old
            number = ~(difference << 1) if difference < 0 else difference << 1
            while number >= 32:
                result += chr((32 | (number & 31)) + 63)
                number >>= 5
            result += chr(number + 63)
        last = integers
    return result


LOOP = [(37, -122), (37, -121.99), (37.01, -121.99), (37.01, -122), (37, -122)]


def activity(identifier=1, days=0, points=LOOP, **extra):
    return {'id': identifier, 'start_date': (datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(days=days)).isoformat(),
            'sport_type': 'Run', 'start_latlng': list(points[0]), 'distance': 4000,
            'map': {'summary_polyline': encode(points)}, 'elapsed_time': 1400, 'moving_time': 1400, **extra}


class RouteTests(unittest.TestCase):
    def test_known_polyline_and_malformed(self):
        self.assertEqual(routes.decode_polyline('_p~iF~ps|U_ulLnnqC_mqNvxq`@'),
                         [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])
        for value in ('', '?', '~~~~~~~', None):
            with self.assertRaises(ValueError):
                routes.decode_polyline(value)

    def test_loop_direction_and_gps_noise(self):
        target = activity()
        self.assertEqual(routes.compare_routes(target, activity(2, 1))['status'], 'similar_route')
        self.assertEqual(routes.compare_routes(target, activity(2, 1, list(reversed(LOOP))))['status'], 'similar_route')
        noisy = [(lat + .00003, lon + .00003) for lat, lon in LOOP]
        self.assertEqual(routes.compare_routes(target, activity(2, 1, noisy))['status'], 'similar_route')

    def test_different_path_extra_laps_and_retracing(self):
        different = [LOOP[0], (37.01, -122.01), (37.02, -122), LOOP[0]]
        self.assertEqual(routes.compare_routes(activity(), activity(2, 1, different))['status'], 'rejected')
        self.assertEqual(routes.compare_routes(activity(), activity(2, 1, LOOP + LOOP[1:], distance=8000))['status'], 'rejected')
        retraced = [LOOP[0], LOOP[1], LOOP[0]]
        self.assertEqual(routes.compare_routes(activity(points=retraced), activity(2, 1, retraced))['status'], 'similar_route')
        self.assertEqual(routes.compare_routes(activity(), activity(2, 1, map={}))['status'], 'unavailable')

    def test_shifted_loop_start_and_imperfect_out_and_back(self):
        shifted = LOOP[2:-1] + LOOP[:3]
        self.assertEqual(routes.compare_routes(activity(), activity(2, 1, shifted))['status'], 'similar_route')
        retraced = [LOOP[0], LOOP[1], LOOP[2], LOOP[1], LOOP[0]]
        imperfect = retraced[:-1] + [(37, -121.999)]
        self.assertEqual(routes.compare_routes(activity(points=retraced), activity(2, 1, imperfect))['status'], 'similar_route')

    def test_overlap_required_in_both_paths(self):
        # Same stated distance must not allow a subset path to pass.
        short = [LOOP[0], LOOP[1]]
        result = routes.compare_routes(activity(), activity(2, 1, short))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['candidate_overlap'], 1.0)
        self.assertLess(result['target_overlap'], .90)

    def test_out_and_back_selection_keeps_other_filters(self):
        retraced = [LOOP[0], LOOP[1], LOOP[0]]
        target = activity(points=retraced)
        candidates = [activity(2, 1, retraced, workout_type=1),
                      activity(3, 2, retraced, elapsed_time=2500), activity(4, 3, retraced)]
        with patch.object(routes, 'recent_activities', return_value=candidates):
            report, selected = routes.select_match('token', target)
        self.assertEqual(selected['id'], 4)
        self.assertEqual(report['candidates'][2]['status'], 'similar_route')

    def test_window_pagination_and_sorting(self):
        first = [activity(100+i, 20) for i in range(100)]
        second = [activity(2, 1), activity(3, 90), activity(4, 91), activity(5, -1), activity()]
        with patch.object(routes, 'fetch_json', side_effect=[first, second]) as fetch:
            result = routes.recent_activities('token', activity())
        self.assertEqual(result[0]['id'], 2)
        self.assertIn(3, [a['id'] for a in result])
        self.assertNotIn(4, [a['id'] for a in result])
        self.assertNotIn(5, [a['id'] for a in result])
        self.assertNotIn(1, [a['id'] for a in result])
        query = parse_qs(urlsplit(fetch.call_args_list[0].args[0]).query)
        self.assertEqual(int(query['before'][0]), int(routes.start_time(activity()).timestamp()))
        self.assertEqual(int(query['before'][0]) - int(query['after'][0]), 90*86400+1)
        self.assertEqual(fetch.call_count, 2)

    def test_most_recent_suitable_not_best_weather(self):
        candidates = [activity(2, 1, list(reversed(LOOP))), activity(3, 2, workout_type=1),
                      activity(4, 3, elapsed_time=2500), activity(5, 4), activity(6, 8)]
        with patch.object(routes, 'recent_activities', return_value=candidates):
            report, selected = routes.select_match('token', activity())
        self.assertEqual(selected['id'], 2)
        self.assertEqual(report['selected_id'], 2)

    def test_no_match_and_missing_target(self):
        with patch.object(routes, 'recent_activities', return_value=[]):
            report, selected = routes.select_match('token', activity())
        self.assertIsNone(selected)
        self.assertIn('90 days', report['reason'])
        with patch.object(routes, 'recent_activities') as fetch:
            report, selected = routes.select_match('token', activity(map={}))
            fetch.assert_not_called()
        self.assertIn('unavailable', report['reason'])

    def test_weather_only_fallback_in_draft(self):
        target = activity(name='Run', athlete={'id': 7}, description='Notes')
        start = routes.start_time(target)
        weather = {'hourly': {'time': [int(start.timestamp())], 'temperature_2m': [18]}}
        report = {'reason': 'No comparable similar-route run found in the previous 90 days', 'selected_id': None}
        with patch.object(sync, 'access_token', return_value='token'), patch.object(sync, 'get_activity', return_value=target), patch.object(sync, 'fetch_json', side_effect=[target, weather]), patch.object(sync, 'weather_request', return_value=('url', start)), patch.object(sync, 'select_match', return_value=(report, None)):
            draft = sync.prepare_draft(1)
        self.assertIn('18°C', draft['proposed_description'])
        self.assertIn('90 days', draft['proposed_description'])
        self.assertTrue(draft['proposed_description'].startswith('Notes'))


if __name__ == '__main__':
    unittest.main()
