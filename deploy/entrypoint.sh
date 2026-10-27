#!/bin/bash
# Activate the pixi environment, then run the container command in it.
set -euo pipefail
# shellcheck disable=SC1091
source /opt/shell-hook.sh
exec "$@"
