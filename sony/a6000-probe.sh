#!/usr/bin/env bash
# Read-only probe of the a6000 over PTP. Records what the Sony README says to record
# before any backend code is trusted. Does not capture.
set -u
echo "== versions"
dpkg-query -W -f='${Package} ${Version}\n' libgphoto2-6t64 gphoto2 2>/dev/null
gphoto2 --version | head -1
echo "== detect"
gphoto2 --auto-detect
echo "== config widgets (label / readonly / current)"
for k in expprogram shutterspeed f-number iso exposurecompensation whitebalance focusmode imagequality capturemode; do
  echo "-- $k"
  gphoto2 --get-config "$k" 2>&1 | grep -E '^(Label|Readonly|Current)'
done
echo "== second read of shutterspeed/iso (Sony populates late)"
sleep 2
for k in shutterspeed iso f-number; do
  echo "-- $k"
  gphoto2 --get-config "$k" 2>&1 | grep -E '^(Readonly|Current)'
done
