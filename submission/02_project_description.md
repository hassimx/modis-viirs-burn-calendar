# Project details

## What it does

Burn Calendar is a web application that answers three questions for any area in Kazakhstan,
Kyrgyzstan, Uzbekistan, Tajikistan or Turkmenistan:

1. **When does this place burn?** A months × years "burn calendar" heat map from 2001 to the current
   month shows the fire season, its shifts and its exceptional years at a glance.
2. **Is this month unusual?** Every month is compared with the median, mean and interquartile range
   of the same calendar month across 25 years. Anomalous months are flagged on the calendar, and a
   summary card states, for example, "October 2026: 1 651 fires vs median 1 551, z = −0.3, normal".
3. **Is fire activity changing?** Annual totals with a Theil–Sen trend and Mann–Kendall significance
   test, plus peak months and years.

Users select an area by drawing a rectangle, polygon or circle on the map, by typing coordinates and a
radius, or by picking one of 48 administrative regions or a whole country. The map shows the
individual satellite hotspots behind the numbers, coloured by sensor. All charts use colour-blind-safe
scales and can be saved as PNG; detections, monthly aggregates and anomalies download as CSV. A public
JSON API (`/api/calendar`, `/api/anomalies`, `/api/stats`, `/api/points`, `/api/export.csv`) lets
early-warning systems use the same data.

## How it works

**Data.** We download the yearly FIRMS country archives for MODIS Collection 6.1 (Terra and Aqua),
VIIRS 375 m S-NPP and NOAA-20 for the five countries, and the current year through the FIRMS area API
(standard and near-real-time products, in 5-day windows with pauses and a local cache). In total
3.9 million detections from November 2000 to November 2026.

**Cleaning.** We keep MODIS detections with confidence ≥ 30 and VIIRS detections with nominal or high
confidence, and drop everything FIRMS flags as a static heat source (gas flares, industrial sites,
volcanoes). Near-real-time points have no such flag, so we remove those within about 1 km of a known
static source. This step matters in western Kazakhstan, where oil and gas flares would otherwise
masquerade as winter fires.

**Harmonization.** Detections are counted on a 0.25° grid per month. Because Aqua only starts in
mid-2002, Terra-only months are first scaled by a seasonal Terra+Aqua/Terra factor. We then fit three
models that convert MODIS counts into VIIRS S-NPP-equivalent counts, using the 2012–2025 overlap:
a seasonal ratio, a seasonal log-linear regression, and a cell × season ratio with empirical-Bayes
shrinkage that lets each grid cell keep its own MODIS/VIIRS relationship (higher in croplands with
many small fires, lower in steppe with large ones). We train on 2012–2018 and test on 2019–2025. The
cell × season model wins on every error measure (cell-month MAE 6.75 vs 8.41 unadjusted, regional
monthly correlation 0.985, annual error 12.7 % vs 73.8 %, bias −9 % vs −73 %) and is applied to
2001–2011. From 2012 on the series is VIIRS S-NPP itself, with NOAA-20 filling months when S-NPP
has an outage. The result is one continuous series where the sensor no longer matters.

**App.** A Python FastAPI backend answers queries from pre-aggregated Parquet files with pandas and
DuckDB in well under a second; any polygon is intersected with the grid using fractional cell
weights. The front end is a single page built with Leaflet and Plotly, without heavy frameworks.
The app runs in a small Docker container on a free cloud tier.

## Benefits

* **Comparable baseline.** Anomalies of this year are measured against 25 years instead of 13, and
  against numbers in the same unit.
* **Hidden trends become visible.** In Kazakhstan, harmonized fire detections fell from
  225 000–390 000 per year in 2002–2006 to 19 000–64 000 in 2022–2025 (−7 % per year, p < 0.001).
  Without harmonization the 2012 sensor jump masks this decline.
* **Local answers.** A fire officer can draw a district or a 30 km radius around a village and get its
  own fire calendar, not a national average.
* **Open and reproducible.** All code, the method notebook and validation metrics are public; one script
  re-downloads and re-processes everything.

## Intended impact and users

Emergency and early-warning services (planning patrols and resources for the expected fire season,
spotting abnormal months early), scientists (consistent long-term fire statistics), farmers and land
managers (stubble-burning and pasture fire patterns), and fire services and forestry agencies
(region-level reporting and prevention campaigns).

## Tools and languages

Python (pandas, NumPy, DuckDB, Shapely, Matplotlib, Jupyter), FastAPI, JavaScript, Leaflet,
Leaflet.draw, Plotly.js, Docker, Render, GitHub.

## Creativity

Instead of a single global correction factor we let the correction vary by season and by place,
with statistical shrinkage so that cells with little data stay close to the regional value. We also
correct two sensor changes that are often ignored: the Terra-only months before Aqua and the S-NPP
outages after 2012.

## Limitations and next steps

Counts are detections, not burned area; cloud cover limits all sensors. Cells where MODIS saw nothing
stay at zero before 2012. Next steps: add MODIS burned area (MCD64A1) and VIIRS burned area as a second
harmonized layer, daily automatic NRT refresh, and alerts when a region enters an abnormal month.
