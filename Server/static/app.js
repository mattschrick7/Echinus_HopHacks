// Echinus dashboard.
//
// The server owns all the data; this file only shows it and sends edits back.
// Two things happen here: a map of nodes and contacts, and a form for telling
// the server where a node is and which way it points.
//
// On the map each node has a colour. Its view cone is drawn in that colour, and
// every contact is ringed in the colours of the nodes whose bearings crossed to
// make it. Contacts fade out over the "trail" window and then disappear, so the
// map shows where things are now rather than everywhere they have ever been.
//
// The same records also go to scene.js, which draws them in 3D with the height
// left in — the map flattens a view cone to a patch of ground and a contact at
// 2 km to a dot. The two views share colours, the trail window and a selection:
// clicking a contact in either one opens it in the other.

const POLL_MS = 2000;
const OFFLINE_AFTER_MS = 5 * 60 * 1000; // no packet for 5 min = offline
const MAX_SPREAD_KM = 50;               // further than this from every other node = probably a typo
const CLOCK_WARN_MS = 1000;             // node clock this far from the hub's = detections won't pair
const VIEW_WARN_MS = 10 * 60 * 1000;    // how long an out-of-view report stays flagged
const CONTACT_LIMIT = 2000;             // most contacts drawn at once

// One colour per node, in node-id order. Okabe–Ito, minus the yellow that
// vanishes on map tiles, so neighbours stay distinguishable for colour-blind eyes.
const NODE_COLOURS = ["#e69f00", "#56b4e9", "#009e73", "#d55e00", "#cc79a7", "#0072b2", "#b8a000", "#999999"];
const UNKNOWN_COLOUR = "#8b929c"; // a contact from before nodes were recorded

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
let latestNodes = [];    // the last node list from the server
let latestContacts = []; // and the last contacts, for the 3D view's first build
let selected = null;     // contact id being inspected in 3D, or null
let picking = false;     // "pick on map" mode
let fitted = false;      // only auto-zoom to the data once
let nodeColour = {};     // node_id -> colour, rebuilt from every poll

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

const colourFor = (id) => nodeColour[id] || UNKNOWN_COLOUR;

function assignColours(nodes) {
  nodeColour = {};
  nodes.forEach((node, i) => { nodeColour[node.node_id] = NODE_COLOURS[i % NODE_COLOURS.length]; });
}

const marker = (position, kind, size, style = "") =>
  L.marker(position, {
    icon: L.divIcon({
      className: "",
      html: `<div class="marker ${kind}" style="width:${size}px;height:${size}px;${style}"></div>`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    }),
  });

// What each camera can see, as the Server outlines it (geometry.view_footprint):
// the view pyramid walked out to a fixed range and flattened onto the map. A
// level camera draws a wedge along its yaw, one aimed straight up draws a patch
// around the node, tilting in between pulls the cone in, and roll turns it.
// Dashed means the lens points below the horizon.
const coneStyle = (node, pitchDeg) => {
  const colour = node.enabled ? colourFor(node.node_id) : UNKNOWN_COLOUR;
  return {
    color: colour,
    weight: 1.5,
    opacity: node.enabled ? 0.8 : 0.4,
    fillColor: colour,
    fillOpacity: node.enabled ? 0.12 : 0.05,
    dashArray: pitchDeg < 0 || !node.enabled ? "6 5" : null,
    interactive: false, // let clicks through, so "pick on map" works inside a cone
  };
};

const cones = {}; // node_id -> polygon, so the editor can preview edits

