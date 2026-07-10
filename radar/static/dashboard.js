(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  const SEVERITY_META = {
    high: { dot: "critical", label: "High" },
    med: { dot: "warning", label: "Med" },
    low: { dot: "good", label: "Low" },
  };

  // Incident lifecycle. Only 4 dot colors exist (good/warning/critical/
  // muted); acknowledged and mitigating share "warning" since both just mean
  // "someone's actively on it," distinguished by label, not color.
  const INCIDENT_META = {
    open: { dot: "critical", label: "Open" },
    acknowledged: { dot: "warning", label: "Acknowledged" },
    mitigating: { dot: "warning", label: "Mitigating" },
    resolved: { dot: "good", label: "Resolved" },
    false_positive: { dot: "muted", label: "False positive" },
  };

  // The next logical forward action(s) offered from each incident state --
  // "false_positive" is a manual escape hatch (drag a Board card there
  // directly), not something to suggest as a "next" step to click toward.
  const INCIDENT_NEXT_ACTIONS = {
    open: [{ status: "acknowledged", label: "Acknowledge" }],
    acknowledged: [
      { status: "mitigating", label: "Start mitigating" },
      { status: "resolved", label: "Resolve" },
    ],
    mitigating: [{ status: "resolved", label: "Resolve" }],
    resolved: [],
    false_positive: [],
  };

  // Platform identity = hue + shape, not hue alone (see dashboard.css for the
  // full history of why 6 mutually-safe, non-red/orange/green hues don't
  // fit). Only 4 validated hues; Reddit/YouTube share one (circle/diamond)
  // and HackerNews/X share another (circle/diamond); Mastodon is shape-only
  // (square) on a neutral tone, as before. This is the dataviz skill's
  // prescribed fix for "more categories than safely-distinguishable hues":
  // composite (hue x shape) encoding, not an invented hue.
  const PLATFORM_COLOR_VAR = {
    reddit: "--platform-reddit",
    youtube: "--platform-youtube",
    x: "--platform-x",
    hackernews: "--platform-hackernews",
    github: "--platform-github",
    stackoverflow: "--platform-stackoverflow",
  };
  const PLATFORM_SHAPE = {
    reddit: "circle",
    youtube: "diamond",
    x: "diamond",
    hackernews: "circle",
    github: "circle",
    stackoverflow: "circle",
    mastodon: "square",
  };
  const PLATFORM_LABEL = {
    reddit: "Reddit",
    youtube: "YouTube",
    x: "X",
    hackernews: "Hacker News",
    github: "GitHub",
    stackoverflow: "Stack Overflow",
    mastodon: "Mastodon",
  };

  function platformShapeSuffix(platform) {
    const shape = PLATFORM_SHAPE[platform];
    return shape && shape !== "circle" ? ` (${shape}-shaped)` : "";
  }

  // Every real, working platform this dashboard can search, plus the ones that
  // exist only as a picker entry -- an interview talking point for what a
  // partner-API-funded version would add next. Neither list is a Platform enum
  // value on the backend for the "coming soon" entries; they're never sent to
  // POST /api/collect.
  const REAL_SOURCES = [
    "reddit",
    "youtube",
    "hackernews",
    "stackoverflow",
    "github",
    "x",
    "mastodon",
  ];
  const COMING_SOON_SOURCES = [
    { key: "discord", label: "Discord" },
    { key: "linkedin", label: "LinkedIn" },
    { key: "tiktok", label: "TikTok" },
    { key: "threads", label: "Threads" },
  ];

  // -- tooltip -----------------------------------------------------------

  const tooltip = document.getElementById("chart-tooltip");

  function showTooltipForElement(el, lines) {
    tooltip.textContent = "";
    lines.forEach(([label, value]) => {
      if (value === undefined || value === null || value === "") return;
      const row = document.createElement("div");
      const span = document.createElement("span");
      span.textContent = label + ": ";
      const strong = document.createElement("strong");
      strong.textContent = String(value);
      row.appendChild(span);
      row.appendChild(strong);
      tooltip.appendChild(row);
    });

    const rect = el.getBoundingClientRect();
    tooltip.style.display = "block";
    const left = Math.min(rect.right + 8, window.innerWidth - 280);
    tooltip.style.left = Math.max(8, left) + "px";
    tooltip.style.top = rect.top + "px";
  }

  function hideTooltip() {
    tooltip.style.display = "none";
  }

  // -- shared helpers ------------------------------------------------------

  async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} failed: ${res.status}`);
    return res.json();
  }

  function formatCategory(category) {
    return category.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  }

  function formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  }

  function formatEngagement(row) {
    const parts = [];
    if (row.likes) parts.push(`👍${row.likes.toLocaleString()}`);
    if (row.comments) parts.push(`💬${row.comments.toLocaleString()}`);
    if (row.score) parts.push(`↑${row.score.toLocaleString()}`);
    if (row.shares) parts.push(`⇄${row.shares.toLocaleString()}`);
    return parts.join(" · ");
  }

  // Poster identifiers are sometimes long (a legacy hash from before
  // HASH_AUTHORS was disabled, or just a long platform user ID) -- truncate
  // for display rather than let it wrap across several lines, but keep the
  // full value reachable via the caller's title/tooltip.
  function truncateForDisplay(value, max) {
    if (!value || value.length <= max) return value;
    return value.slice(0, max) + "…";
  }

  function formatMinutes(seconds) {
    const minutes = seconds / 60;
    if (minutes < 60) return minutes.toFixed(1) + " min";
    return (minutes / 60).toFixed(1) + " hr";
  }

  function badge(meta) {
    const span = document.createElement("span");
    span.className = "badge";
    const dot = document.createElement("span");
    dot.className = "dot dot--" + meta.dot;
    const text = document.createElement("span");
    text.textContent = meta.label;
    span.appendChild(dot);
    span.appendChild(text);
    return span;
  }

  // -- clusters bar chart ---------------------------------------------------
  // One series (alert_count) across nominal category:model groups -- a single
  // hue for every bar, per the dataviz method (color re-encoding an already
  // axis-labeled identity is the anti-pattern, not the goal).

  // Cached so the footprint graph's hub detail panel (rendered on click, long
  // after this loads) can look up a hub's recurrence/brief data by
  // cluster_key without a second round trip -- /api/clusters is small enough
  // to just keep the last-loaded copy around.
  let clusterSummaries = [];

  async function loadClusters() {
    const container = document.getElementById("clusters-chart");
    try {
      clusterSummaries = await fetchJSON("/api/clusters");
      renderClusters(container, clusterSummaries);
    } catch (err) {
      renderEmpty(container, "Failed to load clusters.");
    }
  }

  function renderClusters(container, clusters) {
    container.textContent = "";
    if (!clusters.length) {
      renderEmpty(container, "No clusters yet -- run `radar score` first.");
      return;
    }

    const max = Math.max(...clusters.map((c) => c.alert_count));

    clusters.forEach((c) => {
      const row = document.createElement("div");
      row.className = "bar-row";

      const label = document.createElement("div");
      label.className = "bar-label";
      label.textContent = c.label;
      label.title = c.label;
      if (c.episode_count > 1) {
        const recurring = document.createElement("span");
        recurring.className = "recurring-tag";
        recurring.textContent = `recurring ×${c.episode_count}`;
        label.appendChild(document.createTextNode(" "));
        label.appendChild(recurring);
      }
      if (c.protection_tier === "flagship") {
        const flagship = document.createElement("span");
        flagship.className = "flagship-tag";
        flagship.textContent = "flagship";
        label.appendChild(document.createTextNode(" "));
        label.appendChild(flagship);
      }

      const value = document.createElement("div");
      value.className = "bar-value";
      value.textContent = String(c.alert_count);

      const track = document.createElement("div");
      track.className = "bar-track";
      const fill = document.createElement("div");
      fill.className = "bar-fill";
      const pct = Math.max((c.alert_count / max) * 100, 4);
      fill.style.width = pct + "%";

      const hit = document.createElement("div");
      hit.className = "bar-hit";
      hit.tabIndex = 0;
      const tip = () => {
        const lines = [
          ["Cluster", c.label],
          ["Alerts", c.alert_count],
          ["Max severity", (SEVERITY_META[c.max_severity] || {}).label || c.max_severity],
          ["Latest", formatDate(c.latest_triggered_at)],
        ];
        if (c.episode_count > 1) {
          lines.push(["Recurred", `${c.episode_count} times since ${formatDate(c.first_triggered_at)}`]);
        }
        if (c.protection_tier === "flagship") {
          lines.push(["Protection tier", "Flagship"]);
        }
        lines.push(["Example", c.representative_issue_summary]);
        showTooltipForElement(hit, lines);
      };
      hit.addEventListener("mouseenter", tip);
      hit.addEventListener("mousemove", tip);
      hit.addEventListener("focus", tip);
      hit.addEventListener("mouseleave", hideTooltip);
      hit.addEventListener("blur", hideTooltip);

      track.appendChild(fill);
      track.appendChild(hit);
      row.appendChild(label);
      row.appendChild(value);
      row.appendChild(track);
      container.appendChild(row);
    });
  }

  function renderEmpty(container, message) {
    container.textContent = "";
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = message;
    container.appendChild(p);
  }

  // -- lead-time stat tile + distribution sparkline --------------------------

  async function loadLeadTime() {
    const container = document.getElementById("leadtime-stat");
    try {
      const data = await fetchJSON("/api/lead-time");
      renderLeadTime(container, data);
    } catch (err) {
      renderEmpty(container, "Failed to load lead-time stats.");
    }
  }

  function renderLeadTime(container, data) {
    container.textContent = "";

    const value = document.createElement("p");
    value.className = "stat-value";
    value.textContent =
      data.median_lead_time_seconds != null ? formatMinutes(data.median_lead_time_seconds) : "n/a";

    const label = document.createElement("p");
    label.className = "stat-label";
    label.textContent = `Median lead time — ${data.posts_caught_early} of ${data.posts_with_both_passes} posts caught early`;

    container.appendChild(value);
    container.appendChild(label);

    const values = data.lead_times_seconds || [];
    if (values.length > 1) {
      container.appendChild(buildSparkline(values.map((v) => v / 60)));
    } else {
      const p = document.createElement("p");
      p.className = "empty-state";
      p.textContent = "Not enough data yet for a distribution.";
      container.appendChild(p);
    }
  }

  // -- spam/ads-filtered stat tile --------------------------------------------

  async function loadAdStats() {
    const container = document.getElementById("ads-stat");
    try {
      const data = await fetchJSON("/api/stats");
      renderAdStats(container, data);
    } catch (err) {
      renderEmpty(container, "Failed to load ad-filtering stats.");
    }
  }

  function renderAdStats(container, data) {
    container.textContent = "";

    const value = document.createElement("p");
    value.className = "stat-value";
    value.textContent = String(data.ads_filtered);

    const label = document.createElement("p");
    label.className = "stat-label";
    label.textContent =
      data.ads_filtered === 1 ? "post excluded as spam/ads" : "posts excluded as spam/ads";

    container.appendChild(value);
    container.appendChild(label);
  }

  function buildSparkline(values) {
    const w = 300;
    const h = 40;
    const pad = 4;
    const max = Math.max(...values);
    const min = Math.min(...values);
    const range = max - min || 1;

    const points = values.map((v, i) => {
      const x = pad + (i / (values.length - 1 || 1)) * (w - pad * 2);
      const y = h - pad - ((v - min) / range) * (h - pad * 2);
      return [x, y];
    });
    const d = points.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "sparkline");
    svg.setAttribute("role", "img");
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `Distribution of lead times across ${values.length} posts caught early, sorted ascending`;
    svg.appendChild(title);

    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("class", "spark-line");
    path.setAttribute("d", d);
    svg.appendChild(path);
    return svg;
  }

  // -- cross-platform footprint graph ----------------------------------------
  // Hub = root-cause cluster (category:model_implicated), satellite = one post's
  // latest alert. Color = platform (a genuine identity encoding here, unlike the
  // bar chart above). Radius scales with *area*, not radius, proportional to
  // velocity -- the standard bubble-chart correction for how size is perceived.

  function clusterKeyFor(alert) {
    return alert.category + ":" + alert.model_implicated;
  }

  function clusterLabelFor(alert) {
    return formatCategory(alert.category) + " — " + alert.model_implicated.replace(/_/g, " ");
  }

  function velocityRadius(velocity) {
    const value = Math.max(0, velocity || 0);
    const radius = 6 + Math.sqrt(value) * 2.2;
    return Math.min(Math.max(radius, 6), 28);
  }

  // Watching posts have no velocity yet -- radius is reserved for a real
  // magnitude (velocity), so give these a fixed, small, non-competing size.
  const WATCHING_RADIUS = 7;

  // Hubs carry a persistent radiating text label that satellites don't --
  // clamping only the node's own radius to the canvas bounds still lets that
  // label run past the SVG edge and get clipped. `n.labelWidth` (measured via
  // getBBox() once the label is in the live DOM -- see renderFootprintGraph)
  // gives an exact per-hub margin instead of guessing a single fixed number
  // that's either too small for long labels or wasteful for short ones. Used
  // as a uniform margin in both x and y since a label can radiate in any
  // direction depending on the hub's position in the ring.
  const FALLBACK_HUB_LABEL_MARGIN = 160;

  function nodeMargin(n) {
    if (n.type !== "hub") return n.r;
    const labelReach = n.labelWidth != null ? n.labelWidth : FALLBACK_HUB_LABEL_MARGIN;
    return n.r + 16 + labelReach;
  }

  function clampToBounds(value, margin, max) {
    return Math.min(Math.max(value, margin), max - margin);
  }

  function tickForceSimulation(nodes, edges, width, height) {
    const repulsionStrength = 90;
    const springStrength = 0.02;
    const springLength = 85;
    const centerStrength = 0.006;
    const damping = 0.85;

    for (let a = 0; a < nodes.length; a++) {
      for (let b = a + 1; b < nodes.length; b++) {
        const na = nodes[a];
        const nb = nodes[b];
        const dx = na.x - nb.x;
        const dy = na.y - nb.y;
        const distSq = Math.max(dx * dx + dy * dy, 1);
        const dist = Math.sqrt(distSq);
        // Scaled by combined radius so bigger circles (hubs, high-velocity
        // satellites) command proportionally more personal space.
        const force = (repulsionStrength * (na.r + nb.r)) / distSq;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        na.vx += fx;
        na.vy += fy;
        nb.vx -= fx;
        nb.vy -= fy;
      }
    }

    edges.forEach(({ source, target }) => {
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const diff = dist - springLength;
      const fx = (dx / dist) * diff * springStrength;
      const fy = (dy / dist) * diff * springStrength;
      source.vx += fx;
      source.vy += fy;
      target.vx -= fx;
      target.vy -= fy;
    });

    nodes.forEach((n) => {
      n.vx += (width / 2 - n.x) * centerStrength;
      n.vy += (height / 2 - n.y) * centerStrength;
    });

    let totalMovement = 0;
    nodes.forEach((n) => {
      // A dragged (or drag-released) node is pinned -- it still pushes/pulls
      // its neighbors via the forces above, but its own position is driven
      // directly by the pointer, not physics.
      if (n.fixed) {
        n.vx = 0;
        n.vy = 0;
        return;
      }
      n.vx *= damping;
      n.vy *= damping;
      n.x += n.vx;
      n.y += n.vy;
      const margin = nodeMargin(n);
      n.x = clampToBounds(n.x, margin, width);
      n.y = clampToBounds(n.y, margin, height);
      totalMovement += Math.abs(n.vx) + Math.abs(n.vy);
    });

    return totalMovement;
  }

  // Full fetched set (unfiltered) -- kept around so the legend/category chips
  // can always offer every platform/category ever seen, even ones currently
  // toggled off, and so toggling doesn't require a re-fetch.
  let footprintAllMembers = [];
  // null = show everything; a Set means "only these are visible."
  let footprintPlatformFilter = null;
  let footprintCategoryFilter = null;
  let footprintClientFilter = null;

  const ALL_CATEGORIES = [
    "api_abuse",
    "product_bug",
    "ux_confusion",
    "messaging_gap",
    "credential_theft",
    "abuse",
    "safety",
    "other",
  ];

  const ALL_MODELS = [
    "claude_opus",
    "claude_sonnet",
    "claude_haiku",
    "claude_fable",
    "claude_api_general",
    "claude_code",
    "other_llm",
    "not_applicable",
    "unknown",
  ];

  async function loadFootprintGraph() {
    const container = document.getElementById("footprint-graph");
    const legend = document.getElementById("footprint-legend");
    try {
      const [alerts, watching] = await Promise.all([
        fetchJSON("/api/alerts"),
        fetchJSON("/api/watching"),
      ]);
      // Merge both into one member list so a cluster's cross-platform spread is
      // visible even before any single post has crossed the velocity threshold --
      // scored alerts and watching (not-yet-scored) pain points are visually
      // distinguished per-node, not filtered into separate graphs.
      footprintAllMembers = [
        ...alerts.map((a) => ({ ...a, status: "alert" })),
        ...watching.map((w) => ({ ...w, status: "watching" })),
      ];
      renderFootprintPlatformFilter();
      renderFootprintCategoryFilter();
      renderFootprintClientFilter();
      applyFootprintFilters();
    } catch (err) {
      renderEmpty(container, "Failed to load the footprint graph.");
    }
  }

  function renderFootprintCategoryFilter() {
    const container = document.getElementById("footprint-category-filter");
    container.textContent = "";
    const label = document.createElement("span");
    label.className = "legend-group-label";
    label.textContent = "Category:";
    container.appendChild(label);
    const categoriesPresent = ALL_CATEGORIES.filter((cat) =>
      footprintAllMembers.some((m) => m.category === cat)
    );
    categoriesPresent.forEach((cat) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "category-filter-chip";
      const isActive = !footprintCategoryFilter || footprintCategoryFilter.has(cat);
      chip.classList.toggle("is-active", isActive);
      chip.textContent = formatCategory(cat);
      chip.addEventListener("click", () => {
        // Independent per-chip toggle, same model as the platform legend --
        // starts from "everything on," each click hides/shows just that one
        // category. Materializing the full set on the first click (rather
        // than starting from an empty one) is what makes "toggle off" the
        // correct first-click behavior instead of "toggle on."
        if (!footprintCategoryFilter) {
          footprintCategoryFilter = new Set(categoriesPresent);
        }
        if (footprintCategoryFilter.has(cat)) {
          footprintCategoryFilter.delete(cat);
        } else {
          footprintCategoryFilter.add(cat);
        }
        if (footprintCategoryFilter.size === categoriesPresent.length) {
          footprintCategoryFilter = null; // back to "everything on"
        }
        renderFootprintCategoryFilter();
        applyFootprintFilters();
      });
      chip.title = `Isolate ${formatCategory(cat)}: double-click`;
      chip.addEventListener("dblclick", (evt) => {
        evt.preventDefault();
        footprintCategoryFilter = new Set([cat]);
        renderFootprintCategoryFilter();
        applyFootprintFilters();
      });
      container.appendChild(chip);
    });
  }

  // A program manager watching a handful of enterprise clients wants to cut
  // straight to that client's posts, ignoring generic noise -- same chip
  // mechanism as the category filter above. Hidden entirely (not just empty)
  // when nothing in the current data is client-scoped, since most configs
  // won't have any watched clients set up.
  function renderFootprintClientFilter() {
    const container = document.getElementById("footprint-client-filter");
    container.textContent = "";
    const clientsPresent = Array.from(
      new Set(footprintAllMembers.map((m) => m.client).filter(Boolean))
    ).sort();
    container.hidden = clientsPresent.length === 0;
    if (!clientsPresent.length) return;

    const label = document.createElement("span");
    label.className = "legend-group-label";
    label.textContent = "Client:";
    container.appendChild(label);
    clientsPresent.forEach((clientName) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "category-filter-chip";
      const isActive = !footprintClientFilter || footprintClientFilter.has(clientName);
      chip.classList.toggle("is-active", isActive);
      chip.textContent = clientName;
      chip.addEventListener("click", () => {
        if (!footprintClientFilter) {
          footprintClientFilter = new Set(clientsPresent);
        }
        if (footprintClientFilter.has(clientName)) {
          footprintClientFilter.delete(clientName);
        } else {
          footprintClientFilter.add(clientName);
        }
        if (footprintClientFilter.size === clientsPresent.length) {
          footprintClientFilter = null;
        }
        renderFootprintClientFilter();
        applyFootprintFilters();
      });
      chip.title = `Isolate ${clientName}: double-click`;
      chip.addEventListener("dblclick", (evt) => {
        evt.preventDefault();
        footprintClientFilter = new Set([clientName]);
        renderFootprintClientFilter();
        applyFootprintFilters();
      });
      container.appendChild(chip);
    });
  }

  // Lives in the filter row above the graph (not the legend below it) --
  // it's an interactive control, not a color key, so it belongs with the
  // category chips rather than split across both ends of the card.
  function renderFootprintPlatformFilter() {
    const container = document.getElementById("footprint-platform-filter");
    container.textContent = "";
    const label = document.createElement("span");
    label.className = "legend-group-label";
    label.textContent = "Platform:";
    container.appendChild(label);

    // Enumerated from the *full* unfiltered set, not whatever's currently
    // visible -- otherwise toggling a platform off would make its own
    // checkbox (and the only way to toggle it back on) disappear along with
    // its nodes.
    const platformsPresent = Array.from(
      new Set(footprintAllMembers.map((m) => m.platform))
    ).sort();
    platformsPresent.forEach((p) => {
      const item = document.createElement("label");
      const shape = PLATFORM_SHAPE[p] || "circle";
      const isActive = !footprintPlatformFilter || footprintPlatformFilter.has(p);
      item.className =
        "legend-item legend-item--toggle" + (isActive ? "" : " legend-item--inactive");

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = isActive;
      checkbox.className = "legend-checkbox";

      const swatch = document.createElement("span");
      swatch.className = "legend-swatch" + (shape !== "circle" ? ` legend-swatch--${shape}` : "");
      swatch.style.background = `var(${PLATFORM_COLOR_VAR[p] || "--text-muted"})`;
      const text = document.createElement("span");
      // Only 4 hues are validated as mutually CVD-safe and non-red/orange/green
      // (see dashboard.css); platforms beyond that share a hue and are
      // shape-coded instead -- called out here so the checkbox explains why
      // two entries can have the same color swatch.
      text.textContent = (PLATFORM_LABEL[p] || p) + platformShapeSuffix(p);

      item.appendChild(checkbox);
      item.appendChild(swatch);
      item.appendChild(text);
      item.title = `Show only ${PLATFORM_LABEL[p] || p}: double-click`;

      checkbox.addEventListener("change", () => {
        if (!footprintPlatformFilter) {
          footprintPlatformFilter = new Set(platformsPresent);
        }
        if (checkbox.checked) {
          footprintPlatformFilter.add(p);
        } else {
          footprintPlatformFilter.delete(p);
        }
        if (footprintPlatformFilter.size === platformsPresent.length) {
          footprintPlatformFilter = null;
        }
        renderFootprintPlatformFilter();
        applyFootprintFilters();
      });
      // Double-click: "show only this one" -- e.g. "choose GitHub, only
      // those are shown" -- without unchecking every other platform by hand.
      item.addEventListener("dblclick", (evt) => {
        evt.preventDefault();
        footprintPlatformFilter = new Set([p]);
        renderFootprintPlatformFilter();
        applyFootprintFilters();
      });
      container.appendChild(item);
    });
  }

  function applyFootprintFilters() {
    const container = document.getElementById("footprint-graph");
    const legend = document.getElementById("footprint-legend");
    const filtered = footprintAllMembers.filter((m) => {
      if (footprintPlatformFilter && !footprintPlatformFilter.has(m.platform)) return false;
      if (footprintCategoryFilter && !footprintCategoryFilter.has(m.category)) return false;
      if (footprintClientFilter && !footprintClientFilter.has(m.client)) return false;
      return true;
    });
    renderFootprintGraph(container, legend, filtered);
  }

  function renderFootprintGraph(container, legendContainer, members) {
    container.textContent = "";
    legendContainer.textContent = "";
    const detailPanel = document.getElementById("footprint-detail");
    detailPanel.textContent = "";
    const detailHint = document.createElement("p");
    detailHint.className = "empty-state";
    detailHint.textContent = "Click a node for details. Drag any node to reposition it.";
    detailPanel.appendChild(detailHint);

    if (!members.length) {
      renderEmpty(container, "No classified pain points yet -- run `radar classify` first.");
      return;
    }

    const width = Math.max(container.clientWidth || 900, 300);
    const height = 920;

    const clusters = new Map();
    members.forEach((m) => {
      const key = clusterKeyFor(m);
      if (!clusters.has(key)) {
        clusters.set(key, { key, label: clusterLabelFor(m), members: [] });
      }
      clusters.get(key).members.push(m);
    });
    const clusterList = Array.from(clusters.values());

    const nodes = [];
    const edges = [];
    clusterList.forEach((cluster, ci) => {
      const angle = (ci / clusterList.length) * Math.PI * 2;
      const hub = {
        type: "hub",
        key: cluster.key,
        label: cluster.label,
        platformCount: new Set(cluster.members.map((m) => m.platform)).size,
        memberCount: cluster.members.length,
        alertCount: cluster.members.filter((m) => m.status === "alert").length,
        protectionTier: (clusterSummaries.find((c) => c.cluster_key === cluster.key) || {}).protection_tier,
        r: 22,
        x: width / 2 + Math.cos(angle) * (width * 0.28),
        y: height / 2 + Math.sin(angle) * (height * 0.28),
        vx: 0,
        vy: 0,
      };
      nodes.push(hub);

      cluster.members.forEach((member, mi) => {
        const spread = (mi - (cluster.members.length - 1) / 2) * 0.35;
        const isAlert = member.status === "alert";
        const satellite = {
          type: "satellite",
          member,
          isAlert,
          r: isAlert ? velocityRadius(member.velocity) : WATCHING_RADIUS,
          x: hub.x + Math.cos(angle + spread) * 60,
          y: hub.y + Math.sin(angle + spread) * 60,
          vx: 0,
          vy: 0,
        };
        nodes.push(satellite);
        edges.push({ source: hub, target: satellite });
      });
    });

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent =
      "Root-cause clusters with each member post colored by platform; solid and sized by " +
      "velocity for scored alerts, hollow for classified posts not yet scored; alerts whose " +
      "incident is still open (unacknowledged) flash a red ring";
    svg.appendChild(title);

    const edgeGroup = document.createElementNS(SVG_NS, "g");
    const nodeGroup = document.createElementNS(SVG_NS, "g");
    svg.appendChild(edgeGroup);
    svg.appendChild(nodeGroup);
    container.appendChild(svg);

    const edgeEls = edges.map(() => {
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("class", "graph-edge");
      edgeGroup.appendChild(line);
      return line;
    });

    // Clicking a node populates `detailPanel` (declared above) with a
    // persistent, readable detail card -- unlike the hover tooltip, it survives
    // after the pointer moves away, and it's where a real platform name + "View
    // post" link live instead of a raw post_id.
    function buildSamePosterSection(matches) {
      const section = document.createElement("div");
      section.className = "same-poster-section";

      const heading = document.createElement("h4");
      heading.textContent = `Also posted by this poster (${matches.length})`;
      section.appendChild(heading);

      const list = document.createElement("ul");
      list.className = "same-poster-list";
      matches.forEach((other) => {
        const m = other.member;
        const li = document.createElement("li");
        li.tabIndex = 0;

        const label = document.createElement("span");
        label.className = "same-poster-label";
        label.textContent = `${PLATFORM_LABEL[m.platform] || m.platform} · ${formatCategory(m.category)}`;
        li.appendChild(label);

        const severity = document.createElement("span");
        severity.className = "same-poster-severity";
        severity.textContent = (SEVERITY_META[m.severity] || {}).label || m.severity;
        li.appendChild(severity);

        li.addEventListener("click", () => renderDetail(other));
        li.addEventListener("keydown", (evt) => {
          if (evt.key === "Enter" || evt.key === " ") {
            evt.preventDefault();
            renderDetail(other);
          }
        });

        list.appendChild(li);
      });
      section.appendChild(list);
      return section;
    }

    function renderDetail(n) {
      // Same-poster rings are toggled per-selection, not per-node-creation --
      // always clear before deciding whether to re-apply below.
      nodes.forEach((o) => {
        if (o.posterRingEl) o.posterRingEl.classList.remove("is-visible");
      });

      detailPanel.textContent = "";
      const heading = document.createElement("h3");
      const dl = document.createElement("dl");

      function addRow(term, value, fullTitle) {
        if (value === undefined || value === null || value === "") return;
        const dt = document.createElement("dt");
        dt.textContent = term;
        const dd = document.createElement("dd");
        dd.textContent = value;
        if (fullTitle) dd.title = fullTitle;
        dl.appendChild(dt);
        dl.appendChild(dd);
      }

      let sameAuthorNodes = [];
      const clusterSummary = n.type === "hub" ? clusterSummaries.find((c) => c.cluster_key === n.key) : null;
      if (n.type === "hub") {
        heading.textContent = n.label;
        addRow("Platforms", n.platformCount);
        addRow("Posts", n.memberCount);
        addRow(
          "Status",
          n.alertCount > 0
            ? `⚠ ${n.alertCount} active alert${n.alertCount === 1 ? "" : "s"}`
            : "No active alerts yet"
        );
        if (n.protectionTier === "flagship") {
          addRow("Protection tier", "Flagship");
        }
        if (clusterSummary && clusterSummary.episode_count > 1) {
          addRow(
            "Recurred",
            `${clusterSummary.episode_count} times since ${formatDate(clusterSummary.first_triggered_at)}`
          );
        }
      } else {
        const m = n.member;
        heading.textContent = PLATFORM_LABEL[m.platform] || m.platform;
        addRow("Status", n.isAlert ? "Alert (scored)" : "Watching (not yet scored)");
        addRow("Severity", (SEVERITY_META[m.severity] || {}).label || m.severity);
        if (n.isAlert) {
          addRow("Velocity", m.velocity.toFixed(1));
          addRow(
            "Incident",
            (INCIDENT_META[m.incident_status] || {}).label || m.incident_status,
            m.incident_status === "open" ? "Flashing because nobody has acknowledged this yet." : undefined
          );
        }
        addRow("Category", formatCategory(m.category));
        addRow("Matched term", m.matched_term);
        addRow("Client", m.client);
        addRow("Poster", truncateForDisplay(m.author, 32), m.author);
        addRow("Posted", formatDate(m.created_at));
        addRow("Engagement", formatEngagement(m));
        addRow("Summary", m.issue_summary);

        if (m.author) {
          sameAuthorNodes = nodes.filter(
            (other) => other.type === "satellite" && other !== n && other.member.author === m.author
          );
        }
      }

      detailPanel.appendChild(heading);
      detailPanel.appendChild(dl);

      if (n.type === "hub" && n.key) {
        const actions = document.createElement("div");
        actions.className = "detail-actions";

        const briefBtn = document.createElement("button");
        briefBtn.type = "button";
        briefBtn.className = "detail-jump-link";
        briefBtn.textContent = clusterSummary && clusterSummary.brief ? "Regenerate brief" : "Generate brief";
        actions.appendChild(briefBtn);
        detailPanel.appendChild(actions);

        const briefText = document.createElement("p");
        briefText.className = "detail-brief";
        if (clusterSummary && clusterSummary.brief) {
          briefText.textContent = clusterSummary.brief;
        }
        detailPanel.appendChild(briefText);

        briefBtn.addEventListener("click", async () => {
          briefBtn.disabled = true;
          briefBtn.textContent = "Generating…";
          try {
            const res = await fetch(`/api/clusters/${encodeURIComponent(n.key)}/brief`, { method: "POST" });
            if (!res.ok) throw new Error("brief request failed");
            const data = await res.json();
            briefText.textContent = data.brief;
            if (clusterSummary) clusterSummary.brief = data.brief;
          } catch (err) {
            briefText.textContent = "Failed to generate brief.";
          } finally {
            briefBtn.disabled = false;
            briefBtn.textContent = "Regenerate brief";
          }
        });
      }

      if (n.type === "satellite" && (n.member.url || n.member.post_id)) {
        const actions = document.createElement("div");
        actions.className = "detail-actions";

        if (n.member.url) {
          const link = document.createElement("a");
          link.href = n.member.url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.className = "post-link";
          link.textContent = "View post ↗";
          actions.appendChild(link);
        }

        if (n.member.post_id) {
          const destTab = n.isAlert ? "alerts" : "watching";
          const jumpBtn = document.createElement("button");
          jumpBtn.type = "button";
          jumpBtn.className = "detail-jump-link";
          jumpBtn.textContent = n.isAlert ? "View in Alerts tab →" : "View in Watching tab →";
          jumpBtn.addEventListener("click", () => focusPostInTable(destTab, n.member.post_id));
          actions.appendChild(jumpBtn);
        }

        if (!n.isAlert && n.member.post_id) {
          const promoteBtn = document.createElement("button");
          promoteBtn.type = "button";
          promoteBtn.textContent = "Send to Alerts";
          promoteBtn.addEventListener("click", async () => {
            promoteBtn.disabled = true;
            promoteBtn.textContent = "Sending…";
            try {
              const alert = await promoteWatchingPostToAlert(n.member.post_id);
              // Instant feedback in the still-open panel -- the node's own
              // circle (hollow -> solid) catches up once the background
              // reload below rebuilds the graph with fresh data.
              n.isAlert = true;
              n.member = { ...n.member, ...alert };
              renderDetail(n);
              loadFootprintGraph();
              loadAlerts();
            } catch (err) {
              window.alert("Failed to send this post to Alerts.");
              promoteBtn.disabled = false;
              promoteBtn.textContent = "Send to Alerts";
            }
          });
          actions.appendChild(promoteBtn);
        }

        detailPanel.appendChild(actions);
      }

      if (sameAuthorNodes.length) {
        sameAuthorNodes.forEach((other) => other.posterRingEl && other.posterRingEl.classList.add("is-visible"));
        detailPanel.appendChild(buildSamePosterSection(sameAuthorNodes));
      }
    }

    // Converts a pointer event's client (viewport) coordinates into this SVG's
    // own viewBox coordinate space -- needed because the element's rendered
    // pixel size can differ from the viewBox units it's drawn in.
    function toViewBoxPoint(clientX, clientY) {
      const rect = svg.getBoundingClientRect();
      return {
        x: ((clientX - rect.left) / rect.width) * width,
        y: ((clientY - rect.top) / rect.height) * height,
      };
    }

    // Shared by the main node mark and its risk ring so both stay in sync --
    // "circle" sets r; "square"/"diamond" are a rect of equivalent area,
    // rotated 45deg for diamond (rect x/y is already centered at the node's
    // local origin, so a rotate() around (0,0) is correct as-is).
    function applyShapeGeometry(el, shape, radius) {
      if (shape === "circle") {
        el.setAttribute("r", radius);
        return;
      }
      const side = radius * 1.7;
      el.setAttribute("x", -side / 2);
      el.setAttribute("y", -side / 2);
      el.setAttribute("width", side);
      el.setAttribute("height", side);
      el.setAttribute("rx", 2);
      if (shape === "diamond") {
        el.setAttribute("transform", "rotate(45)");
      }
    }

    const nodeEls = nodes.map((n) => {
      const g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", n.type === "hub" ? "graph-hub" : "graph-node");

      const nodeShape = n.type === "satellite" ? PLATFORM_SHAPE[n.member.platform] || "circle" : "circle";
      const fill = document.createElementNS(SVG_NS, nodeShape === "circle" ? "circle" : "rect");
      fill.setAttribute(
        "class",
        n.type === "hub" ? "hub-fill" + (n.alertCount > 0 ? " hub-fill--has-alert" : "") : "node-fill"
      );
      applyShapeGeometry(fill, nodeShape, n.r);
      if (n.type === "satellite") {
        const colorVar = PLATFORM_COLOR_VAR[n.member.platform] || "--text-muted";
        if (n.isAlert) {
          // Solid fill = a real, scored alert.
          fill.style.fill = `var(${colorVar})`;
        } else {
          // Hollow, platform-colored outline = classified but not yet scored
          // (Watching) -- present without overstating it as a confirmed alert.
          fill.style.fill = "none";
          fill.style.stroke = `var(${colorVar})`;
          fill.style.strokeWidth = "2";
        }
      }

      // Risk outline: a second ring outside the platform-colored node,
      // independent of the fill/stroke above -- so severity reads the same
      // way on both solid (alert) and hollow (watching) nodes without
      // fighting platform color for the same channel. Low severity gets no
      // ring at all (nothing to flag); this is why the platform palette
      // above was rebuilt to avoid red/orange -- a risk ring must never be
      // mistakable for "that's just this platform's color."
      if (n.type === "satellite" && n.member.severity && n.member.severity !== "low") {
        const isHighRisk = n.member.severity === "high";
        const riskRing = document.createElementNS(SVG_NS, nodeShape === "circle" ? "circle" : "rect");
        riskRing.setAttribute(
          "class",
          "node-risk-ring" + (isHighRisk ? " node-risk-ring--high" : " node-risk-ring--med")
        );
        applyShapeGeometry(riskRing, nodeShape, n.r + 3);
        g.appendChild(riskRing);
      }

      // Same-poster highlight: a dashed neutral-ink ring outside the risk
      // ring so it never visually merges with it -- toggled on/off by
      // renderDetail(), not recreated per click. Pattern + neutral color is
      // orthogonal to both the platform-hue and risk-color channels, which
      // are already fully committed and must stay legible alongside this.
      if (n.type === "satellite") {
        const posterRing = document.createElementNS(SVG_NS, nodeShape === "circle" ? "circle" : "rect");
        posterRing.setAttribute("class", "node-poster-ring");
        applyShapeGeometry(posterRing, nodeShape, n.r + 6);
        g.appendChild(posterRing);
        n.posterRingEl = posterRing;
      }

      // Flashes an alert that still needs a human to start on it -- see
      // transition_incident() / INCIDENT_META in the Alerts tab. Drawn
      // outside the risk/poster rings (r+9) so all three stay legible at
      // once on a node that happens to be high-severity, same-poster-linked,
      // AND still unaddressed.
      if (n.type === "satellite" && n.isAlert && n.member.incident_status === "open") {
        const needsActionRing = document.createElementNS(SVG_NS, nodeShape === "circle" ? "circle" : "rect");
        needsActionRing.setAttribute("class", "node-needs-action-ring");
        applyShapeGeometry(needsActionRing, nodeShape, n.r + 9);
        g.appendChild(needsActionRing);
      }

      g.appendChild(fill);

      const hit = document.createElementNS(SVG_NS, "circle");
      hit.setAttribute("class", "hit-area");
      hit.setAttribute("r", Math.max(n.r, 12));
      hit.setAttribute("tabindex", "0");
      const showTip = () => {
        if (n.type === "hub") {
          showTooltipForElement(g, [
            ["Cluster", n.label],
            ["Platforms", n.platformCount],
            ["Posts", n.memberCount],
            [
              "Status",
              n.alertCount > 0
                ? `⚠ ${n.alertCount} active alert${n.alertCount === 1 ? "" : "s"}`
                : "No active alerts yet",
            ],
          ]);
        } else {
          const m = n.member;
          const lines = [
            ["Platform", PLATFORM_LABEL[m.platform] || m.platform],
            ["Status", n.isAlert ? "Alert (scored)" : "Watching (not yet scored)"],
            ["Severity", (SEVERITY_META[m.severity] || {}).label || m.severity],
          ];
          if (n.isAlert) {
            lines.push(["Velocity", m.velocity.toFixed(1)]);
          }
          lines.push(
            ["Category", formatCategory(m.category)],
            ["Matched term", m.matched_term],
            ["Summary", m.issue_summary]
          );
          showTooltipForElement(g, lines);
        }
      };
      hit.addEventListener("mouseenter", showTip);
      hit.addEventListener("focus", showTip);
      hit.addEventListener("mouseleave", hideTooltip);
      hit.addEventListener("blur", hideTooltip);

      // Drag to reposition (stays pinned where dropped); a plain click (no
      // movement) opens the persistent detail panel instead.
      let drag = null;
      const CLICK_MOVE_THRESHOLD = 4;

      hit.addEventListener("pointerdown", (evt) => {
        evt.preventDefault();
        hit.setPointerCapture(evt.pointerId);
        drag = { moved: false, startX: evt.clientX, startY: evt.clientY };
        n.fixed = true;
        g.classList.add("is-dragging");
        wake();
      });

      hit.addEventListener("pointermove", (evt) => {
        if (!drag) return;
        if (
          Math.abs(evt.clientX - drag.startX) > CLICK_MOVE_THRESHOLD ||
          Math.abs(evt.clientY - drag.startY) > CLICK_MOVE_THRESHOLD
        ) {
          drag.moved = true;
        }
        const p = toViewBoxPoint(evt.clientX, evt.clientY);
        const margin = nodeMargin(n);
        n.x = clampToBounds(p.x, margin, width);
        n.y = clampToBounds(p.y, margin, height);
        draw();
      });

      const endDrag = () => {
        if (!drag) return;
        g.classList.remove("is-dragging");
        if (!drag.moved) {
          renderDetail(n);
        }
        drag = null;
      };
      hit.addEventListener("pointerup", endDrag);
      hit.addEventListener("pointercancel", endDrag);

      g.appendChild(hit);

      if (n.type === "hub") {
        const label = document.createElementNS(SVG_NS, "text");
        label.textContent = n.label;
        g.appendChild(label);
        n.labelEl = label;
      }

      nodeGroup.appendChild(g);
      return g;
    });

    // Measured *after* every node is attached to the live SVG (getBBox()
    // needs real layout) -- label strings vary a lot in length ("Safety —
    // unknown" vs. "Credential theft — claude api general"), so a fixed
    // guessed margin either clips the long ones or wastes space on the short
    // ones. Each hub gets exactly the clearance its own label needs.
    nodes.forEach((n) => {
      if (n.type === "hub" && n.labelEl) {
        const bbox = n.labelEl.getBBox();
        n.labelWidth = bbox.width;
        n.labelHeight = bbox.height;
      }
    });

    const hubNodes = nodes.filter((n) => n.type === "hub" && n.labelEl);

    // A label's natural resting spot: radiating straight outward from the
    // graph center (rather than a fixed direction) so labels fan out instead
    // of all colliding in the middle of a ring of hubs. This is a *target*,
    // not the final position -- separateLabels() below nudges labels away
    // from this spot only when another label is actually crowding it.
    function idealLabelOffset(n) {
      const dx = n.x - width / 2;
      const dy = n.y - height / 2;
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const ux = dx / dist;
      const uy = dy / dist;
      const labelDist = n.r + 16;
      return { x: ux * labelDist, y: uy * labelDist, ux };
    }

    // Keeps every hub's label readable even when hubs themselves sit close
    // together: each label springs back toward its natural radiate-out spot,
    // but a small pairwise repulsion pushes apart any two labels whose text
    // boxes actually overlap -- the same "repel, then spring back" shape as
    // the node physics above, just scoped to text boxes instead of circles.
    function separateLabels() {
      const pullStrength = 0.12;
      const padding = 4;

      hubNodes.forEach((n) => {
        const target = idealLabelOffset(n);
        if (n.labelOffsetX == null) {
          n.labelOffsetX = target.x;
          n.labelOffsetY = target.y;
        }
        n.labelOffsetX += (target.x - n.labelOffsetX) * pullStrength;
        n.labelOffsetY += (target.y - n.labelOffsetY) * pullStrength;
        n.labelTextAnchorUx = target.ux;
      });

      for (let a = 0; a < hubNodes.length; a++) {
        for (let b = a + 1; b < hubNodes.length; b++) {
          const na = hubNodes[a];
          const nb = hubNodes[b];
          const ax = na.x + na.labelOffsetX;
          const ay = na.y + na.labelOffsetY;
          const bx = nb.x + nb.labelOffsetX;
          const by = nb.y + nb.labelOffsetY;
          const halfWidthA = (na.labelWidth || 0) / 2 + padding;
          const halfWidthB = (nb.labelWidth || 0) / 2 + padding;
          const halfHeightA = (na.labelHeight || 14) / 2;
          const halfHeightB = (nb.labelHeight || 14) / 2;
          const dx = bx - ax;
          const dy = by - ay;
          const overlapX = halfWidthA + halfWidthB - Math.abs(dx);
          const overlapY = halfHeightA + halfHeightB - Math.abs(dy);
          if (overlapX > 0 && overlapY > 0) {
            // Push apart along whichever axis has the smaller overlap --
            // the minimum-translation direction that resolves the collision.
            if (overlapX < overlapY) {
              const push = (overlapX / 2) * (dx >= 0 ? 1 : -1);
              na.labelOffsetX -= push;
              nb.labelOffsetX += push;
            } else {
              const push = (overlapY / 2) * (dy >= 0 ? 1 : -1);
              na.labelOffsetY -= push;
              nb.labelOffsetY += push;
            }
          }
        }
      }
    }

    function draw() {
      edges.forEach((e, i) => {
        edgeEls[i].setAttribute("x1", e.source.x);
        edgeEls[i].setAttribute("y1", e.source.y);
        edgeEls[i].setAttribute("x2", e.target.x);
        edgeEls[i].setAttribute("y2", e.target.y);
      });
      nodes.forEach((n, i) => {
        nodeEls[i].setAttribute("transform", `translate(${n.x}, ${n.y})`);
      });
      separateLabels();
      hubNodes.forEach((n) => {
        n.labelEl.setAttribute("x", n.labelOffsetX);
        n.labelEl.setAttribute("y", n.labelOffsetY);
        n.labelEl.setAttribute(
          "text-anchor",
          n.labelTextAnchorUx >= 0.15 ? "start" : n.labelTextAnchorUx <= -0.15 ? "end" : "middle"
        );
        n.labelEl.setAttribute("dominant-baseline", "middle");
      });
    }

    draw();

    let ticks = 0;
    const maxTicks = 400;
    let rafScheduled = false;
    function step() {
      const movement = tickForceSimulation(nodes, edges, width, height);
      draw();
      ticks += 1;
      if (movement > 0.5 && ticks < maxTicks) {
        requestAnimationFrame(step);
      } else {
        rafScheduled = false;
      }
    }
    // Restarts the settle loop -- needed because it normally stops once the
    // layout is still, and a drag can disturb an already-settled graph.
    function wake() {
      if (rafScheduled) return;
      rafScheduled = true;
      ticks = 0;
      requestAnimationFrame(step);
    }
    wake();

    // Style key: solid vs. hollow means alert vs. watching, independent of platform --
    // grouped and labeled separately so the neutral swatch doesn't read as a platform color.
    const statusGroup = document.createElement("div");
    statusGroup.className = "legend-group";
    const statusGroupLabel = document.createElement("span");
    statusGroupLabel.className = "legend-group-label";
    statusGroupLabel.textContent = "Status:";
    statusGroup.appendChild(statusGroupLabel);

    const styleKey = [
      { hollow: false, label: "Alert (scored)" },
      { hollow: true, label: "Watching (not yet scored)" },
    ];
    styleKey.forEach(({ hollow, label }) => {
      const item = document.createElement("span");
      item.className = "legend-item";
      const swatch = document.createElement("span");
      swatch.className = hollow ? "legend-swatch legend-swatch--hollow" : "legend-swatch";
      if (!hollow) swatch.style.background = "var(--text-muted)";
      const text = document.createElement("span");
      text.textContent = label;
      item.appendChild(swatch);
      item.appendChild(text);
      statusGroup.appendChild(item);
    });

    // A hub is never a status color itself (it can span platforms), but it does
    // get a red ring the moment any of its members is a real alert -- flagged
    // here explicitly so that ring is never the only thing carrying the meaning.
    const hubAlertItem = document.createElement("span");
    hubAlertItem.className = "legend-item";
    const hubAlertSwatch = document.createElement("span");
    hubAlertSwatch.className = "legend-swatch legend-swatch--hub-alert";
    const hubAlertText = document.createElement("span");
    hubAlertText.textContent = "Hub with an active alert";
    hubAlertItem.appendChild(hubAlertSwatch);
    hubAlertItem.appendChild(hubAlertText);
    statusGroup.appendChild(hubAlertItem);

    legendContainer.appendChild(statusGroup);

    // A ring around a satellite, independent of its platform color/fill --
    // present only for med/high severity so a calm (low-severity) node stays
    // uncluttered.
    const riskGroup = document.createElement("div");
    riskGroup.className = "legend-group";
    const riskGroupLabel = document.createElement("span");
    riskGroupLabel.className = "legend-group-label";
    riskGroupLabel.textContent = "Risk:";
    riskGroup.appendChild(riskGroupLabel);

    [
      { cls: "legend-swatch--risk-med", label: "Danger territory (med severity)" },
      { cls: "legend-swatch--risk-high", label: "Full risk (high severity)" },
    ].forEach(({ cls, label }) => {
      const item = document.createElement("span");
      item.className = "legend-item";
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch legend-swatch--hollow " + cls;
      const text = document.createElement("span");
      text.textContent = label;
      item.appendChild(swatch);
      item.appendChild(text);
      riskGroup.appendChild(item);
    });
    legendContainer.appendChild(riskGroup);

    // A flashing outer ring on an alert whose incident is still "open" --
    // nobody has acknowledged it yet. Its own group since it's a workflow
    // signal (independent of severity/platform), not a content property.
    const actionGroup = document.createElement("div");
    actionGroup.className = "legend-group";
    const actionGroupLabel = document.createElement("span");
    actionGroupLabel.className = "legend-group-label";
    actionGroupLabel.textContent = "Action:";
    actionGroup.appendChild(actionGroupLabel);

    const actionItem = document.createElement("span");
    actionItem.className = "legend-item";
    const actionSwatch = document.createElement("span");
    actionSwatch.className = "legend-swatch legend-swatch--hollow legend-swatch--needs-action";
    const actionText = document.createElement("span");
    actionText.textContent = "Needs addressing (open incident)";
    actionItem.appendChild(actionSwatch);
    actionItem.appendChild(actionText);
    actionGroup.appendChild(actionItem);
    legendContainer.appendChild(actionGroup);
  }

  // -- sortable-table helpers (shared by Watching and Alerts) -----------------

  const SEVERITY_RANK = { high: 3, med: 2, low: 1 };

  // The lifecycle column sorts by its meaningful order (declaration order of
  // INCIDENT_META above, e.g. open before resolved), not alphabetically --
  // derived from the same object literal the badge already renders from, so
  // there's one source of truth for "what order do these statuses go in."
  const INCIDENT_RANK = Object.fromEntries(Object.keys(INCIDENT_META).map((k, i) => [k, i]));

  // A single sortable number for the Engagement column's multi-metric
  // display (👍/💬/↑/⇄ are different units on different platforms -- this
  // isn't meant to be a precise cross-platform score, just "more reactions
  // of any kind sorts higher").
  function engagementTotal(r) {
    return (r.likes || 0) + (r.comments || 0) + (r.score || 0) + (r.shares || 0);
  }

  function applySort(rows, sortState, accessors) {
    if (!sortState.key) return rows;
    const accessor = accessors[sortState.key];
    if (!accessor) return rows;
    return [...rows].sort((a, b) => {
      const va = accessor(a);
      const vb = accessor(b);
      if (va < vb) return -1 * sortState.dir;
      if (va > vb) return 1 * sortState.dir;
      return 0;
    });
  }

  function updateSortIndicators(containerSelector, sortState) {
    document.querySelectorAll(containerSelector + " th[data-sort-key]").forEach((th) => {
      const indicator = th.querySelector(".sort-indicator");
      if (!indicator) return;
      indicator.textContent =
        th.dataset.sortKey === sortState.key ? (sortState.dir === 1 ? "▲" : "▼") : "";
    });
  }

  // Headers are static markup (only <tbody> gets rebuilt per render), so this
  // only needs to run once at init, not after every re-render.
  function initSortableHeaders(containerSelector, sortState, onChange) {
    document.querySelectorAll(containerSelector + " th[data-sort-key]").forEach((th) => {
      th.tabIndex = 0;
      const indicator = document.createElement("span");
      indicator.className = "sort-indicator";
      th.appendChild(indicator);

      const activate = () => {
        const key = th.dataset.sortKey;
        if (sortState.key === key) {
          sortState.dir *= -1;
        } else {
          sortState.key = key;
          sortState.dir = 1;
        }
        updateSortIndicators(containerSelector, sortState);
        onChange();
      };
      th.addEventListener("click", activate);
      th.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter" || evt.key === " ") {
          evt.preventDefault();
          activate();
        }
      });
    });
  }

  // -- watching (classified pain points with no alert yet) --------------------

  const WATCHING_SORT_ACCESSORS = {
    platform: (r) => (PLATFORM_LABEL[r.platform] || r.platform || "").toLowerCase(),
    matched_term: (r) => (r.matched_term || "").toLowerCase(),
    category: (r) => formatCategory(r.category).toLowerCase(),
    severity: (r) => SEVERITY_RANK[r.severity] || 0,
    summary: (r) => (r.issue_summary || "").toLowerCase(),
    poster: (r) => (r.author || "").toLowerCase(),
    posted: (r) => r.created_at || "",
    engagement: (r) => engagementTotal(r),
  };

  let watchingAllRows = [];
  const watchingSort = { key: null, dir: 1 };

  function currentWatchingFilters() {
    return {
      platform: document.getElementById("filter-watching-platform").value,
      category: document.getElementById("filter-watching-category").value,
      severity: document.getElementById("filter-watching-severity").value,
    };
  }

  // Filtering/sorting happens entirely client-side against the already-loaded
  // set -- the Watching list is small enough (order of hundreds) that a
  // dedicated filtered server query isn't worth the extra endpoint surface.
  function applyWatchingView() {
    const tbody = document.getElementById("watching-body");
    const filters = currentWatchingFilters();
    let rows = watchingAllRows.filter((r) => {
      if (filters.platform && r.platform !== filters.platform) return false;
      if (filters.category && r.category !== filters.category) return false;
      if (filters.severity && r.severity !== filters.severity) return false;
      return true;
    });
    rows = applySort(rows, watchingSort, WATCHING_SORT_ACCESSORS);
    renderWatching(tbody, rows, watchingAllRows.length);
  }

  async function loadWatching() {
    const tbody = document.getElementById("watching-body");
    try {
      watchingAllRows = await fetchJSON("/api/watching");
      applyWatchingView();
    } catch (err) {
      tbody.textContent = "";
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 10;
      td.className = "empty-state";
      td.textContent = "Failed to load.";
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  // Shared with the footprint graph's own "Send to Alerts" button
  // (renderFootprintGraph's node-detail panel, above) -- same endpoint, same
  // manual-escalation meaning, just triggered from a different view.
  async function promoteWatchingPostToAlert(postId) {
    const res = await fetch(`/api/watching/${encodeURIComponent(postId)}/promote`, { method: "POST" });
    if (!res.ok) throw new Error("promote request failed");
    return res.json();
  }

  function renderWatching(tbody, rows, totalCount) {
    const count = document.getElementById("watching-count");
    count.textContent =
      totalCount === rows.length
        ? `${rows.length} pain point${rows.length === 1 ? "" : "s"} classified, not yet scored`
        : `${rows.length} of ${totalCount} pain points classified, not yet scored (filtered)`;

    tbody.textContent = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 10;
      td.className = "empty-state";
      td.textContent = "Nothing waiting -- every classified pain point has either alerted or been ruled out.";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    rows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.dataset.postId = row.post_id;

      const platformTd = document.createElement("td");
      platformTd.textContent = PLATFORM_LABEL[row.platform] || row.platform || "—";

      const matchedTermTd = document.createElement("td");
      matchedTermTd.textContent = row.matched_term || "—";

      const categoryTd = document.createElement("td");
      categoryTd.textContent = formatCategory(row.category);

      const severityTd = document.createElement("td");
      severityTd.appendChild(badge(SEVERITY_META[row.severity] || { dot: "muted", label: row.severity }));

      const summaryTd = document.createElement("td");
      summaryTd.className = "summary-cell";
      summaryTd.textContent = row.issue_summary;

      const posterTd = document.createElement("td");
      posterTd.className = "poster-cell";
      posterTd.textContent = truncateForDisplay(row.author, 24) || "—";
      if (row.author) posterTd.title = row.author;

      const postedTd = document.createElement("td");
      postedTd.textContent = formatDate(row.created_at) || "—";

      const engagementTd = document.createElement("td");
      engagementTd.textContent = formatEngagement(row) || "—";

      const postTd = document.createElement("td");
      if (row.url) {
        const link = document.createElement("a");
        link.href = row.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "post-link";
        link.textContent = "View post";
        postTd.appendChild(link);
      }

      const actionsTd = document.createElement("td");
      const rowActions = document.createElement("div");
      rowActions.className = "actions";
      const escalateBtn = document.createElement("button");
      escalateBtn.type = "button";
      escalateBtn.title = "Manually send this post to the Alerts tab, even though it hasn't crossed the velocity threshold on its own.";
      escalateBtn.textContent = "Escalate";
      escalateBtn.addEventListener("click", async () => {
        escalateBtn.disabled = true;
        escalateBtn.textContent = "Sending…";
        try {
          await promoteWatchingPostToAlert(row.post_id);
          loadAllDashboardData();
        } catch (err) {
          window.alert("Failed to send this post to Alerts.");
          escalateBtn.disabled = false;
          escalateBtn.textContent = "Escalate";
        }
      });
      rowActions.appendChild(escalateBtn);
      actionsTd.appendChild(rowActions);

      tr.appendChild(platformTd);
      tr.appendChild(matchedTermTd);
      tr.appendChild(categoryTd);
      tr.appendChild(severityTd);
      tr.appendChild(summaryTd);
      tr.appendChild(posterTd);
      tr.appendChild(postedTd);
      tr.appendChild(engagementTd);
      tr.appendChild(postTd);
      tr.appendChild(actionsTd);
      tbody.appendChild(tr);
    });
  }

  // -- alerts table -----------------------------------------------------------

  const ALERTS_SORT_ACCESSORS = {
    platform: (r) => (PLATFORM_LABEL[r.platform] || r.platform || "").toLowerCase(),
    matched_term: (r) => (r.matched_term || "").toLowerCase(),
    category: (r) => formatCategory(r.category).toLowerCase(),
    severity: (r) => SEVERITY_RANK[r.severity] || 0,
    velocity: (r) => r.velocity,
    incident: (r) => INCIDENT_RANK[r.incident_status] ?? 0,
    claimed: (r) => (r.claimed_by || "").toLowerCase(),
    summary: (r) => (r.issue_summary || "").toLowerCase(),
    poster: (r) => (r.author || "").toLowerCase(),
    posted: (r) => r.created_at || "",
    engagement: (r) => engagementTotal(r),
  };

  let alertsAllRows = [];
  const alertsSort = { key: null, dir: 1 };

  function currentFilters() {
    return {
      category: document.getElementById("filter-category").value,
      severity: document.getElementById("filter-severity").value,
      platform: document.getElementById("filter-alerts-platform").value,
    };
  }

  // Category/severity are server-side filters (existing /api/alerts params);
  // platform is applied client-side against the fetched set below, alongside
  // sorting, without needing a new server-side query param.
  function applyAlertsView() {
    const tbody = document.getElementById("alerts-body");
    const { platform } = currentFilters();
    let rows = platform ? alertsAllRows.filter((a) => a.platform === platform) : alertsAllRows;
    rows = applySort(rows, alertsSort, ALERTS_SORT_ACCESSORS);
    renderAlerts(tbody, rows);
    // The Board tab is a PM's personal workspace, scoped to what they've
    // claimed -- unlike the Alerts table above, unfiltered by platform/sort
    // since it's a small, already-focused set grouped by column instead.
    renderAlertsBoard(document.getElementById("alerts-board"), alertsAllRows.filter((a) => a.claimed_by));
  }

  async function loadAlerts() {
    const tbody = document.getElementById("alerts-body");
    tbody.classList.add("is-loading");

    const filters = currentFilters();
    const params = new URLSearchParams();
    if (filters.category) params.set("category", filters.category);
    if (filters.severity) params.set("severity", filters.severity);

    try {
      alertsAllRows = await fetchJSON("/api/alerts?" + params.toString());
      applyAlertsView();
    } catch (err) {
      tbody.textContent = "";
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 13;
      td.className = "empty-state";
      td.textContent = "Failed to load alerts.";
      tr.appendChild(td);
      tbody.appendChild(tr);
    } finally {
      tbody.classList.remove("is-loading");
    }
  }

  // Tracks which alert's detail row is open across re-renders (every action
  // inside it -- transition/brief/report -- reloads the whole table, same as
  // the existing approve/reject flow) so it re-opens instead of collapsing.
  let expandedAlertPostId = null;

  function renderAlerts(tbody, alerts) {
    tbody.textContent = "";

    if (!alerts.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 13;
      td.className = "empty-state";
      td.textContent = "No alerts match these filters.";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    alerts.forEach((a) => {
      const tr = document.createElement("tr");
      tr.dataset.postId = a.post_id;

      const platformTd = document.createElement("td");
      platformTd.textContent = PLATFORM_LABEL[a.platform] || a.platform || "—";

      const matchedTermTd = document.createElement("td");
      matchedTermTd.textContent = a.matched_term || "—";

      const categoryTd = document.createElement("td");
      categoryTd.textContent = formatCategory(a.category);

      const severityTd = document.createElement("td");
      severityTd.appendChild(badge(SEVERITY_META[a.severity] || { dot: "muted", label: a.severity }));

      const velocityTd = document.createElement("td");
      velocityTd.className = "num";
      velocityTd.textContent = a.velocity.toFixed(1);

      const incidentTd = document.createElement("td");
      incidentTd.appendChild(
        badge(INCIDENT_META[a.incident_status] || { dot: "muted", label: a.incident_status })
      );

      const claimedTd = document.createElement("td");
      if (a.claimed_by) {
        const claimedTag = document.createElement("span");
        claimedTag.className = "action-logged-tag";
        claimedTag.textContent = a.claimed_by;
        claimedTd.appendChild(claimedTag);
      } else {
        const unclaimedTag = document.createElement("span");
        unclaimedTag.className = "unclaimed-tag";
        unclaimedTag.textContent = "Unclaimed";
        claimedTd.appendChild(unclaimedTag);
      }

      const summaryTd = document.createElement("td");
      summaryTd.className = "summary-cell";
      summaryTd.textContent = a.issue_summary;

      const posterTd = document.createElement("td");
      posterTd.className = "poster-cell";
      posterTd.textContent = truncateForDisplay(a.author, 24) || "—";
      if (a.author) posterTd.title = a.author;

      const postedTd = document.createElement("td");
      postedTd.textContent = formatDate(a.created_at) || "—";

      const engagementTd = document.createElement("td");
      engagementTd.textContent = formatEngagement(a) || "—";

      const postTd = document.createElement("td");
      if (a.url) {
        const link = document.createElement("a");
        link.href = a.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "post-link";
        link.textContent = "View post";
        postTd.appendChild(link);
      }

      const actionsTd = document.createElement("td");
      const actions = document.createElement("div");
      actions.className = "actions";

      const claimBtn = document.createElement("button");
      claimBtn.type = "button";
      if (a.claimed_by) {
        claimBtn.textContent = "Release";
        claimBtn.addEventListener("click", async () => {
          claimBtn.disabled = true;
          await releaseAlert(a.post_id);
          claimBtn.disabled = false;
        });
      } else {
        claimBtn.textContent = "Claim";
        claimBtn.addEventListener("click", async () => {
          const name = currentClaimingAs();
          if (!name) return;
          claimBtn.disabled = true;
          await claimAlert(a.post_id, name);
          claimBtn.disabled = false;
        });
      }
      actions.appendChild(claimBtn);

      const detailsBtn = document.createElement("button");
      detailsBtn.type = "button";
      detailsBtn.setAttribute("aria-expanded", "false");
      detailsBtn.textContent = "Details ▾";
      detailsBtn.addEventListener("click", () => toggleAlertDetail(tr, a, detailsBtn));
      actions.appendChild(detailsBtn);
      actionsTd.appendChild(actions);

      tr.appendChild(platformTd);
      tr.appendChild(matchedTermTd);
      tr.appendChild(categoryTd);
      tr.appendChild(severityTd);
      tr.appendChild(velocityTd);
      tr.appendChild(incidentTd);
      tr.appendChild(claimedTd);
      tr.appendChild(summaryTd);
      tr.appendChild(posterTd);
      tr.appendChild(postedTd);
      tr.appendChild(engagementTd);
      tr.appendChild(postTd);
      tr.appendChild(actionsTd);
      tbody.appendChild(tr);

      if (a.post_id === expandedAlertPostId) {
        openAlertDetail(tr, a, detailsBtn);
      }
    });
  }

  // -- claim workflow (Alerts tab -> Board tab) --------------------------------
  // No auth system exists yet (a real login is a future direction) -- claim
  // identity is just a freeform name, persisted locally so it doesn't need
  // retyping on every claim.

  const CLAIMING_AS_KEY = "radar_claiming_as";

  function initClaimingAsInput() {
    const input = document.getElementById("claiming-as-input");
    input.value = localStorage.getItem(CLAIMING_AS_KEY) || "";
    input.addEventListener("input", () => localStorage.setItem(CLAIMING_AS_KEY, input.value));
  }

  function currentClaimingAs() {
    const input = document.getElementById("claiming-as-input");
    let name = input.value.trim();
    if (!name) {
      name = (window.prompt("Claiming as (your name):", "") || "").trim();
      if (name) {
        input.value = name;
        localStorage.setItem(CLAIMING_AS_KEY, name);
      }
    }
    return name;
  }

  async function claimAlert(postId, claimedBy) {
    try {
      const res = await fetch(`/api/alerts/${encodeURIComponent(postId)}/claim`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ claimed_by: claimedBy }),
      });
      if (!res.ok) throw new Error("claim request failed");
      await loadAlerts();
    } catch (err) {
      window.alert("Failed to claim this alert.");
    }
  }

  async function releaseAlert(postId) {
    try {
      const res = await fetch(`/api/alerts/${encodeURIComponent(postId)}/release`, { method: "POST" });
      if (!res.ok) throw new Error("release request failed");
      await loadAlerts();
    } catch (err) {
      window.alert("Failed to release this alert.");
    }
  }

  // -- incident detail row (lifecycle, timeline, exec brief, report) ----------

  function downloadTextFile(filename, content, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function copyToClipboard(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      const original = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => {
        btn.textContent = original;
      }, 1500);
    } catch (err) {
      window.alert("Failed to copy to clipboard.");
    }
  }

  async function transitionIncident(postId, status, note) {
    try {
      const res = await fetch(`/api/alerts/${encodeURIComponent(postId)}/transition`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, note: note || null }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error((body && body.detail) || "transition request failed");
      }
      await loadAlerts();
      return true;
    } catch (err) {
      window.alert(err.message || "Failed to update incident status.");
      return false;
    }
  }

  async function logAlertAction(postId, actionItem, note) {
    try {
      const res = await fetch(`/api/alerts/${encodeURIComponent(postId)}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action_item: actionItem, note: note || null }),
      });
      if (!res.ok) throw new Error("action request failed");
      await loadAlerts();
      return true;
    } catch (err) {
      window.alert("Failed to log action.");
      return false;
    }
  }

  async function fetchAlertActions(postId) {
    try {
      return await fetchJSON(`/api/alerts/${encodeURIComponent(postId)}/actions`);
    } catch (err) {
      return null;
    }
  }

  function renderAlertActionHistory(actions, listEl) {
    listEl.textContent = "";
    if (!actions || !actions.length) {
      const li = document.createElement("li");
      li.className = "empty-state";
      li.textContent = actions === null ? "Failed to load actions." : "No actions logged yet.";
      listEl.appendChild(li);
      return;
    }
    actions.forEach((a) => {
      const li = document.createElement("li");
      li.textContent = `${formatDate(a.created_at)} — ${a.action_label}${a.note ? ": " + a.note : ""}`;
      listEl.appendChild(li);
    });
  }

  async function loadIncidentTimeline(postId, listEl) {
    try {
      const events = await fetchJSON(`/api/alerts/${encodeURIComponent(postId)}/timeline`);
      listEl.textContent = "";
      if (!events.length) {
        const li = document.createElement("li");
        li.className = "empty-state";
        li.textContent = "No status changes yet.";
        listEl.appendChild(li);
        return;
      }
      events.forEach((e) => {
        const li = document.createElement("li");
        const label = (INCIDENT_META[e.to_status] || {}).label || e.to_status;
        li.textContent = `${formatDate(e.created_at)} — ${label}${e.note ? ": " + e.note : ""}`;
        listEl.appendChild(li);
      });
    } catch (err) {
      listEl.textContent = "";
      const li = document.createElement("li");
      li.className = "empty-state";
      li.textContent = "Failed to load timeline.";
      listEl.appendChild(li);
    }
  }

  function buildIncidentDetailPanel(alert) {
    const wrapper = document.createElement("div");
    wrapper.className = "incident-detail-panel";

    // -- lifecycle --
    const lifecycle = document.createElement("div");
    lifecycle.className = "incident-section";
    const lifecycleHeading = document.createElement("h4");
    lifecycleHeading.textContent = "Incident";
    lifecycle.appendChild(lifecycleHeading);
    lifecycle.appendChild(
      badge(INCIDENT_META[alert.incident_status] || { dot: "muted", label: alert.incident_status })
    );

    const nextActions = INCIDENT_NEXT_ACTIONS[alert.incident_status] || [];
    if (nextActions.length) {
      const noteInput = document.createElement("textarea");
      noteInput.className = "incident-note-input";
      noteInput.rows = 2;
      noteInput.placeholder = "Optional note for this transition...";
      lifecycle.appendChild(noteInput);

      const actionRow = document.createElement("div");
      actionRow.className = "actions";
      nextActions.forEach(({ status, label }) => {
        const btn = document.createElement("button");
        btn.type = "button";
        if (status === "resolved") btn.className = "approve";
        btn.textContent = label;
        if (status === "resolved" && !(alert.action_count > 0)) {
          btn.disabled = true;
          btn.title = "Log at least one action before resolving this alert.";
        }
        btn.addEventListener("click", () => transitionIncident(alert.post_id, status, noteInput.value.trim()));
        actionRow.appendChild(btn);
      });
      lifecycle.appendChild(actionRow);
    }
    wrapper.appendChild(lifecycle);

    // -- timeline --
    const timeline = document.createElement("div");
    timeline.className = "incident-section";
    const timelineHeading = document.createElement("h4");
    timelineHeading.textContent = "Timeline";
    timeline.appendChild(timelineHeading);
    const timelineList = document.createElement("ul");
    timelineList.className = "incident-timeline";
    timeline.appendChild(timelineList);
    wrapper.appendChild(timeline);
    loadIncidentTimeline(alert.post_id, timelineList);

    // -- actions taken --
    const actionsSection = document.createElement("div");
    actionsSection.className = "incident-section";
    const actionsHeading = document.createElement("h4");
    actionsHeading.textContent = "Actions taken";
    actionsSection.appendChild(actionsHeading);

    const actionNoteInput = document.createElement("textarea");
    actionNoteInput.className = "incident-note-input";
    actionNoteInput.rows = 2;
    actionNoteInput.placeholder = "Optional note (defaults to the current recommendation)...";
    actionNoteInput.value = alert.coa || "";
    actionsSection.appendChild(actionNoteInput);

    const checklist = document.createElement("div");
    checklist.className = "action-checklist";
    actionsSection.appendChild(checklist);

    const actionsList = document.createElement("ul");
    actionsList.className = "incident-timeline";
    actionsSection.appendChild(actionsList);
    wrapper.appendChild(actionsSection);

    async function refreshActions() {
      const actions = await fetchAlertActions(alert.post_id);
      renderAlertActionHistory(actions, actionsList);
      const loggedItems = new Set((actions || []).map((a) => a.action_label));

      checklist.textContent = "";
      (alert.action_items || []).forEach((item) => {
        const row = document.createElement("div");
        row.className = "action-checklist-row";
        const label = document.createElement("span");
        label.textContent = item;
        row.appendChild(label);
        if (loggedItems.has(item)) {
          const tag = document.createElement("span");
          tag.className = "action-logged-tag";
          tag.textContent = "Logged";
          row.appendChild(tag);
        } else {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = "Log";
          btn.addEventListener("click", async () => {
            btn.disabled = true;
            const ok = await logAlertAction(alert.post_id, item, actionNoteInput.value.trim());
            if (ok) {
              alert.action_count = (alert.action_count || 0) + 1;
              await refreshActions();
            }
            btn.disabled = false;
          });
          row.appendChild(btn);
        }
        checklist.appendChild(row);
      });
    }
    refreshActions();

    // -- exec brief --
    const briefSection = document.createElement("div");
    briefSection.className = "incident-section";
    const briefHeading = document.createElement("h4");
    briefHeading.textContent = "Executive brief";
    briefSection.appendChild(briefHeading);
    const briefBtn = document.createElement("button");
    briefBtn.type = "button";
    briefBtn.textContent = alert.exec_brief ? "Regenerate brief" : "Generate brief";
    briefSection.appendChild(briefBtn);
    const briefText = document.createElement("p");
    briefText.className = "detail-brief";
    briefText.textContent = alert.exec_brief || "";
    briefSection.appendChild(briefText);
    briefBtn.addEventListener("click", async () => {
      briefBtn.disabled = true;
      briefBtn.textContent = "Generating…";
      try {
        const res = await fetch(`/api/alerts/${encodeURIComponent(alert.post_id)}/brief`, { method: "POST" });
        if (!res.ok) throw new Error("brief request failed");
        const data = await res.json();
        briefText.textContent = data.brief;
        alert.exec_brief = data.brief;
      } catch (err) {
        briefText.textContent = "Failed to generate brief.";
      } finally {
        briefBtn.disabled = false;
        briefBtn.textContent = "Regenerate brief";
      }
    });
    wrapper.appendChild(briefSection);

    // -- post-incident report --
    const reportSection = document.createElement("div");
    reportSection.className = "incident-section";
    const reportHeading = document.createElement("h4");
    reportHeading.textContent = "Post-incident report";
    reportSection.appendChild(reportHeading);

    const closingNoteInput = document.createElement("textarea");
    closingNoteInput.className = "incident-note-input";
    closingNoteInput.rows = 2;
    closingNoteInput.placeholder = "What should change so the next escalation is easier?";
    reportSection.appendChild(closingNoteInput);

    const reportActions = document.createElement("div");
    reportActions.className = "actions";
    const generateReportBtn = document.createElement("button");
    generateReportBtn.type = "button";
    generateReportBtn.textContent = alert.incident_report ? "Regenerate report" : "Generate report";
    reportActions.appendChild(generateReportBtn);
    reportSection.appendChild(reportActions);

    const reportOutput = document.createElement("pre");
    reportOutput.className = "incident-report-output";
    reportOutput.textContent = alert.incident_report || "";
    reportSection.appendChild(reportOutput);

    const reportFileActions = document.createElement("div");
    reportFileActions.className = "actions";
    reportFileActions.hidden = !alert.incident_report;
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.textContent = "Copy";
    copyBtn.addEventListener("click", () => copyToClipboard(reportOutput.textContent, copyBtn));
    const downloadBtn = document.createElement("button");
    downloadBtn.type = "button";
    downloadBtn.textContent = "Download .md";
    downloadBtn.addEventListener("click", () =>
      downloadTextFile(`${alert.post_id}-incident-report.md`, reportOutput.textContent, "text/markdown")
    );
    reportFileActions.appendChild(copyBtn);
    reportFileActions.appendChild(downloadBtn);
    reportSection.appendChild(reportFileActions);

    generateReportBtn.addEventListener("click", async () => {
      generateReportBtn.disabled = true;
      generateReportBtn.textContent = "Generating…";
      try {
        const res = await fetch(`/api/alerts/${encodeURIComponent(alert.post_id)}/report`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            closing_note: closingNoteInput.value.trim() || "No closing note provided.",
          }),
        });
        if (!res.ok) throw new Error("report request failed");
        const data = await res.json();
        reportOutput.textContent = data.report_markdown;
        alert.incident_report = data.report_markdown;
        reportFileActions.hidden = false;
      } catch (err) {
        reportOutput.textContent = "Failed to generate report.";
      } finally {
        generateReportBtn.disabled = false;
        generateReportBtn.textContent = "Regenerate report";
      }
    });
    wrapper.appendChild(reportSection);

    return wrapper;
  }

  function openAlertDetail(tr, alert, detailsBtn) {
    expandedAlertPostId = alert.post_id;
    detailsBtn.textContent = "Details ▴";
    detailsBtn.setAttribute("aria-expanded", "true");
    const detailTr = document.createElement("tr");
    detailTr.className = "alert-detail-row";
    const td = document.createElement("td");
    td.colSpan = 13;
    td.appendChild(buildIncidentDetailPanel(alert));
    detailTr.appendChild(td);
    tr.after(detailTr);
  }

  function closeAlertDetail(tr, detailsBtn) {
    expandedAlertPostId = null;
    detailsBtn.textContent = "Details ▾";
    detailsBtn.setAttribute("aria-expanded", "false");
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("alert-detail-row")) next.remove();
  }

  function toggleAlertDetail(tr, alert, detailsBtn) {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("alert-detail-row")) {
      closeAlertDetail(tr, detailsBtn);
    } else {
      openAlertDetail(tr, alert, detailsBtn);
    }
  }

  // -- Kanban board (Alerts tab) ------------------------------------------------
  // Columns are the same incident_status values the table's Details row
  // already transitions through -- the board is just a second way to
  // trigger the same transitionIncident() call, via drag-and-drop instead of
  // a button click.

  const BOARD_COLUMNS = ["open", "acknowledged", "mitigating", "resolved", "false_positive"];

  function openAlertCardModal(alert) {
    const modal = document.getElementById("alert-card-modal");
    const body = document.getElementById("alert-card-modal-body");
    body.textContent = "";
    body.appendChild(buildIncidentDetailPanel(alert));
    modal.hidden = false;
  }

  function closeAlertCardModal() {
    document.getElementById("alert-card-modal").hidden = true;
  }

  function renderAlertCard(alert) {
    const card = document.createElement("div");
    card.className = "board-card";
    card.draggable = true;
    card.dataset.postId = alert.post_id;
    card.tabIndex = 0;

    const badges = document.createElement("div");
    badges.className = "board-card-badges";
    badges.appendChild(badge(SEVERITY_META[alert.severity] || { dot: "muted", label: alert.severity }));
    const platformSpan = document.createElement("span");
    platformSpan.className = "board-card-platform";
    platformSpan.textContent = PLATFORM_LABEL[alert.platform] || alert.platform;
    badges.appendChild(platformSpan);
    card.appendChild(badges);

    const category = document.createElement("div");
    category.className = "board-card-category";
    category.textContent = formatCategory(alert.category);
    if (alert.client) category.textContent += ` · ${alert.client}`;
    card.appendChild(category);

    const summary = document.createElement("p");
    summary.className = "board-card-summary";
    summary.textContent = alert.issue_summary;
    card.appendChild(summary);

    const coaBox = document.createElement("p");
    coaBox.className = "board-card-coa";
    coaBox.textContent = alert.coa || "";
    card.appendChild(coaBox);

    const actionsRow = document.createElement("div");
    actionsRow.className = "board-card-actions";
    const primaryItem = (alert.action_items && alert.action_items[0]) || "Log action taken";
    const logActionBtn = document.createElement("button");
    logActionBtn.type = "button";
    logActionBtn.textContent = primaryItem;
    logActionBtn.addEventListener("click", async (evt) => {
      evt.stopPropagation();
      logActionBtn.disabled = true;
      await logAlertAction(alert.post_id, primaryItem, alert.coa || "");
      logActionBtn.disabled = false;
    });
    actionsRow.appendChild(logActionBtn);
    if (alert.action_count > 0) {
      const loggedTag = document.createElement("span");
      loggedTag.className = "action-logged-tag";
      loggedTag.textContent = "Action logged";
      actionsRow.appendChild(loggedTag);
    }
    card.appendChild(actionsRow);

    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "detail-jump-link";
    openBtn.textContent = "View details →";
    openBtn.addEventListener("click", (evt) => {
      evt.stopPropagation();
      openAlertCardModal(alert);
    });
    card.appendChild(openBtn);

    card.addEventListener("click", () => openAlertCardModal(alert));
    card.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter" || evt.key === " ") {
        evt.preventDefault();
        openAlertCardModal(alert);
      }
    });
    card.addEventListener("dragstart", (evt) => {
      evt.dataTransfer.setData("text/plain", alert.post_id);
      evt.dataTransfer.effectAllowed = "move";
      card.classList.add("is-dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("is-dragging"));

    return card;
  }

  async function handleBoardDrop(postId, newStatus) {
    const card = document.querySelector(`.board-card[data-post-id="${CSS.escape(postId)}"]`);
    if (card) {
      card.classList.add("board-card--moving");
      const coaBox = card.querySelector(".board-card-coa");
      if (coaBox) coaBox.textContent = "Generating recommended action…";
    }
    // Open immediately on the pre-transition data rather than waiting for the
    // transition (which includes a real Claude call to generate the COA, a
    // few real seconds) -- avoids a popup that feels laggy. Re-opens itself
    // once the fresh data lands, unless the user already closed it.
    const preTransitionAlert = alertsAllRows.find((a) => a.post_id === postId);
    if (preTransitionAlert) openAlertCardModal(preTransitionAlert);

    const success = await transitionIncident(postId, newStatus);

    const modal = document.getElementById("alert-card-modal");
    if (success && !modal.hidden) {
      const alert = alertsAllRows.find((a) => a.post_id === postId);
      if (alert) openAlertCardModal(alert);
    }
  }

  // A resolved card falls off the board 24h after its most recent move into
  // Resolved -- keeps the board focused on what's still active. Nothing is
  // deleted: the Table view (and the status filter) still shows every alert
  // regardless of age, so this is purely a Board-view declutter.
  const RESOLVED_ARCHIVE_MS = 24 * 60 * 60 * 1000;

  function isArchivedResolved(alert) {
    if (!alert.resolved_at) return false;
    return Date.now() - new Date(alert.resolved_at).getTime() > RESOLVED_ARCHIVE_MS;
  }

  function renderAlertsBoard(container, alerts) {
    container.textContent = "";
    container.className = "board";

    BOARD_COLUMNS.forEach((status) => {
      const column = document.createElement("div");
      column.className = "board-column" + (status === "false_positive" ? " board-column--muted" : "");
      column.dataset.status = status;

      const allColumnAlerts = alerts.filter((a) => (a.incident_status || "open") === status);
      const columnAlerts =
        status === "resolved" ? allColumnAlerts.filter((a) => !isArchivedResolved(a)) : allColumnAlerts;
      const archivedCount = allColumnAlerts.length - columnAlerts.length;

      const header = document.createElement("div");
      header.className = "board-column-header";
      const meta = INCIDENT_META[status] || { label: status };
      header.textContent = `${meta.label} (${columnAlerts.length})`;
      if (archivedCount > 0) header.textContent += ` · ${archivedCount} archived`;
      column.appendChild(header);

      const cardsContainer = document.createElement("div");
      cardsContainer.className = "board-column-cards";
      columnAlerts.forEach((alert) => cardsContainer.appendChild(renderAlertCard(alert)));
      column.appendChild(cardsContainer);

      column.addEventListener("dragover", (evt) => {
        evt.preventDefault();
        evt.dataTransfer.dropEffect = "move";
        column.classList.add("is-drag-over");
      });
      column.addEventListener("dragleave", () => column.classList.remove("is-drag-over"));
      column.addEventListener("drop", (evt) => {
        evt.preventDefault();
        column.classList.remove("is-drag-over");
        const postId = evt.dataTransfer.getData("text/plain");
        const dragged = alerts.find((a) => a.post_id === postId);
        if (!postId || !dragged || (dragged.incident_status || "open") === status) return;
        if (status === "resolved" && !(dragged.action_count > 0)) {
          window.alert("Log an action before resolving this alert.");
          return;
        }
        handleBoardDrop(postId, status);
      });

      container.appendChild(column);
    });
  }

  function initAlertCardModal() {
    document.getElementById("alert-card-modal-close").addEventListener("click", closeAlertCardModal);
    document.getElementById("alert-card-modal").addEventListener("click", (evt) => {
      if (evt.target.id === "alert-card-modal") closeAlertCardModal();
    });
  }

  // -- tabs -------------------------------------------------------------------

  // Shared by tab-button clicks and by code that needs to jump the user to a
  // specific tab programmatically (e.g. "view this post in the Watching tab"
  // from the footprint graph's detail panel).
  function activateTab(target) {
    document.querySelectorAll(".tab-button").forEach((b) => {
      const active = b.dataset.tab === target;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.hidden = panel.dataset.tab !== target;
    });
  }

  function initTabs() {
    document.querySelectorAll(".tab-button").forEach((button) => {
      button.addEventListener("click", () => activateTab(button.dataset.tab));
    });
  }

  // Settings' own sub-tabs (Sources/Watchlist/Escalation criteria/Model
  // protection) -- exact mirror of activateTab()/initTabs() above, but
  // namespaced to .subtab-button/.subtab-panel/data-subtab so it doesn't
  // collide with the top-level tab logic (which would otherwise hide every
  // sub-panel -- none of them have data-tab="settings" -- the moment
  // Settings itself is activated).
  function activateSettingsSubtab(target) {
    document.querySelectorAll(".subtab-button").forEach((b) => {
      const active = b.dataset.subtab === target;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".subtab-panel").forEach((panel) => {
      panel.hidden = panel.dataset.subtab !== target;
    });
  }

  function initSettingsSubtabs() {
    document.querySelectorAll(".subtab-button").forEach((button) => {
      button.addEventListener("click", () => activateSettingsSubtab(button.dataset.subtab));
    });
  }

  // Jumps to a post's row inside the Watching or Alerts tab -- used by the
  // footprint graph's detail panel so a node click can lead to the same
  // post's full row context (filters, poster, actions) instead of just the
  // compact graph tooltip. Clears that tab's filters first since a stale
  // filter could otherwise hide the very row we're trying to reveal.
  function focusPostInTable(tab, postId) {
    if (tab === "watching") {
      document.getElementById("filter-watching-platform").value = "";
      document.getElementById("filter-watching-category").value = "";
      document.getElementById("filter-watching-severity").value = "";
      applyWatchingView();
    } else {
      document.getElementById("filter-category").value = "";
      document.getElementById("filter-severity").value = "";
      document.getElementById("filter-alerts-platform").value = "";
      applyAlertsView();
    }
    activateTab(tab);

    requestAnimationFrame(() => {
      const tbody = document.getElementById(tab === "watching" ? "watching-body" : "alerts-body");
      const row = tbody.querySelector(`tr[data-post-id="${CSS.escape(postId)}"]`);
      if (!row) return;
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("row-highlight");
      // Hold at full strength long enough to actually be seen, then let the
      // 1.2s background-color transition (see dashboard.css) fade it out.
      setTimeout(() => row.classList.remove("row-highlight"), 1600);
    });
  }

  // -- source picker + live "run collection" trigger --------------------------

  function loadAllDashboardData() {
    loadClusters();
    loadLeadTime();
    loadAdStats();
    loadFootprintGraph();
    loadWatching();
    loadAlerts();
  }

  async function initSourcePicker() {
    const container = document.getElementById("source-checkboxes");
    let available = {};
    try {
      available = await fetchJSON("/api/sources");
    } catch (err) {
      // Fall back to "nothing pre-checked" -- the picker still works, it just
      // can't pre-select what's already configured.
    }

    container.textContent = "";

    REAL_SOURCES.forEach((key) => {
      const label = document.createElement("label");
      label.className = "source-checkbox";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = key;
      input.checked = Boolean(available[key]);
      label.appendChild(input);
      const text = document.createElement("span");
      text.textContent = PLATFORM_LABEL[key] || key;
      label.appendChild(text);
      container.appendChild(label);
    });

    // Cosmetic only -- disabled, never posted to /api/collect. The interview
    // talking point: these would need partner-level API access to light up.
    COMING_SOON_SOURCES.forEach(({ key, label: platformLabel }) => {
      const label = document.createElement("label");
      label.className = "source-checkbox source-checkbox--coming-soon";
      label.title = "Requires partner API access";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.disabled = true;
      label.appendChild(input);
      const text = document.createElement("span");
      text.textContent = platformLabel;
      label.appendChild(text);
      const badgeEl = document.createElement("span");
      badgeEl.className = "coming-soon-badge";
      badgeEl.textContent = "Coming soon";
      label.appendChild(badgeEl);
      container.appendChild(label);
    });
  }

  function checkedRealSources() {
    return Array.from(document.querySelectorAll("#source-checkboxes input[type=checkbox]:checked"))
      .map((el) => el.value)
      .filter(Boolean);
  }

  async function runCollection() {
    const button = document.getElementById("run-collection-btn");
    const status = document.getElementById("collection-status");
    const sources = checkedRealSources();

    if (!sources.length) {
      status.textContent = "Select at least one source first.";
      return;
    }

    button.disabled = true;
    status.textContent = "Running… (a full pass across every search term can take a while against live APIs)";

    // /api/collect runs synchronously against real, rate-limited external
    // APIs -- with several search terms x sources, a live run can genuinely
    // take minutes (politeness pacing + real retry/backoff), not just a
    // couple seconds. Rather than leave the button looking frozen with no
    // feedback, give up waiting client-side after a while; the server keeps
    // working regardless (this only stops the browser from waiting on it),
    // so the data still lands -- refreshing the tabs later will show it.
    const COLLECT_CLIENT_TIMEOUT_MS = 45000;
    const abortController = new AbortController();
    const timeoutId = setTimeout(() => abortController.abort(), COLLECT_CLIENT_TIMEOUT_MS);

    try {
      const res = await fetch("/api/collect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sources }),
        signal: abortController.signal,
      });
      clearTimeout(timeoutId);
      if (!res.ok) throw new Error("collect request failed");
      const result = await res.json();

      const succeeded = result.sources_run.filter((s) => !result.sources_failed.includes(s));
      const label = (s) => PLATFORM_LABEL[s] || s;

      const parts = [`Collected ${result.snapshots_written} new snapshot row${result.snapshots_written === 1 ? "" : "s"}.`];
      if (succeeded.length) {
        parts.push(`Ran: ${succeeded.map(label).join(", ")}.`);
      }
      if (result.sources_skipped_unconfigured.length) {
        parts.push(`Skipped (not configured): ${result.sources_skipped_unconfigured.map(label).join(", ")}.`);
      }
      if (result.sources_failed.length) {
        // A live source can fail (timeout, rate limit) independently of the
        // others -- this is reported, not hidden, rather than surfacing as a
        // generic request failure.
        parts.push(`Failed (see server logs): ${result.sources_failed.map(label).join(", ")}.`);
      }
      status.textContent = parts.join(" ");

      loadAllDashboardData();
    } catch (err) {
      clearTimeout(timeoutId);
      if (err.name === "AbortError") {
        status.textContent =
          "Still running on the server after 45s (live APIs, several search terms) -- " +
          "it'll finish on its own; refresh the tabs in a bit to see the new data.";
      } else {
        status.textContent = "Collection failed -- check server logs.";
      }
    } finally {
      button.disabled = false;
    }
  }

  async function runClassify() {
    const button = document.getElementById("run-classify-btn");
    const status = document.getElementById("classify-status");

    button.disabled = true;
    status.textContent = "Classifying… (up to a couple minutes for a full batch against the live API)";

    // Same client-side-timeout tradeoff as runCollection() above: a live batch
    // of Claude calls (rate-limited pacing + real latency) can run longer than
    // feels responsive to wait on in the browser, but the server keeps working
    // regardless -- this only stops the browser from waiting on it.
    const CLASSIFY_CLIENT_TIMEOUT_MS = 45000;
    const abortController = new AbortController();
    const timeoutId = setTimeout(() => abortController.abort(), CLASSIFY_CLIENT_TIMEOUT_MS);

    try {
      const res = await fetch("/api/classify", { method: "POST", signal: abortController.signal });
      clearTimeout(timeoutId);
      if (!res.ok) throw new Error("classify request failed");
      const result = await res.json();

      status.textContent = result.skipped
        ? "Skipped -- no ANTHROPIC_API_KEY configured."
        : `Classified ${result.posts_classified} post${result.posts_classified === 1 ? "" : "s"}.`;

      loadAllDashboardData();
    } catch (err) {
      clearTimeout(timeoutId);
      if (err.name === "AbortError") {
        status.textContent =
          "Still running on the server after 45s -- it'll finish on its own; refresh the tabs in a bit.";
      } else {
        status.textContent = "Classification failed -- check server logs.";
      }
    } finally {
      button.disabled = false;
    }
  }

  // -- automatic collection schedule (Settings tab, Sources sub-tab) ----------

  async function loadScheduleCard() {
    const checkbox = document.getElementById("schedule-enabled-checkbox");
    const intervalInput = document.getElementById("schedule-interval-input");
    const lastChecked = document.getElementById("schedule-last-checked");
    try {
      const schedule = await fetchJSON("/api/schedule");
      checkbox.checked = schedule.enabled;
      intervalInput.value = schedule.interval_seconds / 3600;
      lastChecked.textContent = schedule.last_collected_at
        ? `Last checked: ${formatDate(schedule.last_collected_at)}`
        : "Last checked: never yet.";
    } catch (err) {
      lastChecked.textContent = "Failed to load the schedule.";
    }
  }

  async function saveSchedule() {
    const button = document.getElementById("save-schedule-btn");
    const status = document.getElementById("schedule-status");
    const checkbox = document.getElementById("schedule-enabled-checkbox");
    const intervalInput = document.getElementById("schedule-interval-input");

    const hours = Number(intervalInput.value);
    if (!hours || hours <= 0) {
      status.textContent = "Enter an interval greater than 0.";
      return;
    }

    button.disabled = true;
    status.textContent = "Saving…";
    try {
      const res = await fetch("/api/schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: checkbox.checked, interval_seconds: Math.round(hours * 3600) }),
      });
      if (!res.ok) throw new Error("schedule save failed");
      status.textContent = "Saved -- takes effect within a few seconds, no restart needed.";
    } catch (err) {
      status.textContent = "Failed to save the schedule.";
    } finally {
      button.disabled = false;
    }
  }

  // -- automatic classification schedule (Settings tab, Sources sub-tab) ------

  async function loadClassifyScheduleCard() {
    const checkbox = document.getElementById("classify-schedule-enabled-checkbox");
    const intervalInput = document.getElementById("classify-schedule-interval-input");
    const lastChecked = document.getElementById("classify-schedule-last-checked");
    try {
      const schedule = await fetchJSON("/api/classify-schedule");
      checkbox.checked = schedule.enabled;
      intervalInput.value = schedule.interval_seconds / 3600;
      lastChecked.textContent = schedule.last_classified_at
        ? `Last classified: ${formatDate(schedule.last_classified_at)}`
        : "Last classified: never yet.";
    } catch (err) {
      lastChecked.textContent = "Failed to load the schedule.";
    }
  }

  async function saveClassifySchedule() {
    const button = document.getElementById("save-classify-schedule-btn");
    const status = document.getElementById("classify-schedule-status");
    const checkbox = document.getElementById("classify-schedule-enabled-checkbox");
    const intervalInput = document.getElementById("classify-schedule-interval-input");

    const hours = Number(intervalInput.value);
    if (!hours || hours <= 0) {
      status.textContent = "Enter an interval greater than 0.";
      return;
    }

    button.disabled = true;
    status.textContent = "Saving…";
    try {
      const res = await fetch("/api/classify-schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: checkbox.checked, interval_seconds: Math.round(hours * 3600) }),
      });
      if (!res.ok) throw new Error("classify schedule save failed");
      status.textContent = "Saved -- takes effect within a few seconds, no restart needed.";
    } catch (err) {
      status.textContent = "Failed to save the schedule.";
    } finally {
      button.disabled = false;
    }
  }

  // -- watchlist editor (Settings tab: terms / clients / risk patterns) -------

  // Reusable editable tag-list: `items` is mutated in place (splice/push) so
  // the caller's array reference stays the single source of truth across
  // re-renders, rather than this component owning its own copy of the state.
  function renderChipList(containerId, items, { max, placeholder, onChange }) {
    const container = document.getElementById(containerId);
    container.textContent = "";

    const list = document.createElement("div");
    list.className = "chip-list";
    items.forEach((item, idx) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      const text = document.createElement("span");
      text.textContent = item;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "chip-remove";
      removeBtn.textContent = "×";
      removeBtn.setAttribute("aria-label", `Remove ${item}`);
      removeBtn.addEventListener("click", () => {
        items.splice(idx, 1);
        onChange();
      });
      chip.appendChild(text);
      chip.appendChild(removeBtn);
      list.appendChild(chip);
    });
    container.appendChild(list);

    const addRow = document.createElement("div");
    addRow.className = "chip-add-row";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = placeholder;
    input.maxLength = 80;
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = "Add";
    const atMax = items.length >= max;
    input.disabled = atMax;
    addBtn.disabled = atMax;

    const doAdd = () => {
      const value = input.value.trim();
      if (!value || items.length >= max) return;
      if (items.some((existing) => existing.toLowerCase() === value.toLowerCase())) {
        input.value = "";
        return;
      }
      items.push(value);
      input.value = "";
      onChange();
    };
    addBtn.addEventListener("click", doAdd);
    input.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter") {
        evt.preventDefault();
        doAdd();
      }
    });
    addRow.appendChild(input);
    addRow.appendChild(addBtn);
    container.appendChild(addRow);

    const counter = document.createElement("p");
    counter.className = "chip-counter";
    counter.textContent = `${items.length} / ${max}`;
    container.appendChild(counter);
  }

  let watchlistMaxItems = 10;
  let editTerms = [];
  let editClients = [];
  let editRiskPatterns = [];

  // Pure client-side mirror of radar/config.py's effective_terms() -- used
  // for the live preview before saving, so editing doesn't need a round trip
  // to the server on every keystroke.
  function computeEffectiveTerms(terms, clients, riskPatterns) {
    const combined = [...terms];
    clients.forEach((client) => {
      riskPatterns.forEach((pattern) => {
        combined.push(`${client} ${pattern}`);
      });
    });
    return combined;
  }

  function updateEffectivePreview() {
    const preview = document.getElementById("effective-terms-preview");
    preview.textContent = "";
    const combined = computeEffectiveTerms(editTerms, editClients, editRiskPatterns);
    if (!combined.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No terms configured -- collection would have nothing to search.";
      preview.appendChild(empty);
      return;
    }
    combined.forEach((term, idx) => {
      const chip = document.createElement("span");
      const isClientScoped = idx >= editTerms.length;
      chip.className = "chip" + (isClientScoped ? " chip--client-scoped" : "");
      chip.textContent = term;
      preview.appendChild(chip);
    });
    const count = document.createElement("p");
    count.className = "chip-counter";
    count.textContent = `${combined.length} effective quer${combined.length === 1 ? "y" : "ies"}`;
    preview.appendChild(count);
  }

  function renderWatchlistEditors() {
    renderChipList("terms-chip-list", editTerms, {
      max: watchlistMaxItems,
      placeholder: "e.g. claude rate limit",
      onChange: () => {
        renderWatchlistEditors();
        updateEffectivePreview();
      },
    });
    renderChipList("clients-chip-list", editClients, {
      max: watchlistMaxItems,
      placeholder: "e.g. McDonald's",
      onChange: () => {
        renderWatchlistEditors();
        updateEffectivePreview();
      },
    });
    renderChipList("risk-patterns-chip-list", editRiskPatterns, {
      max: watchlistMaxItems,
      placeholder: "e.g. jailbreak",
      onChange: () => {
        renderWatchlistEditors();
        updateEffectivePreview();
      },
    });
  }

  async function initSettingsTab() {
    try {
      const data = await fetchJSON("/api/search-terms");
      watchlistMaxItems = data.max_items || 10;
      editTerms = [...data.terms];
      editClients = [...data.clients];
      editRiskPatterns = [...data.risk_patterns];
      renderWatchlistEditors();
      updateEffectivePreview();
    } catch (err) {
      document.getElementById("terms-chip-list").textContent = "Failed to load search terms.";
    }
  }

  async function saveWatchlist() {
    const button = document.getElementById("save-watchlist-btn");
    const status = document.getElementById("watchlist-status");
    button.disabled = true;
    status.textContent = "Saving…";
    try {
      const res = await fetch("/api/search-terms", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          terms: editTerms,
          clients: editClients,
          risk_patterns: editRiskPatterns,
        }),
      });
      if (!res.ok) throw new Error("save failed");
      const data = await res.json();
      editTerms = [...data.terms];
      editClients = [...data.clients];
      editRiskPatterns = [...data.risk_patterns];
      renderWatchlistEditors();
      updateEffectivePreview();
      status.textContent = "Saved -- radar collect (CLI) will use this watchlist too.";
    } catch (err) {
      status.textContent = "Failed to save -- check server logs.";
    } finally {
      button.disabled = false;
    }
  }

  // -- escalation criteria (Settings tab) --------------------------------------

  let escalationCriteria = {};

  async function initEscalationCriteriaTab() {
    const container = document.getElementById("escalation-criteria-list");
    try {
      const data = await fetchJSON("/api/escalation-criteria");
      escalationCriteria = data.categories;
      renderEscalationCriteriaEditor();
    } catch (err) {
      container.textContent = "";
      const p = document.createElement("p");
      p.className = "empty-state";
      p.textContent = "Failed to load escalation criteria.";
      container.appendChild(p);
    }
  }

  function renderEscalationCriteriaEditor() {
    const container = document.getElementById("escalation-criteria-list");
    container.textContent = "";

    ALL_CATEGORIES.forEach((category) => {
      // requires_qa has no editor control below (the QA workflow it used to
      // drive was removed -- Board/COA/incident lifecycle cover that now) but
      // stays in this object and round-trips through save unchanged: the
      // backend's CategoryCriteria model still requires the field.
      const criteria = escalationCriteria[category] || {
        requires_qa: false,
        velocity_threshold: null,
        response_template: "",
        action_items: [],
      };

      const row = document.createElement("div");
      row.className = "escalation-criteria-row";

      const heading = document.createElement("div");
      heading.className = "escalation-criteria-heading";

      const title = document.createElement("span");
      title.className = "escalation-criteria-title";
      title.textContent = formatCategory(category);
      heading.appendChild(title);

      const thresholdLabel = document.createElement("label");
      thresholdLabel.className = "escalation-criteria-threshold";
      thresholdLabel.appendChild(document.createTextNode("Velocity override"));
      const thresholdInput = document.createElement("input");
      thresholdInput.type = "number";
      thresholdInput.step = "any";
      thresholdInput.placeholder = "default";
      if (criteria.velocity_threshold !== null && criteria.velocity_threshold !== undefined) {
        thresholdInput.value = criteria.velocity_threshold;
      }
      thresholdInput.addEventListener("input", () => {
        const value = thresholdInput.value.trim();
        escalationCriteria[category] = {
          ...escalationCriteria[category],
          velocity_threshold: value === "" ? null : Number(value),
        };
      });
      thresholdLabel.appendChild(thresholdInput);
      heading.appendChild(thresholdLabel);
      row.appendChild(heading);

      const templateInput = document.createElement("textarea");
      templateInput.className = "escalation-criteria-template";
      templateInput.rows = 2;
      templateInput.value = criteria.response_template || "";
      templateInput.addEventListener("input", () => {
        escalationCriteria[category] = {
          ...escalationCriteria[category],
          response_template: templateInput.value,
        };
      });
      row.appendChild(templateInput);

      const actionItemsLabel = document.createElement("label");
      actionItemsLabel.className = "escalation-criteria-action-items-label";
      actionItemsLabel.appendChild(document.createTextNode("Board action items (one per line)"));
      const actionItemsInput = document.createElement("textarea");
      actionItemsInput.className = "escalation-criteria-action-items";
      actionItemsInput.rows = 3;
      actionItemsInput.placeholder = "Log action taken";
      actionItemsInput.value = (criteria.action_items || []).join("\n");
      actionItemsInput.addEventListener("input", () => {
        escalationCriteria[category] = {
          ...escalationCriteria[category],
          action_items: actionItemsInput.value
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean),
        };
      });
      actionItemsLabel.appendChild(actionItemsInput);
      row.appendChild(actionItemsLabel);

      container.appendChild(row);
    });
  }

  async function saveEscalationCriteria() {
    const button = document.getElementById("save-escalation-criteria-btn");
    const status = document.getElementById("escalation-criteria-status");
    button.disabled = true;
    status.textContent = "Saving…";
    try {
      const res = await fetch("/api/escalation-criteria", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ categories: escalationCriteria }),
      });
      if (!res.ok) throw new Error("save failed");
      const data = await res.json();
      escalationCriteria = data.categories;
      renderEscalationCriteriaEditor();
      status.textContent = "Saved -- radar score (CLI or dashboard) will use this too.";
    } catch (err) {
      status.textContent = "Failed to save -- check server logs.";
    } finally {
      button.disabled = false;
    }
  }

  // -- model protection tiers (Settings tab) -----------------------------------

  const PROTECTION_TIER_OPTIONS = [
    { value: "flagship", label: "Flagship" },
    { value: "standard", label: "Standard" },
    { value: "legacy", label: "Legacy" },
  ];

  let modelTiers = {};

  async function initModelTiersTab() {
    const container = document.getElementById("model-tiers-list");
    try {
      const data = await fetchJSON("/api/model-tiers");
      modelTiers = data.models;
      renderModelTiersEditor();
    } catch (err) {
      container.textContent = "";
      const p = document.createElement("p");
      p.className = "empty-state";
      p.textContent = "Failed to load model protection tiers.";
      container.appendChild(p);
    }
  }

  function renderModelTiersEditor() {
    const container = document.getElementById("model-tiers-list");
    container.textContent = "";

    ALL_MODELS.forEach((model) => {
      const tier = modelTiers[model] || { protection_tier: "standard", velocity_threshold: null };

      const row = document.createElement("div");
      row.className = "escalation-criteria-row";

      const heading = document.createElement("div");
      heading.className = "escalation-criteria-heading";

      const title = document.createElement("span");
      title.className = "escalation-criteria-title";
      title.textContent = formatCategory(model);
      heading.appendChild(title);

      const tierLabel = document.createElement("label");
      tierLabel.className = "escalation-criteria-qa";
      tierLabel.appendChild(document.createTextNode("Protection tier "));
      const tierSelect = document.createElement("select");
      PROTECTION_TIER_OPTIONS.forEach(({ value, label }) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        option.selected = tier.protection_tier === value;
        tierSelect.appendChild(option);
      });
      tierSelect.addEventListener("change", () => {
        modelTiers[model] = { ...modelTiers[model], protection_tier: tierSelect.value };
      });
      tierLabel.appendChild(tierSelect);
      heading.appendChild(tierLabel);

      const thresholdLabel = document.createElement("label");
      thresholdLabel.className = "escalation-criteria-threshold";
      thresholdLabel.appendChild(document.createTextNode("Velocity override"));
      const thresholdInput = document.createElement("input");
      thresholdInput.type = "number";
      thresholdInput.step = "any";
      thresholdInput.placeholder = "default";
      if (tier.velocity_threshold !== null && tier.velocity_threshold !== undefined) {
        thresholdInput.value = tier.velocity_threshold;
      }
      thresholdInput.addEventListener("input", () => {
        const value = thresholdInput.value.trim();
        modelTiers[model] = {
          ...modelTiers[model],
          velocity_threshold: value === "" ? null : Number(value),
        };
      });
      thresholdLabel.appendChild(thresholdInput);
      heading.appendChild(thresholdLabel);
      row.appendChild(heading);

      container.appendChild(row);
    });
  }

  async function saveModelTiers() {
    const button = document.getElementById("save-model-tiers-btn");
    const status = document.getElementById("model-tiers-status");
    button.disabled = true;
    status.textContent = "Saving…";
    try {
      const res = await fetch("/api/model-tiers", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: modelTiers }),
      });
      if (!res.ok) throw new Error("save failed");
      const data = await res.json();
      modelTiers = data.models;
      renderModelTiersEditor();
      status.textContent = "Saved -- radar score (CLI or dashboard) will use this too.";
    } catch (err) {
      status.textContent = "Failed to save -- check server logs.";
    } finally {
      button.disabled = false;
    }
  }

  // -- init -------------------------------------------------------------------

  initTabs();
  initSettingsSubtabs();
  initSourcePicker();
  initSettingsTab();
  initEscalationCriteriaTab();
  initModelTiersTab();
  initAlertCardModal();
  initClaimingAsInput();
  document.getElementById("save-watchlist-btn").addEventListener("click", saveWatchlist);
  document.getElementById("save-escalation-criteria-btn").addEventListener("click", saveEscalationCriteria);
  document.getElementById("save-model-tiers-btn").addEventListener("click", saveModelTiers);
  document.getElementById("run-collection-btn").addEventListener("click", runCollection);
  document.getElementById("save-schedule-btn").addEventListener("click", saveSchedule);
  loadScheduleCard();

  document.getElementById("run-classify-btn").addEventListener("click", runClassify);
  document.getElementById("save-classify-schedule-btn").addEventListener("click", saveClassifySchedule);
  loadClassifyScheduleCard();

  document.getElementById("filter-category").addEventListener("change", loadAlerts);
  document.getElementById("filter-severity").addEventListener("change", loadAlerts);
  document.getElementById("filter-alerts-platform").addEventListener("change", applyAlertsView);

  document.getElementById("filter-watching-platform").addEventListener("change", applyWatchingView);
  document.getElementById("filter-watching-category").addEventListener("change", applyWatchingView);
  document.getElementById("filter-watching-severity").addEventListener("change", applyWatchingView);

  initSortableHeaders("#tab-watching table", watchingSort, applyWatchingView);
  initSortableHeaders("#tab-alerts table", alertsSort, applyAlertsView);

  loadAllDashboardData();
})();
