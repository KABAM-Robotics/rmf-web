#!/usr/bin/env bash
# Entry point of the dashboard image: write the site's server URLs into the
# built index.html, then serve. Same contract as the upstream ghcr dashboard
# image (RMF_SERVER_URL / TRAJECTORY_SERVER_URL), so a site's compose file needs
# no change to move from the upstream image to this one.
set -euo pipefail
: "${RMF_SERVER_URL:?set RMF_SERVER_URL (e.g. http://<host>:8000)}"
: "${TRAJECTORY_SERVER_URL:=ws://localhost:8006}"
sed -i "s,__RMF_SERVER_URL__,${RMF_SERVER_URL},g" /opt/dashboard/index.html
sed -i "s,__TRAJECTORY_SERVER_URL__,${TRAJECTORY_SERVER_URL},g" /opt/dashboard/index.html
exec serve -s -n -l "${PORT:-3000}" /opt/dashboard