function drawNodes(nodes) {
  nodeLayer.clearLayers();
  for (const id in cones) delete cones[id];

  for (const node of nodes) {
    if (!node.configured) continue; // nothing to plot until it has a position

    cones[node.node_id] = L.polygon(node.footprint, coneStyle(node, node.pitch_deg)).addTo(nodeLayer);

    marker([node.lat, node.lon], node.enabled ? "node" : "node disabled", 16,
           `background:${colourFor(node.node_id)}`)
      .bindPopup(
        `<b>${node.name || node.node_id}</b><br>` +
        `${node.lat.toFixed(5)}, ${node.lon.toFixed(5)} · ${node.alt_m.toFixed(0)} m<br>` +
        `yaw ${node.yaw_deg}° · pitch ${node.pitch_deg}° · roll ${node.roll_deg}°<br>` +
        `view ${node.fov_h_deg}° × ${node.fov_v_deg}° · range ${node.range_m} m<br>` +
        `<small>click to edit · see the panel for last contact</small>`
      )
      .on("click", () => openEditor(node))
      .addTo(nodeLayer);
  }

  Scene3D.setNodes(nodes, nodeColour); // ignored until the 3D view is opened
  if (editing) previewCone(); // a mid-edit redraw keeps the unsaved preview
}

// Redraw the open node's cone from the form as the operator types, before Save.
// The outline comes from the Server so the maths lives in one place; the
// sequence number drops answers that arrive after a newer request.
let previewSeq = 0;
async function previewCone() {
  const cone = cones[editing];
  if (!cone) return; // unplaced node: nothing on the map yet
  const names = ["lat", "lon", "yaw_deg", "pitch_deg", "roll_deg", "fov_h_deg", "fov_v_deg", "range_m"];
  const values = Object.fromEntries(names.map((name) => [name, parseFloat(field(name).value)]));
  if (Object.values(values).some(Number.isNaN)) return;

  const seq = ++previewSeq;
  try {
    // The flat outline for the map and the pyramid's corners for the scene —
    // both from the Server, so aiming a camera previews live in either view.
    const query = new URLSearchParams({ ...values, alt_m: parseFloat(field("alt_m").value) || 0 });
    const [outline, corners] = await Promise.all([
      api(`/api/footprint?${query}`),
      api(`/api/view-cone?${query}`),
    ]);
    if (seq !== previewSeq || !cones[editing]) return;
    cones[editing].setLatLngs(outline);
    cones[editing].setStyle({ dashArray: values.pitch_deg < 0 ? "6 5" : null });
    Scene3D.previewCone(editing, corners);
  } catch {
    // a half-typed value the Server rejects: keep the last good outline
  }
}

// A contact's marker is ringed in the colours of the nodes that saw it: two
// nodes, two halves; three nodes, three thirds.
function contactStyle(contact) {
  const colours = contact.node_ids.length ? contact.node_ids.map(colourFor) : [UNKNOWN_COLOUR];
  const share = 100 / colours.length;
  const stops = colours.map((c, i) => `${c} ${i * share}% ${(i + 1) * share}%`);
  return `background:conic-gradient(${stops.join(",")})`;
}

const contactMarkers = new Map(); // contact id -> marker, kept across polls

function contactPopup(contact) {
  const names = contact.node_ids.length
    ? contact.node_ids.map((id) => `<i class="dot" style="background:${colourFor(id)}"></i> ${id}`).join("<br>")
    : `${contact.node_count} nodes`;
  return `<b>contact #${contact.id}</b><br>` +
    `${contact.lat.toFixed(5)}, ${contact.lon.toFixed(5)}` +
    (contact.alt_m != null ? ` · ${contact.alt_m.toFixed(0)} m` : "") +
    `<br>${names}<br><small>${contact.observed_at} UTC</small>` +
    `<br><button type="button" class="ghost small to-3d" data-contact="${contact.id}">` +
    `see it in 3D</button>`;
}

// Popups are rebuilt as contacts come and go, so listen once, up here.
document.addEventListener("click", (event) => {
  const button = event.target.closest?.(".to-3d");
  if (button) showInScene(Number(button.dataset.contact));
});

