"""Prepare a Strava description draft; --apply publishes that exact saved draft."""

import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from connect_strava import ROOT
from preview_weather import access_token, fetch_json, get_activity, weather_request, weather_sample
from route_matches import select_match

BEGIN = "[Workout Weather Recap]"
END = "[/Workout Weather Recap]"
DEFAULT_DRAFT = ROOT / "description_draft.json"


def format_weather_description(data, start):
    """A compact weather-only block, omitting unavailable measurements."""
    _, sample = weather_sample(data, start)
    lines = ["Weather near the start (estimated)"]
    temperature = []
    for key, label in (("temperature_2m", "Temperature"), ("apparent_temperature", "Feels like")):
        if sample[key] is not None:
            temperature.append(f"{label}: {sample[key]:g}°C")
    if temperature:
        lines.append(" · ".join(temperature))
    if sample["wind_speed_10m"] is not None:
        wind = f"Wind: {sample['wind_speed_10m']:g} km/h"
        direction = sample["wind_direction_10m"]
        if direction is not None and sample["wind_speed_10m"] > 0:
            compass = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int((direction % 360 + 22.5) // 45) % 8]
            wind += f" from {compass}"
        lines.append(wind)
    other = []
    for key, label, unit in (("relative_humidity_2m", "Humidity", "%"), ("uv_index", "UV index", "")):
        if sample[key] is not None:
            other.append(f"{label}: {sample[key]:g}{unit}")
    if other:
        lines.append(" · ".join(other))
    if len(lines) == 1:
        raise RuntimeError("No weather measurements available. Existing description will be preserved.")
    lines.append("Source: Open-Meteo · Hourly estimate at the start location")
    return "\n".join(lines)


def merge_description(original, text):
    if BEGIN in text or END in text:
        raise RuntimeError("Generated text contains reserved markers. Generate a new preview.")
    block = f"{BEGIN}\n{text}\n{END}"
    original = original or ""
    if BEGIN not in original and END not in original:
        return original + ("\n\n" if original else "") + block
    if original.count(BEGIN) != 1 or original.count(END) != 1:
        raise RuntimeError("Description has ambiguous recap markers. Fix them in Strava first.")
    first, last = original.index(BEGIN), original.index(END)
    if last < first:
        raise RuntimeError("Description has reversed recap markers. Fix them in Strava first.")
    return original[:first] + block + original[last + len(END):]


def save_draft(path, draft):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".description-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(draft, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def comparison_text(current_weather, previous_weather, previous):
    differences = []
    for key, label, unit in (("temperature_2m", "temperature", "°C"),
                             ("apparent_temperature", "feels-like temperature", "°C"),
                             ("relative_humidity_2m", "humidity", " percentage points"),
                             ("wind_speed_10m", "wind speed", " km/h")):
        current, old = current_weather.get(key), previous_weather.get(key)
        if current is not None and old is not None:
            delta = current - old
            change = 'unchanged' if abs(delta) < 0.05 else f'{abs(delta):.1f}{unit} {"higher" if delta > 0 else "lower"}'
            differences.append(f'{label} {change}')
    if not differences:
        return 'A route match was found, but comparable weather measurements are unavailable.'
    date = (previous.get('start_date_local') or previous['start_date'])[:10]
    return (f"Compared with your same-direction route match on {date}: " + '; '.join(differences) + '. '
            'These are estimated start-hour conditions, not whole-run measurements. '
            'Weather may affect comfort, but this comparison does not establish its effect on performance. '
            'Wind speed alone does not indicate headwind exposure.')


def prepare_draft(activity_id=None, *, token=None, activity=None):
    token = token or access_token()
    if activity is None:
        selected = get_activity(token, activity_id)
        activity = fetch_json(f"https://www.strava.com/api/v3/activities/{selected['id']}", token=token)
    url, start = weather_request(activity)
    weather_data = fetch_json(url)
    weather_text = format_weather_description(weather_data, start)
    report, match = select_match(token, activity)
    if match:
        other_url, other_start = weather_request(match)
        try:
            _, previous_weather = weather_sample(fetch_json(other_url), other_start)
            _, current_weather = weather_sample(weather_data, start)
            weather_text += '\n\n' + comparison_text(current_weather, previous_weather, match)
        except RuntimeError as error:
            report['weather_warning'] = str(error)
            weather_text += '\n\nA route match was found, but its weather could not be retrieved; showing current weather only.'
    else:
        weather_text += '\n\n' + report['reason'] + '.'
    original = activity.get("description") or ""
    return {
        "activity_id": activity["id"],
        "athlete_id": activity["athlete"]["id"],
        "activity_name": activity["name"],
        "original_description": original,
        "proposed_description": merge_description(original, weather_text),
        "route_selection": report,
    }


def apply_draft(draft, *, token=None, scopes=None):
    # Validate the destination and content before any write.
    activity_id = draft["activity_id"]
    if type(activity_id) is not int or activity_id <= 0:
        raise RuntimeError("Invalid activity ID in draft.")
    if not all(isinstance(draft.get(k), str) for k in ("original_description", "proposed_description")):
        raise RuntimeError("Invalid description in draft.")
    token = token or access_token()
    if scopes is None:
        stored = json.loads((ROOT / "tokens.json").read_text())
        scopes = stored.get("scope", [])
    if isinstance(scopes, str):
        scopes = scopes.replace(",", " ").split()
    if "activity:write" not in scopes:
        raise RuntimeError("Reconnect with write access first: .venv/bin/python connect_strava.py --write")
    url = f"https://www.strava.com/api/v3/activities/{activity_id}"
    current = fetch_json(url, token=token)
    if current["athlete"]["id"] != draft["athlete_id"]:
        raise RuntimeError("The draft belongs to another athlete. Prepare a new preview.")
    description = current.get("description") or ""
    if description == draft["proposed_description"]:
        return "This exact recap is already saved. No update needed."
    if description != draft["original_description"]:
        raise RuntimeError("The description changed since preview. Prepare a new draft to preserve your edits.")
    request = Request(url, method="PUT", data=json.dumps({"description": draft["proposed_description"]}).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            updated = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Strava update returned HTTP {error.code}. Check the activity before retrying.") from None
    except (URLError, TimeoutError):
        raise RuntimeError("Update result is unknown after a connection error. Check Strava before retrying.") from None
    if updated.get("description") != draft["proposed_description"]:
        raise RuntimeError("Strava responded, but the saved description could not be verified. Check the activity.")
    return "Description saved and verified."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activity-id", type=int, help="Activity to preview; defaults to latest suitable outdoor activity")
    parser.add_argument("--draft", type=Path, default=DEFAULT_DRAFT)
    parser.add_argument("--apply", action="store_true", help="Publish the exact saved weather description draft")
    args = parser.parse_args()
    if args.apply:
        if args.activity_id is not None:
            parser.error("--apply uses the saved draft's activity; omit --activity-id")
        draft = json.loads(args.draft.read_text())
        print(apply_draft(draft))
    else:
        draft = prepare_draft(args.activity_id)
        save_draft(args.draft, draft)
        print(f"Activity: {draft['activity_name']}\nhttps://www.strava.com/activities/{draft['activity_id']}")
        print("\nProposed description:\n\n" + draft["proposed_description"])
        print("\nRoute selection: " + draft['route_selection']['reason'])
        if draft['route_selection']['selected_id']:
            print("Comparison activity: " + str(draft['route_selection']['selected_id']))
        print(f"\nPreview only. Draft and original description saved to {args.draft}.")
        print("After reviewing, rerun with --apply (and the same --draft path if customized).")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(str(error))
