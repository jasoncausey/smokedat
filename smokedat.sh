#!/bin/bash
# Start the SmokeDat bridge from this checkout, keeping config, keys and logs in this directory.
# caffeinate keeps the Mac from idle-sleeping while it runs (closing the lid on battery will still sleep it).
cd "$(dirname "$0")"
if [ ! -x venv/bin/smokedat ]; then
  { [ -x venv/bin/python ] || python3 -m venv venv; } && ./venv/bin/pip install -q -e .
fi
exec caffeinate -i ./venv/bin/smokedat --data-dir . "$@"
