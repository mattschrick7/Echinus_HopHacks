# Echinus

A network of cheap cameras that watch the sky, report what moves, and let a
ground station work out where it is.

There are exactly three things to understand:

| | What it is | What it does |
|---|---|---|
| **[Node](Node/)** | Raspberry Pi Zero 2W + camera + SX1262 LoRa HAT | Detects motion. Sends "I saw something 8° left and 12° above where I'm looking" over the radio. Knows nothing else about itself. |
| **[Hub](Hub/)** | Raspberry Pi 4 + SX1262 LoRa HAT | Listens to the radio, forwards every packet to the Server over a websocket. Stores nothing, decides nothing. |
| **[Server](Server/)** | A Linux box running Docker | Holds every node's position and orientation, turns their reports into world bearings, crosses bearings into positions, and serves the dashboard the operator works in. |

```
  Node ─┐
  Node ─┼─ LoRa ──▶  Hub  ──  websocket  ──▶  Server  ──▶  dashboard
  Node ─┘                                       │
                                                └─ SQLite: nodes, detections, contacts
```

## The one design rule

**The Server is the source of truth.** A node has no GPS, no compass and no
calibration step; it only knows its own id. Where each node sits and which way
it points is typed in by the operator on the dashboard and lives in the
Server's database.

That is what keeps the field hardware disposable. Re-aim a camera and you
change one number in a web form — you don't reflash anything. Swap a dead Pi
for a spare with the same id and the system carries on. Lose a Hub and you've
lost radio coverage, not data.

## Repo layout

```
Echinus/
├── pyproject.toml            uv workspace root (the shared .venv lives here)
│
├── shared/echinus-link/      the LoRa wire format + SX1262 driver
│       packets.py            two packet types; the only bytes on the radio
│       radio.py              the HAT, wrapped in send()/recv()
├── shared/configure-boot.sh  camera overlay + UART, written into config.txt
├── shared/fetch-sx126x.sh    pulls Waveshare's driver out of their demo zip
├── shared/radio_check.py     the HAT, driven exactly as Waveshare's demo does
│
├── Node/                     Pi Zero: camera -> LoRa
│       camera.py             Pi camera, or OpenCV off the Pi
│       detector.py           motion detection and clutter filtering
│       preview.py            optional MJPEG view, for aiming a camera
│       __main__.py           the loop: capture, detect, transmit
│
├── Hub/                      Pi 4: LoRa -> websocket
│       relay.py              decode, queue, forward, reconnect
│
└── Server/                   Linux box, in Docker
        app.py                websocket ingest + operator API + dashboard
        db.py                 SQLite: nodes, detections, contacts
        geometry.py           all the coordinate maths, in one file
        tracker.py            crossing bearings into positions
        simulator.py          fake nodes, for working without hardware
        static/scene.js       the 3D view: cones, contacts, bearing lines
        static/, templates/   the dashboard
```

## Getting it running

### Server (your Linux box)

```bash
cd Server
docker compose up --build
```

Dashboard on <http://localhost:8000>. Hubs connect to
`ws://<this-machine>:8000/ws/hub`.

No hardware yet? Run the simulator instead — it invents a few nodes, places
them via the same API the dashboard uses, and flies targets past them:

```bash
docker compose --profile demo up --build
```

### Hub (Pi 4 with the LoRa HAT)

```bash
git clone <repo> ~/echinus
bash ~/echinus/Hub/deploy/install.sh     # creates hub.toml, then run it again
```

Set `[server] url` in `Hub/hub.toml` to your Server. Don't know its IP yet?
Skip the config and pass it on the command line:

```bash
uv run echinus-hub --server ws://192.168.1.50:8000/ws/hub
uv run echinus-hub --listen              # just print what the radio hears
```

### Node (Pi Zero with camera + LoRa HAT)

```bash
git clone <repo> ~/echinus
bash ~/echinus/Node/deploy/install.sh    # creates node.toml, then run it again
```

Set `[node] id` in `Node/node.toml` — unique, 12 characters or fewer. That's
the only per-node setting that matters.

### Prove the radio link first

Before a camera or a Server is involved, check that the two HATs can reach each
other. Each radio prints what the module actually kept when it starts — the
driver's own `set()` can't fail, so an unconfigured HAT otherwise looks exactly
like a working one that nobody is talking to.

Hub, then node:

```bash
uv run echinus-hub  --config Hub/hub.toml  --listen --raw   # on the Pi 4
uv run echinus-node --config Node/node.toml --beacon        # on the Pi Zero
```

`--beacon` sends heartbeats and touches nothing else; `--raw` prints every byte
the hub hears, so you can see traffic that isn't ours — including Waveshare's
own demo, whose messages are plain strings behind the same three-byte sender
header we use.

If that stays silent, drop below our code entirely. `shared/radio_check.py`
constructs `sx126x` exactly as Waveshare's `main.py` does and uses the driver's
own `send()`/`receive()`, with the settings from your `[lora]` table:

```bash
uv run python shared/radio_check.py listen --config Hub/hub.toml
uv run python shared/radio_check.py send   --config Node/node.toml
```

Works there but not above, and the fault is ours. Fails there too, and it's
hardware or settings — in which case, in order of likelihood:

- **The M0 and M1 jumpers must be removed** when the HAT is on a Pi.
- The serial console must be off and the UART on. `install.sh` does both.
- An antenna must be attached before transmitting at 22dBm.
- Every radio must agree on frequency, address and air speed.

### Then, in the dashboard

A node appears in the sidebar the first time a hub relays one of its packets,
flagged **needs position & orientation**. Click it and fill in:

- **latitude / longitude / altitude** — where the node is ("pick on map" is
  easier than typing coordinates)
