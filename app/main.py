"""Harmonized MODIS-VIIRS burn calendar: FastAPI backend.

Area of interest can be given as one of
    region=<id>                       admin-1 region id (see /api/regions)
    country=<ISO3>                    whole country (KAZ, KGZ, UZB, TJK, TKM)
    bbox=west,south,east,north
    polygon=lon lat,lon lat,...       closed or open ring
    lat=..&lon=..&radius_km=..        circle
Date range: start=YYYY-MM, end=YYYY-MM (inclusive, monthly resolution).

Calendar/anomaly/stat numbers come from the harmonized 0.25 deg grid; each
cell contributes in proportion to the share of its area inside the polygon.
Points come from the filtered hotspot table (all sensors).
"""
from __future__ import annotations

import io
import json
import math
from functools import lru_cache
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import shapely
import shapely.affinity
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "processed"
STATIC = Path(__file__).parent / "static"
CELL, LON0, LAT0 = 0.25, 46.0, 35.0
MAX_POINTS = 25_000

app = FastAPI(title="Harmonized MODIS-VIIRS Burn Calendar", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ---------------------------------------------------------------------------
# data loaded once
# ---------------------------------------------------------------------------
HARM = pd.read_parquet(DATA / "harmonized.parquet",
                       columns=["row", "col", "year", "month", "count", "source"])
HARM["count"] = HARM["count"].astype("float64")
HARM["source"] = HARM["source"].astype("category")
HARM[["row", "col", "year", "month"]] = HARM[["row", "col", "year", "month"]].astype("int16")
METRICS = json.loads((DATA / "metrics.json").read_text())
LAST = pd.Timestamp(METRICS["last_date"])
FIRST_YEAR = int(HARM.year.min())
POINTS = str(DATA / "points.parquet")
REGIONS = json.loads((STATIC / "regions.geojson").read_text())
REGION_GEOMS = {f["properties"]["id"]: shape(f["geometry"]) for f in REGIONS["features"]}
COUNTRIES = {}
for f in REGIONS["features"]:
    COUNTRIES.setdefault(f["properties"]["iso"], []).append(REGION_GEOMS[f["properties"]["id"]])
COUNTRIES = {k: unary_union(v) for k, v in COUNTRIES.items()}
COUNTRY_NAMES = {f["properties"]["iso"]: f["properties"]["country"] for f in REGIONS["features"]}

GRID = pd.read_parquet(DATA / "grid_monthly.parquet").astype("int32")
GRID["ym"] = GRID["year"] * 12 + GRID["month"] - 1
STREAMS = {"modis": "MODIS", "viirs": "VIIRS_SNPP", "noaa20": "VIIRS_NOAA20", "noaa21": "VIIRS_NOAA21"}
CELLS = GRID[["row", "col"]].drop_duplicates().reset_index(drop=True)
CELL_POLYS = shapely.box(LON0 + CELL * CELLS.col.values, LAT0 + CELL * CELLS.row.values,
                         LON0 + CELL * (CELLS.col.values + 1), LAT0 + CELL * (CELLS.row.values + 1))
shapely.prepare(CELL_POLYS)
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _con():
    con = duckdb.connect()
    con.execute("SET threads TO 2")
    con.execute("SET memory_limit = '160MB'")
    return con


# ---------------------------------------------------------------------------
# area + time parsing
# ---------------------------------------------------------------------------
def parse_area(request: Request):
    q = request.query_params
    try:
        if q.get("region"):
            return REGION_GEOMS[int(q["region"])], _region_label(int(q["region"]))
        if q.get("country"):
            iso = q["country"].upper()
            return COUNTRIES[iso], COUNTRY_NAMES[iso]
        if q.get("bbox"):
            w, s, e, n = map(float, q["bbox"].split(","))
            return box(w, s, e, n), f"bbox {w:.2f},{s:.2f},{e:.2f},{n:.2f}"
        if q.get("polygon"):
            pts = [tuple(map(float, p.split())) for p in q["polygon"].split(",")]
            poly = Polygon(pts).buffer(0)
            if poly.is_empty:
                raise ValueError("empty polygon")
            return poly, "drawn polygon"
        if q.get("lat") and q.get("lon"):
            lat, lon = float(q["lat"]), float(q["lon"])
            r = float(q.get("radius_km", 25))
            # circle in degrees, corrected for longitude convergence
            circ = Point(0, 0).buffer(1, 64)
            circ = shapely.affinity.scale(circ, r / (111.32 * math.cos(math.radians(lat))), r / 110.57, origin=(0, 0))
            return shapely.affinity.translate(circ, lon, lat), f"{r:g} km around {lat:.3f}, {lon:.3f}"
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, f"bad area parameter: {exc}") from exc
    return COUNTRIES["KAZ"], "Kazakhstan"


