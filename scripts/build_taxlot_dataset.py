#!/usr/bin/env python3
# Offline preprocessing: fetches every taxlot and zoning polygon in the three-county RLIS region, clips to
# the Metropolitan Planning Area (see build_mpa_boundary.py - run that first) since that's the real
# geography this tool's own residential-zoning join cares about (fully contained within the wider
# three-county taxlot extent, which reaches past it into rural/exurban land this tool has no reason to
# cover), and spatially joins each remaining taxlot to its zoning class. Output is a flat JSON array the app
# loads once and filters entirely client-side - no live spatial queries needed at runtime.
#
# This is a trimmed fork of the same-named script in the TOD-PITCH repo, which also computes per-taxlot
# transit-distance fields this tool never uses - removed here rather than carried over, since that would
# otherwise require porting a whole separate transit-stop-fetching pipeline this tool has no use for.
#
# Run with: python3 scripts/build_mpa_boundary.py, then python3 scripts/build_taxlot_dataset.py
# Output:   runtime-data/taxlot_density_data.json (raw, kept locally as a cache/reference - never uploaded)
#           runtime-data/taxlot_density_data.json.gz (the one that actually gets uploaded to R2 - see its
#           own comment below for why)

import gzip
import json
import math
import os
import time
import urllib.request
import urllib.parse
import urllib.error
import http.client

TAXLOT_SERVICE_URL = 'https://services2.arcgis.com/McQ0OlIABe29rJJy/arcgis/rest/services/Taxlots_(Public)/FeatureServer/3/query'
ZONING_SERVICE_URL = 'https://services2.arcgis.com/McQ0OlIABe29rJJy/arcgis/rest/services/Zoning/FeatureServer/1/query'

RUNTIME_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'runtime-data')
# Crash-recovery checkpoints live here, not the repo root - large, gitignored build debris, same folder as
# this app's other non-deployed source files.
NON_ESSENTIAL_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'Non-essential data')

GRID_SIZE = 0.01  # Degrees (~0.5-0.7mi at this latitude) - grid cell size for the zoning-polygon spatial index used in the join below, coarse enough to keep the index small, fine enough to keep per-cell candidate lists short.

# Originally chosen against a real neighborhood's worth of taxlots in the Taxlot Resolution Preview
# artifact - 4.5m simplification tolerance and 5 decimal places (~1m coordinate precision, down from the
# source service's ~1cm) together got the sample down to a fraction of its original size with no visible
# loss of parcel shape at normal map zoom levels. Every visitor downloads this same dataset - no
# lower-fidelity alternative (a "simpler geometry" variant was tried and dropped: 15m/4-decimals only cut
# the actual gzipped download by ~11%, since most parcels are already simple 4-6 vertex rectangles with
# little redundant detail left to strip out - not worth maintaining two dataset variants for that little
# gain). Lowered to 4m to keep slightly more shape detail, at the cost of a bit more data per parcel to
# download and render.
SIMPLIFY_TOLERANCE_METERS = 4
COORDINATE_DECIMALS = 5

METERS_PER_DEGREE = 111320  # Rough - fine at this precision, same approximation the preview artifact used

MAX_RETRIES = 10
RETRY_TRANSIENT_ERRORS = (urllib.error.URLError, http.client.IncompleteRead, http.client.HTTPException, ConnectionError, TimeoutError)

# Empirically transient, not a real parameter problem: across four runs with unrelated changes (page_size
# 1000 vs 200, no orderByFields vs orderByFields=FID), this exact message has shown up at wildly different
# offsets (~554k, ~580k, ~82k, ~116k) instead of failing at the same spot every time - a real
# invalid-parameter error (like the OBJECTID/FID field-name mistake this script hit earlier) fails
# immediately and consistently, every time, which this does not. So it gets retried like a network error
# instead of raising straight away. The retries themselves confirm this: in the run that finally exhausted
# 5 attempts, every single prior occurrence of this exact error DID succeed on a later retry - the error
# rate just climbed over the course of the run (isolated single retries early on, 3-4 in a row by the end),
# consistent with ArcGIS progressively rate-limiting/throttling a long, fast run rather than anything
# actually wrong with the request - hence MAX_RETRIES=10 (up from 5) rather than treating the eventual
# failure as proof retrying doesn't help.
#
# 'Wait timeout for the request exceeded' (a 503, seen against portlandmaps.com's own ArcGIS services in
# add_portland_rip_density.py) is the same class of thing for the same reason - a server-load message, not a
# malformed request, and it showed up well before any client-side timeout/connection error would (those are
# already covered separately by RETRY_TRANSIENT_ERRORS below) - the server itself is saying "try again", so
# this list is exactly where that belongs alongside the invalid-query-parameters message above.
RETRY_ERROR_MESSAGE_SUBSTRINGS = ('Cannot perform query. Invalid query parameters.', 'Wait timeout for the request exceeded')


