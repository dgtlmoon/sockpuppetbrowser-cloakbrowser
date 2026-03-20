#!/bin/sh
# Disable core dumps to prevent large crash files
ulimit -c 0

# Start Xvfb in background if headful mode is requested
if [ "${CHROME_HEADFUL}" = "true" ] || [ "${ENABLE_XVFB}" = "true" ]; then
    echo "Starting Xvfb on display :99"
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX > /dev/null 2>&1 &
    export XVFB_PID=$!
    export DISPLAY=:99
    sleep 1
fi

cd /usr/src/app
exec python3 ./server.py "$@"
