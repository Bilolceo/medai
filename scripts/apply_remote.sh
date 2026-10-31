#!/bin/sh
# Applies a doctor's template to the deployment the Railway CLI is linked to.
#
#   sh scripts/apply_remote.sh data/dr-ISM.json            # writes (bot stays OFF)
#   DRY_RUN=1 sh scripts/apply_remote.sh data/dr-ISM.json  # prints only
#
# The database is reachable only from inside Railway, so the script runs there
# (`railway ssh`) with the facts passed in, and the previous settings are saved
# next to the facts file as <name>.backup.json. Link the doctor's own project
# first (`railway link`); with several services, name the web one:
# RAILWAY_SERVICE=web sh scripts/apply_remote.sh ...
set -e
FACTS="${1:?Usage: $0 <doctor facts .json>}"
[ -f "$FACTS" ] || { echo "No such file: $FACTS" >&2; exit 1; }
B=$(base64 < "$FACTS" | tr -d '\n')
ENVS="DOCTOR_JSON=\$(echo $B | base64 -d) TEMPLATE=${TEMPLATE:-urolog} DRY_RUN=${DRY_RUN:-0} FORCE=${FORCE:-0}"
[ -n "$TENANT_NAME" ] && ENVS="$ENVS TENANT_NAME='$TENANT_NAME'"
OUT=$(railway ssh ${RAILWAY_SERVICE:+-s "$RAILWAY_SERVICE"} -- sh -c "$ENVS python scripts/apply_template.py" </dev/null 2>&1) || true
printf "%s\n" "$OUT" | sed -n '/BACKUP_BEGIN/,/BACKUP_END/p' | sed '1d;$d' > "${FACTS%.json}.backup.json"
printf "%s\n" "$OUT" | grep -vE 'BACKUP_|^\{|Using SSH'
[ -s "${FACTS%.json}.backup.json" ] && echo "backup: ${FACTS%.json}.backup.json" || rm -f "${FACTS%.json}.backup.json"
