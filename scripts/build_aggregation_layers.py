#!/usr/bin/env python3
# One-time build: prepares three new map layers for portland-housing-density-mapper.html's "view by" modes -
# Portland zoning polygons (real city zone boundaries, reusing the cached fetch from
# add_portland_rip_density.py rather than re-fetching), Census Tracts, and Census Blocks (both real
# TIGERweb geometry, fetched fresh here). Each polygon gets existingUnits/acres aggregated from every
# taxlot whose centroid falls inside it (point-in-polygon, grid-indexed for speed - a naive O(polygons x
# taxlots) join would be tens of billions of ops), plus a computed max-density figure where one exists.
#
# Portland's own zoning code does NOT cap multi-dwelling zones (RM1-4/RX) by a flat units/acre number -
# density there is controlled by Floor Area Ratio and other bulk standards instead (confirmed via Portland
# City Code 33.120, Table 120-3: "no maximum" for RM1/RM2/RM3/RM4/RX; only RMP, a manufactured-dwelling-park
# zone, has one: 1 unit/1,500 sq ft). Only the R-zones (R20/R10/R7/R5/R2.5, RIP-eligible) get a real computed
# max-density number here, using the same RIP formula as index.html's own computeRipMaxUnits. Every other
# zone's max density is honestly reported as not applicable rather than a fabricated number.
#
# Run with: python3 scripts/build_aggregation_layers.py
# Reads: Non-essential data/portland_rip_zoning_checkpoint.json, runtime-data/taxlot_density_data.json.gz,
#        runtime-data/city_boundaries.json, live Census TIGERweb REST API
# Writes: runtime-data/zoning_polygons.json.gz, runtime-data/census_tracts.json.gz, runtime-data/census_blocks.json.gz

import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DATA_DIR = os.path.join(BASE_DIR, 'runtime-data')
NON_ESSENTIAL_DATA_DIR = os.path.join(BASE_DIR, 'Non-essential data')

TIGERWEB_URL = 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer'
MAX_RETRIES = 6
RETRY_TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)

# Single source of truth for every zoning/density constant below, shared with portland-housing-density-mapper.html
# (which fetches this same file) so the two can never silently drift the way they once did - see this
# project's own history for the real bug that came from keeping two hand-copied constant tables in sync.
# index.html has its own older, hand-entered copy of these same figures (predates this file) - it doesn't
# read this JSON, so it's the one place left that has to be updated by hand if these numbers ever change.
with open(os.path.join(RUNTIME_DATA_DIR, 'density_formulas.json')) as _f:
    DENSITY_FORMULAS = json.load(_f)

# Real Table 110-8 thresholds (from a real screenshot the user provided earlier in this project's history -
# not guessed), same formula as index.html's own computeRipMaxUnits.
RIP_SIXPLEX_MIN_SQFT = DENSITY_FORMULAS['ripSixplexMinSqft']
COTTAGE_CLUSTER_MIN_SQFT = DENSITY_FORMULAS['cottageClusterMinSqft']
COTTAGE_CLUSTER_MAX_SQFT = DENSITY_FORMULAS['cottageClusterMaxSqft']
COTTAGE_CLUSTER_MAX_UNITS = DENSITY_FORMULAS['cottageClusterMaxUnits']
COTTAGE_CLUSTER_SQFT_PER_UNIT = DENSITY_FORMULAS['cottageClusterSqftPerUnit']  # this tool's own fixed default - no scenario/spinner here to vary it