def _region_label(i):
    p = REGIONS["features"][i]["properties"]
    return f"{p['name']}, {p['country']}"


def parse_period(request: Request):
    q = request.query_params
    try:
        start = pd.Period(q.get("start") or f"{FIRST_YEAR}-01", "M")
        end = pd.Period(q.get("end") or LAST.strftime("%Y-%m"), "M")
    except ValueError as exc:
        raise HTTPException(400, f"bad date: {exc}") from exc
    if end < start:
        start, end = end, start
    return start, end


@lru_cache(maxsize=256)
def _weights(wkb: bytes) -> pd.DataFrame:
    geom = shapely.from_wkb(wkb)
    hit = shapely.intersects(CELL_POLYS, geom)
    idx = np.flatnonzero(hit)
    frac = shapely.area(shapely.intersection(CELL_POLYS[idx], geom)) / (CELL * CELL)
    return pd.DataFrame({"row": CELLS.row.values[idx], "col": CELLS.col.values[idx], "w": frac})


def monthly_series(geom) -> pd.DataFrame:
    """Area-weighted harmonized counts for every month FIRST_YEAR..LAST."""
    w = _weights(shapely.to_wkb(geom))
    d = HARM.merge(w, on=["row", "col"])
    d["v"] = d["count"] * d["w"]
    s = d.groupby(["year", "month"])["v"].sum()
    idx = pd.period_range(f"{FIRST_YEAR}-01", LAST.strftime("%Y-%m"), freq="M")
    out = pd.DataFrame({"period": idx, "year": idx.year, "month": idx.month})
    out["value"] = s.reindex(pd.MultiIndex.from_arrays([out.year, out.month])).fillna(0).values
    src = HARM.drop_duplicates(["year", "month"]).set_index(["year", "month"])["source"]
    out["source"] = src.reindex(pd.MultiIndex.from_arrays([out.year, out.month])).values
    default = pd.Series(np.where(out.year < 2012, "MODIS (harmonized)", "VIIRS S-NPP"), index=out.index)
    out["source"] = out["source"].fillna(default)
    # the current month is incomplete
    out["partial"] = (out.period == pd.Period(LAST, "M")) & (LAST.day < LAST.days_in_month)
    return out


def climatology(series: pd.DataFrame) -> pd.DataFrame:
    """Per calendar month: median, mean, std, quartiles over complete years."""
    base = series[~series.partial & (series.year < LAST.year)]
    g = base.groupby("month")["value"]
    return pd.DataFrame({"median": g.median(), "mean": g.mean(), "std": g.std(ddof=1),
                         "q1": g.quantile(0.25), "q3": g.quantile(0.75)})


def flag(value, c):
    iqr = c["q3"] - c["q1"]
    z = (value - c["mean"]) / c["std"] if c["std"] > 0 else 0.0
    if value > c["q3"] + 1.5 * iqr or z >= 2:
        return z, "high"
    if value < c["q1"] - 1.5 * iqr or z <= -2:
        return z, "low"
    if z >= 1:
        return z, "above normal"
    if z <= -1:
        return z, "below normal"
    return z, "normal"


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {"first_year": FIRST_YEAR, "last_date": str(LAST.date()), "grid_deg": CELL,
            "best_model": METRICS["best_model"], "cv": METRICS["results"],
            "train_years": METRICS["train_years"], "test_years": METRICS["test_years"]}


