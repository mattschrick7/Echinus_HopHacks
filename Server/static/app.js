// Echinus dashboard.
//
// The server owns all the data; this file only shows it and sends edits back.
// Two things happen here: a map of nodes and contacts, and a form for telling
// the server where a node is and which way it points.

const POLL_MS = 2000;
const OFFLINE_AFTER_MS = 5 * 60 * 1000; // no packet for 5 min = offline
const HEADING_LENGTH_M = 250;           // length of the little "looking this way" arrow

const map = L.map("map", { center: [0, 0], zoom: 2 });
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "© OpenStreetMap contributors",
}).addTo(map);

const nodeLayer = L.layerGroup().addTo(map);
const contactLayer = L.layerGroup().addTo(map);

const el = (id) => document.getElementById(id);
const editor = el("editor");

let editing = null;      // node_id currently open in the form, or null
let picking = false;     // "pick on map" mode
let fitted = false;      // only auto-zoom to the data once

// ── helpers ──────────────────────────────────────────────────────────────────

const api = async (path, options) => {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
};

// "2026-09-18 10:04:11" from SQLite is UTC but has no timezone marker.
const parseUtc = (text) => (text ? new Date(text.replace(" ", "T") + "Z") : null);

const isOnline = (node) => {
  const seen = parseUtc(node.last_seen);
  return seen !== null && Date.now() - seen < OFFLINE_AFTER_MS;
};

