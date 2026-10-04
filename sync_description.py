"""Prepare a Strava description draft; --apply publishes that exact saved draft."""

import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from connect_strava import ROOT
from generate_recap import generate_recap, load_key, validate_result
from preview_weather import access_token, build_recap_input, fetch_json, get_activity, weather_request

BEGIN = "[Workout Weather Recap]"
END = "[/Workout Weather Recap]"
DEFAULT_DRAFT = ROOT / "description_draft.json"


def merge_description(original, recap):
    validate_result(recap)
    text = recap["recap"] + "\n" + "\n".join(recap["caveats"])
    if BEGIN in text or END in text:
        raise RuntimeError("Generated text contains reserved markers. Generate a new preview.")
    block = f"{BEGIN}\nAI-generated recap\n{text}\nWeather source: Open-Meteo\n{END}"
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


def prepare_draft(activity_id=None):
    token = access_token()
    selected = get_activity(token, activity_id)
    activity = fetch_json(f"https://www.strava.com/api/v3/activities/{selected['id']}", token=token)
    url, start = weather_request(activity)
    source = "historical_reanalysis" if urlsplit(url).hostname == "archive-api.open-meteo.com" else "recent_forecast"
    facts = build_recap_input(activity, fetch_json(url), start, weather_source=source)
    recap = generate_recap(facts, load_key(), "gemini-3.1-flash-lite")
    original = activity.get("description") or ""
    return {
        "activity_id": activity["id"],
        "athlete_id": activity["athlete"]["id"],
        "activity_name": activity["name"],
        "original_description": original,
        "proposed_description": merge_description(original, recap),
    }


def apply_draft(draft):
    # Validate the destination and content before any write.
    activity_id = draft["activity_id"]
    if type(activity_id) is not int or activity_id <= 0:
        raise RuntimeError("Invalid activity ID in draft.")
    if not all(isinstance(draft.get(k), str) for k in ("original_description", "proposed_description")):
        raise RuntimeError("Invalid description in draft.")
    token = access_token()
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
    parser.add_argument("--apply", action="store_true", help="Publish the exact saved draft without calling Gemini again")
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
        print(f"\nPreview only. Draft and original description saved to {args.draft}.")
        print("After reviewing, rerun with --apply (and the same --draft path if customized).")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(str(error))