@app.get("/api/regions")
def regions():
    return JSONResponse(REGIONS)


@app.get("/api/calendar")
def calendar(request: Request):
    geom, label = parse_area(request)
    start, end = parse_period(request)
    s = monthly_series(geom)
    s = s[(s.period >= start) & (s.period <= end)]
    years = sorted(s.year.unique().tolist())
    z = [[None] * 12 for _ in years]
    for r in s.itertuples():
        z[years.index(r.year)][r.month - 1] = round(r.value, 1)
    src = s.groupby("year")["source"].agg(lambda x: " / ".join(dict.fromkeys(x))).to_dict()
    return {"area": label, "years": years, "months": MONTH_NAMES, "z": z,
            "source_by_year": {int(k): v for k, v in src.items()},
            "unit": "VIIRS S-NPP-equivalent fire detections per month",
            "partial_month": str(pd.Period(LAST, "M")) if s.partial.any() else None}


@app.get("/api/anomalies")
def anomalies(request: Request, month: str | None = Query(None, description="YYYY-MM to highlight")):
    geom, label = parse_area(request)
    start, end = parse_period(request)
    s = monthly_series(geom)
    clim = climatology(s)
    rows = []
    for r in s[(s.period >= start) & (s.period <= end)].itertuples():
        c = clim.loc[r.month]
        z, f = flag(r.value, c)
        rows.append({"period": str(r.period), "year": r.year, "month": r.month, "value": round(r.value, 1),
                     "median": round(c["median"], 1), "mean": round(c["mean"], 1),
                     "q1": round(c["q1"], 1), "q3": round(c["q3"], 1),
                     "z": round(float(z), 2), "flag": f, "partial": bool(r.partial)})
    # annual totals (complete years only for the climatology)
    ann = s.groupby("year").agg(value=("value", "sum"), partial=("partial", "any")).reset_index()
    full = ann[ann.year < LAST.year]
    am, asd = full.value.mean(), full.value.std(ddof=1)
    aq1, aq3, amed = full.value.quantile(.25), full.value.quantile(.75), full.value.median()
    years = []
    for r in ann[(ann.year >= start.year) & (ann.year <= end.year)].itertuples():
        z, f = flag(r.value, {"mean": am, "std": asd, "q1": aq1, "q3": aq3})
        years.append({"year": r.year, "value": round(r.value, 1), "z": round(float(z), 2), "flag": f,
                      "partial": bool(r.year == LAST.year)})
    focus = month or str(pd.Period(LAST, "M") - (1 if LAST.day < 20 else 0))
    sel = next((x for x in rows if x["period"] == focus), None)
    if sel is None:
        fp = pd.Period(focus, "M")
        hit = s[s.period == fp]
        if len(hit):
            r = hit.iloc[0]
            c = clim.loc[r.month]
            z, f = flag(r.value, c)
            sel = {"period": focus, "value": round(r.value, 1), "median": round(c["median"], 1),
                   "mean": round(c["mean"], 1), "q1": round(c["q1"], 1), "q3": round(c["q3"], 1),
                   "z": round(float(z), 2), "flag": f, "partial": bool(r.partial)}
    return {"area": label, "months": rows, "years": years, "selected": sel,
            "climatology": {int(m): {k: round(float(v), 1) for k, v in c.items()} for m, c in clim.iterrows()},
            "baseline": f"{FIRST_YEAR}-{LAST.year - 1}",
            "rule": "high/low: outside Q1-1.5*IQR..Q3+1.5*IQR or |z| >= 2; above/below normal: |z| >= 1"}


