#!/bin/bash
# Install (or remove) the LaunchAgent that keeps the carousel running.
#
# Usage:
#   tools/install_agent.sh              install and start
#   tools/install_agent.sh --uninstall  stop and remove
#   tools/install_agent.sh --launcher   install, but go through bin/ledclock
#
# By default the agent runs the bundled interpreter directly. That works
# because launchd is not an app, so macOS holds the process itself
# responsible for Bluetooth — and the process lives inside LedClock.app,
# which does carry NSBluetoothAlwaysUsageDescription. If your macOS version
# disagrees and the log fills with TCC aborts, reinstall with --launcher to
# route through bin/ledclock's LaunchServices path instead.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.ledclock.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/ledclock.log"
APP="$PROJECT_DIR/tools/LedClock.app"

# bootout returns before the job is actually gone, so an immediate bootstrap
# races it and fails with "service already loaded" — silently, since we
# suppress its stderr. Wait for the label to really disappear.
wait_gone() {
    local label="$1"
    for _ in $(seq 1 40); do
        launchctl list "$label" >/dev/null 2>&1 || return 0
        sleep 0.25
    done
    echo "warning: $label is still loaded" >&2
}

unload() {
    if launchctl list "$LABEL" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null \
            || launchctl unload "$PLIST" 2>/dev/null || true
    fi
    wait_gone "$LABEL"
}

MENU_LABEL="com.ledclock.menubar"
MENU_PLIST="$HOME/Library/LaunchAgents/$MENU_LABEL.plist"

unload_menu() {
    launchctl bootout "gui/$(id -u)/$MENU_LABEL" 2>/dev/null || true
    wait_gone "$MENU_LABEL"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    unload
    unload_menu
    rm -f "$PLIST" "$MENU_PLIST"
    echo "Removed $LABEL and $MENU_LABEL."
    exit 0
fi

if [[ ! -d "$APP" ]]; then
    echo "Missing $APP — run tools/make_app_bundle.sh first." >&2
    exit 1
fi

read -r BASE_PREFIX VERSION <<<"$(
    "$PROJECT_DIR/.venv/bin/python3" -c 'import sys; print(sys.base_prefix, "%d.%d" % sys.version_info[:2])'
)"
SITE_PACKAGES="$PROJECT_DIR/.venv/lib/python$VERSION/site-packages"

if [[ "${1:-}" == "--launcher" ]]; then
    PROGRAM_ARGS="        <string>$PROJECT_DIR/bin/ledclock</string>
        <string>-v</string>
        <string>run</string>"
else
    PROGRAM_ARGS="        <string>$APP/Contents/MacOS/ledclock-python</string>
        <string>-m</string>
        <string>ledclock.cli</string>
        <string>-v</string>
        <string>run</string>"
fi

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
$PROGRAM_ARGS
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONHOME</key>
        <string>$BASE_PREFIX</string>
        <key>PYTHONPATH</key>
        <string>$SITE_PACKAGES:$PROJECT_DIR</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <!-- Not <true/>: that revives the job even when it exited on purpose, so
         the menu bar's Quit did nothing but blank the graph for thirty
         seconds and come back. SuccessfulExit=false restarts a crash and
         respects a clean exit, which is what both of these want. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <!-- The daemon already backs off internally; this only guards against a
         crash loop that exits instantly. -->
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLISTEOF

unload
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
    || launchctl load -w "$PLIST"

# The menu bar app, in its own agent so it can be stopped on its own — and
# because it is a different bundle: this one may draw UI, the daemon may not.
cat > "$MENU_PLIST" <<MENUEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$MENU_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/tools/LedClockMenu.app/Contents/MacOS/ledclock-menu</string>
        <string>-m</string>
        <string>ledclock.menubar</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONHOME</key>
        <string>$BASE_PREFIX</string>
        <key>PYTHONPATH</key>
        <string>$SITE_PACKAGES:$PROJECT_DIR</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <!-- Not <true/>: that revives the job even when it exited on purpose, so
         the menu bar's Quit did nothing but blank the graph for thirty
         seconds and come back. SuccessfulExit=false restarts a crash and
         respects a clean exit, which is what both of these want. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/ledclock-menu.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/ledclock-menu.log</string>
</dict>
</plist>
MENUEOF

unload_menu
launchctl bootstrap "gui/$(id -u)" "$MENU_PLIST" 2>/dev/null \
    || launchctl load -w "$MENU_PLIST"

echo "Installed $LABEL"
echo "Installed $MENU_LABEL (menu bar)"
echo "  plist: $PLIST"
echo "  log:   $LOG"
echo
echo "  launchctl list | grep ledclock"
echo "  tail -f $LOG"
echo "  tools/install_agent.sh --uninstall"
