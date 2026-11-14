"""Build app/static/regions.geojson (admin-1 regions of Central Asia).

Source: Natural Earth 1:10m admin-1 states/provinces (public domain).
Note: Natural Earth predates the 2022 Kazakhstan reform (Abai, Jetisu,
Ulytau regions are still inside East Kazakhstan, Almaty and Karaganda).
"""
import json
from pathlib import Path

import requests
from shapely.geometry import mapping, shape

URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
       "geojson/ne_10m_admin_1_states_provinces.geojson")
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "raw" / "ne_10m_admin_1_states_provinces.geojson"
OUT = ROOT / "app" / "static" / "regions.geojson"
KEEP = {"KAZ", "KGZ", "UZB", "TJK", "TKM"}
RENAME = {"Nur-Sultan": "Astana (city)"}


def main() -> None:
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(requests.get(URL, timeout=300).content)
    src = json.loads(CACHE.read_text(encoding="utf-8"))
    feats = []
    for f in src["features"]:
        p = f["properties"]
        if p.get("adm0_a3") not in KEEP:
            continue
        geom = shape(f["geometry"])
        name = p.get("name_en") or p["name"]
        name = RENAME.get(name, name)
        if p.get("type_en") == "City" and "(city)" not in name:
            name = f"{name} (city)"
        feats.append({
            "type": "Feature",
            "properties": {"name": name, "country": p["admin"], "iso": p["adm0_a3"]},
            "geometry": mapping(geom.simplify(0.02, preserve_topology=True)),
        })
    feats.sort(key=lambda f: (f["properties"]["iso"] != "KAZ", f["properties"]["country"], f["properties"]["name"]))
    for i, f in enumerate(feats):
        f["properties"]["id"] = i
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
    print(f"{len(feats)} regions -> {OUT}")


if __name__ == "__main__":
    main()
