#!/usr/bin/env bash
# Thin wrapper — delegates to shared canonical fleet-manager.sh
export SEABONE_FLEET_DB="${SEABONE_FLEET_DB:-/home/dotmac/projects/.seabone-fleet/fleet.db}"
exec /home/dotmac/projects/shared-scripts/fleet-manager.sh "$@"
