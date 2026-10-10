"use strict";

const $ = (id) => document.getElementById(id);
let readOnly = false;

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed (" + res.status + ")");
  return data;
}

function ago(iso) {
  if (!iso) return "never";
  const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 60) return secs + "s ago";
  if (secs < 3600) return Math.round(secs / 60) + "m ago";
  return Math.round(secs / 3600) + "h ago";
}

function td(text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  return cell;
}

function render(devices) {
  const rows = $("rows");
  rows.replaceChildren();
  let up = 0, down = 0;

  for (const d of devices) {
    if (d.status === "up") up++;
    if (d.status === "down") down++;

    const tr = document.createElement("tr");

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + d.status;
    badge.textContent = d.status.toUpperCase();
    if (d.error) badge.title = d.error;
    statusCell.appendChild(badge);
    tr.appendChild(statusCell);

    tr.appendChild(td(d.name));
    tr.appendChild(td(d.host + ":" + d.port));
    tr.appendChild(td(d.latency_ms === null ? "-" : d.latency_ms + " ms"));
    tr.appendChild(td(d.uptime_pct === null ? "-" : d.uptime_pct + "%"));
    tr.appendChild(td(ago(d.last_checked)));

    const actions = document.createElement("td");
    if (!readOnly) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "link";
      del.textContent = "Remove";
      del.addEventListener("click", () => removeDevice(d));
      actions.appendChild(del);
    }
    tr.appendChild(actions);
    rows.appendChild(tr);
  }

  $("c-total").textContent = devices.length;
  $("c-up").textContent = up;
  $("c-down").textContent = down;
  $("empty").hidden = devices.length > 0;
  $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
}

async function load() {
  try {
    render(await api("/api/devices"));
  } catch (err) {
    $("updated").textContent = "Could not load devices: " + err.message;
  }
}

async function removeDevice(d) {
  if (!confirm("Remove " + d.name + "?")) return;
  try {
    await api("/api/devices/" + d.id, { method: "DELETE" });
    load();
  } catch (err) {
    $("updated").textContent = err.message;
  }
}

$("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("form-error").textContent = "";
  const body = {
    name: $("f-name").value,
    host: $("f-host").value,
    port: Number($("f-port").value),
  };
  try {
    await api("/api/devices", { method: "POST", body: JSON.stringify(body) });
    event.target.reset();
    load();
  } catch (err) {
    $("form-error").textContent = err.message;
  }
});

$("check-now").addEventListener("click", async () => {
  const btn = $("check-now");
  btn.disabled = true;
  try {
    await api("/api/check", { method: "POST" });
    await load();
  } catch (err) {
    $("updated").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

(async function init() {
  try {
    const cfg = await api("/api/config");
    readOnly = cfg.read_only;
  } catch (err) {
    readOnly = true;
  }
  $("add-section").hidden = readOnly;
  load();
  setInterval(load, 10000);
})();