def theil_sen(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    i, j = np.triu_indices(len(x), 1)
    slopes = (y[j] - y[i]) / (x[j] - x[i])
    slope = float(np.median(slopes))
    s = np.sign(y[j] - y[i]).sum()
    n = len(x)
    var = n * (n - 1) * (2 * n + 5) / 18
    zmk = (s - np.sign(s)) / math.sqrt(var) if var > 0 else 0
    p = math.erfc(abs(zmk) / math.sqrt(2))
    return slope, float(np.median(y - slope * x)), p


@app.get("/api/stats")
def stats(request: Request):
    geom, label = parse_area(request)
    start, end = parse_period(request)
    s = monthly_series(geom)
    sel = s[(s.period >= start) & (s.period <= end)]
    ann = sel.groupby("year")["value"].sum()
    complete = ann[ann.index < LAST.year] if len(ann[ann.index < LAST.year]) >= 3 else ann
    trend = None
    if len(complete) >= 3:
        slope, icpt, p = theil_sen(complete.index, complete.values)
        mean = complete.mean()
        trend = {"slope_per_year": round(slope, 1), "pct_per_year": round(100 * slope / mean, 2) if mean else None,
                 "p_value": round(p, 4), "years": [int(complete.index.min()), int(complete.index.max())],
                 "method": "Theil-Sen slope, Mann-Kendall test on annual totals",
                 "line": [[int(y), round(icpt + slope * y, 1)] for y in (complete.index.min(), complete.index.max())]}
    by_month = sel.groupby("month")["value"].mean()
    raw = raw_counts(geom, start, end)
    top_m = sel.nlargest(5, "value")
    return {
        "area": label, "period": [str(start), str(end)],
        "harmonized_total": round(float(sel.value.sum()), 0),
        "raw_detections": raw,
        "top_months": [{"period": str(r.period), "value": round(r.value, 1)} for r in top_m.itertuples()],
        "top_years": [{"year": int(y), "value": round(v, 1)} for y, v in ann.nlargest(5).items()],
        "peak_calendar_month": MONTH_NAMES[int(by_month.idxmax()) - 1] if by_month.sum() > 0 else None,
        "mean_by_calendar_month": {MONTH_NAMES[m - 1]: round(v, 1) for m, v in by_month.items()},
        "annual": [{"year": int(y), "value": round(v, 1), "partial": bool(y == LAST.year)} for y, v in ann.items()],
        "trend": trend,
    }


def _cells(geom):
    w = _weights(shapely.to_wkb(geom))
    return w.assign(interior=w.w > 0.9999)


def _ym(period):
    return period.year * 12 + period.month - 1


def _points_sql(columns, sample=None):
    samp = f" USING SAMPLE reservoir({int(sample)} ROWS) REPEATABLE (42)" if sample else ""
    return (f"SELECT {columns} FROM read_parquet(?) p JOIN cells c "
            f"ON CAST(floor((p.lat - {LAT0}) / {CELL}) AS INTEGER) = c.row "
            f"AND CAST(floor((p.lon - {LON0}) / {CELL}) AS INTEGER) = c.col "
            f"WHERE p.date BETWEEN ? AND ? {{extra}}{samp}")


def area_points(geom, start, end, columns="p.lat, p.lon, p.date, p.time, p.sensor, p.satellite, "
                "p.confidence, p.frp, p.daynight", sample=None, boundary_only=False):
    """Detections inside geom between the first day of start and last day of end."""
    cells = _cells(geom)
    if boundary_only:
        cells = cells[~cells.interior]
    con = _con()
    con.register("cells", cells[["row", "col", "interior"]])
    sql = _points_sql(columns + ", c.interior", sample).format(extra="")
    df = con.execute(sql, [POINTS, start.start_time.date(), end.end_time.date()]).df()
    if df.empty:
        return df
    keep = df["interior"].values | shapely.contains_xy(geom, df.lon.values, df.lat.values)
    return df[keep].drop(columns="interior")


def raw_counts(geom, start, end):
    """Exact detection counts by sensor: grid for interior cells, points for boundary cells."""
    cells = _cells(geom)
    inner = cells[cells.interior][["row", "col"]]
    g = GRID[(GRID.ym >= _ym(start)) & (GRID.ym <= _ym(end))].merge(inner, on=["row", "col"])
    counts = {name: int(g[col].sum()) for col, name in STREAMS.items()}
    b = area_points(geom, start, end, "p.lat, p.lon, p.sensor", boundary_only=True)
    if len(b):
        for k, v in b.groupby("sensor", observed=True).size().items():
            counts[str(k)] = counts.get(str(k), 0) + int(v)
    return {k: v for k, v in counts.items() if v}


@app.get("/api/points")
def points(request: Request, limit: int = MAX_POINTS):
    geom, label = parse_area(request)
    start, end = parse_period(request)
    limit = max(1, min(limit, MAX_POINTS))
    total = sum(raw_counts(geom, start, end).values())
    sample = int(limit * 1.15) + 50 if total > limit else None
    cols = "p.lat, p.lon, p.date, p.sensor, p.frp"
    df = area_points(geom, start, end, cols, sample=sample)
    if sample and len(df) < min(limit, total) * 0.9:  # area much smaller than its cells
        df = area_points(geom, start, end, cols)
    if len(df) > limit:
        df = df.sample(limit, random_state=0)
    return {"area": label, "total": total, "returned": len(df), "sampled": total > len(df),
            "lat": df.lat.astype(float).round(4).tolist(), "lon": df.lon.astype(float).round(4).tolist(),
            "date": df.date.astype(str).tolist(), "sensor": df.sensor.astype(str).tolist(),
            "frp": df.frp.astype(float).round(1).tolist()}


def _stream_points(geom, start, end, header):
    """CSV of all detections in the area, streamed in batches (bounded memory)."""
    cells = _cells(geom)
    con = _con()
    con.register("cells", cells[["row", "col", "interior"]])
    cols = "p.lat, p.lon, p.date, p.time, p.sensor, p.satellite, p.confidence, p.frp, p.daynight, c.interior"
    sql = _points_sql(cols).format(extra="") + " ORDER BY p.date"
    res = con.execute(sql, [POINTS, start.start_time.date(), end.end_time.date()])
    yield header
    first = True
    while True:
        df = res.fetch_df_chunk(50)  # 50 vectors of 2048 rows
        if df.empty:
            break
        keep = df["interior"].values | shapely.contains_xy(geom, df.lon.values, df.lat.values)
        df = df[keep].drop(columns="interior")
        df["lat"], df["lon"] = df.lat.astype(float).round(5), df.lon.astype(float).round(5)
        df["frp"] = df.frp.astype(float).round(2)
        yield df.to_csv(index=False, header=first)
        first = False
    if first:
        yield "lat,lon,date,time,sensor,satellite,confidence,frp,daynight\n"


@app.get("/api/export.csv")
def export_csv(request: Request, kind: str = Query("points", pattern="^(points|aggregates|anomalies)$")):
    geom, label = parse_area(request)
    start, end = parse_period(request)
    tag = f"{start}_{end}"
    header = f"# area: {label}\n# product: harmonized MODIS-VIIRS burn calendar (NASA FIRMS)\n"
    if kind == "points":
        return StreamingResponse(_stream_points(geom, start, end, header), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="fires_points_{tag}.csv"'})
    if kind == "aggregates":
        s = monthly_series(geom)
        df = s[(s.period >= start) & (s.period <= end)][["period", "year", "month", "value", "source", "partial"]]
        df = df.rename(columns={"value": "harmonized_fire_count"})
        df["period"] = df["period"].astype(str)
    else:
        df = pd.DataFrame(anomalies(request, month=None)["months"])
    buf = io.StringIO()
    buf.write(header)
    df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="fires_{kind}_{tag}.csv"'})


@app.get("/healthz")
def health():
    return {"ok": True}
