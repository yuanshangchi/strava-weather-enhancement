"""Read an outdoor Strava activity and preview weather without changing it."""

import argparse
from datetime import datetime, timezone
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from connect_strava import ROOT, load_credentials, save_tokens


def fetch_json(url, *, token=None, form=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urlencode(form).encode() if form else None
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        advice = {
            401: "Run connect_strava.py again to reconnect.",
            403: "Check that activity read access was granted.",
            429: "Rate limit reached. Try again later.",
        }.get(error.code, "Try again later or check the requested activity.")
        raise RuntimeError(f"{urlsplit(url).hostname} returned HTTP {error.code}. {advice}") from None
    except URLError:
        raise RuntimeError("Could not reach the API. Check your network and Python certificates.") from None


def access_token():
    try:
        tokens = json.loads((ROOT / "tokens.json").read_text())
        if tokens["expires_at"] <= time.time() + 60:
            client_id, client_secret = load_credentials()
            refreshed = fetch_json("https://www.strava.com/oauth/token", form={
                "client_id": client_id, "client_secret": client_secret,
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            })
            if not all(refreshed.get(k) for k in ("access_token", "refresh_token", "expires_at")):
                raise RuntimeError("Strava returned incomplete tokens. Reconnect your account.")
            tokens.update(refreshed)
            save_tokens(tokens)
        return tokens["access_token"]
    except (OSError, KeyError, ValueError):
        raise RuntimeError("Local credentials are missing or invalid. Run connect_strava.py first.") from None


def usable_activity(activity):
    coords = activity.get("start_latlng")
    if activity.get("trainer") or str(activity.get("sport_type", "")).startswith("Virtual"):
        return False
    if not isinstance(coords, list) or len(coords) != 2:
        return False
    if not all(isinstance(x, (float, int)) and math.isfinite(x) for x in coords):
        return False
    return -90 <= coords[0] <= 90 and -180 <= coords[1] <= 180 and bool(activity.get("start_date"))


def get_activity(token, activity_id=None):
    base = "https://www.strava.com/api/v3/activities"
    if activity_id:
        activity = fetch_json(f"{base}/{activity_id}", token=token)
        if not usable_activity(activity):
            raise RuntimeError("This activity is indoor or has no usable start coordinates/time.")
        return activity
    # Bounded search: at most the latest 100 activities, newest first.
    for page in range(1, 4):
        batch = fetch_json(f"https://www.strava.com/api/v3/athlete/activities?per_page=40&page={page}", token=token)
        for activity in batch[:20] if page == 3 else batch:
            if usable_activity(activity):
                return activity
        if len(batch) < 40:
            break
    raise RuntimeError("No outdoor activity with start coordinates found in your latest 100 activities.")


def weather_request(activity, now=None):
    start = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise RuntimeError("Activity start time is missing its timezone.")
    start = start.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if start > now:
        raise RuntimeError("Activity start time is in the future.")
    recent = (now.date() - start.date()).days <= 5
    endpoint = "https://api.open-meteo.com/v1/forecast" if recent else "https://archive-api.open-meteo.com/v1/archive"
    fields = ["temperature_2m", "apparent_temperature", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m"]
    if recent:
        fields.append("uv_index")
    params = {
        "latitude": activity["start_latlng"][0], "longitude": activity["start_latlng"][1],
        "start_date": start.date().isoformat(), "end_date": start.date().isoformat(),
        "hourly": ",".join(fields), "timezone": "GMT", "timeformat": "unixtime",
        "temperature_unit": "celsius", "wind_speed_unit": "kmh",
    }
    return endpoint + "?" + urlencode(params), start


def weather_sample(data, start):
    """Select the UTC hour containing the start; keep missing measurements as None."""
    hourly = data.get("hourly", {})
    # Match the UTC hour containing the activity start, including midnight/DST cases.
    target = int(start.timestamp()) // 3600 * 3600
    try:
        index = hourly["time"].index(target)
    except (KeyError, ValueError):
        raise RuntimeError("Weather data is unavailable for the activity's start hour.") from None

    def value(key):
        values = hourly.get(key, [])
        item = values[index] if index < len(values) else None
        return finite_number(item)

    return target, {key: value(key) for key in (
        "temperature_2m", "apparent_temperature", "relative_humidity_2m",
        "wind_speed_10m", "wind_direction_10m", "uv_index",
    )}


def finite_number(value):
    """Use JSON null for missing or invalid numeric measurements, preserving zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def format_duration(seconds):
    """Express whole-second durations in total minutes and remaining seconds."""
    seconds = finite_number(seconds)
    if seconds is None or seconds < 0:
        return None
    minutes, remainder = divmod(round(seconds), 60)
    minute_unit = "minute" if minutes == 1 else "minutes"
    second_unit = "second" if remainder == 1 else "seconds"
    return f"{minutes} {minute_unit} and {remainder} {second_unit}"


def build_recap_input(activity, weather_data, start, *, weather_source):
    """Build an allowlisted factual payload for a future LLM recap."""
    target, sample = weather_sample(weather_data, start)
    distance = finite_number(activity.get("distance"))
    moving = finite_number(activity.get("moving_time"))
    distance_km = distance / 1000 if distance is not None and distance >= 0 else None
    pace = None
    if distance_km and moving is not None and moving > 0:
        pace = round(moving / distance_km)
    return {
        "activity": {
            "sport_type": activity.get("sport_type") or activity.get("type"),
            "distance_km": round(distance_km, 3) if distance_km is not None else None,
            "moving_time": format_duration(moving),
            "elapsed_time": format_duration(activity.get("elapsed_time")),
            "average_moving_pace_seconds_per_km": pace,
            "elevation_gain_m": finite_number(activity.get("total_elevation_gain")),
            "average_heart_rate_bpm": finite_number(activity.get("average_heartrate")),
        },
        "weather": {
            "source": "Open-Meteo",
            "data_type": weather_source,
            "is_estimated": True,
            "coverage": "Start location, hour containing activity start; not the entire route",
            "sample_time_utc": datetime.fromtimestamp(target, timezone.utc).isoformat().replace("+00:00", "Z"),
            "temperature_c": sample["temperature_2m"],
            "feels_like_c": sample["apparent_temperature"],
            "humidity_percent": sample["relative_humidity_2m"],
            "wind_speed_kmh": sample["wind_speed_10m"],
            "wind_from_degrees": sample["wind_direction_10m"],
            "uv_index": sample["uv_index"],
        },
    }


def format_weather(data, start):
    target, sample = weather_sample(data, start)

    def value(key, unit):
        item = sample[key]
        return "unavailable" if item is None else f"{item:g}{unit}"

    return "\n".join([
        "Weather near the start (estimated)",
        f"Temperature: {value('temperature_2m', '°C')} | Feels like: {value('apparent_temperature', '°C')}",
        f"Wind: {value('wind_speed_10m', ' km/h')} | From: {value('wind_direction_10m', '°')}",
        f"Humidity: {value('relative_humidity_2m', '%')} | UV index: {value('uv_index', '')}",
        f"Hourly sample: {datetime.fromtimestamp(target, timezone.utc):%Y-%m-%d %H:%M} UTC",
        "Source: Open-Meteo (https://open-meteo.com/)",
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activity-id", type=int, help="Preview a specific activity instead of the latest outdoor one")
    parser.add_argument("--json", action="store_true", help="Print compact activity and weather facts as JSON")
    args = parser.parse_args()
    activity = get_activity(access_token(), args.activity_id)
    url, start = weather_request(activity)
    weather_data = fetch_json(url)
    if args.json:
        source = "historical_reanalysis" if urlsplit(url).hostname == "archive-api.open-meteo.com" else "recent_forecast"
        payload = build_recap_input(activity, weather_data, start, weather_source=source)
        print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
        return
    weather = format_weather(weather_data, start)
    print(f"Activity: {activity['name']}")
    print(f"https://www.strava.com/activities/{activity['id']}")
    print(f"Start: {activity.get('start_date_local', activity['start_date'])} (activity local time)")
    print("\n" + weather)
    print("\nPreview only. No activities have been changed.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
