(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  const STATUS_META = {
    pending: { dot: "warning", label: "Pending review" },
    approved: { dot: "good", label: "Approved" },
    rejected: { dot: "muted", label: "Rejected" },
    not_required: { dot: "muted", label: "Not required" },
  };

  const SEVERITY_META = {
    high: { dot: "critical", label: "High" },
    med: { dot: "warning", label: "Med" },
    low: { dot: "good", label: "Low" },
  };

  // Platform identity colors for the footprint graph -- categorical slots 1/2/3,
  // validated --pairs all (any two bubbles can end up adjacent in a force layout).
  const PLATFORM_COLOR_VAR = {
    reddit: "--platform-reddit",
    youtube: "--platform-youtube",
    x: "--platform-x",
  };
  const PLATFORM_LABEL = { reddit: "Reddit", youtube: "YouTube", x: "X" };

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

  async function loadClusters() {
    const container = document.getElementById("clusters-chart");
    try {
      const clusters = await fetchJSON("/api/clusters");
      renderClusters(container, clusters);
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
      const tip = () =>
        showTooltipForElement(hit, [
          ["Cluster", c.label],
          ["Alerts", c.alert_count],
          ["Max severity", (SEVERITY_META[c.max_severity] || {}).label || c.max_severity],
          ["Latest", formatDate(c.latest_triggered_at)],
          ["Example", c.representative_issue_summary],
        ]);
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

  function tickForceSimulation(nodes, edges, width, height) {
    const repulsionStrength = 70;
    const springStrength = 0.02;
    const springLength = 55;
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
      n.vx *= damping;
      n.vy *= damping;
      n.x += n.vx;
      n.y += n.vy;
      n.x = Math.min(Math.max(n.x, n.r), width - n.r);
      n.y = Math.min(Math.max(n.y, n.r), height - n.r);
      totalMovement += Math.abs(n.vx) + Math.abs(n.vy);
    });

    return totalMovement;
  }

  // The Footprint tab starts hidden (`display: none`), and a hidden container
  // measures 0 width -- rendering while hidden would freeze the graph's layout
  // to a wrong aspect ratio. So the data loads eagerly, but the actual SVG
  // build is deferred until the tab is first shown and has a real width.
  let footprintAlerts = null;
  let footprintRendered = false;

  async function loadFootprintGraph() {
    const container = document.getElementById("footprint-graph");
    try {
      footprintAlerts = await fetchJSON("/api/alerts");
      renderFootprintGraphIfVisible();
    } catch (err) {
      renderEmpty(container, "Failed to load the footprint graph.");
    }
  }

  function renderFootprintGraphIfVisible() {
    const panel = document.querySelector('.tab-panel[data-tab="footprint"]');
    if (footprintRendered || !footprintAlerts || !panel || panel.hidden) return;
    const container = document.getElementById("footprint-graph");
    const legend = document.getElementById("footprint-legend");
    renderFootprintGraph(container, legend, footprintAlerts);
    footprintRendered = true;
  }

  function renderFootprintGraph(container, legendContainer, alerts) {
    container.textContent = "";
    legendContainer.textContent = "";

    if (!alerts.length) {
      renderEmpty(container, "No alerts yet -- run `radar score` first.");
      return;
    }

    const width = Math.max(container.clientWidth || 600, 300);
    const height = 420;

    const clusters = new Map();
    alerts.forEach((a) => {
      const key = clusterKeyFor(a);
      if (!clusters.has(key)) {
        clusters.set(key, { key, label: clusterLabelFor(a), members: [] });
      }
      clusters.get(key).members.push(a);
    });
    const clusterList = Array.from(clusters.values());

    const nodes = [];
    const edges = [];
    clusterList.forEach((cluster, ci) => {
      const angle = (ci / clusterList.length) * Math.PI * 2;
      const hub = {
        type: "hub",
        label: cluster.label,
        platformCount: new Set(cluster.members.map((m) => m.platform)).size,
        memberCount: cluster.members.length,
        r: 22,
        x: width / 2 + Math.cos(angle) * (width * 0.28),
        y: height / 2 + Math.sin(angle) * (height * 0.28),
        vx: 0,
        vy: 0,
      };
      nodes.push(hub);

      cluster.members.forEach((member, mi) => {
        const spread = (mi - (cluster.members.length - 1) / 2) * 0.35;
        const satellite = {
          type: "satellite",
          alert: member,
          r: velocityRadius(member.velocity),
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
    title.textContent = "Root-cause clusters with each member post colored by platform and sized by velocity";
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

    const nodeEls = nodes.map((n) => {
      const g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", n.type === "hub" ? "graph-hub" : "graph-node");

      const fill = document.createElementNS(SVG_NS, "circle");
      fill.setAttribute("class", n.type === "hub" ? "hub-fill" : "node-fill");
      fill.setAttribute("r", n.r);
      if (n.type === "satellite") {
        const colorVar = PLATFORM_COLOR_VAR[n.alert.platform] || "--text-muted";
        fill.style.fill = `var(${colorVar})`;
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
          ]);
        } else {
          const a = n.alert;
          showTooltipForElement(g, [
            ["Post", a.post_id],
            ["Platform", PLATFORM_LABEL[a.platform] || a.platform],
            ["Velocity", a.velocity.toFixed(1)],
            ["Category", formatCategory(a.category)],
            ["Summary", a.issue_summary],
          ]);
        }
      };
      hit.addEventListener("mouseenter", showTip);
      hit.addEventListener("focus", showTip);
      hit.addEventListener("mouseleave", hideTooltip);
      hit.addEventListener("blur", hideTooltip);
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

    function draw() {
      edges.forEach((e, i) => {
        edgeEls[i].setAttribute("x1", e.source.x);
        edgeEls[i].setAttribute("y1", e.source.y);
        edgeEls[i].setAttribute("x2", e.target.x);
        edgeEls[i].setAttribute("y2", e.target.y);
      });
      nodes.forEach((n, i) => {
        nodeEls[i].setAttribute("transform", `translate(${n.x}, ${n.y})`);
        if (n.labelEl) {
          // Radiate the label outward from the graph center (rather than a
          // fixed direction) so labels fan out instead of colliding in the
          // middle of a ring of hubs.
          const dx = n.x - width / 2;
          const dy = n.y - height / 2;
          const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
          const ux = dx / dist;
          const uy = dy / dist;
          const labelDist = n.r + 16;
          n.labelEl.setAttribute("x", ux * labelDist);
          n.labelEl.setAttribute("y", uy * labelDist);
          n.labelEl.setAttribute("text-anchor", ux >= 0.15 ? "start" : ux <= -0.15 ? "end" : "middle");
          n.labelEl.setAttribute("dominant-baseline", "middle");
        }
      });
    }

    draw();

    let ticks = 0;
    const maxTicks = 400;
    function step() {
      const movement = tickForceSimulation(nodes, edges, width, height);
      draw();
      ticks += 1;
      if (movement > 0.5 && ticks < maxTicks) {
        requestAnimationFrame(step);
      }
    }
    requestAnimationFrame(step);

    const platformsPresent = Array.from(new Set(alerts.map((a) => a.platform))).sort();
    platformsPresent.forEach((p) => {
      const item = document.createElement("div");
      item.className = "legend-item";
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.background = `var(${PLATFORM_COLOR_VAR[p] || "--text-muted"})`;
      const text = document.createElement("span");
      text.textContent = PLATFORM_LABEL[p] || p;
      item.appendChild(swatch);
      item.appendChild(text);
      legendContainer.appendChild(item);
    });
  }

  // -- alerts table -----------------------------------------------------------

  function currentFilters() {
    return {
      status: document.getElementById("filter-status").value,
      category: document.getElementById("filter-category").value,
      severity: document.getElementById("filter-severity").value,
    };
  }

  async function loadAlerts() {
    const tbody = document.getElementById("alerts-body");
    tbody.classList.add("is-loading");

    const filters = currentFilters();
    const params = new URLSearchParams();
    if (filters.status) params.set("status", filters.status);
    if (filters.category) params.set("category", filters.category);
    if (filters.severity) params.set("severity", filters.severity);

    try {
      const alerts = await fetchJSON("/api/alerts?" + params.toString());
      renderAlerts(tbody, alerts);
    } catch (err) {
      tbody.textContent = "";
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "empty-state";
      td.textContent = "Failed to load alerts.";
      tr.appendChild(td);
      tbody.appendChild(tr);
    } finally {
      tbody.classList.remove("is-loading");
    }
  }

  function renderAlerts(tbody, alerts) {
    tbody.textContent = "";

    if (!alerts.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "empty-state";
      td.textContent = "No alerts match these filters.";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    alerts.forEach((a) => {
      const tr = document.createElement("tr");

      const platformTd = document.createElement("td");
      platformTd.textContent = a.platform || "—";

      const categoryTd = document.createElement("td");
      categoryTd.textContent = formatCategory(a.category);

      const severityTd = document.createElement("td");
      severityTd.appendChild(badge(SEVERITY_META[a.severity] || { dot: "muted", label: a.severity }));

      const velocityTd = document.createElement("td");
      velocityTd.className = "num";
      velocityTd.textContent = a.velocity.toFixed(1);

      const qaTd = document.createElement("td");
      qaTd.appendChild(badge(STATUS_META[a.qa_status] || { dot: "muted", label: a.qa_status }));

      const summaryTd = document.createElement("td");
      summaryTd.className = "summary-cell";
      summaryTd.textContent = a.issue_summary;

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
      if (a.qa_status === "pending") {
        const actions = document.createElement("div");
        actions.className = "actions";

        const approveBtn = document.createElement("button");
        approveBtn.className = "approve";
        approveBtn.textContent = "Approve";
        approveBtn.addEventListener("click", () => review(a.post_id, "approved"));

        const rejectBtn = document.createElement("button");
        rejectBtn.className = "reject";
        rejectBtn.textContent = "Reject";
        rejectBtn.addEventListener("click", () => review(a.post_id, "rejected"));

        actions.appendChild(approveBtn);
        actions.appendChild(rejectBtn);
        actionsTd.appendChild(actions);
      }

      tr.appendChild(platformTd);
      tr.appendChild(categoryTd);
      tr.appendChild(severityTd);
      tr.appendChild(velocityTd);
      tr.appendChild(qaTd);
      tr.appendChild(summaryTd);
      tr.appendChild(postTd);
      tr.appendChild(actionsTd);
      tbody.appendChild(tr);
    });
  }

  async function review(postId, decision) {
    try {
      const res = await fetch(`/api/alerts/${encodeURIComponent(postId)}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      if (!res.ok) throw new Error("review request failed");
      await loadAlerts();
    } catch (err) {
      window.alert("Failed to update review status.");
    }
  }

  // -- tabs -------------------------------------------------------------------

  function initTabs() {
    const buttons = Array.from(document.querySelectorAll(".tab-button"));
    const panels = Array.from(document.querySelectorAll(".tab-panel"));

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const target = button.dataset.tab;

        buttons.forEach((b) => {
          const active = b === button;
          b.classList.toggle("is-active", active);
          b.setAttribute("aria-selected", String(active));
        });
        panels.forEach((panel) => {
          panel.hidden = panel.dataset.tab !== target;
        });

        if (target === "footprint") {
          renderFootprintGraphIfVisible();
        }
      });
    });
  }

  // -- init -------------------------------------------------------------------

  initTabs();

  document.getElementById("filter-status").addEventListener("change", loadAlerts);
  document.getElementById("filter-category").addEventListener("change", loadAlerts);
  document.getElementById("filter-severity").addEventListener("change", loadAlerts);

  loadClusters();
  loadLeadTime();
  loadFootprintGraph();
  loadAlerts();
})();
