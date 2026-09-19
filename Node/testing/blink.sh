# Shared onboard-LED "it's working" blinker for boot-time test captures.
#
# Source this file, then call start_blink before the long-running capture and
# stop_blink on exit (via `trap stop_blink EXIT`). Blinking the Pi's onboard
# LED gives a visual "the node is busy capturing" signal with no wifi or
# monitor once the Pi is out in the field. A no-op on machines without the LED
# sysfs entries (e.g. a dev laptop), so the same wrapper works everywhere.

_LED_DIR=""
_BLINK_PID=""

start_blink() {
    for candidate in /sys/class/leds/ACT /sys/class/leds/led0; do
        [ -d "$candidate" ] && _LED_DIR="$candidate" && break
    done
    [ -n "$_LED_DIR" ] || return 0

    echo none > "$_LED_DIR/trigger" 2>/dev/null || true
    ( while true; do
        echo 1 > "$_LED_DIR/brightness" 2>/dev/null
        sleep 0.5
        echo 0 > "$_LED_DIR/brightness" 2>/dev/null
        sleep 0.5
      done ) &
    _BLINK_PID=$!
}

stop_blink() {
    [ -n "$_BLINK_PID" ] && kill "$_BLINK_PID" 2>/dev/null
    if [ -n "$_LED_DIR" ]; then
        # Restore the default activity trigger (mmc0), falling back to just
        # turning the LED off if that trigger isn't available.
        echo mmc0 > "$_LED_DIR/trigger" 2>/dev/null || echo 0 > "$_LED_DIR/brightness" 2>/dev/null
    fi
}
