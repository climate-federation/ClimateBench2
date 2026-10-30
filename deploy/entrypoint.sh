#!/bin/bash
# Activate the pixi environment, then run the container command in it.
set -eo pipefail
# The environment's conda activation scripts read unset variables (e.g.
# magics' MAGPLUS_HOME), so `set -u` must stay off while they run.
# shellcheck disable=SC1091
source /opt/shell-hook.sh
set -u
exec "$@"
