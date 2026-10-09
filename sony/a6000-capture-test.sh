#!/usr/bin/env bash
# Fires the shutter once, downloads, deletes from camera. Bounded by timeout so a
# hang on the delete step is recorded as exit 124 rather than a stuck shell.
set -u
mkdir -p /tmp/a6000 && cd /tmp/a6000 || exit 1
t0=$(date +%s)
timeout 60 gphoto2 --capture-image-and-download --filename 'cap-%Y%m%d-%H%M%S.%C'
rc=$?
echo "exit=$rc elapsed=$(( $(date +%s) - t0 ))s"
ls -la /tmp/a6000
