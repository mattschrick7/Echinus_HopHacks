"""Opt-in Twilio SMS delivery for confirmed targets."""
from __future__ import annotations

import asyncio
import base64
import os
import urllib.parse
import urllib.request

import classification
import db


def enabled() -> bool:
    return os.environ.get("ECHINUS_SMS_ENABLED", "0").lower() in {"1", "true", "yes"}


def _config() -> tuple[str, str, str, str] | None:
    values = tuple(os.environ.get(name, "") for name in (
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_TO_NUMBER"))
    return values if all(values) else None


def format_target_message(target: dict, contacts: list[dict]) -> str:
    details = classification.classify_target(target, contacts)
    speed = target.get("speed_mps")
    speed_text = "unknown" if speed is None else f"{speed:.1f} m/s ({speed * 2.23694:.1f} mph)"
    altitude = target.get("alt_m")
    altitude_text = "unknown" if altitude is None else f"{altitude:.0f} m"
    reasons = ", ".join(details["classification_reasons"])
    return (
        f"ECHINUS TARGET T-{target['number']} CONFIRMED\n"
        f"Type: {details['classification']} ({details['confidence']:.0%})\n"
        f"Position: {target['lat']:.5f}, {target['lon']:.5f}\n"
        f"Altitude: {altitude_text}\n"
        f"Velocity: {speed_text}\n"
        f"Nodes: {', '.join(target['node_ids']) or 'unknown'}\n"
        f"Observations: {target['contact_count']}\n"
        f"Why: {reasons}"
    )


def send_sms(body: str) -> None:
    config = _config()
    if not config:
        raise RuntimeError("Twilio configuration is incomplete")
    account_sid, auth_token, from_number, to_number = config
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode({"From": from_number, "To": to_number, "Body": body}).encode(),
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError(f"Twilio returned HTTP {response.status}")


def process_pending(conn) -> None:
    """Send queued alerts; errors remain recorded for diagnosis and retry."""
    if not enabled() or not _config():
        return
    for alert in db.pending_target_alerts(conn):
        target = db.get_track(conn, alert["track_id"])
        if not target or target["status"] != "active":
            db.mark_target_alert_sent(conn, alert["track_id"])
            continue
        try:
            send_sms(format_target_message(target, db.track_contacts(conn, target["id"])))
        except Exception as exc:
            db.mark_target_alert_error(conn, alert["track_id"], str(exc))
            print(f"target SMS failed for track {alert['track_id']}: {exc}", flush=True)
        else:
            db.mark_target_alert_sent(conn, alert["track_id"])
            print(f"target SMS sent for T-{target['number']}", flush=True)


async def run(conn) -> None:
    while True:
        await asyncio.sleep(1.0)
        await asyncio.to_thread(process_pending, conn)