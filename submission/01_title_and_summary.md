# Project title

**Burn Calendar: one fire record for Central Asia, 2001 to today**

# High-level summary (for the Space Apps form)

Burn Calendar merges 25 years of NASA FIRMS active-fire detections from MODIS (2000–) and VIIRS
(2012–) into a single, sensor-independent monthly fire series for Kazakhstan and Central Asia, and
serves it as a free web app. VIIRS sees 3–5 times more fires than MODIS, so the two records cannot
simply be joined. We calibrated MODIS against VIIRS where both exist (2012–2025), compared three
statistical models with year-blocked cross-validation, and converted 2001–2011 MODIS counts into
VIIRS-equivalent counts, cutting the bias from −73 % to −9 % and the annual error from 74 % to 13 %.
Emergency services, scientists, farmers and foresters can draw any area and instantly see when it
normally burns, whether this month is abnormal against a 25-year baseline, the long-term trend, and
the individual hotspots, and download everything as CSV or PNG. For Kazakhstan the harmonized record
reveals a 7 %-per-year decline in fire activity since the early 2000s that the raw data hide.

# Links

* Live app: https://burn-calendar.onrender.com
* Code: https://github.com/hassimx/modis-viirs-burn-calendar
