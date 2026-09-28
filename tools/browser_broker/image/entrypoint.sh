#!/bin/bash
# Lease container entrypoint (auto-8q7oe.5), run under tini as PID 1.
# Starts the display, the VNC server, the watchdog and the lease agent; when
# any of them exits, everything stops and the container ends.
set -u
: "${BROWSER_SCREEN:?}" "${BROWSER_LEASE_SECRET:?}" "${BROWSER_VNC_PASSWORD:?}" "${BROWSER_LEASE_EXPIRES_AT:?}"
mkdir -p /tmp/lease && chmod 700 /tmp/lease
export DISPLAY=:99

# Take both secrets out of the environment before starting anything, so no
# helper inherits them; only the lease agent receives its secret, by a prefix
# assignment (never on a command line). tini and this shell still hold them in
# their initial environment block (/proc/<pid>/environ, readable by this uid
# only).
lease_secret="$BROWSER_LEASE_SECRET"
vnc_password="$BROWSER_VNC_PASSWORD"
unset BROWSER_LEASE_SECRET BROWSER_VNC_PASSWORD

Xvfb :99 -screen 0 "$BROWSER_SCREEN" -nolisten tcp >/tmp/lease/xvfb.log 2>&1 &
for _ in $(seq 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.1; done

# The password reaches x11vnc through a 0600 file written by the printf
# builtin (never on any command line); "rm:" makes x11vnc delete the file once
# read. (VNC authentication uses only the first 8 characters of the password.)
(umask 077; printf '%s\n' "$vnc_password" > /tmp/lease/vncpass)
unset vnc_password
x11vnc -display :99 -rfbport 5900 -passwdfile rm:/tmp/lease/vncpass -forever -shared \
    -noxdamage -quiet >/tmp/lease/x11vnc.log 2>&1 &

python -m tools.browser_broker.lease_watchdog &
BROWSER_LEASE_SECRET="$lease_secret" python -m tools.browser_broker.lease_agent &
unset lease_secret

wait -n
kill -TERM $(jobs -p) 2>/dev/null
sleep 2
exit 0
