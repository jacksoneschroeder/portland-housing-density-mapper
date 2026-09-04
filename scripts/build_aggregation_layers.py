#!/usr/bin/env python3
# One-time build: prepares three new map layers for portland-housing-density-mapper.html's "view by" modes -
# Portland zoning polygons (real city zone boundaries, reusing the cached fetch from
# add_portland_rip_density.py rather than re-fetching), Census Tracts, and Census Blocks (both real
# TIGERweb geometry, fetched fresh here). Each polygon gets existingUnits/acres aggregated from every
# taxlot whose centroid falls inside it (point-in-polygon, grid-indexed for speed - a naive O(polygons x
# taxlots) join would be tens of billions of ops), plus a computed max-density figure where one exists.
#
# Several Portland zones have a real Floor Area Ratio limit but no maximum-density (units/acre) standard at
# all - confirmed directly against each chapter's own development-standards summary table: RM1-4/RX
# (33.120.210), CM1-3/CE/CX (33.130.205), EX (33.140.205), CI2/IR (33.150.205). Citations here deliberately
# name a section, not a table number - a table's own sequential number can and did shift (33.120's "Summary
# of Development Standards" table moved from 120-3 to 120-4 when the city inserted a new minimum-lot-size
# table ahead of it), while the section a rule actually lives under does not. For these, FAR is converted
# into a unit ceiling by assuming FAR_UNIT_SIZE_SQFT of floor area per unit (see taxlot_maximum_units's own
# comment for why), capped at whichever is lower of that or Metro's own generalized zoneClass estimate. Two
# other Employment/Institutional zones (EG1, EG2, and CI1) share Chapter 33.140/33.150's own FAR-only pattern
# but get a real 0 instead - their own Primary Uses tables (140-1, 150-1) show Household Living as genuinely
# prohibited (CI1) or limited to only a narrow existing-hotel/motel-to-affordable-housing conversion
# (EG1/EG2), so a computed FAR-derived ceiling would misleadingly imply broad capacity to build new housing
# where the code actually bans or nearly bans it. Real flat caps exist for three zones only: RMP (1 unit/1,500
# sq ft, 33.120.212), CR (1 unit/2,500 sq ft, a Ch. 33.130 table footnote conditional on no Retail
# Sales/Service or Office use - this tool has no per-taxlot use-type field to check that condition, so it's
# applied unconditionally as a best-effort estimate), and RF (1 unit/87,120 sq ft, 33.610.100 Standard C -
# attached houses aren't allowed in RF at all, so this is the only standard that applies).
# The R-zones (R20/R10/R7/R5/R2.5, RIP-eligible) get a real computed max-density number using the same RIP
# formula as index.html's own ripMaximumUnits. Every other zone's max density is estimated from Metro's own
# generalized zoneClass density table instead of a real Portland-code number.
#
# build_salem_layers() is the same idea for Salem, but simpler: Salem has no researched zoning-code density
# formulas yet (see cities.json's own hasMaxDensityFormulas: false), so maximumUnits/maximumDensity are stubbed
# to None for every Salem zoning polygon/tract/block instead of computed - mirrors index.html's own
# genericUnknownMaximumDensity. Zoning polygons are fetched fresh from Salem's own Zoning_Designation service
# (no cached checkpoint to reuse the way Portland's add_portland_rip_density.py fetch is reused below); census
# tracts/blocks reuse fetch_tigerweb_layer as-is (already bbox-parameterized, not Portland-specific) with a
# bbox derived from that zoning-polygon fetch's own extent - Salem isn't in city_boundaries.json (that file is
# Metro's own "Cities by impact" layer, an unrelated feature - see build_city_boundaries.py), so there's no
# separate city-boundary source to derive it from the way Portland's build_portland_layers() does.
#
# Run with: python3 scripts/build_aggregation_layers.py [portland|salem]
# Reads (portland): Non-essential data/portland_rip_zoning_checkpoint.json,
#                    runtime-data/portland_taxlot_density_data.json.gz, runtime-data/city_boundaries.json,
#                    live Census TIGERweb REST API
# Reads (salem):     runtime-data/salem_taxlot_density_data.json.gz, live Salem Zoning_Designation service,
#                    live Census TIGERweb REST API
# Writes: runtime-data/{city}_zoning_polygons.json.gz, runtime-data/{city}_census_tracts.json.gz,
#         runtime-data/{city}_census_blocks.json.gz

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DATA_DIR = os.path.join(BASE_DIR, 'runtime-data')
NON_ESSENTIAL_DATA_DIR = os.path.join(BASE_DIR, 'Non-essential data')

