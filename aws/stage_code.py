"""Stage an explicit allowlist so local tokens, .env and drafts cannot be uploaded."""
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
FILES = ('connect_strava.py', 'preview_weather.py', 'route_matches.py', 'sync_description.py')


def main():
    destination = HERE / 'build'
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    for name in FILES:
        shutil.copy2(HERE.parent / name, destination / name)
    for name in ('webhook.py', 'worker.py', 'requirements.txt'):
        shutil.copy2(HERE / name, destination / name)
    print('Staged only: ' + ', '.join(sorted(p.name for p in destination.iterdir())))


if __name__ == '__main__':
    main()