// Contacts are updated in place rather than redrawn, so they can fade smoothly
// (a CSS transition on opacity) and an open popup survives the next poll.
function drawContacts(contacts) {
  const trail = trailSeconds();
  const live = new Set();

  for (const contact of contacts) {
    live.add(contact.id);
    const style = contactStyle(contact);
    let m = contactMarkers.get(contact.id);
    if (!m || m.ringStyle !== style) { // new, or the node colours moved
      if (m) m.remove();
      m = marker([contact.lat, contact.lon], "contact", 12, style)
        .bindPopup(contactPopup(contact))
        .addTo(contactLayer);
      m.ringStyle = style;
      contactMarkers.set(contact.id, m);
    }
    m.setOpacity(Math.max(0, 1 - contact.age_s / trail));
    m.setZIndexOffset(-Math.round(contact.age_s)); // newest on top
  }

  for (const [id, m] of contactMarkers) {
    if (!live.has(id)) { m.remove(); contactMarkers.delete(id); }
  }

  markSelected();
  Scene3D.setContacts(contacts, trail, nodeColour); // ignored until 3D is opened
}

// ── 3D view ──────────────────────────────────────────────────────────────────

function setView(which) {
  document.body.dataset.view = which;
  el("view-2d").classList.toggle("active", which === "2d");
  el("view-3d").classList.toggle("active", which === "3d");

  if (which !== "3d") {
    Scene3D.hide();
    return;
  }
  // Built the first time it is asked for: no WebGL context for an operator who
  // only ever wants the map.
  if (!Scene3D.isReady()) {
    Scene3D.init(el("scene"), {
      onContact: selectContact,
      onNode: openEditor,        // same as clicking a node on the map
      onEmpty: () => selectContact(null),
    });
    Scene3D.setNodes(latestNodes, nodeColour);
    Scene3D.setContacts(latestContacts, trailSeconds(), nodeColour);
    Scene3D.setSelected(selected);
  }
  Scene3D.show();
}

// The selected contact is ringed white on the map and carries its bearing
// lines in the scene, so both views point at the same record.
function selectContact(contactId) {
  selected = contactId;
  Scene3D.setSelected(contactId);
  markSelected();
}

function markSelected() {
  for (const [id, m] of contactMarkers) {
    m.getElement()?.firstElementChild?.classList.toggle("selected", id === selected);
  }
}

function showInScene(contactId) {
  selectContact(contactId);
  map.closePopup();
  setView("3d");
  Scene3D.focus(contactId);
}

el("view-2d").onclick = () => setView("2d");
el("view-3d").onclick = () => setView("3d");
el("reframe").onclick = () => Scene3D.reframe();

function fitOnce(nodes, contacts) {
  if (fitted) return;
  const placed = nodes.filter((n) => n.configured).map((n) => [n.lat, n.lon]);
  const points = placed.length ? placed : contacts.map((c) => [c.lat, c.lon]);
  if (!points.length) return;
  map.fitBounds(L.latLngBounds(points).pad(0.3), { maxZoom: 15 });
  fitted = true;
}

map.on("click", (event) => {
  if (!picking) return;
  editor.elements.lat.value = event.latlng.lat.toFixed(6);
  editor.elements.lon.value = event.latlng.lng.toFixed(6);
  setPicking(false);
  previewCone();
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
         <i class="dot ${node.configured ? "node" : "unconfigured"} ${isOnline(node) ? "" : "off"}"
            ${node.configured ? `style="background:${colourFor(node.node_id)}"` : ""}></i>
         <b>${node.name || node.node_id}</b>
         <span class="id">${node.node_id}</span>
       </div>
       <div class="node-sub">
         ${node.configured
           ? `${node.lat.toFixed(4)}, ${node.lon.toFixed(4)} · yaw ${node.yaw_deg}° pitch ${node.pitch_deg}°`
           : `<em>needs position &amp; orientation</em>`}
         · ${ago(node.last_seen)}${node.enabled ? "" : " · disabled"}
       </div>
       ${healthWarnings(node).map((w) => `<div class="node-warn">⚠ ${w}</div>`).join("")}`;
    card.onclick = () => openEditor(node);
    list.appendChild(card);
  }
}

// Problems only real hardware has: the simulator shares one clock and one
// field of view with the Server, so it never trips these. See ingest.py.
function healthWarnings(node) {
  const warnings = [];
  if (node.clock_offset_ms != null && Math.abs(node.clock_offset_ms) > CLOCK_WARN_MS) {
    const seconds = (node.clock_offset_ms / 1000).toFixed(1);
    warnings.push(`clock ${node.clock_offset_ms > 0 ? "+" : ""}${seconds}s off the hub's — ` +
                  `its detections won't pair with other nodes. Check NTP on the node.`);
  }
  const outOfView = parseUtc(node.out_of_view_at);
  if (outOfView && Date.now() - outOfView < VIEW_WARN_MS) {
    warnings.push(`reported ${node.out_of_view_note}, outside the ${node.fov_h_deg}° × ${node.fov_v_deg}° ` +
                  `set here — match Field of view to the node's node.toml.`);
  }
  return warnings;
}

