// dashboard.js — render the 8-section Lighthouse + Firecrawl payload.
// Consumes window.DASHBOARD_DATA written by build_dashboard.py.
// Each KPI card carries data-snapshot-value (from meta.kpi_snapshot) and
// data-raw-value for UI ↔ build-log golden-snapshot reconciliation.

(function () {
  'use strict';

  const D = window.DASHBOARD_DATA;
  if (!D) {
    document.body.innerHTML =
      '<div style="padding:40px;color:#ef4444;font-family:monospace;">Failed to load DASHBOARD_DATA.</div>';
    return;
  }

  // Subject identity — used in chart labels, tooltips, plateau annotations.
  // Hoisted to IIFE-top so every render function can reference it.
  const SUBJECT_SLUG = 'hr_embarcadero';
  const SUBJECT_DISPLAY_NAME = 'Embarcadero';

  // Register chartjs-plugin-annotation for the subject flat-stretch lines on Section 1
  // quarterly panels. Plugin loads from the CDN before this script in index.html;
  // skip silently if missing (defensive — annotations would just not render).
  if (window.Chart && window['chartjs-plugin-annotation']) {
    Chart.register(window['chartjs-plugin-annotation']);
  } else if (window.Chart && typeof window.ChartAnnotation !== 'undefined') {
    Chart.register(window.ChartAnnotation);
  }

  // ===========================================================================
  // Helpers
  // ===========================================================================
  const $ = (sel) => document.querySelector(sel);

  const fmt = {
    int: (v) => (v == null || !isFinite(v)) ? '—' : Math.round(v).toLocaleString('en-US'),
    usd: (v) => (v == null || !isFinite(v)) ? '—' : '$' + Math.round(v).toLocaleString('en-US'),
    usdF: (v, d = 0) => (v == null || !isFinite(v)) ? '—' : '$' + v.toFixed(d),
    pct: (v, d = 1) => (v == null || !isFinite(v)) ? '—' : `${(v * 100).toFixed(d)}%`,
    pctRaw: (v, d = 1) => (v == null || !isFinite(v)) ? '—' : `${v.toFixed(d)}%`,
    r: (v, d = 3) => {
      if (v == null || !isFinite(v)) return 'n/a';
      return v >= 0 ? `+${v.toFixed(d)}` : v.toFixed(d);
    },
  };

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null) continue;
      if (k === 'className') e.className = v;
      else if (k === 'dataset') Object.assign(e.dataset, v);
      else if (k === 'style') e.setAttribute('style', v);
      else if (k.startsWith('on') && typeof v === 'function') {
        e.addEventListener(k.slice(2).toLowerCase(), v);
      } else {
        e.setAttribute(k, v);
      }
    }
    for (const c of children) {
      if (c == null || c === false) continue;
      e.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return e;
  }

  function clearChildren(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // Destroy any Chart instance bound to a canvas before creating a new one.
  // Defends against memory leaks if a render function is ever invoked twice
  // (e.g. theme switch, resize re-render, future hot-reload). Safe to call
  // when no prior chart exists.
  function freshCanvas(ctx) {
    if (ctx && window.Chart && typeof Chart.getChart === 'function') {
      const prior = Chart.getChart(ctx);
      if (prior) prior.destroy();
    }
    return ctx;
  }

  // 0..1 -> color along panel-2 -> accent-yellow
  function lerpColor(t) {
    t = Math.max(0, Math.min(1, t || 0));
    const a = [0x16, 0x1b, 0x24];
    const b = [0xf0, 0xb8, 0x29];
    return `rgb(${[0,1,2].map(i => Math.round(a[i] + (b[i] - a[i]) * t)).join(',')})`;
  }

  // ===========================================================================
  // Header / sub-line / generated timestamp
  // ===========================================================================
  function renderHeader() {
    const meta = D.meta || {};
    const lhRows = (meta.lighthouse_rows || 0).toLocaleString();
    const fcCells = D.firecrawl?.coverage?.n_cells ?? 0;
    $('#sub-line').textContent =
      `${SUBJECT_DISPLAY_NAME} Booking.com any-tier · SF luxury comp set · Lighthouse as-of ${meta.lighthouse_as_of || '—'} · ` +
      `${lhRows} Lighthouse rows + ${fcCells} Firecrawl BAR cells`;
    $('#lh-as-of').textContent = `LH ${meta.lighthouse_as_of || '—'}`;
    $('#ts-badge').textContent = (meta.generated || '').slice(0, 16).replace('T', ' ') + ' UTC';
  }

  // ===========================================================================
  // Section 2 (was §1): Forward demand environment — 365d demand strip + OTB strip
  //                    + monthly date axis labels + top-5 compression dates table
  // ===========================================================================
  function _monthlyAxis(dates) {
    if (!dates.length) return el('div');
    const monthStarts = [];
    const seen = new Set();
    dates.forEach((d, i) => {
      const ym = d.slice(0, 7);
      if (!seen.has(ym)) { seen.add(ym); monthStarts.push({ idx: i, label: ym }); }
    });
    const wrap = el('div', { style: 'position: relative; height: 13px; margin-top: 2px;' });
    for (const { idx, label } of monthStarts) {
      const pct = (idx / dates.length * 100).toFixed(2);
      wrap.appendChild(el('div', {
        style: `position: absolute; left: ${pct}%; font-size: 9px; color: var(--muted-2); ` +
               `font-family: var(--tabular); white-space: nowrap; transform: translateX(-2px);`,
      }, label));
    }
    return wrap;
  }

  function renderForwardDemand() {
    const md = D.lighthouse?.market_demand || [];
    const root = $('#market-demand-strip');
    clearChildren(root);
    if (!md.length) {
      root.appendChild(el('div', { className: 'h2-sub' }, 'No market demand data.'));
      return;
    }
    const demandPop = md.filter(r => r.market_demand_frac != null).length;
    const otbPop = md.filter(r => r.market_otb_frac != null).length;
    const allDates = md.map(r => r.arrival_date);

    function strip(label, key, populated, total) {
      const wrap = el('div', { style: 'margin-bottom:14px;' });
      wrap.appendChild(el('div', { className: 'h2-sub', style: 'margin-bottom:4px;' },
        `${label} — populated ${populated} of ${total} days (${fmt.pctRaw(populated / total * 100, 0)}).`));
      const stripEl = el('div', { className: 'heatmap-strip' });
      for (const r of md) {
        const v = r[key];
        const cell = el('div', {
          className: 'heatmap-cell' + (v == null ? ' empty' : ''),
          title: `${r.arrival_date} (${r.dow}): ${v == null ? 'n/a' : (v * 100).toFixed(0) + '%'}`,
          style: v == null ? null : `background: ${lerpColor(v)};`,
        });
        stripEl.appendChild(cell);
      }
      wrap.appendChild(stripEl);
      wrap.appendChild(_monthlyAxis(allDates));
      return wrap;
    }
    root.appendChild(strip('Market demand index (60-day forward window)', 'market_demand_frac', demandPop, md.length));
    root.appendChild(strip('Market OTB (365-day forward year)', 'market_otb_frac', otbPop, md.length));

    if (demandPop < 100) $('#demand-horizon-caveat').style.display = '';
  }

  function renderTopCompressionDates() {
    const md = D.lighthouse?.market_demand || [];
    const t = $('#top-compression-dates');
    if (!t) return;
    clearChildren(t);
    const top5 = md.filter(r => r.market_demand_frac != null)
                   .sort((a, b) => b.market_demand_frac - a.market_demand_frac)
                   .slice(0, 5);
    const thead = el('thead', {}, el('tr', {},
      el('th', {}, 'Rank'),
      el('th', {}, 'Date'),
      el('th', {}, 'Day'),
      el('th', {}, 'Market demand'),
      el('th', {}, 'Market OTB'),
    ));
    const tbody = el('tbody');
    top5.forEach((r, i) => {
      tbody.appendChild(el('tr', {},
        el('td', { style: 'text-align:left;' }, '#' + (i + 1)),
        el('td', { style: 'text-align:left;' }, r.arrival_date),
        el('td', { style: 'text-align:left;' }, r.dow),
        el('td', {}, fmt.pctRaw(r.market_demand_frac * 100, 1)),
        el('td', {}, r.market_otb_frac == null ? '—' : fmt.pctRaw(r.market_otb_frac * 100, 1)),
      ));
    });
    t.appendChild(thead);
    t.appendChild(tbody);
  }

  // ===========================================================================
  // Section 3: Subject-vs-comp-median spread (replaced 2026-05-07 per Kerry Mack
  // RM review). Reads D.derived.subject_vs_comp_median_spread; renders a verdict
  // banner + 3 KPI cards + dual-line chart over the demand window with high-bucket
  // bands behind. Methodology details collapsed below.
  // ===========================================================================
  const SUBJECT_VS_COMP_VERDICT_LABEL = {
    YIELDS_WITH_MARKET: 'YIELDS WITH MARKET',
    PARTIALLY_DYNAMIC:  'PARTIALLY YIELDS',
    ANCHORED:           'ANCHORED',
    ANTI_YIELDS:        'ANTI-YIELDS',
    INSUFFICIENT_DEMAND_WINDOW: 'INSUFFICIENT DEMAND WINDOW',
  };

  function _fmtSignedUSD(v, digits) {
    if (v == null || !isFinite(v)) return '—';
    const sign = v >= 0 ? '+' : '−';
    return sign + '$' + Math.abs(v).toFixed(digits || 0).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function _fmtPlainUSD(v) {
    if (v == null || !isFinite(v)) return '—';
    return '$' + Math.round(v).toLocaleString();
  }

  function renderSubjectVsCompVerdict() {
    const payload = D.derived?.subject_vs_comp_median_spread;
    if (!payload) return;
    const h = payload.headlines || {};
    const v = payload.verdict || {};
    const cls = v.classification || 'INSUFFICIENT_DEMAND_WINDOW';
    const banner = $('#verdict-class');
    const labelEl = $('#verdict-label');
    const ratEl = $('#verdict-rationale');
    if (banner) {
      banner.textContent = (SUBJECT_VS_COMP_VERDICT_LABEL[cls] || cls);
      banner.className = 'verdict-classification ' + cls;
      banner.setAttribute('data-snapshot-classification', cls);
    }
    if (labelEl) {
      labelEl.textContent = v.label || '—';
      labelEl.setAttribute('data-snapshot-label', v.label || '');
    }
    if (ratEl) {
      // Multi-LOS payloads count cells, not dates — derive unique-date count
      // for the displayed sentence so it reads "6 high-demand days" not "16".
      const _highDates = new Set();
      for (const r of (payload.rows || [])) {
        if (r.bucket === 'high') _highDates.add(r.arrival_date);
      }
      const dt = h.delta_typical_usd, dh = h.delta_high_usd, nh = _highDates.size;
      const widensNarrows = (h.spread_movement_usd != null && h.spread_movement_usd < 0) ? 'widens' : 'narrows';
      const aboveBelow = (x) => (x != null && x >= 0) ? 'above' : 'below';
      const parts = [];
      if (dt != null && nh != null && dh != null) {
        const verb = dt < 0 ? 'discount' : 'premium';
        parts.push('Embarcadero prices a median ' + _fmtPlainUSD(Math.abs(dt)) + ' ' +
                   aboveBelow(dt) + ' comp set on normal days; on the ' + nh +
                   ' high-demand days in the next 60, that ' + verb + ' ' +
                   widensNarrows + ' to ' + _fmtPlainUSD(Math.abs(dh)) + '.');
      }
      if (cls === 'ANTI_YIELDS') {
        parts.push("Read: Embarcadero isn't yielding BAR up and isn't gating LOS aggressively into compression — comps are doing one or the other, Embarcadero is doing neither. The discipline gap is in absolute-dollar terms (see Section 5 for the LOS pattern).");
      } else if (cls === 'YIELDS_WITH_MARKET') {
        parts.push("Read: Embarcadero's BAR yields up materially when the market does. Spread movement clears the +$50 threshold over a robust high-demand sample.");
      } else if (cls === 'ANCHORED') {
        parts.push('Read: Embarcadero holds its rate posture flat — spread to comp median moves less than the $25 noise floor between normal and high-demand days.');
      } else if (cls === 'PARTIALLY_DYNAMIC') {
        parts.push('Read: directional yielding present (spread moves more than $25) but below the +$50 confidence threshold.');
      } else if (cls === 'INSUFFICIENT_DEMAND_WINDOW') {
        parts.push('Read: too few high-demand days in the populated 60-day Lighthouse window (n_high < 5) to call the question.');
      }
      ratEl.textContent = parts.join(' ');
    }
  }

  function renderSubjectVsCompKpis() {
    const payload = D.derived?.subject_vs_comp_median_spread;
    if (!payload) return;
    const h = payload.headlines || {};

    function setKpi(elId, refId, value, label) {
      const el = document.getElementById(elId);
      const ref = document.getElementById(refId);
      if (el) {
        el.textContent = _fmtSignedUSD(value);
        el.style.color = (value != null && value < 0) ? 'var(--red)' : 'var(--ink-bright)';
      }
      if (ref) ref.textContent = label;
    }
    // Multi-LOS payloads count cells, not dates — derive unique-date counts
    // for the KPI subtitles so they read "6 high-demand dates" not "16".
    const _normalDates = new Set(), _highDates = new Set();
    for (const r of (payload.rows || [])) {
      if (r.bucket === 'normal') _normalDates.add(r.arrival_date);
      else if (r.bucket === 'high') _highDates.add(r.arrival_date);
    }
    setKpi('kpi-delta-typical', 'kpi-delta-typical-ref',
           h.delta_typical_usd, 'n=' + _normalDates.size + ' normal-demand dates (<0.50)');
    setKpi('kpi-delta-high', 'kpi-delta-high-ref',
           h.delta_high_usd, 'n=' + _highDates.size + ' high-demand dates (≥0.80)');
    const sm = h.spread_movement_usd;
    const smEl = document.getElementById('kpi-spread-movement');
    const smRef = document.getElementById('kpi-spread-movement-ref');
    if (smEl) {
      smEl.textContent = _fmtSignedUSD(sm);
      let color = 'var(--ink-bright)';
      if (sm != null) {
        if (sm <= -25) color = 'var(--red)';
        else if (sm >= 50) color = 'var(--green)';
        else color = 'var(--amber)';
      }
      smEl.style.color = color;
    }
    if (smRef) smRef.textContent = 'high − typical · red ≤ −$25 · green ≥ +$50';
  }

  function renderSubjectVsCompChart() {
    const ctx = document.getElementById('subject-vs-comp-chart');
    if (!ctx) return;
    const payload = D.derived?.subject_vs_comp_median_spread;
    const rows = payload?.rows || [];
    if (!rows.length) {
      ctx.parentElement.innerHTML = '<div class="h2-sub" style="padding:30px;">No demand-window dates found.</div>';
      return;
    }
    // SFOEM Lighthouse export carries one row per (LOS, arrival_date), so
    // payload.rows contains up to 3 rows per date (LOS=1/3/7). The chart
    // expects one point per date — collapse by date, taking the median rate
    // across LOS. Per-date metadata (bucket, market_demand_frac,
    // n_comps_available) is LOS-invariant so the first row's value is used.
    const _median = (xs) => {
      const arr = xs.filter(v => v != null).slice().sort((a, b) => a - b);
      if (!arr.length) return null;
      const m = Math.floor(arr.length / 2);
      return arr.length % 2 ? arr[m] : 0.5 * (arr[m - 1] + arr[m]);
    };
    const _byDate = new Map();
    for (const r of rows) {
      if (!_byDate.has(r.arrival_date)) _byDate.set(r.arrival_date, []);
      _byDate.get(r.arrival_date).push(r);
    }
    const sorted = [..._byDate.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([d, gs]) => ({
        arrival_date: d,
        aka_rate_usd: _median(gs.map(g => g.aka_rate_usd)),
        comp_median_rate_usd: _median(gs.map(g => g.comp_median_rate_usd)),
        daily_delta_usd: _median(gs.map(g => g.daily_delta_usd)),
        market_demand_frac: gs[0].market_demand_frac,
        bucket: gs[0].bucket,
        n_comps_available: gs[0].n_comps_available,
      }));
    const allDates = sorted.map(r => r.arrival_date);
    const akaSeries = sorted.map(r => r.aka_rate_usd);
    const compSeries = sorted.map(r => r.comp_median_rate_usd);

    const opts = chartOptions({
      x: { type: 'category', labels: allDates, ticks: { maxTicksLimit: 14 } },
      y: { title: { display: true, text: 'Rate (USD)' } },
    });
    opts.interaction = { mode: 'index', intersect: false };
    opts.plugins.tooltip.mode = 'index';
    opts.plugins.tooltip.intersect = false;
    opts.plugins.tooltip.callbacks = {
      title: (items) => items.length ? items[0].label : '',
      label: (item) => {
        const ds = item.dataset;
        const v = item.parsed?.y;
        if (v == null) return null;
        if (ds._key === 'subject')  return SUBJECT_DISPLAY_NAME + ': $' + Math.round(v).toLocaleString();
        if (ds._key === 'comp') return 'Comp Median: $' + Math.round(v).toLocaleString();
        return null;
      },
      afterBody: (items) => {
        const idx = items.length ? items[0].dataIndex : -1;
        if (idx < 0) return null;
        const r = sorted[idx];
        if (!r) return null;
        const out = [];
        if (r.daily_delta_usd != null) {
          const sign = r.daily_delta_usd >= 0 ? '+' : '−';
          out.push('daily_delta = ' + sign + '$' + Math.abs(Math.round(r.daily_delta_usd)).toLocaleString() +
                   ' (' + SUBJECT_DISPLAY_NAME + ' ' + (r.daily_delta_usd >= 0 ? 'above' : 'below') + ' comp median)');
        } else {
          out.push('daily_delta: n/a (no comps available this date)');
        }
        out.push('market_demand_frac = ' + (r.market_demand_frac != null ? r.market_demand_frac.toFixed(3) : '—'));
        out.push('bucket = ' + r.bucket + ' · comps_available = ' + r.n_comps_available);
        return out;
      },
    };

    // One vertical band per unique high-demand date. type:'line' with
    // xMin === xMax renders a vertical stripe across the y-axis without
    // extending into adjacent dates, so contiguous high dates show as
    // separate stripes rather than merging into one block.
    const annotations = {};
    sorted.forEach((r, i) => {
      if (r.bucket !== 'high') return;
      annotations['high_band_' + i] = {
        type: 'line',
        xMin: r.arrival_date,
        xMax: r.arrival_date,
        borderColor: 'rgba(190,200,220,0.40)',
        borderWidth: 8,
        drawTime: 'beforeDatasetsDraw',
      };
    });
    opts.plugins.annotation = { annotations };

    new Chart(freshCanvas(ctx), {
      type: 'line',
      data: {
        datasets: [
          {
            _key: 'comp',
            label: 'Comp Median',
            data: compSeries,
            borderColor: 'rgba(190,200,220,0.85)',
            backgroundColor: 'rgba(0,0,0,0)',
            borderWidth: 2,
            borderDash: [6, 4],
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0.2,
            fill: false,
            spanGaps: true,
          },
          {
            _key: 'subject',
            label: SUBJECT_DISPLAY_NAME,
            data: akaSeries,
            borderColor: 'rgba(0,123,193,1)',
            backgroundColor: 'rgba(0,0,0,0)',
            borderWidth: 2.5,
            pointRadius: 0,
            pointHoverRadius: 5,
            tension: 0.2,
            fill: false,
            spanGaps: true,
          },
        ],
      },
      options: opts,
    });
  }

  function renderSubjectVsCompMethodology() {
    const root = $('#subject-vs-comp-methodology');
    if (!root) return;
    clearChildren(root);
    const payload = D.derived?.subject_vs_comp_median_spread;
    if (!payload) return;
    const t = payload.thresholds || {};
    const h = payload.headlines || {};
    const rows = payload.rows || [];
    const compSet = (payload.comp_set || []).slice().sort();
    const compNames = compSet.map(p => COMPSET_PROP_LABEL[p] || p).join(', ');

    // Multi-LOS payloads count cells, not dates — derive unique-date counts
    // for displayed sample sizes so they match the KPI subtitles.
    const _normDates = new Set(), _shoulderDates = new Set(), _highDates = new Set();
    for (const r of rows) {
      if (r.bucket === 'normal') _normDates.add(r.arrival_date);
      else if (r.bucket === 'shoulder') _shoulderDates.add(r.arrival_date);
      else if (r.bucket === 'high') _highDates.add(r.arrival_date);
    }
    const audit = payload.high_bucket_audit || null;
    const ul = el('ul', {});
    ul.appendChild(el('li', {},
      'Universe: arrival dates where Lighthouse populates market_demand_frac (≈ 60 forward days). ' +
      'Subject = ' + SUBJECT_DISPLAY_NAME + ' Booking.com any-tier (cheapest available BAR). ' +
      'comp_median = median of available comps’ Booking.com any-tier on the same date.'));
    ul.appendChild(el('li', {},
      'Comp set (n=' + compSet.length + '): ' + compNames + '. Subject excluded.'));
    ul.appendChild(el('li', {},
      'Buckets: normal < ' + t.shoulder_threshold + ' · shoulder [' + t.shoulder_threshold + ', ' + t.high_threshold + ') · high ≥ ' + t.high_threshold + '. ' +
      'Sample sizes: n_normal=' + _normDates.size + ', n_shoulder=' + _shoulderDates.size + ', n_high=' + _highDates.size + '.'));
    ul.appendChild(el('li', {},
      'Verdict thresholds: noise floor ±$' + t.noise_floor_usd + ' (ANCHORED if |spread_movement| < $' + t.noise_floor_usd + '); ' +
      'YIELDS_WITH_MARKET at +$' + t.yields_usd + ' with n_high ≥ ' + t.min_high_n + '; ANTI_YIELDS at −$' + t.noise_floor_usd + ' or worse. ' +
      '$' + t.noise_floor_usd + ' floor is below the day-to-day delta-of-delta noise on this Lighthouse pull; $' + t.yields_usd + ' is roughly 2× noise.'));
    if (audit) {
      const partN = audit.panel_size_distribution || {};
      const sf = (partN['5'] || partN[5] || 0);
      const reduced = (partN['3'] || partN[3] || 0) + (partN['4'] || partN[4] || 0);
      const tally = audit.excluded_status_tally || {};
      const sold = (tally.sold_out || 0);
      const losR = (tally.los_restricted || 0);
      const notLoaded = (tally.not_loaded || 0);
      const totalExcluded = audit.excluded_total != null ? audit.excluded_total : (sold + losR + notLoaded);
      ul.appendChild(el('li', {},
        'High-bucket comp-availability: of ' + _highDates.size + ' high-demand dates, ' + sf +
        ' have full 5-comp panels and ' + reduced + ' have reduced panels (3 or 4 comps).'));
      ul.appendChild(el('li', {},
        'Excluded comp cells = sold_out or LOS-restricted (' + losR + ' LOS, ' + sold +
        ' sold-out across ' + totalExcluded + ' exclusions). Zero not_loaded data gaps in this pull.'));
      const sens = audit.sensitivity || {};
      if (sens.delta_high_full != null && sens.delta_high_asis != null) {
        const shift = sens.delta_high_full - sens.delta_high_asis;
        const sign = shift >= 0 ? '+' : '−';
        ul.appendChild(el('li', {},
          'Sensitivity check: dropping the comp_n=3 date shifts delta_high by ' + sign + '$' + Math.abs(Math.round(shift)) +
          ' ($' + Math.round(sens.delta_high_asis) + ' → $' + Math.round(sens.delta_high_full) + '), ' +
          'well inside the $' + t.noise_floor_usd + ' noise floor.'));
      }
    }
    if (rows.length) {
      const high = rows.filter(r => r.bucket === 'high').map(r => r.arrival_date).sort();
      ul.appendChild(el('li', {},
        'High-bucket dates: ' + (high.length ? high.join(', ') : '(none)') + '.'));
    }
    root.appendChild(ul);
  }


  // ===========================================================================
  // Section 4: Embarcadero Sunday tier ladder (Firecrawl, Sunday-only).
  // Single horizontal bar — median Direct BAR per canonical SKU on Sunday
  // arrival dates only. Sundays = system rolling-default rate before RM
  // intervenes (Kerry Mack, 2026-05-07). Two view-premium delta annotations
  // rendered below the chart in tabular-mono.
  // ===========================================================================
  function renderAKATierLadder() {
    const ctx = document.getElementById('aka-tier-ladder');
    if (!ctx) return;
    const payload = D.firecrawl?.aka_sunday_tier_ladder || {};
    const rows = payload.rows || [];
    const labelMap = {
    };
    if (!rows.length) {
      ctx.parentElement.innerHTML =
        '<div class="h2-sub" style="padding:30px;">No Sunday observations in Firecrawl payload.</div>';
      return;
    }
    const THIN_SAMPLE_THRESHOLD = 3;
    const isThin = (n) => (n != null && n < THIN_SAMPLE_THRESHOLD);
    const labels  = rows.map(r =>
      (labelMap[r.canonical_room_id] || r.canonical_room_id) +
      (isThin(r.n_sundays) ? ' *' : '')
    );
    const medians = rows.map(r => r.median_usd ?? null);
    const obsCounts = rows.map(r => r.n_sundays ?? 0);
    // Lighter fill for thin-sample bars (n < 3) so the IC reader sees the
    // sample-size constraint at a glance, not just in the caveat block.
    const bgColors = rows.map(r =>
      isThin(r.n_sundays) ? 'rgba(240,184,41,0.28)' : 'rgba(240,184,41,0.7)');
    const borderColors = rows.map(r =>
      isThin(r.n_sundays) ? 'rgba(240,184,41,0.55)' : 'rgba(240,184,41,1)');

    const opts = chartOptions({
      x: { title: { display: true, text: 'Median Sunday Direct BAR (USD)' }, beginAtZero: true },
      y: { ticks: { font: { size: 11 } } },
    });
    opts.indexAxis = 'y';
    opts.plugins.legend = { display: false };
    opts.plugins.tooltip.callbacks = {
      title: (items) => items.length ? items[0].label : '',
      label: (ctx2) => {
        const v = ctx2.parsed?.x;
        const n = obsCounts[ctx2.dataIndex];
        if (v == null) return null;
        const thin = isThin(n) ? '  (thin sample)' : '';
        return `Median Sunday BAR: $${Math.round(v)} · n=${n}${thin}`;
      },
    };
    new Chart(freshCanvas(ctx), {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Median Sunday Direct BAR (USD)',
          data: medians,
          backgroundColor: bgColors,
          borderColor: borderColors,
          borderWidth: 1,
        }],
      },
      options: opts,
    });

    // Look up sample size per canonical SKU so the delta annotations can
    // disclose thin sample sizes inline (asterisk on the bar isn't enough
    // when the IC reader is scanning the deltas line).
    const nBySku = Object.fromEntries(rows.map(r => [r.canonical_room_id, r.n_sundays ?? 0]));
    const deltasRoot = document.getElementById('aka-tier-ladder-deltas');
    if (deltasRoot) {
      clearChildren(deltasRoot);
      const deltas = payload.view_premium_deltas || {};
      const order = [
        ['1BR_PLATINUM_HSV_minus_1BR_PLATINUM', '1BR Platinum HSV − 1BR Platinum',
         '1BR_PLATINUM', '1BR_PLATINUM_HSV'],
        ['2BR_PLATINUM_HSV_minus_2BR_PLATINUM', '2BR Platinum HSV − 2BR Platinum',
         '2BR_PLATINUM', '2BR_PLATINUM_HSV'],
      ];
      let renderedDeltas = 0;
      order.forEach(([k, label, baseSku, viewSku]) => {
        const d = deltas[k];
        const v = d?.value_usd;
        if (v == null) {
          return;  // skip empty pairs entirely — no placeholder line for SKUs the deal doesn't have
        }
        const baseN = nBySku[baseSku];
        const viewN = nBySku[viewSku];
        const baseTag = isThin(baseN) ? ` (${baseSku === '1BR_PLATINUM' ? '1BR' : '2BR'} base n=${baseN} — thin)` : '';
        const text = `${label} = $${v.toFixed(0)} (observed view premium)${baseTag}`;
        deltasRoot.appendChild(el('div', {}, text));
        renderedDeltas += 1;
      });
      if (renderedDeltas > 0) {
        deltasRoot.appendChild(el('div', {
          style: 'margin-top: 4px; color: var(--muted-2); font-size: 10px;',
        }, '* = thin sample (n < 3 Sundays); bar fill lightened.'));
      }
    }
    const nEl = document.getElementById('aka-tier-ladder-n');
    if (nEl) nEl.textContent = String(payload.n_sundays_observed ?? 0);
  }

  // ===========================================================================
  // Section 1: 4 quarterly comp-set rate panels with shared legend + AKA flat-stretch
  // annotations. Each panel auto-scales y so summer compression doesn't mask
  // shoulder-season flatness. Shared legend toggles all 4 in unison.
  // ===========================================================================
  const COMPSET_PROP_LABEL = {
    hr_embarcadero: 'Hyatt Regency SF Embarcadero',
    hr_soma: 'Hyatt Regency SF SoMa',
    clancy: 'The Clancy',
    hilton_us: 'Hilton SF Union Square',
    grand_hyatt: 'Grand Hyatt SF',
    palace: 'Palace Hotel',
    marquis: 'Marriott Marquis SF',
    ic_sf: 'InterContinental SF',
    st_francis: 'Westin St. Francis',
  };
  // SFOEM palette: subject hr_embarcadero is Hyatt blue. hr_soma was originally
  // sky, but sky reads visually too similar to Hyatt blue on the Section 1
  // quarterly panels — swapped hr_soma ↔ clancy so the control comp now reads
  // violet for clear contrast against the subject.
  const COMPSET_STYLE_MAP = {
    hr_embarcadero:  { b: 'rgba(0,123,193,1)',     f: 'rgba(0,123,193,0.20)',  w: 2.5 },
    hr_soma:         { b: 'rgba(167,139,250,0.85)',f: 'rgba(167,139,250,0.10)', w: 1.4 },
    clancy:          { b: 'rgba(56,189,248,0.85)', f: 'rgba(56,189,248,0.10)', w: 1.4 },
    hilton_us:       { b: 'rgba(94,234,212,0.85)', f: 'rgba(94,234,212,0.10)', w: 1.4 },
    grand_hyatt:     { b: 'rgba(248,113,113,0.85)',f: 'rgba(248,113,113,0.10)', w: 1.4 },
    palace:          { b: 'rgba(190,200,220,0.85)',f: 'rgba(190,200,220,0.10)', w: 1.4 },
    marquis:         { b: 'rgba(251,191,36,0.85)', f: 'rgba(251,191,36,0.10)', w: 1.4 },
    ic_sf:           { b: 'rgba(52,211,153,0.85)', f: 'rgba(52,211,153,0.10)', w: 1.4 },
    st_francis:      { b: 'rgba(244,114,182,0.85)',f: 'rgba(244,114,182,0.10)', w: 1.4 },
  };
  const COMPSET_PROPS_ALL = ['hr_embarcadero', 'hr_soma', 'clancy', 'hilton_us', 'grand_hyatt', 'palace', 'marquis', 'ic_sf', 'st_francis'];
  const COMPSET_RENDER_ORDER = ['hr_soma', 'clancy', 'hilton_us', 'grand_hyatt', 'palace', 'marquis', 'ic_sf', 'st_francis', 'hr_embarcadero'];

  // Module-level array so the shared legend can toggle visibility across all 4 charts.
  const compsetChartInstances = [];
  const compsetVisible = Object.fromEntries(COMPSET_PROPS_ALL.map(p => [p, true]));

  function _buildCompsetSharedLegend() {
    const legend = $('#compset-shared-legend');
    if (!legend) return;
    clearChildren(legend);
    legend.appendChild(el('label', {}, 'Properties:'));
    for (const p of COMPSET_PROPS_ALL) {
      const isOn = compsetVisible[p];
      const style = isOn ? `border-left: 3px solid ${COMPSET_STYLE_MAP[p].b};` : '';
      const pill = el('span', {
        className: 'pill' + (isOn ? ' active' : ''),
        tabindex: '0',
        role: 'button',
        'aria-pressed': isOn ? 'true' : 'false',
        style,
      }, COMPSET_PROP_LABEL[p]);
      const toggle = () => {
        compsetVisible[p] = !compsetVisible[p];
        pill.classList.toggle('active');
        pill.setAttribute('aria-pressed', compsetVisible[p] ? 'true' : 'false');
        pill.setAttribute('style',
          compsetVisible[p] ? `border-left: 3px solid ${COMPSET_STYLE_MAP[p].b};` : '');
        // Sync visibility across all 4 panels.
        compsetChartInstances.forEach(chart => {
          const idx = chart.data.datasets.findIndex(ds => ds._propKey === p);
          if (idx >= 0) chart.setDatasetVisibility(idx, compsetVisible[p]);
          chart.update();
        });
      };
      pill.addEventListener('click', toggle);
      pill.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') {
          ev.preventDefault();
          toggle();
        }
      });
      legend.appendChild(pill);
    }
  }

  function _buildPanelAnnotations(stretches, panelStart, panelEnd, panelLabels) {
    // Map each AKA flat stretch into a Chart.js annotation if it overlaps this panel.
    // xMin/xMax must reference labels actually present on the panel's category axis,
    // otherwise Chart.js silently fails to draw. We snap to the first/last label
    // inside the stretch's clipped range.
    const labelSet = new Set(panelLabels);
    const annotations = {};
    stretches.forEach((s, idx) => {
      // Skip if no overlap with panel range.
      if (s.end_date < panelStart || s.start_date > panelEnd) return;
      const xMinDate = s.start_date > panelStart ? s.start_date : panelStart;
      const xMaxDate = s.end_date < panelEnd ? s.end_date : panelEnd;
      // Snap xMin to the first label ≥ xMinDate; xMax to last label ≤ xMaxDate.
      const snappedMin = panelLabels.find(d => d >= xMinDate);
      let snappedMax = null;
      for (let k = panelLabels.length - 1; k >= 0; k--) {
        if (panelLabels[k] <= xMaxDate) { snappedMax = panelLabels[k]; break; }
      }
      if (!snappedMin || !snappedMax || snappedMin > snappedMax) return;
      annotations[`flat_${idx}`] = {
        type: 'line',
        yMin: s.rate, yMax: s.rate,
        xMin: snappedMin, xMax: snappedMax,
        borderColor: 'rgba(0, 123, 193, 0.6)',
        borderWidth: 2,
        borderDash: [6, 4],
        label: {
          display: true,
          content: `${SUBJECT_DISPLAY_NAME} flat at $${s.rate} (${s.days} days, ${s.start_date}—${s.end_date})`,
          position: 'start',
          backgroundColor: 'rgba(15, 22, 34, 0.85)',
          color: 'rgba(0, 123, 193, 1)',
          font: { size: 10, family: '"JetBrains Mono", monospace' },
          padding: 4,
        },
      };
    });
    return annotations;
  }

  function renderCompsetRateLinesByQuarter() {
    const data = D.derived?.compset_rate_lines_by_quarter || {};
    const stretches = D.derived?.compset_flat_stretches || [];
    const root = $('#compset-quarterly-panels');
    if (!root) return;
    clearChildren(root);
    compsetChartInstances.length = 0;

    _buildCompsetSharedLegend();

    const Q1_Y_CLIP = 1500;

    for (const [qLabel, qData] of Object.entries(data)) {
      const isQ1 = qLabel === 'Q1';

      // Header
      const panel = el('div', { className: 'quarterly-panel' });
      const headerSub = el('div', { className: 'h2-sub', style: 'margin-bottom:6px;' },
        el('strong', { style: 'color: var(--ink-bright);' }, `${qLabel} · ${qData.start} — ${qData.end}`),
        ` — ${qData.observation}`,
      );
      panel.appendChild(headerSub);
      if (isQ1) {
        panel.appendChild(el('div', {
          className: 'h2-sub',
          style: 'margin-bottom:6px; font-size:10px; color: var(--muted-2);',
        }, `Y-axis clipped at $${Q1_Y_CLIP.toLocaleString()} so ${SUBJECT_DISPLAY_NAME}'s flat plateau stays readable. ` +
            `Spikes above the cap render as outline markers labeled with the actual rate.`));
      }
      const canvasWrap = el('div', {
        className: 'canvas-wrap', style: 'height: 240px;',
      });
      const canvas = el('canvas', { id: `compset-q-${qLabel}` });
      canvasWrap.appendChild(canvas);
      panel.appendChild(canvasWrap);
      root.appendChild(panel);

      // Build chart
      const allDates = [...new Set(
        COMPSET_PROPS_ALL.flatMap(p => (qData.lines[p] || []).map(pt => pt.arrival_date)),
      )].sort();
      if (!allDates.length) {
        canvasWrap.innerHTML = '<div class="h2-sub" style="padding:30px;">No data in this quarter.</div>';
        continue;
      }
      const datasets = COMPSET_RENDER_ORDER.map(p => {
        const map = Object.fromEntries((qData.lines[p] || []).map(pt => [pt.arrival_date, pt.rate_usd]));
        return {
          label: COMPSET_PROP_LABEL[p],
          _propKey: p,
          data: allDates.map(d => ({ x: d, y: map[d] ?? null })),
          borderColor: COMPSET_STYLE_MAP[p].b,
          backgroundColor: COMPSET_STYLE_MAP[p].f,
          borderWidth: COMPSET_STYLE_MAP[p].w,
          spanGaps: false,
          tension: 0.2,
          pointRadius: 0,
          pointHoverRadius: 4,
          hidden: !compsetVisible[p],
        };
      });

      const opts = chartOptions({
        x: { type: 'category', labels: allDates, ticks: { maxTicksLimit: 8 } },
        y: { beginAtZero: false, title: { display: true, text: 'Rate (USD)' } },
      });
      if (isQ1) {
        opts.scales.y.max = Q1_Y_CLIP;
      }
      opts.plugins.legend = { display: false }; // shared legend at top of section
      opts.interaction = { mode: 'index', intersect: false };
      opts.plugins.tooltip.mode = 'index';
      opts.plugins.tooltip.intersect = false;

      const annotations = _buildPanelAnnotations(stretches, qData.start, qData.end, allDates);
      if (isQ1) {
        // Per-clipped-point markers + label annotations for any property whose
        // rate exceeds the cap. Marker sits at (date, cap) in property color;
        // label sits just under the cap with the actual rate so the IC reader
        // sees what the spike actually was.
        for (const p of COMPSET_RENDER_ORDER) {
          const map = Object.fromEntries((qData.lines[p] || []).map(pt => [pt.arrival_date, pt.rate_usd]));
          for (const d of allDates) {
            const r = map[d];
            if (r == null || r <= Q1_Y_CLIP) continue;
            const safeId = `${p}_${d}`.replace(/[^a-z0-9_]/gi, '_');
            annotations[`clip_pt_${safeId}`] = {
              type: 'point',
              xValue: d, yValue: Q1_Y_CLIP,
              backgroundColor: 'rgba(15,22,34,0)',
              borderColor: COMPSET_STYLE_MAP[p].b,
              borderWidth: 2,
              radius: 5,
            };
            annotations[`clip_lbl_${safeId}`] = {
              type: 'label',
              xValue: d, yValue: Q1_Y_CLIP,
              yAdjust: -14,
              content: ['$' + Math.round(r).toLocaleString()],
              backgroundColor: 'rgba(15,22,34,0.85)',
              color: COMPSET_STYLE_MAP[p].b,
              borderColor: COMPSET_STYLE_MAP[p].b,
              borderWidth: 1,
              font: { size: 9, family: '"JetBrains Mono", monospace' },
              padding: 2,
            };
          }
        }
      }
      opts.plugins.annotation = { annotations };

      const chart = new Chart(freshCanvas(canvas), {
        type: 'line',
        data: { datasets },
        options: opts,
      });
      compsetChartInstances.push(chart);
    }
  }

  // ===========================================================================
  // Section 1: Flatness scorecard — plateau detection (≥10 consecutive days
  // within 5% day-over-day) on each property's Booking.com Any-tier BAR.
  // Reads lighthouse.flatness_scorecard payload; renders a 6-property table.
  // The scorecard puts AKA's flat percentage against comp parallel numbers in
  // one row of the page so the IC reader can read flatness, not amplitude.
  // ===========================================================================
  function renderFlatnessScorecard() {
    const root = $('#flatness-scorecard');
    if (!root) return;
    clearChildren(root);
    const payload = D.lighthouse?.flatness_scorecard || {};
    const rows = payload.rows || [];
    if (!rows.length) {
      root.appendChild(el('div', { className: 'h2-sub' }, 'No flatness data.'));
      return;
    }
    const labelMap = COMPSET_PROP_LABEL;
    const fmtVals = (arr) => {
      if (!arr || !arr.length) return '—';
      const top = arr.slice(0, 5).map(v => '$' + Math.round(v));
      return top.join(', ') + (arr.length > 5 ? '…' : '');
    };
    const thead = el('thead', {},
      el('tr', {},
        el('th', {}, 'Property'),
        el('th', {}, 'Days observed'),
        el('th', {}, 'Days on flat plateau'),
        el('th', {}, '% flat'),
        el('th', {}, 'Longest plateau (days)'),
        el('th', { style: 'text-align:left;' }, 'Plateau values ($)'),
      ),
    );
    const tbody = el('tbody', {});
    for (const r of rows) {
      const isSubject = r.property === SUBJECT_SLUG;
      const tr = el('tr', { style: isSubject ? 'background: rgba(0,123,193,0.08);' : '' });
      tr.appendChild(el('td', {}, labelMap[r.property] || r.property));
      tr.appendChild(el('td', {}, String(r.n_observed ?? 0)));
      tr.appendChild(el('td', {}, String(r.n_on_plateau ?? 0)));
      tr.appendChild(el('td', {}, r.pct_flat != null ? (r.pct_flat * 100).toFixed(0) + '%' : '—'));
      tr.appendChild(el('td', {}, String(r.longest_plateau_days ?? 0)));
      tr.appendChild(el('td', { style: 'text-align:left;' }, fmtVals(r.plateau_values_usd)));
      tbody.appendChild(tr);
    }
    root.appendChild(thead);
    root.appendChild(tbody);

    const subjectRow = rows.find(r => r.property === SUBJECT_SLUG);
    const summary = document.getElementById(`flatness-${SUBJECT_SLUG}-summary`);
    if (summary && subjectRow) {
      summary.textContent =
        `${subjectRow.n_on_plateau} of ${subjectRow.n_observed} observed dates are on a flat plateau (${(subjectRow.pct_flat * 100).toFixed(0)}%; longest ${subjectRow.longest_plateau_days} days).`;
    }
  }

  // ===========================================================================
  // Section 5 (was §6 pre-2026-05-07 cleanup; Penthouse RM scatter removed):
  // LOS-restriction trigger heatmap
  // ===========================================================================
  function renderLOSGrid() {
    const data = D.lighthouse?.los_restrictions || [];
    const root = $('#los-grid');
    clearChildren(root);
    if (!data.length) {
      root.appendChild(el('div', { className: 'h2-sub' },
        'No LOS restrictions detected in Booking.com any-tier feed.'));
      return;
    }
    const props = COMPSET_PROPS_ALL;
    // Short labels for the column headers — full COMPSET_PROP_LABEL names are
    // too wide for the heatmap cells. Fall back to slug if unmapped.
    const propLabel = {
      hr_embarcadero: SUBJECT_DISPLAY_NAME,
      hr_soma: 'SoMa',
      clancy: 'Clancy',
      hilton_us: 'Hilton US',
      grand_hyatt: 'Grand Hyatt',
      palace: 'Palace',
      marquis: 'Marquis',
      ic_sf: 'IC SF',
      st_francis: 'St Francis',
    };
    const dates = [...new Set(data.map(r => r.arrival_date))].sort();
    const cellMap = {};
    for (const r of data) cellMap[r.arrival_date + '|' + r.property] = r.los_restriction;

    const grid = el('div', {
      className: 'heatmap-grid',
      style: `grid-template-columns: 110px repeat(${props.length}, 1fr); max-height: 400px; overflow:auto;`,
    });
    grid.appendChild(el('div', { className: 'label-cell' }, 'arrival_date'));
    for (const p of props) grid.appendChild(el('div', { className: 'label-cell' }, propLabel[p]));
    for (const d of dates) {
      grid.appendChild(el('div', { className: 'label-cell', style: 'text-align:left;' }, d));
      for (const p of props) {
        const v = cellMap[d + '|' + p];
        if (v == null) {
          grid.appendChild(el('div', { className: 'data-cell na' }, '·'));
        } else {
          const c = v >= 3 ? 'rgba(239,68,68,0.45)' : 'rgba(251,191,36,0.45)';
          grid.appendChild(el('div', {
            className: 'data-cell',
            style: `background: ${c}; color: var(--ink-bright);`,
            title: `${d} ${propLabel[p]}: LOS${v}`,
          }, 'L' + v));
        }
      }
    }
    root.appendChild(grid);
    root.appendChild(el('div', { className: 'heatmap-legend' },
      `${data.length} cells · ${dates.length} dates · `,
      el('span', { className: 'swatch', style: 'background: rgba(251,191,36,0.45);' }), ' LOS2 · ',
      el('span', { className: 'swatch', style: 'background: rgba(239,68,68,0.45);' }), ' LOS3+',
    ));
  }

  // ===========================================================================
  // Section 6 (was §7): Raw data explorer — toggle Lighthouse/Firecrawl, sort, filter
  // ===========================================================================
  const explorerState = { source: 'firecrawl', sortKey: null, sortDir: 1, filterText: '' };

  function renderExplorer() {
    const pills = document.querySelectorAll('[data-explorer-source]');
    pills.forEach(p => {
      const activate = () => {
        pills.forEach(q => {
          q.classList.remove('active');
          q.setAttribute('aria-pressed', 'false');
        });
        p.classList.add('active');
        p.setAttribute('aria-pressed', 'true');
        explorerState.source = p.dataset.explorerSource;
        explorerState.sortKey = null;
        renderExplorerTable();
      };
      p.addEventListener('click', activate);
      p.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') {
          ev.preventDefault();
          activate();
        }
      });
    });
    const filter = $('#explorer-filter');
    filter.addEventListener('input', () => {
      explorerState.filterText = filter.value.toLowerCase();
      renderExplorerTable();
    });
    renderExplorerTable();
  }

  function renderExplorerTable() {
    const t = $('#explorer-table');
    clearChildren(t);
    // Per-source schema. `cellRender` overrides the default numeric/string
    // rendering for specific columns (e.g. RATE → "$1,044"). `headerLabel`
    // overrides the column-header display (defaults to the key name).
    let cols = [], rows = [], cellRender = {}, headerLabel = {};
    if (explorerState.source === 'firecrawl') {
      const cells = D.firecrawl?.channel_parity?.cells || [];
      cols = ['date', 'code', 'min', 'max', 'spread', 'spread_pct', 'min_channel', 'max_channel'];
      rows = cells.map(c => ({
        date: c.date, code: c.code,
        min: c.min, max: c.max,
        spread: c.spread, spread_pct: c.spread_pct,
        min_channel: c.min_channel, max_channel: c.max_channel,
      }));
    } else if (explorerState.source === 'lighthouse_rates') {
      const lr = D.derived?.lighthouse_rates_explorer || [];
      cols = ['arrival_date', 'dow', 'property', 'rate_usd', 'room_tier', 'channel'];
      headerLabel = {
        arrival_date: 'ARRIVAL_DATE', dow: 'DOW', property: 'PROPERTY',
        rate_usd: 'RATE', room_tier: 'ROOM_TIER', channel: 'CHANNEL',
      };
      cellRender = {
        rate_usd: v => (v == null ? '—' : '$' + Math.round(v).toLocaleString()),
      };
      rows = lr.map(r => ({
        arrival_date: r.arrival_date, dow: r.dow, property: r.property,
        rate_usd: r.rate_usd, room_tier: r.room_tier, channel: r.channel,
      }));
    } else {
      const md = D.lighthouse?.market_demand || [];
      cols = ['arrival_date', 'dow', 'market_demand_frac', 'market_otb_frac'];
      rows = md.map(r => ({
        arrival_date: r.arrival_date, dow: r.dow,
        market_demand_frac: r.market_demand_frac,
        market_otb_frac: r.market_otb_frac,
      }));
    }
    if (explorerState.filterText) {
      const f = explorerState.filterText;
      rows = rows.filter(r => Object.values(r).some(v => String(v).toLowerCase().includes(f)));
    }
    if (explorerState.sortKey) {
      const k = explorerState.sortKey, dir = explorerState.sortDir;
      rows = [...rows].sort((a, b) => {
        const av = a[k], bv = b[k];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir;
        return String(av).localeCompare(String(bv)) * dir;
      });
    }
    const thead = el('thead'), tr = el('tr');
    for (const c of cols) {
      const isSorted = explorerState.sortKey === c;
      const ariaSort = isSorted ? (explorerState.sortDir > 0 ? 'ascending' : 'descending') : 'none';
      const th = el('th', {
        style: 'cursor:pointer; user-select:none;',
        role: 'columnheader',
        tabindex: '0',
        'aria-sort': ariaSort,
      }, headerLabel[c] || c);
      const activate = () => {
        if (explorerState.sortKey === c) explorerState.sortDir *= -1;
        else { explorerState.sortKey = c; explorerState.sortDir = 1; }
        renderExplorerTable();
      };
      th.addEventListener('click', activate);
      th.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') {
          ev.preventDefault();
          activate();
        }
      });
      if (isSorted) {
        th.appendChild(document.createTextNode(explorerState.sortDir > 0 ? ' ↑' : ' ↓'));
      }
      tr.appendChild(th);
    }
    thead.appendChild(tr);
    t.appendChild(thead);

    const tbody = el('tbody');
    const sliced = rows.slice(0, 500);
    for (const r of sliced) {
      const trr = el('tr');
      for (const c of cols) {
        const v = r[c];
        let txt;
        if (cellRender[c]) {
          txt = cellRender[c](v);
        } else if (v == null) {
          txt = '—';
        } else if (typeof v === 'number') {
          txt = (Math.abs(v) < 1 && v !== 0) ? v.toFixed(3) : v.toFixed(0);
        } else {
          txt = String(v);
        }
        trr.appendChild(el('td', {}, txt));
      }
      tbody.appendChild(trr);
    }
    t.appendChild(tbody);
    $('#explorer-rowcount').textContent =
      `${rows.length.toLocaleString()} rows` + (rows.length > 500 ? ' (showing first 500)' : '');
  }

  // ===========================================================================
  // Methodology body
  // ===========================================================================
  function renderMethodology() {
    const body = $('#methodology-body');
    const meta = D.meta || {};
    clearChildren(body);
    const ul = el('ul');
    function add(html) { const li = document.createElement('li'); li.innerHTML = html; ul.appendChild(li); }
    add(`<strong>Lighthouse Rate Insight</strong>: institutional 365-day forward rate-shop. ` +
        `Sources: <code>brandcom</code>, <code>bookingcom</code>, <code>expedia</code>, <code>priceline</code>. ` +
        `Market demand index populated for ~60 forward days only; Market OTB populated full year.`);
    add(`<strong>Firecrawl scrape</strong>: room-type-level depth across direct + Hotels.com + Booking; ` +
        `pending — populates after the scrape phase runs (currently rendering in <code>--lighthouse-only</code> mode).`);
    add(`<strong>Comp set (Lighthouse)</strong>: ${(meta.lighthouse_comps_present || []).join(', ')}. ` +
        `Park Central NY excluded at parser layer (Lighthouse subscription artifact, not a true comp).`);
    add(`<strong>Section 3 verdict logic</strong> (subject-vs-comp-median spread (replaced the prior DOW-stratified rm_verdict construct on methodological grounds — baselines must be reproducible by hand without proprietary statistical scaffolding)): bucket each populated demand date by <code>market_demand_frac</code> (normal &lt; 0.50 · shoulder [0.50, 0.80) · high ≥ 0.80) and report the median <code>daily_delta = subject_rate − comp_median</code> per bucket. <code>spread_movement = delta_high − delta_typical</code>. Verdict is <strong>YIELDS_WITH_MARKET</strong> if spread_movement ≥ +$50 with n_high ≥ 5; <strong>ANCHORED</strong> if |spread_movement| &lt; $25; <strong>ANTI_YIELDS</strong> at −$25 or worse; <strong>INSUFFICIENT_DEMAND_WINDOW</strong> if n_high &lt; 5. The $25 floor sits below the day-to-day delta-of-delta noise on this Lighthouse pull; $50 is roughly 2× noise.`);
    add(`<strong>Section 1 flatness scorecard</strong>: for each of the 9 properties, count days observed (Booking.com any-tier BAR available + non-null), days inside any detected plateau (≥10 consecutive arrival dates within 5% day-over-day; calendar gaps from sold_out / not_loaded do NOT break a run), and surface plateau medians sorted by run length. One <code>detect_plateaus()</code> helper powers both the scorecard and the quarterly-panel flat-stretch annotations — single source of truth for "what is a plateau."`);
    add(`<strong>Lighthouse rows total</strong>: <code>${(meta.lighthouse_rows || 0).toLocaleString()}</code>; as-of <code>${meta.lighthouse_as_of || '—'}</code>.`);
    body.appendChild(ul);
  }

  // ===========================================================================
  // Chart options factory (dark theme)
  // ===========================================================================
  function chartOptions(scales) {
    const grid = { color: 'rgba(31,37,48,0.7)', drawBorder: false };
    const ticks = { color: 'rgba(138,146,163,1)', font: { family: '"JetBrains Mono", monospace', size: 10 } };
    const titleStyle = { color: 'rgba(138,146,163,1)', font: { family: '"JetBrains Mono", monospace', size: 11 } };
    const out = {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: {
          labels: {
            color: 'rgba(230,230,230,1)',
            font: { family: '"JetBrains Mono", monospace', size: 11 },
          },
        },
        tooltip: {
          backgroundColor: '#11151c', borderColor: '#2a3140', borderWidth: 1,
          titleColor: '#ffffff', bodyColor: '#e6e6e6',
          titleFont: { family: '"JetBrains Mono", monospace', size: 11 },
          bodyFont:  { family: '"JetBrains Mono", monospace', size: 11 },
        },
      },
      scales: {},
    };
    for (const [name, opts] of Object.entries(scales || {})) {
      out.scales[name] = Object.assign({ grid: { ...grid }, ticks: { ...ticks } }, opts);
      if (out.scales[name].title) {
        out.scales[name].title = Object.assign({ display: true, ...titleStyle }, out.scales[name].title);
      }
    }
    return out;
  }

  // ===========================================================================
  // Boot
  // ===========================================================================
  function boot() {
    try {
      renderHeader();
      // Section 1: flatness scorecard + 4 quarterly comp-set rate panels (shared legend).
      renderFlatnessScorecard();
      renderCompsetRateLinesByQuarter();
      // Section 2: forward demand + top-5 dates
      renderForwardDemand();
      renderTopCompressionDates();
      // Section 3: subject-vs-comp-median spread (verdict banner + 3 KPI cards +
      // dual-line chart + collapsed methodology details).
      renderSubjectVsCompVerdict();
      renderSubjectVsCompKpis();
      renderSubjectVsCompChart();
      renderSubjectVsCompMethodology();
      // Section 4: AKA Sunday tier ladder (Firecrawl, Sunday-only)
      renderAKATierLadder();
      // Sections 5-6: LOS heatmap + raw data explorer (Penthouse scatter removed in
      // the 2026-05-07 cleanup pass; renumbered from 6/7 → 5/6).
      renderLOSGrid();
      renderExplorer();
      renderMethodology();
      window.__DASHBOARD_BOOTED__ = true;
    } catch (e) {
      console.error('Dashboard boot failed:', e);
      document.body.insertAdjacentHTML('afterbegin',
        '<div style="background:#ef4444;color:#fff;padding:12px;font-family:monospace;">Dashboard error: ' +
        (e && e.message ? e.message : e) + '</div>');
      throw e;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