const ago = (text) => {
  const seen = parseUtc(text);
  if (!seen) return "never seen";
  const seconds = Math.round((Date.now() - seen) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
};

// ── map ──────────────────────────────────────────────────────────────────────

const marker = (position, kind, size) =>
  L.marker(position, {
    icon: L.divIcon({
      className: "",
      html: `<div class="marker ${kind}" style="width:${size}px;height:${size}px"></div>`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    }),
  });

// Where the camera is pointing, as a short line drawn from the node.
function headingLine(node) {
  const bearing = (node.yaw_deg * Math.PI) / 180;
  const dLat = (HEADING_LENGTH_M * Math.cos(bearing)) / 111320;
  const dLon =
    (HEADING_LENGTH_M * Math.sin(bearing)) /
    (111320 * Math.cos((node.lat * Math.PI) / 180));
  return L.polyline(
    [[node.lat, node.lon], [node.lat + dLat, node.lon + dLon]],
    { color: "#4ea1ff", weight: 2, opacity: 0.7 }
  );
}

function drawMap(nodes, contacts) {
  nodeLayer.clearLayers();
  contactLayer.clearLayers();
  const points = [];

  for (const node of nodes) {
    if (!node.configured) continue; // nothing to plot until it has a position
    const position = [node.lat, node.lon];
    points.push(position);

    marker(position, node.enabled ? "node" : "node disabled", 16)
      .bindPopup(
        `<b>${node.name || node.node_id}</b><br>` +
        `${node.lat.toFixed(5)}, ${node.lon.toFixed(5)} · ${node.alt_m.toFixed(0)} m<br>` +
        `yaw ${node.yaw_deg}° · pitch ${node.pitch_deg}° · roll ${node.roll_deg}°<br>` +
        `<small>click to edit · see the panel for last contact</small>`
      )
      .on("click", () => openEditor(node))
      .addTo(nodeLayer);

    headingLine(node).addTo(nodeLayer);
  }

  for (const contact of contacts) {
    const position = [contact.lat, contact.lon];
    points.push(position);
    marker(position, "contact", 12)
      .bindPopup(
        `<b>contact #${contact.id}</b><br>` +
        `${contact.lat.toFixed(5)}, ${contact.lon.toFixed(5)}<br>` +
        (contact.alt_m != null ? `${contact.alt_m.toFixed(0)} m · ` : "") +
        `${contact.node_count} nodes<br><small>${contact.observed_at} UTC</small>`
      )
      .addTo(contactLayer);
  }

  if (!fitted && points.length) {
    map.fitBounds(L.latLngBounds(points).pad(0.3), { maxZoom: 15 });
    fitted = true;
  }
}

map.on("click", (event) => {
  if (!picking) return;
  editor.elements.lat.value = event.latlng.lat.toFixed(6);
  editor.elements.lon.value = event.latlng.lng.toFixed(6);
  setPicking(false);
});

function setPicking(on) {
  picking = on;
  el("pick").classList.toggle("active", on);
  el("pick").textContent = on ? "click the map…" : "pick on map";
}

// ── node list ────────────────────────────────────────────────────────────────

function drawNodeList(nodes) {
  const list = el("node-list");
  list.innerHTML = "";

  if (!nodes.length) {
    list.innerHTML = `<p class="empty">No nodes yet. They appear here automatically
      the first time a hub relays one of their packets.</p>`;
    return;
  }

  for (const node of nodes) {
    const card = document.createElement("div");
    card.className = "node" + (node.node_id === editing ? " selected" : "");
    card.innerHTML =
      `<div class="node-top">
         <i class="dot ${node.configured ? "node" : "unconfigured"} ${isOnline(node) ? "" : "off"}"></i>
         <b>${node.name || node.node_id}</b>
         <span class="id">${node.node_id}</span>
       </div>
       <div class="node-sub">
         ${node.configured
           ? `${node.lat.toFixed(4)}, ${node.lon.toFixed(4)} · yaw ${node.yaw_deg}° pitch ${node.pitch_deg}°`
           : `<em>needs position &amp; orientation</em>`}
         · ${ago(node.last_seen)}${node.enabled ? "" : " · disabled"}
       </div>`;
    card.onclick = () => openEditor(node);
    list.appendChild(card);
  }
}

function drawFeed(detections) {
  el("feed").innerHTML = detections
    .slice(0, 25)
    .map(
      (d) =>
        `<div class="row"><span class="id">${d.node_id}</span>` +
        `<span>az ${d.cam_az_deg.toFixed(1)}° el ${d.cam_el_deg.toFixed(1)}°</span>` +
        `<span class="muted">${d.world_az_deg == null
          ? "unplaced"
          : `→ ${d.world_az_deg.toFixed(0)}° / ${d.world_el_deg.toFixed(0)}°`}</span></div>`
    )
    .join("") || `<p class="empty">Nothing detected yet.</p>`;
}

// ── editor ───────────────────────────────────────────────────────────────────

// Note: always go through editor.elements — a form's own properties (`name`,
// `action`, …) shadow same-named fields, so editor.name is not the input.
const field = (name) => editor.elements[name];

const TEXT_FIELDS = ["name", "lat", "lon", "alt_m", "yaw_deg", "pitch_deg", "roll_deg", "notes"];

function openEditor(node) {
  editing = node.node_id;
  editor.hidden = false;
  el("editor-title").textContent = node.node_id;

  for (const name of TEXT_FIELDS) field(name).value = node[name];
  if (!node.configured) {
    // Don't offer 0,0 as a starting point — it's a real place in the Atlantic,
    // and a stray Save would put the node there. Make the operator type it.
    field("lat").value = "";
    field("lon").value = "";
  }
  field("enabled").checked = !!node.enabled;
  editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeEditor() {
  editing = null;
  editor.hidden = true;
  setPicking(false);
}

editor.onsubmit = async (event) => {
  event.preventDefault();
  const number = (name) => parseFloat(field(name).value) || 0;
  const body = {
    name: field("name").value,
    lat: parseFloat(field("lat").value),
    lon: parseFloat(field("lon").value),
    alt_m: number("alt_m"),
    yaw_deg: number("yaw_deg"),
    pitch_deg: number("pitch_deg"),
    roll_deg: number("roll_deg"),
    enabled: field("enabled").checked ? 1 : 0,
    notes: field("notes").value,
  };
  await api(`/api/nodes/${encodeURIComponent(editing)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  closeEditor();
  fitted = false; // re-frame the map now that a node has moved
  poll();
};

el("cancel").onclick = closeEditor;
el("pick").onclick = () => setPicking(!picking);

el("delete").onclick = async () => {
  if (!confirm(`Delete ${editing}? Its detections stay in the database.`)) return;
  await api(`/api/nodes/${encodeURIComponent(editing)}`, { method: "DELETE" });
  closeEditor();
  poll();
};

el("add-node").onclick = async () => {
  const nodeId = prompt("Node id (must match the node's node.toml, max 12 characters):");
  if (!nodeId) return;
  try {
    openEditor(await api("/api/nodes", { method: "POST", body: JSON.stringify({ node_id: nodeId }) }));
    poll();
  } catch (error) {
    alert(error.message);
  }
};

// ── poll ─────────────────────────────────────────────────────────────────────

// Redraw only when something actually changed, so an open popup or a hovered
// card doesn't get torn down every couple of seconds.
const lastDrawn = {};
function changed(key, value) {
  const serialised = JSON.stringify(value);
  if (lastDrawn[key] === serialised) return false;
  lastDrawn[key] = serialised;
  return true;
}

async function poll() {
  try {
    const [status, nodes, contacts, detections] = await Promise.all([
      api("/api/status"),
      api("/api/nodes"),
      api("/api/contacts?limit=200"),
      api("/api/detections?limit=25"),
    ]);

    // The map only cares about placement, so ignore last_seen ticking over.
    const placement = nodes.map((n) =>
      [n.node_id, n.name, n.lat, n.lon, n.alt_m, n.yaw_deg, n.pitch_deg, n.roll_deg,
       n.configured, n.enabled].join());

    if (changed("map", [placement, contacts])) drawMap(nodes, contacts);
    if (changed("list", [nodes, editing])) drawNodeList(nodes);
    if (changed("feed", detections)) drawFeed(detections);

    const hubs = status.hubs.length ? status.hubs.join(", ") : "no hub connected";
    el("status").textContent = `${hubs} · ${nodes.length} node(s) · ${contacts.length} contact(s)`;
    el("status").classList.toggle("bad", status.hubs.length === 0);
  } catch {
    el("status").textContent = "cannot reach the server";
    el("status").classList.add("bad");
  }
}

poll();
setInterval(poll, POLL_MS);
