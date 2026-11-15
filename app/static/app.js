/* Burn Calendar front end: Leaflet map + Plotly charts over the FastAPI API. */
(() => {
  const $ = (id) => document.getElementById(id);
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Okabe-Ito palette (colour-blind safe)
  const SENSOR = {
    MODIS: { c: "#E69F00", n: "MODIS (Terra/Aqua, 1 km)" },
    VIIRS_SNPP: { c: "#0072B2", n: "VIIRS S-NPP (375 m)" },
    VIIRS_NOAA20: { c: "#009E73", n: "VIIRS NOAA-20 (375 m)" },
    VIIRS_NOAA21: { c: "#CC79A7", n: "VIIRS NOAA-21 (375 m)" },
  };
  const fmt = (v, d = 0) => (v == null ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d }));
  const pTxt = (p) => (p < 0.001 ? "p < 0.001" : `p = ${p.toFixed(3)}`);
  const plotCfg = (name) => ({
    responsive: true, displaylogo: false,
    toImageButtonOptions: { format: "png", filename: name, scale: 2 },
    modeBarButtonsToRemove: ["lasso2d", "select2d"],
  });
  const baseLayout = {
    margin: { l: 52, r: 16, t: 10, b: 40 }, paper_bgcolor: "#fff", plot_bgcolor: "#fff",
    font: { family: "IBM Plex Sans, sans-serif", size: 12, color: "#1d1f22" },
  };

  let META = null;
  let area = { country: "KAZ" };
  let areaLabel = "Kazakhstan";

  // ---------------------------------------------------------------- map
  const map = L.map("map", { preferCanvas: true, zoomSnap: 0.25 }).setView([48, 67], 4.5);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors", maxZoom: 16,
  }).addTo(map);
  const canvas = L.canvas({ padding: 0.3 });
  const pointsLayer = L.layerGroup().addTo(map);
  const aoiLayer = L.featureGroup().addTo(map);
  let regionsLayer = null;
  const drawn = new L.FeatureGroup().addTo(map);
  map.addControl(new L.Control.Draw({
    position: "topright",
    draw: { polyline: false, marker: false, circlemarker: false,
            rectangle: { shapeOptions: { color: "#c2410c" } },
            polygon: { shapeOptions: { color: "#c2410c" } },
            circle: { shapeOptions: { color: "#c2410c" } } },
    edit: { featureGroup: drawn, edit: false },
  }));
  map.on(L.Draw.Event.CREATED, (e) => {
    drawn.clearLayers();
    const l = e.layer;
    if (e.layerType === "circle") {
      const c = l.getLatLng();
      area = { lat: c.lat.toFixed(4), lon: c.lng.toFixed(4), radius_km: (l.getRadius() / 1000).toFixed(1) };
      areaLabel = `${area.radius_km} km around ${area.lat}, ${area.lon}`;
    } else if (e.layerType === "rectangle") {
      const b = l.getBounds();
      area = { bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(4)).join(",") };
      areaLabel = "drawn rectangle";
    } else {
      const ring = l.getLatLngs()[0].map((p) => `${p.lng.toFixed(4)} ${p.lat.toFixed(4)}`);
      ring.push(ring[0]);
      area = { polygon: ring.join(",") };
      areaLabel = "drawn polygon";
    }
    drawn.addLayer(l);
    $("region").value = "";
    refresh();
  });

  function showAoi() {
    aoiLayer.clearLayers();
    if (area.region != null && regionsLayer) {
      regionsLayer.eachLayer((l) => {
        if (String(l.feature.properties.id) === String(area.region)) {
          L.geoJSON(l.feature, { style: { color: "#c2410c", weight: 2, fill: false } }).addTo(aoiLayer);
        }
      });
    } else if (area.country && regionsLayer) {
      regionsLayer.eachLayer((l) => {
        if (l.feature.properties.iso === area.country) {
          L.geoJSON(l.feature, { style: { color: "#c2410c", weight: 1.5, fill: false } }).addTo(aoiLayer);
        }
      });
    } else if (area.lat) {
      L.circle([+area.lat, +area.lon], { radius: area.radius_km * 1000, color: "#c2410c", fill: false }).addTo(aoiLayer);
    }
    if (aoiLayer.getLayers().length) map.fitBounds(aoiLayer.getBounds(), { padding: [20, 20], maxZoom: 9 });
    else if (drawn.getLayers().length) map.fitBounds(drawn.getBounds(), { padding: [20, 20], maxZoom: 9 });
  }

  // ---------------------------------------------------------------- params
  function params(extra = {}) {
    const p = new URLSearchParams({ ...area, ...extra });
    if ($("start").value) p.set("start", $("start").value);
    if ($("end").value) p.set("end", $("end").value);
    return p;
  }
  const api = (path, extra) => fetch(`/api/${path}?${params(extra)}`).then((r) => {
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  });

  function setDownloads() {
    $("dlPoints").href = `/api/export.csv?${params({ kind: "points" })}`;
    $("dlAgg").href = `/api/export.csv?${params({ kind: "aggregates" })}`;
    $("dlAnom").href = `/api/export.csv?${params({ kind: "anomalies" })}`;
  }

  // ---------------------------------------------------------------- charts
  function drawCalendar(cal, anom) {
    const log = $("logscale").checked;
    const z = cal.z.map((r) => r.map((v) => (v == null ? null : log ? Math.log10(1 + v) : v)));
    const src = cal.years.map((y) => MONTHS.map(() => cal.source_by_year[y]));
    const flags = {};
    anom.months.forEach((m) => { flags[m.period] = m; });
    const custom = cal.years.map((y, i) => MONTHS.map((_, j) => {
      const f = flags[`${y}-${String(j + 1).padStart(2, "0")}`];
      return [cal.z[i][j], src[i][j], f ? f.flag : "", f ? f.z : "", f ? f.median : ""];
    }));
    const maxv = Math.max(1, ...cal.z.flat().filter((v) => v != null));
    let colorbar = { title: { text: "fires / month", side: "right" }, thickness: 12, outlinewidth: 0 };
    if (log) {
      const ticks = [0, 1, 10, 100, 1000, 10000, 100000, 1000000].filter((t) => t <= maxv * 1.01);
      colorbar = { ...colorbar, tickvals: ticks.map((t) => Math.log10(1 + t)), ticktext: ticks.map((t) => fmt(t)) };
    }
    const heat = {
      type: "heatmap", x: MONTHS, y: cal.years.map(String), z, customdata: custom,
      colorscale: $("cscale").value, colorbar, xgap: 1, ygap: 1, zmin: 0,
      hovertemplate: "<b>%{x} %{y}</b><br>%{customdata[0]:,.0f} fires (VIIRS-equivalent)<br>source: %{customdata[1]}<br>anomaly: %{customdata[2]} (z=%{customdata[3]})<br>median for month: %{customdata[4]:,.0f}<extra></extra>",
    };
    const mk = (kind, sym, name) => {
      const pts = anom.months.filter((m) => m.flag === kind);
      return { type: "scatter", mode: "markers", x: pts.map((m) => MONTHS[m.month - 1]), y: pts.map((m) => String(m.year)),
               marker: { symbol: sym, size: 9, color: "#ffffff", line: { color: "#111111", width: 1.2 } },
               name, hoverinfo: "skip", showlegend: false };
    };
    const shapes = [];
    if (cal.years.includes(2011) && cal.years.includes(2012)) {
      const yb = cal.years.indexOf(2012) - 0.5;
      shapes.push({ type: "line", xref: "paper", x0: -0.02, x1: 1, y0: yb, y1: yb,
                    yref: "y", line: { color: "#c2410c", width: 2, dash: "dot" } });
    }
    const n = cal.years.length;
    $("calendar").style.height = `${Math.max(260, Math.min(640, 40 + n * 19))}px`;
    Plotly.react("calendar", [heat, mk("high", "triangle-up", "high"), mk("low", "triangle-down", "low")], {
      ...baseLayout, margin: { l: 48, r: 10, t: 28, b: 10 },
      yaxis: { range: [n - 0.5, -0.5], type: "category", tickfont: { family: "IBM Plex Mono", size: 11 } },
      xaxis: { side: "top", type: "category" }, shapes,
    }, plotCfg("burn_calendar"));
  }

  function drawAnomalies(an) {
    const ms = an.months.slice(-Math.min(an.months.length, 60));
    const x = ms.map((m) => m.period);
    const col = (f) => ({ high: "#b2182b", low: "#2166ac", "above normal": "#ef8a62", "below normal": "#67a9cf" }[f] || "#9aa0a6");
    const traces = [
      { x, y: ms.map((m) => m.q3), type: "scatter", mode: "lines", line: { width: 0 }, hoverinfo: "skip", showlegend: false },
      { x, y: ms.map((m) => m.q1), type: "scatter", mode: "lines", line: { width: 0 }, fill: "tonexty",
        fillcolor: "rgba(120,120,120,0.18)", name: "IQR (Q1–Q3)", hoverinfo: "skip" },
      { x, y: ms.map((m) => m.value), type: "bar", name: "observed", marker: { color: ms.map((m) => col(m.flag)) },
        customdata: ms.map((m) => [m.flag, m.z, m.median]),
        hovertemplate: "<b>%{x}</b><br>%{y:,.0f} fires<br>median %{customdata[2]:,.0f}<br>z = %{customdata[1]} · %{customdata[0]}<extra></extra>" },
      { x, y: ms.map((m) => m.median), type: "scatter", mode: "lines", name: "median", line: { color: "#1d1f22", width: 1.5, dash: "dot" } },
      { x, y: ms.map((m) => m.mean), type: "scatter", mode: "lines", name: "mean", line: { color: "#7a5195", width: 1 }, visible: "legendonly" },
    ];
    Plotly.react("anom", traces, {
      ...baseLayout, barmode: "overlay", bargap: 0.15, legend: { orientation: "h", y: 1.12, x: 0 },
      yaxis: { title: { text: "fires / month" }, rangemode: "tozero", gridcolor: "#eee" },
      xaxis: { type: "category", tickangle: -45, nticks: 20 },
      margin: { ...baseLayout.margin, t: 30, b: 60 },
    }, plotCfg("monthly_anomalies"));

    const s = an.selected;
    if (s) {
      const cls = s.flag.split(" ")[0];
      const [yy, mm] = s.period.split("-");
      const ratio = s.median > 0 ? `${fmt((s.value / s.median) * 100)}% of median` : "median is 0";
      $("focusBox").innerHTML = `<b>${MONTHS[+mm - 1]} ${yy}</b>${s.partial ? " (month in progress)" : ""}: <b>${fmt(s.value)}</b> fires vs median <b>${fmt(s.median)}</b> (IQR ${fmt(s.q1)}–${fmt(s.q3)}), ${ratio}, z = <b>${s.z > 0 ? "+" : ""}${s.z}</b> <span class="flag ${cls}">${s.flag}</span>`;
    } else {
      $("focusBox").textContent = "Pick a month to assess.";
    }
  }

  function drawAnnual(st, an) {
    const yrs = an.years;
    const col = (f) => ({ high: "#b2182b", low: "#2166ac", "above normal": "#ef8a62", "below normal": "#67a9cf" }[f] || "#9aa0a6");
    const traces = [{
      type: "bar", x: yrs.map((y) => y.year), y: yrs.map((y) => y.value), name: "annual total",
      marker: { color: yrs.map((y) => col(y.flag)), opacity: yrs.map((y) => (y.partial ? 0.45 : 1)),
                line: { color: yrs.map((y) => (y.year < 2012 ? "#E69F00" : "#0072B2")), width: 1.5 } },
      customdata: yrs.map((y) => [y.flag, y.z, y.partial ? " (year in progress)" : ""]),
      hovertemplate: "<b>%{x}</b>%{customdata[2]}<br>%{y:,.0f} fires<br>z = %{customdata[1]} · %{customdata[0]}<extra></extra>",
    }];
    if (st.trend) {
      traces.push({ type: "scatter", mode: "lines", x: st.trend.line.map((p) => p[0]), y: st.trend.line.map((p) => p[1]),
                    name: "Theil–Sen trend", line: { color: "#1d1f22", width: 2, dash: "dash" } });
      const t = st.trend;
      $("trendSub").textContent = `trend ${t.pct_per_year > 0 ? "+" : ""}${t.pct_per_year}%/yr, Mann–Kendall ${pTxt(t.p_value)}`;
    } else $("trendSub").textContent = "";
    Plotly.react("annual", traces, {
      ...baseLayout, legend: { orientation: "h", y: 1.12, x: 0 }, margin: { ...baseLayout.margin, t: 30 },
      yaxis: { title: { text: "fires / year (VIIRS-equivalent)" }, gridcolor: "#eee", rangemode: "tozero" },
      xaxis: { dtick: 2 },
    }, plotCfg("annual_totals"));
  }

  function drawKpis(st, an) {
    const raw = st.raw_detections || {};
    const rawTotal = Object.values(raw).reduce((a, b) => a + b, 0);
    const topM = st.top_months[0];
    const topY = st.top_years[0];
    const t = st.trend;
    const s = an.selected;
    const trendTxt = t ? `<span class="${t.slope_per_year > 0 ? "up" : "down"}">${t.pct_per_year > 0 ? "▲ +" : "▼ "}${t.pct_per_year}%</span>` : "–";
    const items = [
      { k: "Fires in period (harmonized)", v: fmt(st.harmonized_total), s: `${fmt(rawTotal)} raw detections, all sensors` },
      { k: "Peak month", v: topM ? `${MONTHS[+topM.period.slice(5) - 1]} ${topM.period.slice(0, 4)}` : "–", s: topM ? `${fmt(topM.value)} fires` : "" },
      { k: "Peak year", v: topY ? topY.year : "–", s: topY ? `${fmt(topY.value)} fires` : "" },
      { k: "Fire season peak", v: st.peak_calendar_month || "–", s: "highest mean calendar month" },
      { k: "Trend per year", v: trendTxt, s: t ? `${pTxt(t.p_value)} (${t.years[0]}–${t.years[1]})` : "" },
    ];
    $("kpis").innerHTML = items.map((i) => `<div class="kpi"><div class="k">${i.k}</div><div class="v">${i.v}</div><div class="s">${i.s}</div></div>`).join("");
  }

  async function drawPoints() {
    pointsLayer.clearLayers();
    $("mapSub").textContent = "loading points…";
    const p = await api("points");
    const seen = new Set();
    for (let i = 0; i < p.lat.length; i++) {
      const s = SENSOR[p.sensor[i]] || { c: "#666" };
      seen.add(p.sensor[i]);
      L.circleMarker([p.lat[i], p.lon[i]], { renderer: canvas, radius: 2.6, weight: 0, fillOpacity: 0.75, fillColor: s.c })
        .bindTooltip(`${p.date[i]} · ${p.sensor[i].replace("_", " ")} · FRP ${p.frp[i]} MW`)
        .addTo(pointsLayer);
    }
    $("mapSub").textContent = p.sampled ? `${fmt(p.returned)} of ${fmt(p.total)} detections shown (random sample)` : `${fmt(p.total)} detections`;
    $("mapLegend").innerHTML = Object.entries(SENSOR).filter(([k]) => seen.has(k))
      .map(([, v]) => `<span><i style="background:${v.c}"></i>${v.n}</span>`).join("") || "<span>No detections in this area and period.</span>";
  }

  async function refresh() {
    $("areaLabel").textContent = areaLabel;
    setDownloads();
    showAoi();
    document.querySelector(".content").classList.add("loading");
    try {
      const focus = $("focus").value;
      const [cal, an, st] = await Promise.all([api("calendar"), api("anomalies", focus ? { month: focus } : {}), api("stats")]);
      $("areaLabel").textContent = cal.area;
      drawCalendar(cal, an);
      drawAnomalies(an);
      drawAnnual(st, an);
      drawKpis(st, an);
      if (an.selected && !focus) $("focus").value = an.selected.period;
      document.querySelector(".content").classList.remove("loading");
      await drawPoints();
    } catch (err) {
      console.error(err);
      $("kpis").innerHTML = `<div class="kpi"><div class="k">Error</div><div class="s">${err.message}</div></div>`;
    } finally {
      document.querySelector(".content").classList.remove("loading");
    }
  }

  // ---------------------------------------------------------------- controls
  function setRange(kind) {
    const last = META.last_date.slice(0, 7);
    const [ly, lm] = last.split("-").map(Number);
    if (kind === "all") { $("start").value = `${META.first_year}-01`; $("end").value = last; }
    if (kind === "viirs") { $("start").value = "2012-01"; $("end").value = last; }
    if (kind === "modis") { $("start").value = `${META.first_year}-01`; $("end").value = "2011-12"; }
    if (kind === "12") {
      const d = new Date(Date.UTC(ly, lm - 12, 1));
      $("start").value = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`; $("end").value = last;
    }
  }
  document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => { setRange(b.dataset.range); refresh(); }));
  $("go").addEventListener("click", refresh);
  $("cscale").addEventListener("change", refresh);
  $("logscale").addEventListener("change", refresh);
  $("focus").addEventListener("change", refresh);
  $("region").addEventListener("change", () => {
    const v = $("region").value;
    drawn.clearLayers();
    if (v.startsWith("c:")) { area = { country: v.slice(2) }; }
    else if (v !== "") { area = { region: v }; }
    areaLabel = $("region").selectedOptions[0].textContent.trim();
    refresh();
  });
  $("applyPoint").addEventListener("click", () => {
    const lat = parseFloat($("lat").value), lon = parseFloat($("lon").value), r = parseFloat($("rad").value) || 25;
    if (Number.isNaN(lat) || Number.isNaN(lon)) { $("lat").focus(); return; }
    drawn.clearLayers();
    area = { lat, lon, radius_km: r };
    areaLabel = `${r} km around ${lat}, ${lon}`;
    $("region").value = "";
    refresh();
  });

  async function init() {
    META = await fetch("/api/meta").then((r) => r.json());
    const cv = META.cv;
    $("meta").textContent = `data through ${META.last_date}\n0.25° grid · model: ${META.best_model.replaceAll("_", " ")}`;
    const rows = Object.entries(cv).map(([k, v]) => `<tr class="${k === META.best_model ? "best" : ""}"><td>${k.replaceAll("_", " ")}</td><td>${v.cell_month_MAE.toFixed(2)}</td><td>${v.cell_month_r.toFixed(3)}</td><td>${v.regional_month_r.toFixed(3)}</td><td>${v.annual_abs_pct_error.toFixed(1)}%</td><td>${v.cell_month_bias_pct.toFixed(1)}%</td></tr>`).join("");
    $("cvTable").innerHTML = `<thead><tr><th>Model (test 2019–2025)</th><th>MAE cell-month</th><th>r cell-month</th><th>r regional</th><th>annual error</th><th>bias</th></tr></thead><tbody>${rows}</tbody>`;

    const regions = await fetch("/api/regions").then((r) => r.json());
    const sel = $("region");
    const countries = {};
    regions.features.forEach((f) => { countries[f.properties.iso] = f.properties.country; });
    sel.innerHTML = `<option value="" disabled>Custom area (map)</option>` +
      Object.entries(countries).map(([iso, name]) => `<optgroup label="${name}"><option value="c:${iso}">${name}: whole country</option>` +
        regions.features.filter((f) => f.properties.iso === iso).map((f) => `<option value="${f.properties.id}">  ${f.properties.name}</option>`).join("") + "</optgroup>").join("");
    sel.value = "c:KAZ";
    regionsLayer = L.geoJSON(regions, {
      style: { color: "#8a8f98", weight: 0.8, fillOpacity: 0 },
      onEachFeature: (f, l) => {
        l.bindTooltip(`${f.properties.name}, ${f.properties.country}`, { sticky: true });
        l.on("click", () => { area = { region: f.properties.id }; sel.value = String(f.properties.id); drawn.clearLayers();
                              areaLabel = `${f.properties.name}, ${f.properties.country}`; refresh(); });
      },
    }).addTo(map);
    regionsLayer.bringToBack();
    // default: last 5 complete years + current year for a quick first view of the points
    $("start").value = `${META.first_year}-01`;
    $("end").value = META.last_date.slice(0, 7);
    refresh();
  }
  init();
})();
