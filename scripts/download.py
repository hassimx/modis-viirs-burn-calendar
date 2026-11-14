"""Reproducible download of NASA FIRMS active-fire data for Central Asia.

1. Yearly country archives (https://firms.modaps.eosdis.nasa.gov/country/):
   MODIS C6.1 (Terra+Aqua) 2000-2025, VIIRS S-NPP 375 m 2012-2025,
   VIIRS NOAA-20 375 m 2018-2025.
2. Recent detections (after the end of the archives, up to today) from the
   FIRMS area API, in 5-day windows, with a pause between calls and an on-disk
   cache so re-runs only fetch what is missing. Needs FIRMS_MAP_KEY.

Usage:
    python scripts/download.py                # archives + API
    python scripts/download.py --skip-api     # archives only
    python scripts/download.py --countries Kazakhstan
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

BASE = "https://firms.modaps.eosdis.nasa.gov"
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

COUNTRIES = ["Kazakhstan", "Kyrgyzstan", "Uzbekistan", "Tajikistan", "Turkmenistan"]
# west, south, east, north - covers all five countries
BBOX = (46.0, 35.0, 88.0, 56.0)

ARCHIVES = {
    # FIRMS folder name: (first year, last year)
    "modis": (2000, 2025),
    "viirs-snpp": (2012, 2025),
    "viirs-jpss1": (2018, 2025),
}

# API sources: standard-processing (SP) first, then near-real-time (NRT)
API_SOURCES = {
    "modis": ["MODIS_SP", "MODIS_NRT"],
    "viirs-snpp": ["VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"],
    "viirs-jpss1": ["VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT"],
    "viirs-jpss2": ["VIIRS_NOAA21_NRT"],
}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "burn-calendar-downloader/1.0"


def fetch(url: str, retries: int = 6, timeout: int = 300) -> bytes | None:
    """GET with exponential backoff. Returns None on 404."""
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:  # network hiccup / 5xx
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{retries} in {wait}s: {exc}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"failed after {retries} attempts: {url}")


def download_archives(countries: list[str]) -> None:
    for sensor, (y0, y1) in ARCHIVES.items():
        for year in range(y0, y1 + 1):
            for country in countries:
                out = RAW / "country" / sensor / f"{sensor}_{year}_{country}.csv"
                if out.exists() and out.stat().st_size > 0:
                    continue
                url = f"{BASE}/data/country/{sensor}/{year}/{sensor}_{year}_{country}.csv"
                data = fetch(url)
                out.parent.mkdir(parents=True, exist_ok=True)
                if data is None:
                    print(f"missing  {url}")
                    out.write_bytes(b"")  # remember that it does not exist
                    continue
                out.write_bytes(data)
                print(f"saved    {out.relative_to(ROOT)} ({len(data) / 1e6:.1f} MB)")


def archive_end(sensor: str) -> dt.date:
    """Last acquisition date present in the yearly archives of a sensor."""
    last = None
    for f in sorted((RAW / "country" / sensor).glob("*.csv")):
        if f.stat().st_size == 0:
            continue
        d = pd.read_csv(f, usecols=["acq_date"])["acq_date"].max()
        last = d if last is None or d > last else last
    return dt.date.fromisoformat(last) if last else dt.date(2025, 12, 31)


def download_api(key: str, days: int = 5, pause: float = 1.0) -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    bbox = ",".join(str(v) for v in BBOX)
    for sensor, sources in API_SOURCES.items():
        start = archive_end(sensor) + dt.timedelta(days=1) if sensor in ARCHIVES else dt.date(2025, 1, 1)
        cache = RAW / "api" / sensor
        cache.mkdir(parents=True, exist_ok=True)
        day = start
        while day <= today:
            out = cache / f"{sensor}_{day.isoformat()}_{days}d.csv"
            # windows that end in the last 3 days are re-fetched (NRT still filling in)
            fresh = (today - day).days > days + 3
            if not (out.exists() and fresh):
                frames = []
                for src in sources:
                    url = f"{BASE}/api/area/csv/{key}/{src}/{bbox}/{days}/{day.isoformat()}"
                    data = fetch(url)
                    time.sleep(pause)
                    if not data or data.lstrip().startswith(b"Invalid"):
                        continue
                    try:
                        df = pd.read_csv(io.BytesIO(data))
                    except pd.errors.EmptyDataError:
                        continue
                    if len(df):
                        df["source"] = src
                        frames.append(df)
                df = pd.concat(frames) if frames else pd.DataFrame()
                df.to_csv(out, index=False)
                print(f"api      {sensor} {day} .. +{days}d: {len(df)} rows")
            day += dt.timedelta(days=days)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", nargs="+", default=COUNTRIES)
    ap.add_argument("--skip-api", action="store_true")
    ap.add_argument("--skip-archives", action="store_true")
    args = ap.parse_args()
    if not args.skip_archives:
        download_archives(args.countries)
    if not args.skip_api:
        key = os.environ.get("FIRMS_MAP_KEY")
        if not key:
            sys.exit("FIRMS_MAP_KEY is not set; skipping API download")
        download_api(key)


if __name__ == "__main__":
    main()