def post_query(url, params):
    data = urllib.parse.urlencode(params).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    # ArcGIS's public services drop the odd connection mid-response on a fetch this large (~650 pages for
    # taxlots alone) - retrying the single failed page is far cheaper than losing everything fetched so
    # far, which is what happened the first time this ran (33k taxlots in, one IncompleteRead killed the
    # whole multi-minute run).
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            if 'error' in body:
                message = body['error'].get('message', '')
                if any(s in message for s in RETRY_ERROR_MESSAGE_SUBSTRINGS) and attempt < MAX_RETRIES:
                    wait = min(2 ** attempt, 30)  # Capped - 2**10 would be 17 minutes on the last attempt otherwise
                    print('  transient-looking ArcGIS error (%s), retrying in %ds (attempt %d/%d)...' % (message, wait, attempt, MAX_RETRIES))
                    time.sleep(wait)
                    continue
                raise RuntimeError('ArcGIS query error %s: %s' % (body['error'].get('code'), message))
            return body
        except RETRY_TRANSIENT_ERRORS as e:
            if attempt == MAX_RETRIES:
                raise
            wait = min(2 ** attempt, 30)
            print('  transient error (%s), retrying in %ds (attempt %d/%d)...' % (e, wait, attempt, MAX_RETRIES))
            time.sleep(wait)


def fetch_all_features(url, where, out_fields, extra=None, checkpoint_name=None, page_size=1000):
    # Resumable: every page is appended to checkpoint_name (if given) as it arrives, and a prior partial
    # checkpoint is picked back up on the next run instead of re-fetching pages already saved - on top of
    # post_query's own per-page retries, so a run that dies anyway (e.g. the process itself gets killed)
    # still doesn't have to start over from offset 0.
    features = []
    offset = 0

    if checkpoint_name and os.path.exists(checkpoint_name):
        with open(checkpoint_name) as f:
            features = json.load(f)
        offset = len(features)
        print('  resuming %s from checkpoint: %d features already fetched' % (checkpoint_name, offset))

    while True:
        params = {
            'where': where,
            'outFields': out_fields,
            'outSR': '4326',
            'resultOffset': offset,
            'resultRecordCount': page_size,
            'f': 'json',
        }
        if extra:
            params.update(extra)
        body = post_query(url, params)
        page = body.get('features', [])
        features.extend(page)
        print('  fetched page at offset %d: %d features (running total %d)' % (offset, len(page), len(features)))
        if checkpoint_name:
            with open(checkpoint_name, 'w') as f:
                json.dump(features, f)
        if not body.get('exceededTransferLimit'):
            break
        # A small deliberate pace, not just a reaction to failures - the last run's error rate climbed
        # over time (isolated retries early on, several in a row by the end), consistent with ArcGIS
        # throttling a long fast run rather than one-off flakiness. Slowing down proactively is cheap
        # compared to losing an hour of progress to an exhausted retry budget near the very end.
        time.sleep(0.3)
        offset += len(page)
    return features