def rip_max_units(zone, sqft):
    if zone not in RIP_SIXPLEX_MIN_SQFT or sqft is None:
        return None
    best = 2
    if sqft >= RIP_SIXPLEX_MIN_SQFT[zone]:
        best = max(best, 6)
    cc_min = COTTAGE_CLUSTER_MIN_SQFT.get(zone)
    if cc_min is not None and cc_min <= sqft <= COTTAGE_CLUSTER_MAX_SQFT:
        best = max(best, min(int(sqft // COTTAGE_CLUSTER_SQFT_PER_UNIT), COTTAGE_CLUSTER_MAX_UNITS))
    return best


# Portland City Code 33.120 Table 120-3 - confirmed real via direct lookup, not guessed. RMP is the one
# multi-dwelling zone with an actual flat cap (RMP_UNITS_PER_ACRE below); RM1-4/RX have none at all
# (FAR-limited instead) - for those (and for MUR-zoned taxlots, which never carry a portlandZoneClass at
# all - see add_portland_rip_density.py's own comment on why its RIP fetch only ever covers SFR/MFR-classified
# taxlots), fall back to Metro's own generalized zoneClass density figure as an approximation
# (ZONE_CLASS_DENSITY below).
NO_CAP_ZONES = set(DENSITY_FORMULAS['noCapZones'])

# Metro's own generalized zoning density table (Metro RLIS zoning metadata), full SFR/MFR/MUR coverage.
# RM1-4/RX map to it because the city code leaves them genuinely uncapped (confirmed near-1:1 real-data
# mapping: RM1->MFR3 (100%), RM2->MFR4 (100%), RM3->MFR5 (100%), RM4->MFR6 (99%)); MUR-zoned taxlots map to
# it because portlandZoneClass is simply never populated for them in the first place - not because they're
# uncapped by the same city-code fact.
ZONE_CLASS_DENSITY = DENSITY_FORMULAS['zoneClassDensity']
RMP_UNITS_PER_ACRE = DENSITY_FORMULAS['rmpUnitsPerAcre']  # Portland City Code 33.120 Table 120-3 - real, 1 unit/1,500 sq ft

# A parcel/area is only kept if residential use is genuinely allowed there - "low density" on commercial,
# industrial, or park land doesn't mean underused housing capacity, it means housing isn't zoned for that
# land at all. Metro's zoneClass (SFR/MFR/MUR prefix) has full coverage on every taxlot, unlike
# portlandZoneClass, which is a clean 100%/0% split by zone family rather than a partial gap: every
# SFR/MFR-classified Portland taxlot has one (add_portland_rip_density.py's fetch covers that whole scope),
# every MUR-classified one has none (RIP doesn't apply there at all, so it was never in scope to begin with) -
# so it's the filter used at the taxlot level, everywhere taxlots feed into these datasets. Zoning-polygon
# mode additionally filters by each polygon's own real Portland zone code (Title 33.110 single-dwelling +
# 33.120 multi-dwelling zones) - the more precise signal when a whole zone, not a taxlot, is what's shown.
RESIDENTIAL_METRO_PREFIXES = tuple(DENSITY_FORMULAS['residentialMetroPrefixes'])
RESIDENTIAL_PORTLAND_ZONES = set(DENSITY_FORMULAS['residentialPortlandZones'])


def is_residential_metro_zone(metro_zone):
    return bool(metro_zone) and metro_zone.startswith(RESIDENTIAL_METRO_PREFIXES)


def taxlot_maximum_units(zone, sqft, acres, existing_units, metro_zone):
    # A per-taxlot "maximum allowed units" number, always defined, for aggregating a real "maximum density" onto
    # zoning polygons/blocks/tracts the same way existingUnits is already aggregated (sum of units / sum of
    # acres). Precedence: a real known city-code number (RMP) first; then the real RIP computation for
    # R-zones; then Metro's generalized zoneClass figure as a labeled estimate - this covers both the zones
    # the city code genuinely leaves uncapped (RM1-4/RX) AND MUR-zoned taxlots, whose portlandZoneClass is
    # always None (add_portland_rip_density.py's RIP fetch only ever covers SFR/MFR-classified taxlots), not
    # just the no-cap case; everything else (commercial/industrial/unmodeled) falls back to the parcel's own
    # existing units - the same "no known ceiling to compare against" convention index.html itself already
    # uses (existingMaxUnitsForTaxlot), rather than fabricating a number or silently excluding the parcel
    # from area aggregates (which would skew the denominator inconsistently).
    if zone == 'RMP':
        return max(existing_units, int(RMP_UNITS_PER_ACRE * acres))
    if zone in RIP_SIXPLEX_MIN_SQFT:
        rip = rip_max_units(zone, sqft)
        if rip is not None:
            return max(existing_units, rip)
    if metro_zone in ZONE_CLASS_DENSITY:
        return max(existing_units, int(ZONE_CLASS_DENSITY[metro_zone] * acres))
    return existing_units


def post_query(url, params):
    data = urllib.parse.urlencode(params).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            if 'error' in body:
                message = body['error'].get('message', '')
                if attempt < MAX_RETRIES:
                    wait = min(2 ** attempt, 30)
                    print('  ArcGIS error (%s), retrying in %ds (attempt %d/%d)...' % (message, wait, attempt, MAX_RETRIES))
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


def fetch_tigerweb_layer(layer_id, envelope, out_fields, checkpoint_name=None, page_size=1000):
    features = []
    offset = 0
    if checkpoint_name and os.path.exists(checkpoint_name):
        with open(checkpoint_name) as f:
            features = json.load(f)
        offset = len(features)
        print('  resuming from checkpoint: %d features already fetched' % offset)
    while True:
        params = {
            'geometry': json.dumps(envelope),
            'geometryType': 'esriGeometryEnvelope',
            'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': out_fields,
            'outSR': 4326,
            'returnGeometry': 'true',
            'resultOffset': offset,
            'resultRecordCount': page_size,
            'f': 'json',
        }
        body = post_query('%s/%d/query' % (TIGERWEB_URL, layer_id), params)
        page = body.get('features', [])
        features.extend(page)
        print('  fetched page at offset %d: %d features (running total %d)' % (offset, len(page), len(features)))
        if checkpoint_name:
            with open(checkpoint_name, 'w') as f:
                json.dump(features, f)
        if not body.get('exceededTransferLimit') or not page:
            break
        offset += len(page)
        time.sleep(0.2)
    return features


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_gzip_json(path):
    with gzip.open(path, 'rt') as f:
        return json.load(f)


def write_gzip_json(path, obj):
    raw = json.dumps(obj, separators=(',', ':')).encode('utf-8')
    with gzip.open(path, 'wb', compresslevel=9) as f:
        f.write(raw)
    print('wrote', path, '(%.2f MB)' % (os.path.getsize(path) / 1e6))


def point_in_ring(x, y, ring):
    n = len(ring)
    inside = False
    x1, y1 = ring[-1]
    for i in range(n):
        x2, y2 = ring[i]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_rings(lng, lat, rings):
    # Every ring is a candidate boundary (odd-count-wins), the same simple, reliable convention already
    # established in build_taxlot_dataset.py's own point_in_polygon, rather than trusting winding direction
    # to reliably distinguish outer rings from holes across two different data sources (Esri zoning export,
    # TIGERweb).
    count = 0
    for ring in rings:
        if point_in_ring(lng, lat, ring):
            count += 1
    return count % 2 == 1


class GridIndex:
    # Buckets taxlot centroids into ~0.005-degree (~500m) cells so each polygon's aggregation only tests
    # the handful of taxlots near it, not all ~183k Portland taxlots - the same bounding-box-prefilter
    # trick used for the Clackamas isochrone rasterization earlier this session, applied to point data.
    CELL = 0.005

    def __init__(self, points):
        self.buckets = {}
        for pt_idx, (lng, lat) in enumerate(points):
            key = (int(lng / self.CELL), int(lat / self.CELL))
            self.buckets.setdefault(key, []).append(pt_idx)

    def candidates(self, lng0, lat0, lng1, lat1):
        ix0, ix1 = int(lng0 / self.CELL) - 1, int(lng1 / self.CELL) + 1
        iy0, iy1 = int(lat0 / self.CELL) - 1, int(lat1 / self.CELL) + 1
        out = []
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                out.extend(self.buckets.get((ix, iy), ()))
        return out


def ring_bbox(rings):
    lngs = [p[0] for ring in rings for p in ring]
    lats = [p[1] for ring in rings for p in ring]
    return min(lngs), min(lats), max(lngs), max(lats)


def aggregate_onto_polygons(polygons_rings, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone, taxlot_sqft, taxlot_metro_zone):
    # existingDensity and maximumDensity (both computed by the caller as units/acres) are true area-weighted
    # averages - sum of each taxlot's own existing/maximum units, divided by the same sum of acres - not a
    # zone-level label or a majority-vote shortcut. That keeps the two figures directly comparable, and
    # matches the taxlot-mode popup's own per-parcel numbers exactly when a polygon happens to hold just one.
    results = []
    t0 = time.time()
    for i, rings in enumerate(polygons_rings):
        bbox = ring_bbox(rings)
        cand = grid.candidates(*bbox)
        total_units = 0.0
        total_acres = 0.0
        total_maximum_units = 0.0
        for pt_idx in cand:
            lng, lat = taxlot_lnglat[pt_idx]
            if not point_in_rings(lng, lat, rings):
                continue
            units = taxlot_units[pt_idx]
            acres = taxlot_acres[pt_idx]
            total_units += units
            total_acres += acres
            total_maximum_units += taxlot_maximum_units(taxlot_zone[pt_idx], taxlot_sqft[pt_idx], acres, units, taxlot_metro_zone[pt_idx])
        results.append({
            'existingUnits': total_units,
            'acres': round(total_acres, 4),
            'maximumUnits': round(total_maximum_units, 1),
        })
        if (i + 1) % 2000 == 0:
            print('  ...%d/%d (%.1fs)' % (i + 1, len(polygons_rings), time.time() - t0))
    return results


def main():
    print('Loading taxlot dataset...')
    tax = load_gzip_json(os.path.join(RUNTIME_DATA_DIR, 'taxlot_density_data.json.gz'))
    fields = tax['fields']
    idx = {f: i for i, f in enumerate(fields)}
    city_i, lat_i, lng_i, eu_i, ac_i, pzc_i, sqft_i, zc_i = (
        idx['city'], idx['lat'], idx['lng'], idx['existingUnits'], idx['acres'],
        idx['portlandZoneClass'], idx['sqft'], idx['zoneClass'],
    )
    taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone, taxlot_sqft, taxlot_metro_zone = [], [], [], [], [], []
    for r in tax['rows']:
        if r[city_i] != 'PORTLAND':
            continue
        if not r[ac_i] or r[ac_i] <= 0:
            continue
        if not is_residential_metro_zone(r[zc_i]):
            continue
        taxlot_lnglat.append((r[lng_i], r[lat_i]))
        taxlot_units.append(r[eu_i] or 0)
        taxlot_acres.append(r[ac_i])
        taxlot_zone.append(r[pzc_i])
        taxlot_sqft.append(r[sqft_i])
        taxlot_metro_zone.append(r[zc_i])
    print('Portland taxlots with acres > 0:', len(taxlot_lnglat))

    print('Building grid index over taxlot centroids...')
    grid = GridIndex(taxlot_lnglat)

    print('Loading Portland city boundary...')
    city_data = load_json(os.path.join(RUNTIME_DATA_DIR, 'city_boundaries.json'))
    portland = next(c for c in city_data['cities'] if c['name'] == 'Portland')
    p_lngs = [p[0] for ring in portland['rings'] for p in ring]
    p_lats = [p[1] for ring in portland['rings'] for p in ring]
    bbox = (min(p_lngs), min(p_lats), max(p_lngs), max(p_lats))
    print('Portland bbox:', bbox)

    print()
    print('=== Zoning polygons ===')
    zoning_features = load_json(os.path.join(NON_ESSENTIAL_DATA_DIR, 'portland_rip_zoning_checkpoint.json'))
    print('cached zoning features:', len(zoning_features))
    zoning_rings = [f['geometry']['rings'] for f in zoning_features]
    zoning_zones = [f['attributes']['ZONE'] for f in zoning_features]
    zoning_agg = aggregate_onto_polygons(zoning_rings, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone, taxlot_sqft, taxlot_metro_zone)
    zoning_out = []
    for rings, zone, agg in zip(zoning_rings, zoning_zones, zoning_agg):
        if agg['acres'] <= 0 or zone not in RESIDENTIAL_PORTLAND_ZONES:
            continue
        zoning_out.append({
            'zone': zone,
            'rings': rings,
            'existingUnits': round(agg['existingUnits']),
            'acres': agg['acres'],
            'existingDensity': round(agg['existingUnits'] / agg['acres'], 2),
            'maximumDensity': round(agg['maximumUnits'] / agg['acres'], 2),
        })
    write_gzip_json(os.path.join(RUNTIME_DATA_DIR, 'zoning_polygons.json.gz'), zoning_out)

    for layer_id, layer_name, out_name, checkpoint_name in [
        (0, 'Census Tracts', 'census_tracts.json.gz', 'census_tracts_checkpoint.json'),
        (2, '2020 Census Blocks', 'census_blocks.json.gz', 'census_blocks_checkpoint.json'),
    ]:
        print()
        print('=== %s ===' % layer_name)
        envelope = {'xmin': bbox[0], 'ymin': bbox[1], 'xmax': bbox[2], 'ymax': bbox[3], 'spatialReference': {'wkid': 4326}}
        checkpoint_path = os.path.join(NON_ESSENTIAL_DATA_DIR, checkpoint_name)
        features = fetch_tigerweb_layer(layer_id, envelope, 'GEOID,NAME', checkpoint_name=checkpoint_path)
        print('fetched %d features' % len(features))
        rings_list = []
        names = []
        for feat in features:
            geom = feat.get('geometry') or {}
            rings = geom.get('rings')
            if not rings:
                continue
            rings_list.append(rings)
            names.append(feat['attributes'].get('NAME') or feat['attributes'].get('GEOID'))
        agg = aggregate_onto_polygons(rings_list, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone, taxlot_sqft, taxlot_metro_zone)
        out = []
        for rings, name, a in zip(rings_list, names, agg):
            if a['acres'] <= 0:
                continue
            out.append({
                'name': name,
                'rings': rings,
                'existingUnits': round(a['existingUnits']),
                'acres': a['acres'],
                'existingDensity': round(a['existingUnits'] / a['acres'], 2),
                'maximumDensity': round(a['maximumUnits'] / a['acres'], 2),
            })
        write_gzip_json(os.path.join(RUNTIME_DATA_DIR, out_name), out)


if __name__ == '__main__':
    main()
