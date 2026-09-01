#!/usr/bin/env bash
# Stages the handful of files portland-housing-density-mapper.html actually needs into dist_assets/ for
# wrangler deploy to serve as static assets - see wrangler.jsonc's own assets.directory. This repo only ever
# serves this one page plus its own small set of dependencies, so an explicit allowlist here is simpler and
# safer than a git-archive-plus-denylist approach. A plain copy (not git archive) is fine specifically
# because Cloudflare Workers Builds always runs this against a fresh checkout of the pushed commit - there's
# no uncommitted local state to accidentally pull in the way a local wrangler deploy run could have.
#
# The giant taxlot_density_data.json.gz (~70MB, over Cloudflare's 25MiB static-asset limit) is deliberately
# NOT staged here - see workers/density-mapper-worker.js, which serves it from R2 instead. The taxlot-mode
# fallback URL in portland-housing-density-mapper.html's own JS will 404 in production if that R2 route ever
# fails, rather than falling back to a local copy.
#
# Run with: bash scripts/stage_deploy_assets.sh
# Invoked automatically by wrangler.jsonc's own "build.command" before every `wrangler deploy`/`wrangler dev`.

set -euo pipefail
cd "$(dirname "$0")/.."

DIST_DIR="dist_assets"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR/runtime-data" "$DIST_DIR/workers"

cp portland-housing-density-mapper.html "$DIST_DIR/"
cp "Density favicon.png" "$DIST_DIR/"
cp runtime-data/density_formulas.json "$DIST_DIR/runtime-data/"
cp runtime-data/zoning_polygons.json.gz "$DIST_DIR/runtime-data/"
cp runtime-data/census_tracts.json.gz "$DIST_DIR/runtime-data/"
cp runtime-data/census_blocks.json.gz "$DIST_DIR/runtime-data/"
cp workers/taxlot-parse-worker.js "$DIST_DIR/workers/"

echo "Staged $(find "$DIST_DIR" -type f | wc -l) files into $DIST_DIR/"