TIGERWEB_URL = 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer'
SALEM_ZONING_URL = 'https://services.arcgis.com/kIA6yS9KDGqZL7U3/arcgis/rest/services/Zoning_Designation/FeatureServer/0/query'
MAX_RETRIES = 6
RETRY_TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)

# Single source of truth for every zoning/density constant below (Portland's own - Salem has no formulas of its
# own yet, see cities.json's hasMaxDensityFormulas) - index.html fetches this exact same file at runtime, so
# the two can never silently drift the way they once did (see this project's own history for the real bug that
# came from keeping two hand-copied constant tables in sync instead). Replaces the old Portland-only
# density_formulas.json entirely - cities.json holds every city's config, keyed by city id.
with open(os.path.join(RUNTIME_DATA_DIR, 'cities.json')) as _f:
    CITY_CONFIGS = json.load(_f)
DENSITY_FORMULAS = CITY_CONFIGS['portland']['formulas']
SALEM_RESIDENTIAL_ZONES = set(CITY_CONFIGS['salem']['formulas']['residentialZones'])

# Real Table 110-8 thresholds (from a real screenshot the user provided earlier in this project's history -
# not guessed), same formula as index.html's own ripMaximumUnits.
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


# zone -> {'far': <Maximum FAR>, 'citation': <real code section>} for every zone with a real FAR limit but no
# maximum-density standard of its own - see this file's own header comment for the full list and citations.
FAR_VALUES = DENSITY_FORMULAS['farValues']
# 500 sq ft/unit - the same deliberately small, "how many could conceivably fit" assumption
# COTTAGE_CLUSTER_SQFT_PER_UNIT above already uses, for the same reason: this produces a real ceiling, not a
# typical/expected unit size.
FAR_UNIT_SIZE_SQFT = DENSITY_FORMULAS['farUnitSizeSqft']

# zone -> real code section - Household Living is genuinely prohibited (CI1, 33.150.100 Table 150-1) or
# limited to only a narrow existing-hotel/motel-to-affordable-housing conversion (EG1/EG2, 33.140.100 Table
# 140-1), not just density-capped like FAR_VALUES above. A computed FAR-derived ceiling would misleadingly
# imply broad capacity to build new housing on sites where the code actually bans or nearly bans it, so these
# get a real, explicit 0 instead.
NO_UNITS_ZONES = DENSITY_FORMULAS['noUnitsZones']

# Metro's own generalized zoning density table (Metro RLIS zoning metadata), full SFR/MFR/MUR coverage - the
# fallback estimate for every zone with no real flat Portland-code cap, an upper bound on the FAR_VALUES
# zones' own derived ceiling, or no dedicated formula in this tool at all.
ZONE_CLASS_DENSITY = DENSITY_FORMULAS['zoneClassDensity']
RMP_UNITS_PER_ACRE = DENSITY_FORMULAS['rmpUnitsPerAcre']  # Portland City Code 33.120.212 - real, 1 unit/1,500 sq ft
CR_UNITS_PER_ACRE = DENSITY_FORMULAS['crUnitsPerAcre']  # Portland City Code Ch. 33.130, a table footnote - real, 1 unit/2,500 sq ft (conditional on no Retail Sales/Service or Office use; applied unconditionally here, no per-taxlot use-type field to check)
RF_UNITS_PER_ACRE = DENSITY_FORMULAS['rfUnitsPerAcre']  # Portland City Code 33.610.100 Standard C - real, 1 unit/87,120 sq ft

