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
Moving and elapsed durations remain separate. Units are included in field names;
missing measurements are `null`, not zero. Both output modes use the same hourly sample.
The payload omits coordinates, account IDs, tokens, titles, and descriptions.
No LLM is called yet. Use `--activity-id` with `--json` to select a workout.

Status: connection and weather preview implemented; description updates and deployment are next.
