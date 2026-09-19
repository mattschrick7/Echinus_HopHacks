# Node test captures

Record real footage off a node so the detector can be tuned against it later,
without needing the hardware in front of you.

`capture_sample.py` records from the node's camera and trims the first and last
ten seconds, so the shake from setting up and walking away from the tripod
doesn't end up in the fixture. Clips land in `samples/`, which is git-ignored.

```bash
# from the repo root
uv run python Node/testing/capture_sample.py Node/testing/samples/target1.mp4 --duration 120

# trim an existing file instead of capturing
uv run python Node/testing/capture_sample.py out.mp4 --input raw.mov
```

`blink_and_capture.sh` wraps the same script and flashes the Pi's onboard LED
while it records, so you get an "it's working" signal in the field with no
monitor or wifi. `echinus-test-capture.service` runs that wrapper at boot:

```bash
sed "s|%REPO_DIR%|$HOME/echinus|g" Node/testing/echinus-test-capture.service \
    | sudo tee /etc/systemd/system/echinus-test-capture.service
sudo systemctl enable --now echinus-test-capture.service
```

Replay a clip through the real detector:

```bash
uv run echinus-node --config Node/node.toml --dry-run --preview \
    --source Node/testing/samples/target1.mp4 --loop
```
