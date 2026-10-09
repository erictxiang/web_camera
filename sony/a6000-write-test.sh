#!/usr/bin/env bash
# Round-trip write test: set a value, read it back, restore. Does not capture.
set -u
rt() { # key value
  echo "-- set $1 = $2"
  gphoto2 --set-config "$1=$2" 2>&1 | grep -vE '^$'
  sleep 1
  gphoto2 --get-config "$1" 2>&1 | grep -E '^Current'
}
echo "== shutterspeed"
rt shutterspeed 1/250
rt shutterspeed 1/125
echo "== iso"
rt iso 400
rt iso "Auto ISO"
echo "== f-number"
rt f-number 5.6
rt f-number 3.5
