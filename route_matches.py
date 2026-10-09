"""Find recent similar-route runs using distance and bidirectional path overlap."""

import argparse
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import json
import math
from urllib.parse import urlencode

from preview_weather import access_token, fetch_json, finite_number, get_activity, usable_activity

WINDOW_DAYS = 90
DISTANCE_TOLERANCE = 0.10
PATH_METERS = 50
MIN_OVERLAP = 0.90


def start_time(activity):
    start = datetime.fromisoformat(activity['start_date'].replace('Z', '+00:00'))
    if start.tzinfo is None:
        raise ValueError('Missing activity timezone')
    return start.astimezone(timezone.utc)


def is_run(activity):
    return activity.get('sport_type', activity.get('type')) in ('Run', 'TrailRun') and usable_activity(activity)


def decode_polyline(encoded):
    if not isinstance(encoded, str) or not encoded:
        raise ValueError('Missing route geometry')
    index, latitude, longitude = 0, 0, 0
    points = []
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            number, shift = 0, 0
            while True:
                if index >= len(encoded) or shift > 30:
                    raise ValueError('Invalid polyline')
                byte = ord(encoded[index]) - 63
                index += 1
                if not 0 <= byte <= 63:
                    raise ValueError('Invalid polyline character')
                number |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            deltas.append(~(number >> 1) if number & 1 else number >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        point = (latitude / 1e5, longitude / 1e5)
        if not (-90 <= point[0] <= 90 and -180 <= point[1] <= 180):
            raise ValueError('Invalid coordinates')
        if not points or points[-1] != point:
            points.append(point)
    if len(points) < 2:
        raise ValueError('Insufficient route geometry')
    return points


def project(points, origin):
    # Local metric projection, appropriate for nearby running routes.
    scale = math.cos(math.radians(origin[0]))
    return [(6371000 * math.radians((lon - origin[1] + 180) % 360 - 180) * scale,
             6371000 * math.radians(lat - origin[0])) for lat, lon in points]


def resample(points, count=None):
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.dist(a, b))
    length = cumulative[-1]
    if length < 1:
        raise ValueError('Route too short')
    count = count or min(801, max(2, math.ceil(length / 25) + 1))
    result = []
    for i in range(count):
        distance = length * i / (count - 1)
        j = min(bisect_right(cumulative, distance) - 1, len(points) - 2)
        fraction = (distance - cumulative[j]) / (cumulative[j + 1] - cumulative[j])
        result.append(tuple(a + fraction * (b - a) for a, b in zip(points[j], points[j + 1])))
    return result


def segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    fraction = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2)) if length2 else 0
    return math.dist(p, (a[0] + fraction * dx, a[1] + fraction * dy))


def coverage(samples, path):
    return sum(any(segment_distance(p, a, b) <= PATH_METERS for a, b in zip(path, path[1:]))
               for p in samples) / len(samples)


def compare_routes(target, candidate):
    """Return explainable match diagnostics, never classify missing GPS as a match."""
    result = {'status': 'rejected', 'reason': ''}
    if not is_run(candidate):
        return result | {'reason': 'Not an outdoor run with location/time'}
    distance = finite_number(target.get('distance'))
    other = finite_number(candidate.get('distance'))
    if not distance or not other or other <= 0 or abs(other - distance) / distance > DISTANCE_TOLERANCE:
        return result | {'reason': 'Distance differs by more than 10% or is missing'}
    try:
        a_ll = decode_polyline((target.get('map') or {}).get('summary_polyline'))
        b_ll = decode_polyline((candidate.get('map') or {}).get('summary_polyline'))
        a, b = project(a_ll, a_ll[0]), project(b_ll, a_ll[0])
        ab, ba = coverage(resample(a), b), coverage(resample(b), a)
        result.update(target_overlap=round(ab, 3), candidate_overlap=round(ba, 3))
        if min(ab, ba) < MIN_OVERLAP:
            return result | {'reason': 'Less than 90% overlap in one or both paths'}
        return result | {'status': 'similar_route', 'reason': 'Similar distance and at least 90% overlap in both paths'}
    except (ValueError, TypeError):
        return result | {'status': 'unavailable', 'reason': 'Missing or invalid route geometry'}


def recent_activities(token, target):
    end = start_time(target)
    beginning = end - timedelta(days=WINDOW_DAYS)
    activities, seen = [], set()
    page = 1
    while True:
        params = urlencode({'after': int(beginning.timestamp()) - 1, 'before': int(end.timestamp()),
                            'page': page, 'per_page': 100})
        batch = fetch_json('https://www.strava.com/api/v3/athlete/activities?' + params, token=token)
        if not isinstance(batch, list):
            raise RuntimeError('Unexpected activity-list response; route search incomplete.')
        if not batch:
            break
        new_ids = {a['id'] for a in batch} - seen
        if not new_ids:
            raise RuntimeError('Pagination repeated; route search incomplete.')
        for activity in batch:
            if activity['id'] not in seen and activity['id'] != target['id']:
                try:
                    if beginning <= start_time(activity) < end:
                        activities.append(activity)
                except (KeyError, ValueError):
                    pass
            seen.add(activity['id'])
        if len(batch) < 100:
            break
        page += 1
    return sorted(activities, key=start_time, reverse=True)


def select_match(token, target):
    report = {'window_days': WINDOW_DAYS, 'target_id': target['id'], 'selected_id': None, 'candidates': []}
    if not is_run(target):
        return report | {'reason': 'Route comparison is available for outdoor runs only'}, None
    try:
        decode_polyline((target.get('map') or {}).get('summary_polyline'))
    except (ValueError, TypeError):
        return report | {'reason': 'Target route geometry unavailable; comparison could not be assessed'}, None
    selected = None
    for candidate in recent_activities(token, target):
        if not is_run(candidate):
            continue
        diagnostics = compare_routes(target, candidate)
        suitable = diagnostics['status'] == 'similar_route'
        # Treat untagged runs and generic runs (0) alike, not as proof of equal effort.
        if suitable and (target.get('workout_type') or 0) != (candidate.get('workout_type') or 0):
            suitable = False
            diagnostics['reason'] = 'Route matches, but workout type differs'
        elapsed = finite_number(candidate.get('elapsed_time'))
        moving = finite_number(candidate.get('moving_time'))
        if suitable and elapsed is not None and moving is not None and elapsed - moving > 600:
            suitable = False
            diagnostics['reason'] = 'Over 10 minutes total nonmoving time; excluded conservatively (stop reason unknown)'
        report['candidates'].append({'activity_id': candidate['id'], 'date': candidate['start_date'],
                                     'distance_m': candidate.get('distance'), 'suitable': suitable, **diagnostics})
        if suitable and selected is None:
            selected = candidate
            report['selected_id'] = candidate['id']
    report['reason'] = ('Most recent suitable similar-route run' if selected
                        else 'No comparable similar-route run found in the previous 90 days')
    return report, selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activity-id', type=int, required=True)
    args = parser.parse_args()
    token = access_token()
    target = get_activity(token, args.activity_id)
    report, _ = select_match(token, target)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error))