# A parcel/area is only kept if residential use is genuinely allowed there - "low density" on commercial,
# industrial, or park land doesn't mean underused housing capacity, it means housing isn't zoned for that
# land at all. Metro's zoneClass (SFR/MFR/MUR prefix) has full coverage on every taxlot, and so does
# portlandZoneClass now (add_portland_rip_density.py's join covers every SFR/MFR/MUR-classified Portland
# taxlot, not just SFR/MFR) - so either signal works as the filter used at the taxlot level, everywhere
# taxlots feed into these datasets. Zoning-polygon mode additionally filters by each polygon's own real
# Portland zone code (Title 33.110 single-dwelling + 33.120 multi-dwelling + 33.130 commercial/mixed-use
# zones) - the more precise signal when a whole zone, not a taxlot, is what's shown.
RESIDENTIAL_METRO_PREFIXES = tuple(DENSITY_FORMULAS['residentialMetroPrefixes'])
RESIDENTIAL_PORTLAND_ZONES = set(DENSITY_FORMULAS['residentialPortlandZones'])


def is_residential_metro_zone(metro_zone):
    return bool(metro_zone) and metro_zone.startswith(RESIDENTIAL_METRO_PREFIXES)


def taxlot_maximum_units(zone, sqft, acres, existing_units, metro_zone):
    # A per-taxlot "maximum allowed units" number, always defined, for aggregating a real "maximum density" onto
    # zoning polygons/blocks/tracts the same way existingUnits is already aggregated (sum of units / sum of
    # acres). Precedence: a real known city-code flat cap (RMP, CR, RF) first; then a real 0 for a zone where
    # Household Living is genuinely prohibited or all-but-prohibited (NO_UNITS_ZONES); then the real RIP
    # computation for R-zones; then for a zone with a real FAR limit but no maximum-density standard of its
    # own (FAR_VALUES), whichever is lower of a FAR-derived unit ceiling (FAR * acres * 43560 /
    # FAR_UNIT_SIZE_SQFT - Metro's regional zoneClass figure can run well above what this taxlot's own real
    # FAR would actually allow, so it's used as a cap on the FAR-derived number here, not a replacement for
    # it) or Metro's own generalized zoneClass estimate; then Metro's zoneClass estimate alone for any other
    # zone this tool has no dedicated formula for; everything else (commercial/industrial/unmodeled) falls
    # back to the parcel's own existing units - the same "no known ceiling to compare against" convention
    # index.html itself already uses (taxlotMaximumDensity's own final fallback branch), rather than
    # fabricating a number or silently excluding the parcel from area aggregates (which would skew the
    # denominator inconsistently).
    # Each real formula's own number is reported as-is, even when a taxlot's real existing unit count already
    # exceeds it (a legal nonconforming lot) - it's a real zoning-code ceiling, not a claim about what's
    # already built, so it never gets bumped up to match existing.
    if zone == 'RMP':
        return int(RMP_UNITS_PER_ACRE * acres)
    if zone == 'CR':
        return int(CR_UNITS_PER_ACRE * acres)
    if zone == 'RF':
        return int(RF_UNITS_PER_ACRE * acres)
    if zone in NO_UNITS_ZONES:
        return 0
    if zone in RIP_SIXPLEX_MIN_SQFT:
        rip = rip_max_units(zone, sqft)
        if rip is not None:
            return rip
    if zone in FAR_VALUES:
        far_units = int(FAR_VALUES[zone]['far'] * acres * 43560 / FAR_UNIT_SIZE_SQFT)
        if metro_zone in ZONE_CLASS_DENSITY:
            return min(far_units, int(ZONE_CLASS_DENSITY[metro_zone] * acres))
        return far_units
    if metro_zone in ZONE_CLASS_DENSITY:
        return int(ZONE_CLASS_DENSITY[metro_zone] * acres)
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


