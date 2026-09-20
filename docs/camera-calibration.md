# Camera orientation calibration

Status: **idea, not built.** A plan for replacing hand-typed yaw/pitch/roll with
orientation measured against a known reference.

## The problem

A node knows only its own id. It has no GPS, no compass and no IMU, so it has
no way to learn which way it points. No startup script on the node can work
this out: orientation has to be measured against something whose direction is
already known.

Yaw is the angle that matters. A 5° yaw error at 2 km moves a crossing by about
175 m. Pitch and roll are easy by comparison.

## References we considered

| Reference | Gives | Accuracy | Notes |
|---|---|---|---|
| Accelerometer / IMU (e.g. BNO085) | pitch, roll | ~1° | ~$20/node; could ride in the heartbeat |
| Magnetometer | yaw | 5-15° | thrown off by nearby metal and needs a declination correction; not good enough |
| Star plate-solve (tetra3) | yaw, pitch, roll | <0.1° | needs a dark, clear sky plus the node's lat/lon and the time |
| Sun position | yaw, pitch | ~0.5° | only works if the sun is in the 62° field of view |
| Known target, solved on the Server | yaw, pitch, roll | ~1° | no new hardware; keeps the Server as source of truth |
| Landmarks clicked in a snapshot | yaw, pitch, roll | ~0.5-1° | no hardware, daytime; needs 3+ known landmarks in frame |

**Chosen direction:** calibrate on the Server against a known target. There is
no drone, so the target is a **phone at a fixed, known altitude**.

## What the Server needs

Already stored:

- `detections.cam_az_deg`, `cam_el_deg`, `node_time_ms`: the measured angles
- `nodes.lat`, `lon`, `alt_m`: where each bearing starts
- `nodes.clock_offset_ms`: flags a node whose clock has drifted

New:

- **Truth points:** `(time, lat, lon, alt)` for the calibration light. Take the
  altitude from building floor heights, not from phone GPS (GPS altitude is off
  by about 10 m).

Solve for: yaw, pitch, roll. Optional extra unknowns: a clock offset and the
horizontal field of view.

Method: least squares on the angle error, with outlier rejection, as the inverse
of `geometry.camera_to_world_azel`.

Quality gate: the RMS residual (typical leftover error per point).

- **Under about 0.5°:** trust the fit.
- **Over about 2°:** the node's position, clock or FOV is wrong, not its aim.

## The catch: the phone must be in frame

The simulator pitches cameras 30° up with a 48.8° vertical FOV, so the bottom of
the frame is **5.6° above the horizon**. A phone held at the camera's height
never appears. To be seen, it must sit above the camera by about 10% of its
distance from the camera:

| Distance | Height above the camera |
|---|---|
| 50 m | ~5 m |
| 100 m | ~10 m |
| 300 m | ~30 m |

Phone GPS is off by 3-5 m horizontally: about 6° of error at 50 m, and about 1°
at 300 m. So the target has to be both **far and high**. That means a rooftop or
parking garage 100-200 m from the node.

## Procedure

1. Put the node on the ground and the phone on a roof or garage level of known
   height.
2. Run a **flashlight strobe app** and **stand still** at 8-10 spots spread
   across the camera's view, for about 15 s each. Averaging the GPS fixes brings
   each spot to within 1-2 m. The strobe gives the motion detector a change to
   react to even though the phone isn't moving, and standing still removes the
   clock-alignment error.
3. Do it at dusk or at night so the light stands out.
4. Upload the spots to the Server and run the solve. Check the residual, then
   save yaw/pitch/roll to the node.

## Work needed

- [ ] **Node calibration mode:** drop `min_active_pixels` from 50 to about 3
      (a phone light 150 m away is only a few pixels), and only in this mode.
- [ ] **Server endpoint:** accept truth points for a node and a time window.
- [ ] **Solver:** in `geometry.py` or a new `calibrate.py`, using
      `scipy.optimize.least_squares` with a robust loss.
- [ ] **Dashboard:** a "calibrate" action on the node editor that shows the fit,
      the residual, and a button to apply the result.
- [ ] **Fallback:** click-landmarks-in-a-snapshot via `preview.py`, using the
      same solver.
- [ ] **Later:** tetra3 star solve as a night-time re-check.

## Open question

Is there a tall building or parking garage within about 200 m of the node
sites? If not, use the landmark method instead.