def load_taxlot_dataset(path=None):
    # The other half of write_taxlot_dataset() below - reconstructs the familiar list-of-dicts shape (one
    # dict per taxlot, keyed by field name) from the compact columnar format that function writes, so every
    # script reading the dataset (this file's own main(), fetch_taxlot_addresses.py,
    # fetch_taxlot_housing_units.py, add_portland_rip_density.py) can keep using plain t['fieldname'] access
    # unchanged, same as taxlot-parse-worker.js's own rowsToObjects() does client-side. A raw json.load()
    # here would silently return the wrong shape entirely (a 2-key {'fields':.., 'rows':..} dict, not a list
    # of taxlots) rather than erroring obviously - this is the one place that mistake can happen, so every
    # reader should go through here instead of calling json.load() on this file directly.
    if path is None:
        path = os.path.join(RUNTIME_DATA_DIR, 'taxlot_density_data.json')
    with open(path) as f:
        parsed = json.load(f)
    fields = parsed['fields']
    return [dict(zip(fields, row)) for row in parsed['rows']]


def write_taxlot_dataset(taxlots, out_path=None):
    # Single source of truth for serializing the taxlot dataset, shared by every script that writes it
    # (this one, fetch_taxlot_addresses.py, fetch_taxlot_housing_units.py) - previously each script
    # duplicated its own near-identical json.dumps+gzip write block.
    #
    # Compact columnar format ({'fields': [...], 'rows': [[...], ...]}) instead of one JSON object per
    # taxlot repeating every field name - measured directly on the real (unforked) dataset once
    # 'address'/'siteLat'/'siteLng'/'existingUnits' were added: repeated key names alone accounted for 60%
    # of the file's own byte size, which pushed the decompressed JSON past the ~537MB hard ceiling Chrome's
    # V8 places on a single JS string - taxlot-parse-worker.js's own Blob.text() call silently returned an
    # empty string past that point (no error thrown), which is what actually broke taxlot loading. This
    # format writes every field name exactly once (in 'fields') and each taxlot as a plain positional array
    # in the same order - taxlot-parse-worker.js reconstructs the named-object shape immediately after
    # parsing, so every other consumer is unaffected. A taxlot missing a field entirely (e.g. 'address' on a
    # taxlot with no MAF match) gets null in that position via .get(), not a KeyError.
    if out_path is None:
        out_path = os.path.join(RUNTIME_DATA_DIR, 'taxlot_density_data.json')

    fields = set()
    for t in taxlots:
        fields.update(t.keys())
    fields = sorted(fields)
    rows = [[t.get(f) for f in fields] for t in taxlots]

    encoded = json.dumps({'fields': fields, 'rows': rows}, separators=(',', ':')).encode('utf-8')
    with open(out_path, 'wb') as f:
        f.write(encoded)
    print('Wrote %s (%d bytes)' % (out_path, len(encoded)))

    # ~79% smaller in practice (repetitive numeric patterns across hundreds of thousands of records compress
    # extremely well even after the columnar rewrite above), which is also what actually gets this under
    # R2's ~300MB dashboard upload cap without needing the S3-compatible API or wrangler for a manual
    # upload. Upload THIS file to R2 under the object key taxlot_density_data.json (same key the Worker
    # already reads, see workers/density-mapper-worker.js) - not the raw one above, which is kept locally
    # only as a cache/reference. That Worker does NOT set Content-Encoding: gzip on this (see its own
    # comment on why that turned out to be unreliable behind a Cloudflare Access setup) -
    # taxlot-parse-worker.js decompresses it client-side instead, via the native DecompressionStream API.
    gz_path = out_path + '.gz'
    with gzip.open(gz_path, 'wb', compresslevel=9) as f:
        f.write(encoded)
    print('Wrote %s (%d bytes, %.1f%% smaller)' % (gz_path, os.path.getsize(gz_path), 100 * (1 - os.path.getsize(gz_path) / len(encoded))))
    return out_path, gz_path


def point_in_polygon(lng, lat, rings):
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
    return inside