def aggregate_onto_polygons(polygons_rings, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone=None, taxlot_sqft=None, taxlot_metro_zone=None, max_units_fn=None):
    # existingDensity and maximumDensity (both computed by the caller as units/acres) are true area-weighted
    # means - sum of each taxlot's own existing/maximum units, divided by the same sum of acres - not a
    # zone-level label or a majority-vote shortcut. That keeps the two figures directly comparable, and
    # matches the taxlot-mode popup's own per-parcel numbers exactly when a polygon happens to hold just one.
    #
    # max_units_fn is optional (None for a city with no researched zoning-code density formulas yet - Salem
    # currently, see build_salem_layers() - mirrors index.html's own genericUnknownMaximumDensity): every
    # polygon's own maximumUnits comes back None too in that case, since there's nothing real to sum in the
    # first place, not just an unset default.
    results = []
    t0 = time.time()
    for i, rings in enumerate(polygons_rings):
        bbox = ring_bbox(rings)
        cand = grid.candidates(*bbox)
        total_units = 0.0
        total_acres = 0.0
        total_maximum_units = 0.0 if max_units_fn else None
        for pt_idx in cand:
            lng, lat = taxlot_lnglat[pt_idx]
            if not point_in_rings(lng, lat, rings):
                continue
            units = taxlot_units[pt_idx]
            acres = taxlot_acres[pt_idx]
            total_units += units
            total_acres += acres
            if max_units_fn:
                total_maximum_units += max_units_fn(taxlot_zone[pt_idx], taxlot_sqft[pt_idx], acres, units, taxlot_metro_zone[pt_idx])
        results.append({
            'existingUnits': total_units,
            'acres': round(total_acres, 4),
            'maximumUnits': round(total_maximum_units, 1) if total_maximum_units is not None else None,
        })
        if (i + 1) % 2000 == 0:
            print('  ...%d/%d (%.1fs)' % (i + 1, len(polygons_rings), time.time() - t0))
    return results


