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
# Optional map-image URL rewrite for sites that front the api-server with a TLS
# proxy (Waterloo: MAP_IMAGE_URL_FROM=http://<ip>:8000 MAP_IMAGE_URL_TO=https://<host>/server).
# Unset = no rewrite; the placeholders are left in place and ignored by the app.
if [[ -n "${MAP_IMAGE_URL_FROM:-}" ]]; then
  sed -i "s,__MAP_IMAGE_URL_FROM__,${MAP_IMAGE_URL_FROM},g" /opt/dashboard/index.html
  sed -i "s,__MAP_IMAGE_URL_TO__,${MAP_IMAGE_URL_TO:-},g" /opt/dashboard/index.html
fi
exec serve -s -n -l "${PORT:-3000}" /opt/dashboard