function drawFeed(detections) {
  el("feed").innerHTML = detections
    .slice(0, 25)
    .map(
      (d) =>
        `<div class="row"><span class="id"><i class="dot" style="background:${colourFor(d.node_id)}"></i> ${d.node_id}</span>` +
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

const TEXT_FIELDS = ["name", "lat", "lon", "alt_m", "yaw_deg", "pitch_deg", "roll_deg",
                     "fov_h_deg", "fov_v_deg", "range_m", "notes"];

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
  // Throw away any unsaved preview: force the next poll to redraw from the server.
  delete lastDrawn.nodes;
  poll();
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
    fov_h_deg: parseFloat(field("fov_h_deg").value) || 62.2,
    fov_v_deg: parseFloat(field("fov_v_deg").value) || 48.8,
    range_m: parseFloat(field("range_m").value) || 1500,
    enabled: field("enabled").checked ? 1 : 0,
    notes: field("notes").value,
  };
  if (!confirmFarAway(body)) return;
  await api(`/api/nodes/${encodeURIComponent(editing)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  closeEditor();
  fitted = false; // re-frame the map now that a node has moved
  poll();
};

// A slip in the latitude or longitude (37.77 typed as 19.77) throws a node
// hundreds of kilometres off the map, where it silently vanishes from view. It
// also wrecks tracking for every node, because the tracker works on a flat map
// centred on the average node position. So a save that lands far from all the
// other nodes has to be confirmed.
function confirmFarAway(body) {
  const others = latestNodes.filter((n) => n.configured && n.node_id !== editing);
  if (!others.length) return true; // the first node can go anywhere
  const nearest = Math.min(...others.map((n) => distanceKm(body.lat, body.lon, n.lat, n.lon)));
  if (nearest <= MAX_SPREAD_KM) return true;
  return confirm(
    `This puts ${editing} ${Math.round(nearest).toLocaleString()} km from the nearest other node.\n\n` +
    `Latitude ${body.lat}, longitude ${body.lon} — is that right, or a typo?`
  );
}

// Great-circle distance; plenty accurate for "is this a typo".
function distanceKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * rad) / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(a));
}

el("cancel").onclick = closeEditor;
editor.addEventListener("input", previewCone);
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

// ── trail ────────────────────────────────────────────────────────────────────

// How long a contact stays on the map, fading as it goes. Remembered per browser.
const trailSelect = el("trail");
try { trailSelect.value = localStorage.getItem("echinus.trail") || trailSelect.value; } catch {}
const trailSeconds = () => parseFloat(trailSelect.value);
trailSelect.onchange = () => {
  try { localStorage.setItem("echinus.trail", trailSelect.value); } catch {}
  poll();
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
      api(`/api/contacts?limit=${CONTACT_LIMIT}&max_age_s=${trailSeconds()}`),
      api("/api/detections?limit=25"),
    ]);
    latestNodes = nodes;
    latestContacts = contacts;
    assignColours(nodes);

    // The cones only care about placement, so ignore last_seen ticking over.
    const placement = nodes.map((n) =>
      [n.node_id, n.name, n.lat, n.lon, n.alt_m, n.yaw_deg, n.pitch_deg, n.roll_deg,
       n.fov_h_deg, n.fov_v_deg, n.range_m, n.configured, n.enabled].join());

    if (changed("nodes", placement)) drawNodes(nodes);
    drawContacts(contacts); // every poll: ages move on even when nothing new arrives
    fitOnce(nodes, contacts);
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

setView("2d");
poll();
setInterval(poll, POLL_MS);
