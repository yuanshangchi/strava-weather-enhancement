# Strava Weather

Personal project to add estimated weather conditions to Strava activity descriptions.

## Setup

Python 3.13. A local virtual environment has been created in `.venv`.
To recreate it on another machine, run `python3 -m venv .venv`.

Activate it from this directory:

```sh
source .venv/bin/activate
```

## First milestone

Connect to Strava, fetch one outdoor activity, and preview weather near its start location and time. Writing descriptions and deploying webhook automation come afterward.

## Register the app

Visit https://www.strava.com/settings/api and create an application.
Use `localhost` as the Authorization Callback Domain for local development.
Strava currently requires a subscription to create an API application.

Keep client secrets and tokens out of source control. Local `.env` and `tokens.json` files are ignored.

## Connect your account

Store `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env` as `KEY=value` lines.
This machine's credentials have already been saved locally with owner-only access.

```sh
.venv/bin/python connect_strava.py
```

Authorize in your browser. The script listens on loopback port 8000 for up to
10 minutes and saves the returned tokens to owner-only `tokens.json`.
It requests activity read access, including Only You activities, and does not
request write access or change any activities. We'll add writing later.

## Preview weather

```sh
.venv/bin/python preview_weather.py
```

Reads your latest outdoor activity with GPS, then sends its start coordinates and
date to Open-Meteo to retrieve weather. No Strava activities are modified.
Expired Strava tokens are refreshed and saved automatically.
Use `--activity-id 123456789` to select a specific activity.

The preview uses the UTC hour containing the start time and Celsius/km/h units.
Activities up to five calendar days old use recent forecast data; older ones use
historical reanalysis. Weather is a modeled estimate at the start location, not
a measurement along the whole route. UV is unavailable in the historical path.

Run offline checks with `.venv/bin/python -m unittest -v`.

## Compact facts for a recap

```sh
.venv/bin/python preview_weather.py --json
```

This prints a JSON object with `activity` and `weather` sections. It calculates
distance in kilometers and pace in seconds per kilometer using moving time.
Moving and elapsed durations remain separate, formatted as total minutes and seconds
(e.g. `87 minutes and 7 seconds`), including durations longer than an hour.
Pace is still calculated from raw seconds. Units are included in other field names;
missing measurements are `null`, not zero. Both output modes use the same hourly sample.
The payload omits coordinates, account IDs, tokens, titles, and descriptions.
No LLM is called yet. Use `--activity-id` with `--json` to select a workout.

## Gemini recap demo

Add `GEMINI_API_KEY=...` to `.env` (lowercase `gemini_api_key` is also accepted).
No additional Python packages are needed; the script uses Gemini's REST API.

```sh
.venv/bin/python generate_recap.py
```

This sends only `sample_recap_input.json`, a synthetic workout, to Gemini.
The default model is `gemini-3.1-flash-lite`, listed as free-tier eligible; actual
access and quota depend on your AI Studio project. It does not change billing.
Use `--model NAME` to choose another model. `--input FILE` sends the selected JSON
file instead; this is an explicit data upload to Google, so inspect its contents first.
Free-tier content may be used to improve Google's products.

The script requests structured JSON with `recap` and `caveats`, rejects blocked,
truncated, or malformed output, and prints latency/token usage to stderr.
Output validation checks shape and size, not factual accuracy; review recaps.
Previously saved JSON with `moving_time_seconds` and `elapsed_time_seconds` is
converted automatically before requesting a recap; the input file is left unchanged.
Quota errors stop with guidance, without automatic retries. No Strava updates occur.

References: https://ai.google.dev/gemini-api/docs/pricing and
https://ai.google.dev/gemini-api/docs/generate-content/structured-output

## Preview and save a Strava description

### Recent route selection

```sh
.venv/bin/python route_matches.py --activity-id 19917296168
```

Searches the 90 days before that activity, paginating through all activity summaries.
Reports each outdoor run's match status and selects the most recent suitable
same-direction run. No route database, LLM call, or Strava write is involved.

Initial matching thresholds (heuristics to verify against your routes):
- Distance within 10% of the target, endpoints within 200 m.
- At least 90% of each path within 50 m of the other, using decoded summary
  polylines and approximately 25 m samples (capped at 801 samples per path).
- Direction from the average separation of 101 ordered, distance-normalized
  positions, compared forward and backward. The winning error must be at most
  150 m, at least 50 m smaller, and under 80% of the other direction's error.
- Opposite direction and uncertain direction are shown but never auto-selected.
- Known workout types must agree (untagged and generic runs are grouped together).
  Candidates with over 10 minutes total nonmoving time are conservatively excluded;
  this does not imply one long pause or a restroom stop.

Summary GPS can be simplified, hidden, or missing. Small loops, retraced paths,
different loop start points, and out-and-back routes can be marked uncertain.
Missing target geometry produces an explicit unavailable result. API errors stop
the search rather than falsely reporting no match. A route that was last run more
than 90 days ago is not distinguished from a new route; the search never widens.
Route similarity does not prove equal effort or conditions. Direction matching
does not calculate headwind exposure.

```sh
.venv/bin/python sync_description.py
```

This selects your latest suitable outdoor activity, fetches its full description
and weather, and formats a weather-only block without calling Gemini. It prints a proposed
description and saves it alongside the original in owner-only `description_draft.json`.
It preserves text outside its `[Workout Weather Recap]` block and replaces that
block on subsequent previews, including previously saved AI recaps. The existing
markers are retained so older blocks can be replaced without duplication.
Unavailable weather measurements are omitted. Use `--activity-id ID` to choose another activity.

The preview now searches for a recent route match. For a selected match it appends
start-hour weather differences, without claiming that weather caused a performance
change. Without a match it retains current weather and states that no comparable
same-direction run was found in the prior 90 days (or that GPS is unavailable).
Historical-weather failures are explicitly labeled instead of choosing another
run for its weather. Match diagnostics are included in the saved draft.
Use a separate draft while testing, e.g. `--draft route_preview.json`, to keep your
previous draft intact. Running with no activity ID may select a non-running outdoor
activity; those get weather only, with a message that route matching supports runs.

After reviewing the draft, authorize write access once, then save it:

```sh
.venv/bin/python connect_strava.py --write
.venv/bin/python sync_description.py --apply
```

Applying uses the exact saved draft without regenerating the recap. It checks the
current description for intervening edits, changes only `description`, and checks
the returned description. The draft retains the original text for manual recovery.
There is still a small race if another edit happens between the final read and PUT;
avoid editing the activity while applying. Previewing again replaces the draft, so
copy it first if you want to keep an older backup. Custom `--draft PATH` files should
also be kept out of source control.

Status: weather-only description updates implemented; the standalone Gemini recap
script remains available for LLM experiments. Deployment is next.

## AWS webhook (prepared, not deployed)

See [aws/README.md](aws/README.md) for the Lambda receiver, FIFO worker,
cloud credential migration, deployment, and Strava subscription steps.
Publishing defaults to disabled. Local preview/apply commands still work.
