"""Raw FIRMS files -> Parquet tables, harmonization, metrics and figures.

Outputs (data/processed/):
    points.parquet          filtered hotspots, all sensors
    grid_monthly.parquet    0.25 deg cell x month counts per sensor stream
    harmonized.parquet      single VIIRS-equivalent cell x month series 2001-now
    cv_predictions.parquet  test-period predictions of every model
    metrics.json            cross-validation metrics + chosen model
Figures (docs/figures/): before_after.png, validation.png, seasonal_ratio.png
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import harmonize as H  # noqa: E402

OUT = H.PROCESSED
FIG = ROOT / "docs" / "figures"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)

    pts = H.load_points()
    pts = pts[pd.to_datetime(pts["date"]).dt.year >= 2001]
    pts.to_parquet(OUT / "points.parquet", index=False, compression="zstd", row_group_size=200_000)
    print("points:", len(pts), pts.groupby("sensor", observed=True).size().to_dict())

    g = H.grid_monthly(pts)
    g.to_parquet(OUT / "grid_monthly.parquet", index=False)

    results, cv = H.cross_validate(g)
    best = H.best_model_name(results)
    cv.to_parquet(OUT / "cv_predictions.parquet", index=False)

    harm, model = H.harmonize(g, best)
    harm.to_parquet(OUT / "harmonized.parquet", index=False)
    meta = {
        "train_years": [2012, 2018], "test_years": [2019, 2025],
        "grid_deg": H.CELL, "results": results, "best_model": best,
        "terra_only_factor": H.terra_only_factor(g).round(4).to_dict(),
        "last_date": str(pd.to_datetime(pts["date"]).max().date()),
    }
    if hasattr(model, "k") and model.k is not None:
        meta["monthly_ratio_k"] = model.k.round(4).to_dict()
    if hasattr(model, "base") and model.base is not None:
        meta["monthly_ratio_k"] = model.base.k.round(4).to_dict()
    (OUT / "metrics.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(results, indent=1))
    print("best:", best)

    figures(g, harm, cv, results, best)


def figures(g, harm, cv, results, best) -> None:
    plt.rcParams.update({"figure.dpi": 130, "font.size": 9})
    # --- before / after -------------------------------------------------------
    ann = g.groupby("year")[["modis", "viirs"]].sum()
    hy = harm.groupby("year")["count"].sum()
    _, model = H.harmonize(g, best)
    ov = g[g.year >= 2012].copy()
    ov["pred"] = model.predict(ov)
    hov = ov.groupby("year")["pred"].sum()
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.8))
    ax[0].plot(ann.index, ann.modis, "o-", color="#e66101", label="MODIS Terra+Aqua (raw)")
    ax[0].plot(ann.index[ann.index >= 2012], ann.viirs[ann.index >= 2012], "s-", color="#5e3c99",
               label="VIIRS S-NPP 375 m (raw)")
    ax[0].set_title("Before: two sensors, two levels")
    ax[1].plot(hy.index, hy.values, "o-", color="#1b7837", label="Harmonized series")
    ax[1].plot(hov.index, hov.values, ":", color="#e66101", label="MODIS -> VIIRS-equivalent (overlap check)")
    ax[1].axvline(2011.5, color="grey", lw=0.8, ls="--")
    ax[1].text(2006, 0.03, "MODIS-based (2001-2011)", ha="center", transform=ax[1].get_xaxis_transform())
    ax[1].text(2018.5, 0.03, "VIIRS S-NPP (2012-now)", ha="center", transform=ax[1].get_xaxis_transform())
    ax[1].set_title(f"After: one series ({best})")
    for a in ax:
        a.set_ylabel("fire detections per year")
        a.legend(frameon=False, fontsize=8, loc="upper right")
        a.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG / "before_after.png")
    plt.close(fig)

    # --- validation ----------------------------------------------------------
    names = [k for k in results if k != "no_adjustment"]
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.9))
    reg = cv.groupby(["year", "month"])[["viirs"] + [f"pred_{n}" for n in names + ["no_adjustment"]]].sum()
    t = pd.to_datetime(dict(year=reg.index.get_level_values(0), month=reg.index.get_level_values(1), day=1))
    ax[0].plot(t, reg.viirs, color="black", lw=1.4, label="VIIRS observed")
    for n, c in zip(names, ["#1b9e77", "#d95f02", "#7570b3"]):
        ax[0].plot(t, reg[f"pred_{n}"], lw=1, color=c, label=n)
    ax[0].plot(t, reg["pred_no_adjustment"], lw=0.8, color="grey", ls=":", label="MODIS raw")
    ax[0].set_yscale("symlog", linthresh=100)
    ax[0].set_title("Test period 2019-2025, regional monthly totals")
    ax[0].legend(frameon=False, fontsize=7)
    p = cv[f"pred_{best}"]
    ax[1].hexbin(np.log1p(cv.viirs), np.log1p(p), gridsize=45, bins="log", cmap="viridis", mincnt=1)
    lim = np.log1p(max(cv.viirs.max(), p.max()))
    ax[1].plot([0, lim], [0, lim], color="red", lw=0.8)
    ax[1].set_xlabel("log(1 + VIIRS observed)")
    ax[1].set_ylabel("log(1 + predicted)")
    ax[1].set_title(f"Cell-months, {best}  r={results[best]['cell_month_r']:.2f}")
    x = np.arange(len(names) + 1)
    labels = names + ["no_adjustment"]
    ax[2].bar(x, [results[n]["cell_month_MAE"] for n in labels], color=["#1b9e77", "#d95f02", "#7570b3", "#bbbbbb"])
    ax[2].set_xticks(x, labels, rotation=20, fontsize=7)
    ax[2].set_title("Cell-month MAE on test years (lower is better)")
    for a in ax:
        a.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG / "validation.png")
    plt.close(fig)

    # --- seasonal ratio ------------------------------------------------------
    k = H.MonthlyRatio().fit(H.complete_overlap(g, range(2012, 2026))).k
    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    ax.bar(k.index, k.values, color="#5e3c99")
    ax.set_xticks(range(1, 13))
    ax.set_xlabel("month")
    ax.set_ylabel("VIIRS / MODIS detections")
    ax.set_title("Seasonal VIIRS:MODIS ratio k_m (2012-2025)")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(FIG / "seasonal_ratio.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
