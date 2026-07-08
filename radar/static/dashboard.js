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

  // -- init -------------------------------------------------------------------

  document.getElementById("filter-status").addEventListener("change", loadAlerts);
  document.getElementById("filter-category").addEventListener("change", loadAlerts);
  document.getElementById("filter-severity").addEventListener("change", loadAlerts);

  loadClusters();
  loadLeadTime();
  loadAlerts();
})();