- **yaw** — the compass bearing the lens points (0 = north, 90 = east)
- **pitch** — how far above the horizon it points (90 = straight up)
- **roll** — twist about the lens axis; 0 for a level camera

Save, and its detections start counting toward tracking. Detections that
arrived before you placed it are kept, but can't be used — only new ones.

## How a detection becomes a position

1. A node's camera sees a bright change. `detector.py` picks the strongest
   motion blob and converts its centre to degrees off the lens axis.
2. That, the node id and a timestamp go out as a 30-byte LoRa packet.
3. The Hub decodes it and forwards it as JSON.
4. The Server looks up the node's orientation and converts
   camera-relative angles into a compass bearing and elevation
   (`geometry.camera_to_world_azel`).
5. `tracker.py` collects bearings from the same moment, crosses every pair
   from different nodes, and keeps only the pairs that genuinely meet — in
   front of both cameras, at a believable height. Crossings close together
   merge, so an object three nodes agree on is one contact, not three.
6. The contact appears on the map.

A single node seeing something proves a direction, not a position — so one
node alone never produces a contact. Two are the minimum; three make it solid.

### The 3D view

The crossing that makes a contact happens in the air, and a map has nowhere to
put height: a view cone flattens to a patch of ground, and a drone at 2 km sits
on the same pixel as one on a roof. The **3D** toggle beside the status line
draws the same records — the same node colours, the same trail window — in
metres east, north and up, with a kilometre grid at the nodes' own altitude.

- each node's view pyramid, drawn from `geometry.view_cone`: the shape the
  map's footprint is the shadow of, so a camera aimed at the sky reaches up
- every contact on a line down to the grid, ringed in the colours of the nodes
  that saw it and fading over the trail window, exactly as on the map
- clicking a contact — in either view, or from "see it in 3D" in its map popup —
  draws the lines of bearing that crossed to make it, one per node, in that
  node's colour

Aiming a camera previews live in both views: type a yaw or a pitch and the flat
footprint and the 3D cone both follow, because both come from the Server.

### Hardware per node

- Raspberry Pi Zero 2W
- Arducam 8MP IMX219 (B0036) — use the tapered 22-pin ribbon for the Zero
- Waveshare SX1262 LoRa HAT
- A 2.5A+ supply with a *thick* cable. Thin cables cause silent undervoltage
  that kills camera init while leaving wifi up; `vcgencmd get_throttled`
  should read `0x0`.

`install.sh` writes the boot config for you — the camera overlay and the UART
the LoRa HAT needs — into a marked block in `/boot/firmware/config.txt`, backs
up the original, and tells you to reboot. For a different sensor:

```bash
CAMERA_OVERLAY=imx477 bash Node/deploy/install.sh
```

The camera wants a full power cycle, not just a reboot. Check it afterwards
with `rpicam-hello --list-cameras` (expect `imx219`). Ignore
`vcgencmd get_camera` — it reports `supported=0` even when libcamera is fine.

## Development

```bash
uv sync
uv run pytest        # all three components
```

Run the detector on a laptop webcam or a recorded clip, no radio involved:

```bash
uv run echinus-node --config Node/node.toml.example --dry-run --preview
uv run echinus-node --config Node/node.toml.example --dry-run --source clip.mp4 --loop
```

Run the Server outside Docker:

```bash
cd Server
pip install -r requirements.txt
ECHINUS_DB=./echinus.db uvicorn app:app --reload --port 8000
python simulator.py      # in another shell
```

The tests are the fastest way to understand the system. `Server/tests/
test_geometry.py` pins down every coordinate convention, and
`Server/tests/test_tracking.py` walks bearings through to contacts.

## Operator API

The dashboard is a thin client over these; `curl` works just as well.

| | |
|---|---|
| `GET /api/nodes` | every node and its operator-set position/orientation |
| `POST /api/nodes` | add a node by hand (body: `{"node_id": "node-07"}`) |
| `PATCH /api/nodes/{id}` | set position, orientation, name, notes, enabled |
| `DELETE /api/nodes/{id}` | remove a node (its detections stay) |
| `GET /api/detections` | recent raw reports |
| `GET /api/contacts` | recent triangulated positions |
| `GET /api/status` | connected hubs, node count |
| `WS /ws/hub` | where hubs connect |

## Things worth knowing

- **Every radio must agree.** Frequency, address and air speed in `[lora]`
  have to match across every node and the hub, or nothing arrives. Only the
  values Waveshare's driver has table entries for work — `radio.py` lists them
  and rejects anything else up front, because the driver itself fails with a
  `TypeError` from inside its own code.
- **The HAT runs in fixed-point mode**, which Waveshare's driver hard-codes.
  Every transmission must start with three bytes of destination (address high,
  address low, channel), or the module reads your payload's first bytes as an
  address and sends it to nobody. `radio.py` adds them, plus the three bytes
  Waveshare's demo receiver reads back as the sender — so their tools and ours
  can read each other's traffic.
- **Clocks matter.** Detections are matched by the node's own timestamp, so
  the Pis need NTP. The systemd units wait for time sync; if the hub is the
  only network, point `chrony` on the nodes at it.
- **`sx126x.py` isn't on PyPI.** The install scripts fetch Waveshare's driver;
  if the download fails, copy it in by hand — they tell you where.
- **The websocket is unauthenticated.** Fine on a LAN. Put it behind a reverse
  proxy with TLS and a token before exposing it to the internet.
- **Tuning lives at the top of two files.** Detection sensitivity in
  `Node/node.toml`; how strict tracking is (`MAX_GAP_M`, `BUCKET_MS`,
  `MIN_NODES`) in the constants at the top of `Server/tracker.py`.