def perpendicular_distance(pt, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    mag = math.sqrt(dx * dx + dy * dy)
    if mag > 0:
        dx, dy = dx / mag, dy / mag
    pvx, pvy = pt[0] - a[0], pt[1] - a[1]
    dot = pvx * dx + pvy * dy
    sx, sy = a[0] + dot * dx, a[1] + dot * dy
    ax, ay = pt[0] - sx, pt[1] - sy
    return math.sqrt(ax * ax + ay * ay)


def rdp_simplify(points, epsilon):
    # Standard recursive Ramer-Douglas-Peucker, same algorithm (and same math) as the Taxlot Resolution
    # Preview artifact used, so what got previewed there is exactly what this produces.
    if len(points) < 3 or epsilon <= 0:
        return points
    dmax, index = 0, 0
    end = len(points) - 1
    for i in range(1, end):
        d = perpendicular_distance(points[i], points[0], points[end])
        if d > dmax:
            dmax, index = d, i
    if dmax > epsilon:
        left = rdp_simplify(points[:index + 1], epsilon)
        right = rdp_simplify(points[index:], epsilon)
        return left[:-1] + right
    return [points[0], points[end]]


def compress_rings(rings, tolerance_meters, decimals):
    epsilon_deg = tolerance_meters / METERS_PER_DEGREE
    compressed = []
    for ring in rings:
        simplified = rdp_simplify(ring, epsilon_deg)
        compressed.append([[round(pt[0], decimals), round(pt[1], decimals)] for pt in simplified])
    return compressed


def grid_cells_for_bbox(bbox, cell_size):
    bx0, by0, bx1, by1 = bbox
    x0, x1 = int(bx0 // cell_size), int(bx1 // cell_size)
    y0, y1 = int(by0 // cell_size), int(by1 // cell_size)
    return [(gx, gy) for gx in range(x0, x1 + 1) for gy in range(y0, y1 + 1)]


def load_mpa_boundary():
    path = os.path.join(RUNTIME_DATA_DIR, 'mpa_boundary.json')
    if not os.path.exists(path):
        raise RuntimeError('runtime-data/mpa_boundary.json not found - run scripts/build_mpa_boundary.py first')
    with open(path) as f:
        return json.load(f)['rings']


# point_in_polygon above tests one point against every edge of every ring - fine for the zoning join (each
# zoning polygon is small, a few dozen vertices), but the MPA boundary's own outer ring alone is thousands
# of vertices, and every one of the ~645k taxlots needs this check. Ray casting only ever needs edges whose
# y-range actually straddles the point's own latitude - bucketing edges into coarse latitude rows once
# (same GRID_SIZE the zoning join already uses) means a per-point check only tests the handful of edges
# that could possibly cross that point's own row, instead of every edge in the whole boundary. Cuts the real
# full-dataset run from an impractical multi-hour scan down to a small fraction of the join step's own time.
def build_boundary_row_index(rings, cell_size):
    index = {}
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            row0 = int(min(yi, yj) // cell_size)
            row1 = int(max(yi, yj) // cell_size)
            for row in range(row0, row1 + 1):
                index.setdefault(row, []).append((xi, yi, xj, yj))
            j = i
    return index


def point_in_boundary_indexed(lng, lat, row_index, cell_size):
    edges = row_index.get(int(lat // cell_size))
    if not edges:
        return False
    inside = False
    for xi, yi, xj, yj in edges:
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
    return inside


def main():
    print('Loading MPA boundary...')
    mpa_rings = load_mpa_boundary()
    mpa_row_index = build_boundary_row_index(mpa_rings, GRID_SIZE)

    print('Fetching every taxlot in the region (no distance restriction - full regional coverage)...')
    taxlot_features = fetch_all_features(
        TAXLOT_SERVICE_URL, '1=1', 'TLID,GIS_ACRES',
        # orderByFields is the actual fix here, not the smaller page_size below: dropping page_size from
        # 1000 to 200 only moved the reproducible "400: Invalid query parameters" failure from offset
        # ~554k to ~580.6k, rather than eliminating it - a dead giveaway that this was never about
        # response/geometry size. Without an explicit sort, ArcGIS doesn't guarantee stable ordering across
        # resultOffset/resultRecordCount pages on a table this large, and deep offset+fetch on an unordered
        # query is a known cause of exactly this kind of server-side 400 at large offsets. This layer's
        # object-id field is FID, not the more common OBJECTID (confirmed via ?f=json on the layer).
        {'returnGeometry': 'true', 'returnCentroid': 'true', 'orderByFields': 'FID'},
        checkpoint_name=os.path.join(NON_ESSENTIAL_DATA_DIR, 'taxlot_features_checkpoint.json'),
        page_size=200,
    )
    print('Total taxlots: %d' % len(taxlot_features))

    print('Fetching every zoning polygon in the region...')
    # ZONEGEN_CL is the service's own "Zoning Generalized Classification" field - the official ~10 broad
    # buckets (COM/FUD/IND/MFR/MUE/MUR/PF/POS/RUR/SFR) - fetched here directly rather than reconstructed
    # client-side by prefix-matching ZONE_CLASS codes, since the service already has the authoritative
    # mapping.
    zoning_features = fetch_all_features(
        ZONING_SERVICE_URL, '1=1', 'ZONE_CLASS,ZONEGEN_CL',
        {'returnGeometry': 'true', 'orderByFields': 'FID'},
        checkpoint_name=os.path.join(NON_ESSENTIAL_DATA_DIR, 'zoning_features_checkpoint.json'),
    )
    print('Total zoning polygons: %d' % len(zoning_features))

    # Build a coarse spatial grid index over the zoning polygons so the join below only has to test a
    # handful of candidates per taxlot instead of scanning every zoning polygon - necessary at full
    # regional scale (500k+ taxlots even after the MPA clip below rejects the rest early), where a plain
    # bbox-filtered linear scan would be far too slow.
    print('Building spatial index over zoning polygons...')
    zoning_grid = {}
    for zf in zoning_features:
        xs = [pt[0] for ring in zf['geometry']['rings'] for pt in ring]
        ys = [pt[1] for ring in zf['geometry']['rings'] for pt in ring]
        zf['_bbox'] = (min(xs), min(ys), max(xs), max(ys))
        for cell in grid_cells_for_bbox(zf['_bbox'], GRID_SIZE):
            zoning_grid.setdefault(cell, []).append(zf)

    print('Joining each taxlot to its zoning class...')
    output = []
    seen_tlid = set()
    for i, tf in enumerate(taxlot_features):
        tlid = tf['attributes']['TLID']
        if tlid in seen_tlid:
            continue
        seen_tlid.add(tlid)

        lng, lat = tf['centroid']['x'], tf['centroid']['y']
        if not point_in_boundary_indexed(lng, lat, mpa_row_index, GRID_SIZE):
            continue  # Outside the Metropolitan Planning Area - see build_mpa_boundary.py's own comment on why this is the actually-correct geography, not just a size optimization. Checked first, before the (more expensive) zoning join below, since it rejects a real chunk of the three-county extent outright.
        cell = (int(lng // GRID_SIZE), int(lat // GRID_SIZE))
        zone_class = None
        zone_gen_class = None
        for zf in zoning_grid.get(cell, []):
            bx0, by0, bx1, by1 = zf['_bbox']
            if lng < bx0 or lng > bx1 or lat < by0 or lat > by1:
                continue
            if point_in_polygon(lng, lat, zf['geometry']['rings']):
                zone_class = zf['attributes']['ZONE_CLASS']
                zone_gen_class = zf['attributes']['ZONEGEN_CL']
                break
        if zone_class is None:
            continue  # Taxlot didn't fall inside any fetched zoning polygon - excluded rather than guessed, matching this app's own convention.
        if not tf.get('geometry', {}).get('rings'):
            continue  # A handful of taxlots have a valid centroid but no real parcel polygon (null/missing geometry from the source service) - same "excluded rather than guessed" convention as the zone_class check above, since there's no shape to draw on the map either way.

        output.append({
            'tlid': tlid,
            'acres': tf['attributes']['GIS_ACRES'],
            'zoneClass': zone_class,
            'zoneGenClass': zone_gen_class,
            'lat': lat,
            'lng': lng,
            'rings': compress_rings(tf['geometry']['rings'], SIMPLIFY_TOLERANCE_METERS, COORDINATE_DECIMALS),  # Real (simplified/rounded) parcel boundary, so the map can draw the actual taxlot shape instead of a centroid dot.
        })

        if (i + 1) % 10000 == 0:
            print('  joined %d / %d taxlots' % (i + 1, len(taxlot_features)))

    print('Final dataset: %d taxlots' % len(output))
    write_taxlot_dataset(output)


if __name__ == '__main__':
    main()
