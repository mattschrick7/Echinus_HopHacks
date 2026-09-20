// Echinus 3D view.
//
// The map draws what the nodes see flattened: a view cone becomes a patch of
// ground, and a contact 2 km up sits on the same pixel as one on the roof.
// This draws the same records with the height left in — the view pyramid the
// Server hands over in `view_cone`, and every contact on a line down to the
// ground — in a local east/north/up frame in metres. No globe and no terrain:
// a deployment spans a few kilometres, which is flat enough that this mirrors
// the Server's own flat-earth maths (geometry.py) and nothing more.
//
// Three.js is y-up, so the mapping is x = east, y = up, z = -north. Distances
// stay in metres end to end — the grid squares really are a kilometre across.
//
// Node colours, the contact rings and the fade over the trail window all come
// from app.js, so the two views say the same thing in the same colours.

const Scene3D = (() => {
  const METRES_PER_DEGREE = 111320;   // geometry.METRES_PER_DEGREE
  const NODE_SIZE_M = 55;
  const MIN_SPAN_M = 1500;            // stops a lone contact filling the screen
  const GRID_CELL_M = 1000;
  const CONTACT_PX = 12;              // the size the map draws a contact at
  const UNKNOWN_COLOUR = "#8b929c";   // app.js UNKNOWN_COLOUR

  const COLOUR = {
    selected: 0xffffff,
    target: 0xff2d78,     // style.css --target: a selected target's flight path
    grid: 0x2a2d33,
    gridEdge: 0x3b4049,
    north: 0x7fbf7f,
    background: new THREE.Color(0x0d0f12),
  };

  let renderer, scene, camera, raycaster, labelHost, infoHost;
  let nodeGroup = null;               // ground, node markers, view cones
  let contactGroup = null;            // contact points and their drop lines
  let cones = {};                     // node_id -> Group, so edits can preview
  let nodeMarkers = [];               // resized each frame, see keepMarkersLegible
  let contactClouds = [];             // THREE.Points, one per set of nodes that agreed
  let highlight = null;               // marker drawn on the selected contact
  let pathGroup = null;               // the selected target's flight path
  let path = null;                    // { target, contacts } behind pathGroup
  let pathLabels = [];                // its label; kept apart from `labels`,
                                      // which setNodes clears
  let targetGroup = null;             // the heading arrows
  let targetLabels = [];              // and their speeds, kept apart the same way
  let labels = [];                    // { element, position }, projected each frame
  const pieTextures = new Map();      // "node-a,node-b" -> the ring texture for it

  let nodes = [];
  let contacts = [];
  let targets = [];
  let colours = {};                   // node_id -> "#rrggbb", from app.js
  let origin = null;                  // the ENU reference: the nodes' mean position
  let selected = null;
  let handlers = {};
  let visible = false;
  let needsRender = false;
  let framed = false;

  const orbit = {
    target: new THREE.Vector3(),
    distance: 6000,
    azimuth: 0,          // 0 = camera due south of the target, looking north
    elevation: 0.42,     // radians above the horizon
  };

  // -- geodetic -> scene ------------------------------------------------------

  const radians = (degrees) => (degrees * Math.PI) / 180;
  const colourOf = (nodeId) => colours[nodeId] || UNKNOWN_COLOUR;

  // The same flat-earth projection the Server triangulates in, so what you see
  // here is what the tracker computed.
  function toScene(lat, lon, alt) {
    const east = (lon - origin.lon) * METRES_PER_DEGREE * Math.cos(radians(origin.lat));
    const north = (lat - origin.lat) * METRES_PER_DEGREE;
    return new THREE.Vector3(east, (alt || 0) - origin.alt, -north);
  }

  // Somewhere to centre the scene when no node has a position yet. A target's
  // path is included, not just the live contacts: selecting a target that was
  // lost minutes ago is the one case where there is something worth drawing and
  // the trail window is empty.
  const anchors = () => [
    ...contacts.map((c) => [c.lat, c.lon, c.alt_m || 0]),
    ...(path ? path.contacts.map((c) => [c.lat, c.lon, c.alt_m || 0]) : []),
    ...targets.map((t) => [t.lat, t.lon, t.alt_m || 0]),
  ];

  // Everything is drawn around the nodes, so they set the reference point. A
  // deployment with nothing placed yet falls back to what it has been given.
  function originFor(placed) {
    const points = placed.length ? placed.map((n) => [n.lat, n.lon, n.alt_m]) : anchors();
    if (!points.length) return null;
    const mean = (i) => points.reduce((total, p) => total + p[i], 0) / points.length;
    return { lat: mean(0), lon: mean(1), alt: mean(2) };
  }

  const sameOrigin = (a, b) =>
    a && b && a.lat === b.lat && a.lon === b.lon && a.alt === b.alt;

  // -- setting up -------------------------------------------------------------

  function init(container, callbacks) {
    handlers = callbacks || {};

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(COLOUR.background);
    container.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(55, 1, 1, 400000);
    raycaster = new THREE.Raycaster();

    labelHost = container.querySelector(".scene-labels");
    infoHost = container.querySelector(".scene-info");

    bindControls();
    new ResizeObserver(resize).observe(container);
    resize();
    requestAnimationFrame(frame);
  }

  const isReady = () => renderer !== undefined;

  function dispose(object) {
    object.traverse((child) => {
      if (child.geometry) child.geometry.dispose();
      if (child.material) child.material.dispose();
    });
  }

  function replace(group, builder) {
    if (group) {
      dispose(group);
      scene.remove(group);
    }
    const fresh = new THREE.Group();
    scene.add(fresh);
    builder(fresh);
    needsRender = true;
    return fresh;
  }

  // -- nodes, their cones, and the ground -------------------------------------

  function setNodes(list, nodeColours) {
    if (!isReady()) return;
    nodes = list;
    colours = nodeColours || {};

    const placed = nodes.filter((n) => n.configured);
    const wanted = originFor(placed);
    const moved = !sameOrigin(origin, wanted);
    origin = wanted || origin;

    labels.forEach((label) => label.element.remove());
    labels = [];
    nodeMarkers = [];
    cones = {};

    nodeGroup = replace(nodeGroup, (group) => {
      if (!origin) return;
      addGround(group, placed);
      placed.forEach((node) => addNode(group, node));
    });

    // The reference point moved, so metres mean something different now.
    if (moved) {
      setContacts(contacts, currentTrail, colours);
      drawPath();
      drawTargets();
    }
    frameOnce();
  }

  function addGround(group, placed) {
    const reach = Math.max(
      MIN_SPAN_M,
      ...placed.map((node) => node.range_m || 0),
      ...spread(placed.map((node) => toScene(node.lat, node.lon, node.alt_m)))
    );
    const cells = Math.max(4, Math.ceil((reach * 2.5) / GRID_CELL_M));
    const size = cells * GRID_CELL_M;

    const grid = new THREE.GridHelper(size, cells, COLOUR.gridEdge, COLOUR.grid);
    grid.material.transparent = true;
    // Over the map the grid is a ruler, not the floor, so it steps back.
    grid.material.opacity = basemapOn ? 0.22 : 0.55;
    group.add(grid);

    group.add(line([new THREE.Vector3(), new THREE.Vector3(0, 0, -size / 2)], COLOUR.north, 0.5));
    addLabel("N", "north", new THREE.Vector3(0, 0, -size / 2));

    updateBasemap(size);
  }

  // -- the map, on the ground -------------------------------------------------
  //
  // The same OpenStreetMap tiles the 2D view uses, laid flat at the nodes'
  // altitude, so a contact is somewhere rather than just some height. Tiles
  // are placed by their own Mercator corners rather than by a scale factor,
  // which keeps them honest against the flat-earth frame everything else
  // uses: over a few kilometres the two agree to well under a pixel.

  const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  const TILE_SUBDOMAINS = ["a", "b", "c"];
  const EQUATOR_M = 40075016.686;
  const TILES_WANTED = 6;         // roughly, across the ground the scene covers
  const MAX_TILES_ACROSS = 9;     // a ceiling on what one view will fetch
  const MIN_ZOOM = 8, MAX_ZOOM = 18;

  let basemapGroup = null;
  let basemapKey = null;          // zoom and tile range currently laid out
  let basemapOn = true;
  const tileTextures = new Map(); // url -> texture, kept across rebuilds

  const lonToTile = (lon, zoom) => ((lon + 180) / 360) * 2 ** zoom;

  const latToTile = (lat, zoom) => {
    const phi = radians(lat);
    return ((1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2) * 2 ** zoom;
  };

  const tileToLon = (x, zoom) => (x / 2 ** zoom) * 360 - 180;

  const tileToLat = (y, zoom) =>
    (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / 2 ** zoom))) * 180) / Math.PI;

  // Enough detail to be worth drawing, few enough tiles to be polite to the
  // tile servers: pick the zoom that puts about TILES_WANTED across the scene.
  function zoomFor(size) {
    const metresPerTile = (zoom) => (EQUATOR_M * Math.cos(radians(origin.lat))) / 2 ** zoom;
    let zoom = MIN_ZOOM;
    while (zoom < MAX_ZOOM && metresPerTile(zoom + 1) * TILES_WANTED > size) zoom += 1;
    return zoom;
  }

  function updateBasemap(size) {
    if (!origin) return;
    const zoom = zoomFor(size);
    const half = size / 2;

    // The tiles covering the scene's ground square, clamped so one view can
    // never ask for hundreds.
    const west = lonToTile(origin.lon - half / (METRES_PER_DEGREE * Math.cos(radians(origin.lat))), zoom);
    const east = lonToTile(origin.lon + half / (METRES_PER_DEGREE * Math.cos(radians(origin.lat))), zoom);
    const north = latToTile(origin.lat + half / METRES_PER_DEGREE, zoom);
    const south = latToTile(origin.lat - half / METRES_PER_DEGREE, zoom);

    const range = {
      x0: Math.floor(west), x1: Math.floor(east),
      y0: Math.floor(north), y1: Math.floor(south),
    };
    range.x1 = Math.min(range.x1, range.x0 + MAX_TILES_ACROSS - 1);
    range.y1 = Math.min(range.y1, range.y0 + MAX_TILES_ACROSS - 1);

    const key = `${zoom}/${range.x0},${range.y0}-${range.x1},${range.y1}/${basemapOn}`;
    if (key === basemapKey) return;   // same ground, same tiles: leave it alone
    basemapKey = key;

    if (basemapGroup) {
      scene.remove(basemapGroup);     // textures are cached, so don't dispose them
      basemapGroup = null;
    }
    if (!basemapOn) {
      needsRender = true;
      return;
    }

    basemapGroup = new THREE.Group();
    scene.add(basemapGroup);
    for (let x = range.x0; x <= range.x1; x += 1) {
      for (let y = range.y0; y <= range.y1; y += 1) basemapGroup.add(tile(x, y, zoom));
    }
    needsRender = true;
  }

  function tile(x, y, zoom) {
    const url = TILE_URL
      .replace("{s}", TILE_SUBDOMAINS[(x + y) % TILE_SUBDOMAINS.length])
      .replace("{z}", zoom)
      .replace("{x}", x)
      .replace("{y}", y);

    // Corner to corner in the scene's own frame, so a tile lands where its
    // ground really is rather than where a nominal tile width would put it.
    const topLeft = toScene(tileToLat(y, zoom), tileToLon(x, zoom), origin.alt);
    const bottomRight = toScene(tileToLat(y + 1, zoom), tileToLon(x + 1, zoom), origin.alt);

    const geometry = new THREE.PlaneGeometry(bottomRight.x - topLeft.x, bottomRight.z - topLeft.z);
    geometry.rotateX(-Math.PI / 2);   // stand it on the ground, north at the top

    const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      // Tinted well down: full-brightness tiles glare against a dark scene
      // and bury the contacts drawn above them. Streets stay readable, which
      // is all the ground has to do.
      color: 0x6b7076,
      map: texture(url),
      depthWrite: false,             // the grid and drop lines sit on top
    }));
    mesh.position.set((topLeft.x + bottomRight.x) / 2, -1, (topLeft.z + bottomRight.z) / 2);
    mesh.renderOrder = -1;
    return mesh;
  }

  function texture(url) {
    if (tileTextures.has(url)) return tileTextures.get(url);
    const loaded = new THREE.TextureLoader()
      .setCrossOrigin("anonymous")   // WebGL refuses a texture it can't vouch for
      .load(url, () => { needsRender = true; }, undefined, () => { needsRender = true; });
    loaded.minFilter = THREE.LinearFilter;  // tiles aren't powers of two once tinted
    loaded.generateMipmaps = false;
    tileTextures.set(url, loaded);
    return loaded;
  }

  // Off for a clean look, or when there is no way out to the tile servers.
  function toggleBasemap() {
    basemapOn = !basemapOn;
    basemapKey = null;
    setNodes(nodes, colours);   // redraws the grid at its other opacity too
    return basemapOn;
  }

  const spread = (points) =>
    points.length ? [Math.max(...points.map((p) => p.length()))] : [];

  function addNode(group, node) {
    const position = toScene(node.lat, node.lon, node.alt_m);
    const colour = new THREE.Color(node.enabled ? colourOf(node.node_id) : UNKNOWN_COLOUR);

    // Unit-sized: keepMarkersLegible scales it every frame.
    const marker = new THREE.Mesh(
      new THREE.OctahedronGeometry(1),
      new THREE.MeshBasicMaterial({ color: colour })
    );
    marker.position.copy(position);
    marker.userData = { kind: "node", node };
    group.add(marker);
    nodeMarkers.push(marker);

    if (node.view_cone) {
      cones[node.node_id] = buildCone(position, node.view_cone, colour, node.enabled);
      group.add(cones[node.node_id]);
    }

    const label = addLabel(node.name || node.node_id, "node", position);
    label.style.color = `#${colour.getHexString()}`;
  }

  // The view pyramid, as the Server works it out in geometry.view_cone: apex
  // at the node, four corners at the node's detection range. The map draws
  // this shape's shadow; here it is the shape itself, so a camera aimed at the
  // sky reaches up instead of pooling around its own feet.
  function buildCone(apex, cornerPoints, colour, enabled) {
    const group = new THREE.Group();
    const corners = cornerPoints.map(([lat, lon, alt]) => toScene(lat, lon, alt));

    const faces = [];
    const edges = [];
    corners.forEach((corner, i) => {
      const next = corners[(i + 1) % corners.length];
      faces.push(apex, corner, next);      // a side of the pyramid
      edges.push(apex, corner);            // the edge the camera looks along
      edges.push(corner, next);            // and the far face's outline
    });

    const geometry = new THREE.BufferGeometry().setFromPoints(faces);
    group.add(new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      color: colour,
      transparent: true,
      opacity: enabled ? 0.07 : 0.03,
      side: THREE.DoubleSide,
      depthWrite: false,   // contacts inside the cone must stay visible
    })));

    group.add(new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(edges),
      new THREE.LineBasicMaterial({
        color: colour,
        transparent: true,
        opacity: enabled ? 0.55 : 0.25,
      })
    ));
    return group;
  }

  // Redraw one node's cone from unsaved form values, the way the map previews
  // its footprint while the operator types. Corners come from the Server.
  function previewCone(nodeId, cornerPoints) {
    if (!isReady() || !origin || !cones[nodeId] || !nodeGroup) return;
    const node = nodes.find((n) => n.node_id === nodeId);
    if (!node) return;

    const apex = cones[nodeId].userData.apex || toScene(node.lat, node.lon, node.alt_m);
    dispose(cones[nodeId]);
    nodeGroup.remove(cones[nodeId]);

    const colour = new THREE.Color(node.enabled ? colourOf(nodeId) : UNKNOWN_COLOUR);
    cones[nodeId] = buildCone(apex, cornerPoints, colour, node.enabled);
    cones[nodeId].userData.apex = apex;
    nodeGroup.add(cones[nodeId]);
    needsRender = true;
  }

  // -- contacts ---------------------------------------------------------------

  let currentTrail = 15;

  // Contacts are drawn as points, grouped by which nodes agreed on them: one
  // cloud per set, each with a ring texture in those nodes' colours — the same
  // pie the map puts on its markers. Age rides in the per-point alpha, so a
  // contact fades out over the trail window exactly as it does on the map.
  function setContacts(list, trailSeconds, nodeColours) {
    if (!isReady()) return;
    contacts = list;
    currentTrail = trailSeconds || currentTrail;
    if (nodeColours) colours = nodeColours;
    if (!origin) origin = originFor(nodes.filter((n) => n.configured));

    contactClouds = [];
    contactGroup = replace(contactGroup, (group) => {
      if (!origin) return;

      const bySignature = new Map();
      for (const contact of contacts) {
        const signature = contact.node_ids.join(",");
        if (!bySignature.has(signature)) bySignature.set(signature, []);
        bySignature.get(signature).push(contact);
      }

      const drops = [];
      const dropColours = [];
      for (const [signature, group_] of bySignature) {
        group.add(cloudFor(signature, group_));
        for (const contact of group_) {
          const position = toScene(contact.lat, contact.lon, contact.alt_m);
          // A line straight down to the grid: what makes a floating dot read
          // as "up there" rather than "over there".
          drops.push(position.x, position.y, position.z, position.x, 0, position.z);
          // Lines can't fade by alpha and stay cheap, so they fade towards the
          // background colour instead, which looks the same on this backdrop.
          const faded = blend(contact).lerp(COLOUR.background, 1 - fadeOf(contact) * 0.7);
          dropColours.push(faded.r, faded.g, faded.b, faded.r, faded.g, faded.b);
        }
      }
      if (drops.length) group.add(dropLines(drops, dropColours));
    });

    applySelection();
  }

  const fadeOf = (contact) =>
    Math.max(0.05, Math.min(1, 1 - (contact.age_s || 0) / currentTrail));

  // One contact's nodes averaged into a single colour, for its drop line.
  function blend(contact) {
    const ids = contact.node_ids.length ? contact.node_ids : [null];
    const total = new THREE.Color(0, 0, 0);
    ids.forEach((id) => total.add(new THREE.Color(colourOf(id))));
    return total.multiplyScalar(1 / ids.length);
  }

  function cloudFor(signature, group) {
    const vertices = [];
    const tints = [];
    const ids = [];

    for (const contact of group) {
      const position = toScene(contact.lat, contact.lon, contact.alt_m);
      vertices.push(position.x, position.y, position.z);
      // White times the texture leaves the ring's own colours alone; the alpha
      // is what carries the fade.
      tints.push(1, 1, 1, fadeOf(contact));
      ids.push(contact.id);
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
    geometry.setAttribute("color", new THREE.Float32BufferAttribute(tints, 4));

    const points = new THREE.Points(geometry, new THREE.PointsMaterial({
      size: CONTACT_PX,
      sizeAttenuation: false,
      map: pieFor(signature, group[0].node_ids),
      vertexColors: true,
      transparent: true,
      depthWrite: false,
      alphaTest: 0.1,
    }));
    points.userData.contactIds = ids;
    contactClouds.push(points);
    return points;
  }

  // The map rings a contact in the colours of the nodes that saw it — two
  // nodes, two halves. Same pie here, drawn once per set of nodes and reused.
  function pieFor(signature, nodeIds) {
    if (pieTextures.has(signature)) return pieTextures.get(signature);

    const wedges = nodeIds.length ? nodeIds.map(colourOf) : [UNKNOWN_COLOUR];
    const size = 64;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext("2d");
    const middle = size / 2;
    const radius = middle - 5;
    const share = (2 * Math.PI) / wedges.length;

    wedges.forEach((colour, i) => {
      ctx.beginPath();
      ctx.moveTo(middle, middle);
      // Start at twelve o'clock and go clockwise, like the map's conic-gradient.
      ctx.arc(middle, middle, radius, -Math.PI / 2 + i * share, -Math.PI / 2 + (i + 1) * share);
      ctx.closePath();
      ctx.fillStyle = colour;
      ctx.fill();
    });

    ctx.beginPath();                      // the map's white border
    ctx.arc(middle, middle, radius, 0, 2 * Math.PI);
    ctx.lineWidth = 4;
    ctx.strokeStyle = "#fff";
    ctx.stroke();

    const texture = new THREE.CanvasTexture(canvas);
    pieTextures.set(signature, texture);
    return texture;
  }

  function dropLines(positions, vertexColours) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.Float32BufferAttribute(vertexColours, 3));
    return new THREE.LineSegments(
      geometry,
      new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.6 })
    );
  }

  function line(points, colour, opacity) {
    return new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(points),
      new THREE.LineBasicMaterial({ color: colour, transparent: true, opacity })
    );
  }

  // -- labels -----------------------------------------------------------------
  //
  // HTML rather than sprites: crisp at any zoom, and it styles like the rest
  // of the dashboard. Positions are projected onto the canvas every frame.

  function addLabel(text, className, position) {
    const element = document.createElement("span");
    element.className = `scene-label ${className}`;
    element.textContent = text;
    labelHost.appendChild(element);
    labels.push({ element, position: position.clone() });
    return element;
  }

  function drawLabels() {
    const width = renderer.domElement.clientWidth;
    const height = renderer.domElement.clientHeight;
    for (const { element, position } of [...labels, ...pathLabels, ...targetLabels]) {
      const projected = position.clone().project(camera);
      const behind = projected.z > 1;
      element.style.display = behind ? "none" : "";
      if (behind) continue;
      element.style.left = `${((projected.x + 1) / 2) * width}px`;
      element.style.top = `${((1 - projected.y) / 2) * height}px`;
    }
  }

  // -- a target's flight path -------------------------------------------------
  //
  // Every contact the Server chained into one target, joined in time order and
  // drawn at height — with a faint curtain down to the grid, so the climb and
  // descent read at a glance, which the map's flat line cannot show.

  let framedPathId = null;             // the target the camera last swung to

  function setPath(target, pathContacts) {
    path = target ? { target, contacts: pathContacts || [] } : null;
    if (!isReady()) return;  // kept, and drawn when the view is first opened
    drawPath();
    drawTargets();           // selecting a lost target is what gives it an arrow
    if (path && framedPathId !== path.target.id) framePath();
    framedPathId = path ? path.target.id : null;
  }

  function drawPath() {
    pathLabels.forEach((label) => label.element.remove());
    pathLabels = [];
    pathGroup = replace(pathGroup, (group) => {
      if (!path || !origin || !path.contacts.length) return;
      const points = path.contacts.map((c) => toScene(c.lat, c.lon, c.alt_m));

      if (points.length > 1) group.add(line(points, COLOUR.target, 0.95));

      const curtain = [];
      points.forEach((p) => curtain.push(p.x, p.y, p.z, p.x, 0, p.z));
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(curtain, 3));
      group.add(new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({
        color: COLOUR.target, transparent: true, opacity: 0.18,
      })));

      // Its shadow on the grid, for reading the ground track against the nodes.
      group.add(line(points.map((p) => new THREE.Vector3(p.x, 0, p.z)), COLOUR.target, 0.35));

      const dots = new THREE.BufferGeometry().setFromPoints(points);
      group.add(new THREE.Points(dots, new THREE.PointsMaterial({
        color: COLOUR.target, size: 5, sizeAttenuation: false,
      })));

      const latest = points[points.length - 1];
      const element = document.createElement("span");
      element.className = `scene-label target-label ${path.target.status || ""}`;
      element.textContent = `T-${path.target.number}`;
      labelHost.appendChild(element);
      pathLabels.push({ element, position: latest.clone() });
    });
  }

  // -- where each target is heading -------------------------------------------
  //
  // A target carries a smoothed velocity as well as a position — targets.py
  // runs an alpha-beta filter over its contacts — so it can be drawn and not
  // just listed: an arrow from the last fix along that velocity, HORIZON_S
  // long. It is exactly the dead reckoning the tracker predicts with when it
  // decides which contact belongs to which target, so the arrow is pointing
  // where the Server itself will look next.
  //
  // Only tracked targets get one. A lost target's velocity is frozen at its
  // last contact, and an arrow would claim it is still flying — so it gets one
  // only while it is the selected target, dimmed, to show which way it was
  // going when it went.

  const HORIZON_S = 10;         // an arrow reaches where the target will be in this long
  const MIN_SPEED_MPS = 0.5;    // below this it is hovering and a heading is noise
  const CLIMB_MPS = 1.0;        // and this much up or down is worth saying
  const MAX_HEAD_M = 120;       // a fast target's arrowhead stops growing here

  // A target's velocity in the scene's axes: x = east, y = up, z = -north,
  // the same mapping toScene uses for its position.
  const velocityOf = (t) =>
    t.vel_e == null || t.vel_n == null || t.vel_u == null
      ? null
      : new THREE.Vector3(t.vel_e, t.vel_u, -t.vel_n);

  function setTargets(list) {
    targets = list || [];
    if (!isReady()) return;  // kept, and drawn when the view is first opened
    drawTargets();
  }

  function drawTargets() {
    targetLabels.forEach((label) => label.element.remove());
    targetLabels = [];
    const selectedId = path ? path.target.id : null;
    targetGroup = replace(targetGroup, (group) => {
      if (!origin) return;
      for (const target of targets) {
        if (target.status === "active" || target.id === selectedId) {
          addArrow(group, target, target.id === selectedId);
        }
      }
    });
  }

  function addArrow(group, target, isSelected) {
    const velocity = velocityOf(target);
    if (!velocity || velocity.length() < MIN_SPEED_MPS) return;

    const speed = velocity.length();
    const direction = velocity.clone().normalize();
    const from = toScene(target.lat, target.lon, target.alt_m);
    const to = from.clone().addScaledVector(velocity, HORIZON_S);
    const live = target.status === "active";
    const opacity = live ? 0.9 : 0.35;

    group.add(line([from, to], COLOUR.target, opacity));

    // Built by hand rather than with ArrowHelper, whose geometry is shared
    // across every instance: replace() disposes what it is given, and that
    // would take the shape out from under the next frame's arrows.
    const head = Math.min(speed * HORIZON_S * 0.28, MAX_HEAD_M);
    const cone = new THREE.Mesh(
      new THREE.ConeGeometry(head * 0.34, head, 12),
      new THREE.MeshBasicMaterial({ color: COLOUR.target, transparent: true, opacity })
    );
    // ConeGeometry points up the y axis; swing it onto the heading, then sit it
    // back by half its length so the tip lands on `to` rather than overshooting.
    cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction);
    cone.position.copy(to).addScaledVector(direction, -head / 2);
    group.add(cone);

    // The number is already on the path label for the selected target, so the
    // arrow only carries it for the others — which is what makes the rest of
    // the targets identifiable in the scene at all.
    const climb = target.vel_u > CLIMB_MPS ? " ↑" : target.vel_u < -CLIMB_MPS ? " ↓" : "";
    const element = document.createElement("span");
    element.className = `scene-label target-label speed ${target.status || ""}`;
    element.textContent =
      (isSelected ? "" : `T-${target.number} · `) + `${speed.toFixed(0)} m/s${climb}`;
    labelHost.appendChild(element);
    targetLabels.push({ element, position: to.clone() });
  }

  // Swing round to a target the first time it's selected, taking in its whole path.
  function framePath() {
    if (!path || !origin || !path.contacts.length) return;
    const points = path.contacts.map((c) => toScene(c.lat, c.lon, c.alt_m));
    points.push(...points.map((p) => new THREE.Vector3(p.x, 0, p.z)));
    frameAll(points);
  }

  // -- selection --------------------------------------------------------------

  function setSelected(contactId) {
    selected = contactId;
    applySelection();
    needsRender = true;
  }

  function applySelection() {
    if (!contactGroup) return;
    if (highlight) {
      contactGroup.remove(highlight);
      dispose(highlight);
      highlight = null;
    }

    const contact = contacts.find((c) => c.id === selected);
    setInfo(contact || null);
    if (!contact || !origin) return;

    const position = toScene(contact.lat, contact.lon, contact.alt_m);
    highlight = new THREE.Group();

    // Sized as a fraction of the camera distance, so the ring holds its size
    // on screen however far out you are — see keepMarkersLegible.
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(0.016, 0.023, 28),
      new THREE.MeshBasicMaterial({ color: COLOUR.selected, side: THREE.DoubleSide })
    );
    ring.position.copy(position);
    ring.userData.billboard = true;
    highlight.add(ring);
    highlight.add(
      line([position, new THREE.Vector3(position.x, 0, position.z)], COLOUR.selected, 0.8)
    );

    // The bearings that made this fix: one line per node that agreed, in that
    // node's colour, running from the node up to where they crossed. This is
    // the whole argument for the contact existing, drawn — and in 3D it is
    // finally visible, because the crossing happens in the air.
    //
    // Each line is drawn node -> fix. The raw reported bearing differs from it
    // by the residual the tracker allowed (MEETING_ANGLE_DEG, and the fix is
    // the best point between the rays, not on either of them), which is well
    // under a degree of what you see here.
    for (const nodeId of contact.node_ids) {
      const node = nodes.find((n) => n.node_id === nodeId && n.configured);
      if (!node) continue;
      const from = toScene(node.lat, node.lon, node.alt_m);
      highlight.add(line([from, position], new THREE.Color(colourOf(nodeId)), 0.85));
    }

    contactGroup.add(highlight);
  }

  function setInfo(contact) {
    if (!infoHost) return;
    if (!contact) {
      infoHost.innerHTML = contacts.length
        ? `<p class="empty">Click a contact — here or on the map — to inspect it.</p>`
        : `<p class="empty">No contacts in the trail window yet: two nodes have to
           see the same thing at the same moment.</p>`;
      return;
    }
    const altitude = contact.alt_m == null ? "unknown" : `${contact.alt_m.toFixed(0)} m`;
    const seenBy = contact.node_ids.length
      ? contact.node_ids
          .map((id) => `<i class="dot" style="background:${colourOf(id)}"></i>${id}`)
          .join("<br>")
      : `${contact.node_count} nodes`;

    infoHost.innerHTML =
      `<b>contact #${contact.id}</b>` +
      `<div class="scene-fact"><span>altitude</span><b>${altitude}</b></div>` +
      `<div class="scene-fact"><span>position</span>` +
      `<div>${contact.lat.toFixed(5)}, ${contact.lon.toFixed(5)}</div></div>` +
      `<div class="scene-fact"><span>crossed by</span><div>${seenBy}</div></div>` +
      `<div class="scene-fact"><span>seen</span>` +
      `<div>${contact.age_s == null ? contact.observed_at : `${contact.age_s.toFixed(1)}s ago`}</div></div>`;
  }

  // Swing the camera onto a contact. Asking for one means wanting to see it,
  // so come in close if the view was framed on the whole deployment — but
  // never pull back from wherever the operator had already zoomed to.
  const FOCUS_DISTANCE_M = 4000;

  function focus(contactId) {
    const contact = contacts.find((c) => c.id === contactId);
    if (!contact || !origin) return;
    orbit.target.copy(toScene(contact.lat, contact.lon, contact.alt_m));
    orbit.distance = Math.min(orbit.distance, FOCUS_DISTANCE_M);
    needsRender = true;
  }

  function everything() {
    if (!origin) return [];
    return [
      ...nodes.filter((n) => n.configured).map((n) => toScene(n.lat, n.lon, n.alt_m)),
      ...nodes.filter((n) => n.view_cone)
        .flatMap((n) => n.view_cone.map(([lat, lon, alt]) => toScene(lat, lon, alt))),
      ...contacts.map((c) => toScene(c.lat, c.lon, c.alt_m)),
      // A selected target and its path are the only things worth seeing once
      // the trail window has emptied, so "fit all" has to take them in too.
      ...(path ? path.contacts.map((c) => toScene(c.lat, c.lon, c.alt_m)) : []),
      ...targets.map((t) => toScene(t.lat, t.lon, t.alt_m)),
    ];
  }

  function frameOnce() {
    if (framed) return;
    const positions = everything();
    if (!positions.length) return;
    frameAll(positions);
    framed = true;
  }

  function frameAll(positions) {
    const box = new THREE.Box3().setFromPoints(positions);
    box.getCenter(orbit.target);
    const size = box.getSize(new THREE.Vector3());
    orbit.distance = Math.max(size.x, size.y, size.z, MIN_SPAN_M) * 1.7;
    needsRender = true;
  }

  // "Show me everything again" — the 3D twin of the map's initial fit.
  function reframe() {
    const positions = everything();
    if (positions.length) frameAll(positions);
  }

  // -- controls ---------------------------------------------------------------
  //
  // Orbit, zoom and pan, written out rather than pulled in: it is forty lines,
  // and OrbitControls would mean vendoring a second file and a module loader.

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

  function bindControls() {
    const canvas = renderer.domElement;
    let dragging = null;
    let moved = 0;

    canvas.addEventListener("pointerdown", (event) => {
      dragging = {
        x: event.clientX,
        y: event.clientY,
        pan: event.shiftKey || event.button === 1 || event.button === 2,
      };
      moved = 0;
      canvas.setPointerCapture(event.pointerId);
    });

    canvas.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const dx = event.clientX - dragging.x;
      const dy = event.clientY - dragging.y;
      dragging.x = event.clientX;
      dragging.y = event.clientY;
      moved += Math.abs(dx) + Math.abs(dy);

      if (dragging.pan) {
        // Metres per pixel at the target's depth, so a drag keeps pace with
        // the ground however far out the camera is.
        const scale =
          (2 * orbit.distance * Math.tan(radians(camera.fov / 2))) / canvas.clientHeight;
        const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
        const up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
        orbit.target.addScaledVector(right, -dx * scale).addScaledVector(up, dy * scale);
      } else {
        orbit.azimuth -= dx * 0.005;
        orbit.elevation = clamp(orbit.elevation + dy * 0.005, radians(-20), radians(89));
      }
      needsRender = true;
    });

    // A press that barely moved is a click, not a drag.
    canvas.addEventListener("pointerup", (event) => {
      if (dragging && moved < 5) pick(event);
      dragging = null;
    });
    canvas.addEventListener("pointercancel", () => { dragging = null; });
    canvas.addEventListener("contextmenu", (event) => event.preventDefault());

    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      orbit.distance = clamp(orbit.distance * Math.exp(event.deltaY * 0.001), 50, 400000);
      needsRender = true;
    }, { passive: false });
  }

  function pick(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    const pointer = new THREE.Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1
    );
    raycaster.setFromCamera(pointer, camera);
    // A point has no area to hit, so the threshold does the work — widened
    // with distance so a dot stays clickable when you are zoomed out.
    raycaster.params.Points.threshold = orbit.distance * 0.012;

    const hits = raycaster.intersectObjects(contactClouds);
    if (hits.length) {
      handlers.onContact?.(hits[0].object.userData.contactIds[hits[0].index]);
      return;
    }

    const markerHits = raycaster.intersectObjects(nodeMarkers);
    if (markerHits.length) {
      handlers.onNode?.(markerHits[0].object.userData.node);
      return;
    }
    handlers.onEmpty?.();
  }

  // -- frame loop -------------------------------------------------------------

  function updateCamera() {
    const horizontal = Math.cos(orbit.elevation) * orbit.distance;
    camera.position.set(
      orbit.target.x + horizontal * Math.sin(orbit.azimuth),
      orbit.target.y + Math.sin(orbit.elevation) * orbit.distance,
      orbit.target.z + horizontal * Math.cos(orbit.azimuth)
    );
    camera.lookAt(orbit.target);
  }

  // Nodes and the selection ring are landmarks, not objects to scale: at ten
  // kilometres out a real 55 m node is a third of a pixel and may as well not
  // be drawn. Both are sized from the camera distance instead, so they hold
  // still on screen while the kilometre grid carries the actual scale. A node
  // never shrinks below its true size, so flying right up to one still reads.
  function keepMarkersLegible() {
    const size = Math.max(NODE_SIZE_M, orbit.distance * 0.008);
    nodeMarkers.forEach((marker) => marker.scale.setScalar(size));

    highlight?.children.forEach((child) => {
      if (!child.userData.billboard) return;
      child.scale.setScalar(orbit.distance);
      child.quaternion.copy(camera.quaternion);  // or the ring reads as a line
    });
  }

  // Nothing animates on its own, so a frame is only drawn when something
  // actually changed — an idle 3D view costs nothing.
  function frame() {
    requestAnimationFrame(frame);
    if (!visible || !needsRender) return;
    needsRender = false;

    updateCamera();
    keepMarkersLegible();
    renderer.render(scene, camera);
    drawLabels();
  }

  function resize() {
    if (!isReady()) return;
    const container = renderer.domElement.parentElement;
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height) return;  // hidden: nothing to size to yet
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    needsRender = true;
  }

  function show() {
    visible = true;
    resize();
    needsRender = true;
  }

  function hide() {
    visible = false;
  }

  return {
    init, isReady, show, hide,
    setNodes, setContacts, setTargets, previewCone,
    setSelected, focus, reframe, setPath, toggleBasemap,
  };
})();
