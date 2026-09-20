import alerts
import db


def test_target_alert_message_contains_operator_useful_details(tmp_path):
    connection = db.connect(str(tmp_path / "alerts.db"))
    target = {
        "id": 1,
        "number": 4,
        "lat": 37.7749,
        "lon": -122.4194,
        "alt_m": 320.0,
        "speed_mps": 24.0,
        "node_ids": ["node-a", "node-b"],
        "contact_count": 8,
    }
    contacts = [
        {"lat": 37.7749, "lon": -122.4194, "alt_m": 320.0, "node_time_ms": 0},
        {"lat": 37.7750, "lon": -122.4194, "alt_m": 320.0, "node_time_ms": 1000},
        {"lat": 37.7751, "lon": -122.4194, "alt_m": 320.0, "node_time_ms": 2000},
        {"lat": 37.7752, "lon": -122.4194, "alt_m": 320.0, "node_time_ms": 3000},
    ]

    message = alerts.format_target_message(target, contacts)

    assert "T-4 CONFIRMED" in message
    assert "Position: 37.77490, -122.41940" in message
    assert "Altitude: 320 m" in message
    assert "Velocity: 24.0 m/s" in message
    assert "node-a, node-b" in message
    connection.close()


def test_alert_queue_is_idempotent(tmp_path):
    connection = db.connect(str(tmp_path / "alerts.db"))
    db.queue_target_alert(connection, 7)
    db.queue_target_alert(connection, 7)

    assert len(db.pending_target_alerts(connection)) == 1
    connection.close()