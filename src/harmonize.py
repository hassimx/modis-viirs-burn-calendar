"""MODIS -> VIIRS-equivalent harmonization of FIRMS active-fire counts.

Pipeline
--------
1. ``load_points``      read FIRMS archives + API files, filter, return one table
2. ``grid_monthly``     count detections per 0.25 deg cell x month and sensor
3. ``fit`` / ``predict`` three candidate models that map MODIS (Terra+Aqua)
                        counts to VIIRS S-NPP 375 m counts
4. ``cross_validate``   train 2012-2018, test 2019-2025, metrics per model
5. ``harmonize``        refit the best model on the full overlap and build the
                        single series: MODIS-based 2001-2011, VIIRS 2012-now

Filtering rules
---------------
* MODIS: confidence >= 30 (%)
* VIIRS: confidence in {nominal, high}
* type == 0 (presumed vegetation fire) where the field exists. Points from the
  NRT API carry no ``type``; they are dropped if they fall within ~1 km of a
  location that the archive flags as a static/industrial heat source (type 1-3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"

CELL = 0.25                 # grid resolution, degrees
LON0, LAT0 = 46.0, 35.0     # grid origin (south-west corner of the study area)
STATIC_RES = 0.01           # static-source mask resolution, degrees (~1 km)
AQUA_START = pd.Timestamp("2002-08-01")  # first full month with Aqua MODIS
GAP_THRESHOLD = 0.7       # S-NPP/NOAA-20 monthly ratio below which S-NPP is treated as a gap

SENSOR_NAMES = {
    "modis": "MODIS",
    "viirs-snpp": "VIIRS_SNPP",
    "viirs-jpss1": "VIIRS_NOAA20",
    "viirs-jpss2": "VIIRS_NOAA21",
}


# ----------------------------------------------------------------------------
# 1. points
# ----------------------------------------------------------------------------
def _read_one(f: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(f, low_memory=False)
    except pd.errors.EmptyDataError:  # window without detections
        return pd.DataFrame()


def _read(files: list[Path]) -> pd.DataFrame:
    frames = [_read_one(f) for f in files if f.stat().st_size > 0]
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _confidence_ok(df: pd.DataFrame, sensor: str) -> pd.Series:
    if sensor == "modis":
        return pd.to_numeric(df["confidence"], errors="coerce") >= 30
    conf = df["confidence"].astype(str).str.lower().str[0]
    return conf.isin(["n", "h"])


def _static_keys(lat: np.ndarray, lon: np.ndarray) -> pd.Series:
    return pd.Series(np.round(lat / STATIC_RES).astype(np.int64) * 100_000
                     + np.round(lon / STATIC_RES).astype(np.int64))


def _region_mask(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """True for points inside the five Central Asian countries."""
    import json

    import shapely
    from shapely.geometry import shape
    from shapely.ops import unary_union

    gj = json.loads((ROOT / "app" / "static" / "regions.geojson").read_text())
    poly = unary_union([shape(f["geometry"]) for f in gj["features"]]).buffer(0.02)
    return shapely.contains_xy(poly, lon, lat)


def load_points(raw: Path = RAW) -> pd.DataFrame:
    """All filtered detections, one row per hotspot."""
    out, static = [], set()
    for sensor, name in SENSOR_NAMES.items():
        arch = _read(sorted((raw / "country" / sensor).glob("*.csv")))
        api = _read(sorted((raw / "api" / sensor).glob("*.csv")))
        for origin, df in (("archive", arch), ("api", api)):
            if df.empty:
                continue
            df = df[_confidence_ok(df, sensor)].copy()
            if "type" in df.columns and df["type"].notna().any():
                bad = df["type"].fillna(0) != 0
                static.update(_static_keys(df.loc[bad, "latitude"].values,
                                           df.loc[bad, "longitude"].values))
                df = df[~bad]
            df["sensor"] = name
            df["origin"] = origin
            out.append(df)
    pts = pd.concat(out, ignore_index=True)

    # API points: no `type` field -> static-source mask + clip to countries
    is_api = pts["origin"].eq("api").values
    if is_api.any():
        lat, lon = pts["latitude"].values, pts["longitude"].values
        keys = _static_keys(lat, lon)
        near = np.zeros(len(pts), bool)
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                near |= (keys + dlat * 100_000 + dlon).isin(static).values
        inside = _region_mask(lat, lon)
        pts = pts[~is_api | (~near & inside)]

    pts = pts.assign(
        date=pd.to_datetime(pts["acq_date"]),
        satellite=pts["satellite"].astype(str),
        conf=pts["confidence"].astype(str),
    )
    # duplicates appear where SP and NRT API windows overlap the archives
    pts = pts.drop_duplicates(["sensor", "latitude", "longitude", "acq_date", "acq_time"])
    res = pd.DataFrame({
        "lat": pts["latitude"].astype("float32"),
        "lon": pts["longitude"].astype("float32"),
        "date": pts["date"].dt.date,
        "time": pts["acq_time"].astype("int16"),
        "sensor": pts["sensor"].astype("category"),
        "satellite": pts["satellite"].astype("category"),
        "confidence": pts["conf"].astype("category"),
        "frp": pts["frp"].astype("float32"),
        "daynight": pts["daynight"].astype("category"),
    })
    return res.sort_values("date").reset_index(drop=True)


# ----------------------------------------------------------------------------
# 2. grid
# ----------------------------------------------------------------------------
def cell_index(lat, lon):
    row = np.floor((np.asarray(lat) - LAT0) / CELL).astype(int)
    col = np.floor((np.asarray(lon) - LON0) / CELL).astype(int)
    return row, col


def cell_center(row, col):
    return LAT0 + (np.asarray(row) + 0.5) * CELL, LON0 + (np.asarray(col) + 0.5) * CELL


def grid_monthly(points: pd.DataFrame) -> pd.DataFrame:
    """Counts per cell x month with columns per sensor stream.

    Columns: row, col, year, month, modis (= terra + aqua), terra, aqua,
    viirs (S-NPP), noaa20, noaa21.
    """
    p = points
    row, col = cell_index(p["lat"].values, p["lon"].values)
    d = pd.to_datetime(p["date"])
    stream = np.select(
        [p["sensor"].eq("MODIS") & p["satellite"].str.startswith("T"),
         p["sensor"].eq("MODIS"),
         p["sensor"].eq("VIIRS_SNPP"),
         p["sensor"].eq("VIIRS_NOAA20"),
         p["sensor"].eq("VIIRS_NOAA21")],
        ["terra", "aqua", "viirs", "noaa20", "noaa21"], default="other")
    g = (pd.DataFrame({"row": row, "col": col, "year": d.dt.year, "month": d.dt.month,
                       "stream": stream})
         .groupby(["row", "col", "year", "month", "stream"]).size()
         .unstack("stream", fill_value=0).reset_index())
    for c in ("terra", "aqua", "viirs", "noaa20", "noaa21"):
        if c not in g:
            g[c] = 0
    g["modis"] = g["terra"] + g["aqua"]
    return g[["row", "col", "year", "month", "modis", "terra", "aqua", "viirs", "noaa20", "noaa21"]]


def terra_only_factor(g: pd.DataFrame) -> pd.Series:
    """Month-of-year factor (Terra+Aqua)/Terra estimated over 2003-2011.

    Used to bring Terra-only months (before Aqua, Aug 2002) to the
    Terra+Aqua level before harmonization.
    """
    s = g[(g.year >= 2003) & (g.year <= 2011)].groupby("month")[["modis", "terra"]].sum()
    return (s["modis"] / s["terra"]).rename("terra_factor")


def complete_overlap(g: pd.DataFrame, years: range) -> pd.DataFrame:
    """Cell x month table for given years where either sensor saw a fire."""
    d = g[g.year.isin(years)]
    return d[(d.modis > 0) | (d.viirs > 0)].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 3. models
# ----------------------------------------------------------------------------
@dataclass
class MonthlyRatio:
    """V = k_m * M, k_m = sum(V)/sum(M) for each calendar month (pooled)."""
    name: str = "monthly_ratio"
    k: pd.Series = field(default=None, repr=False)

    def fit(self, d: pd.DataFrame):
        s = d.groupby("month")[["viirs", "modis"]].sum()
        self.k = s["viirs"] / s["modis"].clip(lower=1)
        return self

    def predict(self, d: pd.DataFrame) -> np.ndarray:
        return d["modis"].values * d["month"].map(self.k).fillna(self.k.mean()).values


@dataclass
class LogLinear:
    """log1p(V) = a_m + b_m * log1p(M) (seasonal intercept and slope).

    Fitted by OLS on cell-months with M > 0; back-transformed with Duan's
    smearing estimator. Cell-months with M = 0 are predicted as 0.
    """
    name: str = "loglinear_seasonal"
    coef: dict = field(default_factory=dict, repr=False)

    def fit(self, d: pd.DataFrame):
        d = d[d.modis > 0]
        for m, dm in d.groupby("month"):
            x, y = np.log1p(dm.modis.values), np.log1p(dm.viirs.values)
            X = np.c_[np.ones_like(x), x]
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            smear = np.mean(np.exp(y - X @ beta))
            self.coef[m] = (beta[0], beta[1], smear)
        return self

    def predict(self, d: pd.DataFrame) -> np.ndarray:
        a, b, s = (d["month"].map({m: c[i] for m, c in self.coef.items()}).values for i in range(3))
        x = np.log1p(d["modis"].values)
        yhat = np.exp(a + b * x) * s - 1
        return np.where(d["modis"].values > 0, np.clip(yhat, 0, None), 0.0)


@dataclass
class CellSeasonalRatio:
    """V = k_m * r_c * M with an empirical-Bayes shrunk cell factor r_c.

    r_c = (sum_c V + alpha) / (sum_c k_m M + alpha): cells with few detections
    stay close to the regional seasonal ratio (r_c -> 1), cells with a long
    record get their own MODIS/VIIRS relationship (e.g. croplands with many
    small fires that only VIIRS resolves). alpha is chosen inside the training
    period by an inner year-split.
    """
    name: str = "cell_seasonal_ratio"
    alpha: float | None = None
    base: MonthlyRatio = field(default=None, repr=False)
    r: pd.Series = field(default=None, repr=False)

    def _fit(self, d, alpha):
        self.base = MonthlyRatio().fit(d)
        tmp = d.assign(kM=self.base.predict(d))
        s = tmp.groupby(["row", "col"])[["viirs", "kM"]].sum()
        self.r = (s["viirs"] + alpha) / (s["kM"] + alpha)
        self.alpha = alpha
        return self

    def fit(self, d: pd.DataFrame):
        if self.alpha is not None:
            return self._fit(d, self.alpha)
        years = sorted(d.year.unique())
        cut = years[len(years) * 2 // 3]
        tr, va = d[d.year < cut], d[d.year >= cut]
        best = min((mae(va.viirs, CellSeasonalRatio(alpha=a)._fit(tr, a).predict(va)), a)
                   for a in (1, 3, 10, 30, 100, 300, 1000, 3000, 10000))
        return self._fit(d, best[1])

    def predict(self, d: pd.DataFrame) -> np.ndarray:
        idx = pd.MultiIndex.from_arrays([d["row"], d["col"]])
        r = self.r.reindex(idx).fillna(1.0).values
        return self.base.predict(d) * r


MODEL_CLASSES = [MonthlyRatio, LogLinear, CellSeasonalRatio]


# ----------------------------------------------------------------------------
# 4. validation
# ----------------------------------------------------------------------------
def mae(y, yhat):
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(yhat))))


def metrics(test: pd.DataFrame, pred: np.ndarray) -> dict:
    y = test["viirs"].values
    t = test.assign(pred=pred)
    reg = t.groupby(["year", "month"])[["viirs", "pred"]].sum()
    ann = t.groupby("year")[["viirs", "pred"]].sum()
    return {
        "cell_month_MAE": mae(y, pred),
        "cell_month_RMSE": float(np.sqrt(np.mean((y - pred) ** 2))),
        "cell_month_r": float(np.corrcoef(y, pred)[0, 1]),
        "cell_month_bias_pct": float(100 * (pred.sum() - y.sum()) / y.sum()),
        "regional_month_MAE": mae(reg.viirs, reg.pred),
        "regional_month_r": float(np.corrcoef(reg.viirs, reg.pred)[0, 1]),
        "regional_month_r_log": float(np.corrcoef(np.log1p(reg.viirs), np.log1p(reg.pred))[0, 1]),
        "annual_abs_pct_error": float(np.mean(np.abs(ann.pred - ann.viirs) / ann.viirs) * 100),
        "n_cell_months": int(len(test)),
    }


def cross_validate(g: pd.DataFrame, train=range(2012, 2019), test=range(2019, 2026)):
    tr, te = complete_overlap(g, train), complete_overlap(g, test)
    results, preds = {}, {}
    for cls in MODEL_CLASSES:
        m = cls().fit(tr)
        p = m.predict(te)
        results[m.name] = metrics(te, p)
        if isinstance(m, CellSeasonalRatio):
            results[m.name]["alpha"] = m.alpha
        preds[m.name] = p
    # reference: raw MODIS counts used as-is (no harmonization)
    results["no_adjustment"] = metrics(te, te["modis"].values.astype(float))
    preds["no_adjustment"] = te["modis"].values.astype(float)
    return results, te.assign(**{f"pred_{k}": v for k, v in preds.items()})


def best_model_name(results: dict) -> str:
    cands = {k: v for k, v in results.items() if k != "no_adjustment"}
    return min(cands, key=lambda k: cands[k]["cell_month_MAE"])


# ----------------------------------------------------------------------------
# 5. harmonized series
# ----------------------------------------------------------------------------
def harmonize(g: pd.DataFrame, model_name: str, first_year=2001, overlap=range(2012, 2026)):
    """Single cell x month series in VIIRS S-NPP-equivalent counts.

    Returns columns row, col, year, month, count, source, modis, viirs.
    """
    cls = next(c for c in MODEL_CLASSES if c().name == model_name)
    model = cls().fit(complete_overlap(g, overlap))

    pre = g[(g.year >= first_year) & (g.year < 2012)].copy()
    pre["modis"] = pre["modis"].astype(float)
    tf = terra_only_factor(g)
    date = pd.to_datetime(dict(year=pre.year, month=pre.month, day=1))
    terra_only = date < AQUA_START
    pre.loc[terra_only, "modis"] = pre.loc[terra_only, "terra"] * pre.loc[terra_only, "month"].map(tf)
    pre["count"] = model.predict(pre)
    pre["source"] = np.where(terra_only, "MODIS Terra (harmonized)", "MODIS Terra+Aqua (harmonized)")

    post = g[(g.year >= 2012)].copy()
    post["count"] = post["viirs"].astype(float)
    post["source"] = "VIIRS S-NPP"
    # S-NPP data gaps (outages, NRT latency): when a month's regional S-NPP total
    # falls below 70 % of NOAA-20 (same instrument, 50 min apart), use NOAA-20
    # scaled by the long-term S-NPP / NOAA-20 ratio for that month instead.
    both = g[(g.year >= 2019) & (g.year <= 2025)][["viirs", "noaa20"]].sum()
    snpp_per_n20 = both["viirs"] / max(both["noaa20"], 1)
    tot = post.groupby(["year", "month"])[["viirs", "noaa20"]].transform("sum")
    gap = (tot["noaa20"] > 100) & (tot["viirs"] < GAP_THRESHOLD * tot["noaa20"])
    post.loc[gap, "count"] = post.loc[gap, "noaa20"].astype(float) * snpp_per_n20
    post.loc[gap, "source"] = "VIIRS NOAA-20 (S-NPP gap fill)"

    out = pd.concat([pre, post], ignore_index=True)
    out = out[out["count"] > 0]
    lat, lon = cell_center(out.row, out.col)
    out = out.assign(lat=lat.astype("float32"), lon=lon.astype("float32"),
                     count=out["count"].astype("float32"))
    return out[["row", "col", "lat", "lon", "year", "month", "count", "source", "modis", "viirs"]], model