# Shared by both build_portland_layers() and build_salem_layers() - fetches the 2 TIGERweb layers this app
# uses (Census Tracts, 2020 Census Blocks) for whatever bbox is passed in, aggregates onto them, and writes
# each city's own {city}_census_tracts.json.gz/{city}_census_blocks.json.gz. max_units_fn/taxlot_zone/
# taxlot_sqft/taxlot_metro_zone are forwarded to aggregate_onto_polygons as-is (None for Salem - see its own
# comment on what that means).
def build_census_layers(city_id, bbox, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone=None, taxlot_sqft=None, taxlot_metro_zone=None, max_units_fn=None):
    for layer_id, layer_name, out_name, checkpoint_name in [
        (0, 'Census Tracts', '%s_census_tracts.json.gz' % city_id, '%s_census_tracts_checkpoint.json' % city_id),
        (2, '2020 Census Blocks', '%s_census_blocks.json.gz' % city_id, '%s_census_blocks_checkpoint.json' % city_id),
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
        agg = aggregate_onto_polygons(rings_list, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone, taxlot_sqft, taxlot_metro_zone, max_units_fn)
        out = []
        for rings, name, a in zip(rings_list, names, agg):
            if a['acres'] <= 0:
                continue
            out.append({
                'name': name,
                'rings': rings,
                'existingUnits': round(a['existingUnits']),
                'maximumUnits': a['maximumUnits'],
                'acres': a['acres'],
                'existingDensity': round(a['existingUnits'] / a['acres'], 2),
                'maximumDensity': round(a['maximumUnits'] / a['acres'], 2) if a['maximumUnits'] is not None else None,
            })
        write_gzip_json(os.path.join(RUNTIME_DATA_DIR, out_name), out)


def build_portland_layers():
    print('Loading taxlot dataset...')
    tax = load_gzip_json(os.path.join(RUNTIME_DATA_DIR, 'portland_taxlot_density_data.json.gz'))
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
    zoning_ids = [f['attributes']['OBJECTID'] for f in zoning_features]
    zoning_agg = aggregate_onto_polygons(zoning_rings, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone, taxlot_sqft, taxlot_metro_zone, taxlot_maximum_units)
    zoning_out = []
    for rings, zone, polygon_id, agg in zip(zoning_rings, zoning_zones, zoning_ids, zoning_agg):
        if agg['acres'] <= 0 or zone not in RESIDENTIAL_PORTLAND_ZONES:
            continue
        zoning_out.append({
            'zone': zone,
            'polygonId': polygon_id,
            'rings': rings,
            'existingUnits': round(agg['existingUnits']),
            'maximumUnits': agg['maximumUnits'],
            'acres': agg['acres'],
            'existingDensity': round(agg['existingUnits'] / agg['acres'], 2),
            'maximumDensity': round(agg['maximumUnits'] / agg['acres'], 2),
        })
    write_gzip_json(os.path.join(RUNTIME_DATA_DIR, 'portland_zoning_polygons.json.gz'), zoning_out)

    build_census_layers('portland', bbox, grid, taxlot_lnglat, taxlot_units, taxlot_acres, taxlot_zone, taxlot_sqft, taxlot_metro_zone, taxlot_maximum_units)


def fetch_all_salem_zoning_features():
    # Salem's own Zoning_Designation service, fetched fresh every run (unlike Portland's zoning polygons,
    # which reuse add_portland_rip_density.py's own cached checkpoint) - Salem's ~4,800 polygons are small
    # enough that there's no real cost to just re-fetching, so there's no checkpoint file for it at all.
    features = []
    offset = 0
    while True:
        body = post_query(SALEM_ZONING_URL, {
            'where': '1=1', 'outFields': 'OBJECTID,ZNP_1', 'returnGeometry': 'true', 'outSR': 4326,
            'resultOffset': offset, 'resultRecordCount': 1000, 'f': 'json',
        })
        page = body.get('features', [])
        features.extend(page)
        print('  fetched page at offset %d: %d features (running total %d)' % (offset, len(page), len(features)))
        if not body.get('exceededTransferLimit') or not page:
            break
        offset += len(page)
        time.sleep(0.2)
    return features


def build_salem_layers():
    print('Loading Salem taxlot dataset...')
    tax = load_gzip_json(os.path.join(RUNTIME_DATA_DIR, 'salem_taxlot_density_data.json.gz'))
    fields = tax['fields']
    idx = {f: i for i, f in enumerate(fields)}
    lat_i, lng_i, ac_i = idx['lat'], idx['lng'], idx['acres']

    taxlot_lnglat, taxlot_units, taxlot_acres = [], [], []
    for r in tax['rows']:
        if not r[ac_i] or r[ac_i] <= 0:
            continue
        taxlot_lnglat.append((r[lng_i], r[lat_i]))
        taxlot_units.append(0)  # No existing-unit data exists for Salem's parcels at all - see cities.json's hasExistingUnits: false.
        taxlot_acres.append(r[ac_i])
    print('Salem taxlots with acres > 0:', len(taxlot_lnglat))

    print('Building grid index over taxlot centroids...')
    grid = GridIndex(taxlot_lnglat)

    print()
    print('=== Zoning polygons ===')
    zoning_features = fetch_all_salem_zoning_features()
    print('fetched zoning features:', len(zoning_features))
    zoning_rings, zoning_zones, zoning_ids = [], [], []
    for f in zoning_features:
        rings = f.get('geometry', {}).get('rings')
        if not rings:
            continue
        zoning_rings.append(rings)
        zoning_zones.append(f['attributes']['ZNP_1'])
        zoning_ids.append(f['attributes']['OBJECTID'])

    # Salem's own bbox, derived from this same zoning-polygon fetch's own extent (no separate city-boundary
    # source needed - Salem isn't in city_boundaries.json, see this file's own header comment) - used below for
    # the census tract/block fetch.
    all_lngs = [p[0] for rings in zoning_rings for ring in rings for p in ring]
    all_lats = [p[1] for rings in zoning_rings for ring in rings for p in ring]
    bbox = (min(all_lngs), min(all_lats), max(all_lngs), max(all_lats))
    print('Salem bbox (from zoning extent):', bbox)

    zoning_agg = aggregate_onto_polygons(zoning_rings, grid, taxlot_lnglat, taxlot_units, taxlot_acres)
    zoning_out = []
    for rings, zone, polygon_id, agg in zip(zoning_rings, zoning_zones, zoning_ids, zoning_agg):
        if agg['acres'] <= 0 or zone not in SALEM_RESIDENTIAL_ZONES:
            continue
        zoning_out.append({
            'zone': zone,
            'polygonId': polygon_id,
            'rings': rings,
            'existingUnits': round(agg['existingUnits']),
            'maximumUnits': None,
            'acres': agg['acres'],
            'existingDensity': round(agg['existingUnits'] / agg['acres'], 2),
            'maximumDensity': None,
        })
    write_gzip_json(os.path.join(RUNTIME_DATA_DIR, 'salem_zoning_polygons.json.gz'), zoning_out)

    build_census_layers('salem', bbox, grid, taxlot_lnglat, taxlot_units, taxlot_acres)


BUILDERS = {'portland': build_portland_layers, 'salem': build_salem_layers}


def main():
    city = sys.argv[1] if len(sys.argv) > 1 else 'portland'
    if city not in BUILDERS:
        raise SystemExit('Unknown city %r - expected one of %s' % (city, sorted(BUILDERS)))
    BUILDERS[city]()


if __name__ == '__main__':
    main()
