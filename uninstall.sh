#!/bin/bash
# Remove MagnetKit's services, app and command link. Pass --purge to also delete config, cache and logs.
# Downloaded files are never touched.
set -euo pipefail
DEST="$HOME/.local/share/magnetkit"
if [[ -f "$DEST/magnet.py" ]]; then
  /usr/bin/python3 "$DEST/magnet.py" _uninstall "$@"
  rm -rf "$DEST"
else
  /usr/bin/python3 "$(dirname "$0")/magnet.py" _uninstall "$@"
fi
