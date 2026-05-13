"""
Build app/static/geo/helsinki_neighbourhoods.geojson from the HRI
Kaupunginosajako WFS layer.

Joins each HRI feature to our `neighbourhood.id` via the HRI `tunnus`
property, and injects `id` + `name_fi` onto the feature's properties
(everything else from HRI is preserved so future debugging is easier).

Source data: Helsinki Region Infoshare ("Helsingin kaupunginosajako"),
licence CC BY 4.0. Attribution required when redistributing.

Run once after fetching the HRI dump to data/external/kaupunginosajako.geojson.
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "external" / "kaupunginosajako.geojson"
DB = ROOT / "data" / "index.db"
OUT = ROOT / "app" / "static" / "geo" / "helsinki_neighbourhoods.geojson"


def main() -> None:
    if not SRC.exists():
        sys.exit(f"missing source: {SRC} (fetch from HRI WFS first)")
    if not DB.exists():
        sys.exit(f"missing db: {DB}")

    src = json.loads(SRC.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)
    ours = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT id, name_fi FROM neighbourhood WHERE is_quarter = 0"
        )
    }

    matched = []
    unmatched = []
    for feat in src["features"]:
        props = feat.get("properties") or {}
        tunnus = props.get("tunnus")
        if tunnus not in ours:
            unmatched.append((tunnus, props.get("nimi_fi")))
            continue
        # Replace properties: keep just the bits the client needs.
        feat["properties"] = {
            "id": tunnus,
            "name_fi": ours[tunnus],
            "hri_nimi_fi": props.get("nimi_fi"),
            "hri_nimi_se": props.get("nimi_se"),
        }
        matched.append(tunnus)

    out_doc = {
        "type": "FeatureCollection",
        "metadata": {
            "source": "Helsinki Region Infoshare — Helsingin kaupunginosajako",
            "licence": "CC BY 4.0",
            "fetched_via": "https://kartta.hel.fi/ws/geoserver/avoindata/ows (WFS)",
        },
        "features": [f for f in src["features"]
                     if (f.get("properties") or {}).get("id") in matched],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out_doc, ensure_ascii=False), encoding="utf-8")

    print(f"matched: {len(matched)}  unmatched: {len(unmatched)}")
    for tunnus, name in unmatched:
        print(f"  skipped: tunnus={tunnus!r} nimi_fi={name!r}")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
