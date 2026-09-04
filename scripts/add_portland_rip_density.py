#!/usr/bin/env python3
# One-time enrichment: adds Portland's own real zoning code (portlandZoneClass, e.g. "R5"/"R2.5" - Metro's
# zoneClass/zoneGenClass fields are a REGIONAL generalized classification, not Portland's native zone codes)
# to every Portland taxlot already classified residential (SFR/MFR/MUR) by Metro's own join in
# build_taxlot_dataset.py, plus real assessor lot square footage (sqft, falling back to acres*43560 only when
# the assessor has no record for that taxlot - see this script's own "unmatched" count) for the subset of
# those that land in an RIP-eligible zone (real sqft is what index.html's own ripMaximumUnits and
# build_aggregation_layers.py's own rip_max_units need to compute a real RIP figure at read time - this
# script doesn't precompute that figure itself, just the raw sqft it depends on). Scoped to Portland only
# (not the whole 3-county region) - RIP is a Portland City Code program (Chapter 33.110), not a regional one.
# RIP's minimum-lot-size table (RIP_SIXPLEX_MIN_SQFT below) only ever applies to Portland's single-dwelling
# R20/R10/R7/R5/R2.5 zones - a real taxlot's zoneClass (SFR/MFR/MUR) doesn't determine RIP eligibility on its
# own, only the real portlandZoneClass this script's own zoning join produces does, which is why the sqft
# fetch below is scoped to rip_eligible (computed after the join) rather than the wider portland_res.
#
# portlandZoneClass: point-in-polygon join against the City of Portland's own zoning layer
# (COP_OpenData_ZoningCode/MapServer/16, field ZONE) - confirmed via direct inspection to carry real distinct
# codes (R2.5/R5/R7/R10/R20/RM1-4/RMP/RX/commercial codes/etc.), not Metro's generalized buckets. Reuses the
# exact same point_in_polygon/grid_cells_for_bbox spatial-index approach build_taxlot_dataset.py already uses
# for Metro's own zoning join - same algorithm, different (Portland-specific) polygon source.
#
# sqft: real assessor square footage from Portland's own taxlot/assessor layer (Public/Taxlots/MapServer/0,
# field A_T_SQFT), joined directly by TLID - confirmed empirically (5/5 real samples checked byte-for-byte)
# that this dataset's own TLID field ALREADY matches that service's TLID format exactly, no normalization
# needed (an earlier note in this project's own history flagged a format mismatch - Metro's compact
# "21E35BB01800" vs this service's padded "1N1E34CC  -02000" - that mismatch no longer exists as of whatever
# rebuild most recently regenerated this dataset). Falls back to acres*43560 only for the taxlots the
# assessor never recorded (a real, expected data-quality gap - not every taxlot has a full assessor record).
#
# Run with: python3 scripts/add_portland_rip_density.py
# Reads/writes: runtime-data/portland_taxlot_density_data.json (+ .gz) - Portland only, see build_taxlot_dataset.py's load_taxlot_dataset()/write_taxlot_dataset() own comment on why Salem has no equivalent enrichment step.

import json
import os
import time

from build_taxlot_dataset import (
    RUNTIME_DATA_DIR, NON_ESSENTIAL_DATA_DIR, post_query, point_in_polygon,
    grid_cells_for_bbox, GRID_SIZE, load_taxlot_dataset, write_taxlot_dataset,
)

PORTLAND_ZONING_URL = 'https://www.portlandmaps.com/od/rest/services/COP_OpenData_ZoningCode/MapServer/16/query'
PORTLAND_ASSESSOR_URL = 'https://www.portlandmaps.com/arcgis/rest/services/Public/Taxlots/MapServer/0/query'
TAXLOT_DATASET_PATH = os.path.join(RUNTIME_DATA_DIR, 'portland_taxlot_density_data.json')
ZONING_CHECKPOINT_PATH = os.path.join(NON_ESSENTIAL_DATA_DIR, 'portland_rip_zoning_checkpoint.json')
ASSESSOR_CHECKPOINT_PATH = os.path.join(NON_ESSENTIAL_DATA_DIR, 'portland_rip_assessor_checkpoint.json')

# Same canonical list index.html's own isResidentialMetroZone and build_aggregation_layers.py's
# RESIDENTIAL_METRO_PREFIXES both already read from this exact file - reused here instead of a second,
# narrower, independently-maintained copy (this used to be a small hardcoded set of just the single-dwelling
# zone classes RIP itself applies to, which meant every MUR-classified taxlot - a lot of Portland's denser
# mixed-use/commercial-residential zones (RM1-4, RX, CR, CM1-3, CE, CX) - never even got a real Portland zone
# code looked up at all, regardless of whether a real zoning polygon actually covered it).
with open(os.path.join(RUNTIME_DATA_DIR, 'density_formulas.json')) as _f:
    RESIDENTIAL_METRO_PREFIXES = tuple(json.load(_f)['residentialMetroPrefixes'])
ASSESSOR_BATCH_SIZE = 1000

# Real RIP minimum-lot-size-by-housing-type-and-zone figures (Portland City Code 33.110.265, Table 110-7) -
# only used here to decide which taxlots are worth a real assessor sqft fetch (RIP itself, and its cottage-
# cluster figures, are computed at read time by index.html's own ripMaximumUnits and
# build_aggregation_layers.py's own rip_max_units, not by this script).
RIP_SIXPLEX_MIN_SQFT = {'R20': 12000, 'R10': 6000, 'R7': 4200, 'R5': 3000, 'R2.5': 1500}


def fetch_portland_zoning_features():
    # Resumable the same way fetch_all_features (build_taxlot_dataset.py) is: a checkpoint's own length IS the
    # next resultOffset to ask for, since ArcGIS pagination is deterministic - this only works correctly
    # because every page (including a possibly-still-partial one) is checkpointed as it arrives (see below),
    # not just once at the very end.
    features = []
    if os.path.exists(ZONING_CHECKPOINT_PATH):
        with open(ZONING_CHECKPOINT_PATH) as f:
            features = json.load(f)
        print('  resuming from checkpoint: %d zoning features already fetched' % len(features))
    offset = len(features)
    page_size = 200  # This layer's own maxRecordCount - see this script's own git history for the empirical check
    while True:
        params = {
            'where': '1=1', 'outFields': 'ZONE,OBJECTID', 'outSR': '4326', 'returnGeometry': 'true',
            'resultOffset': offset, 'resultRecordCount': page_size, 'f': 'json',
        }
        body = post_query(PORTLAND_ZONING_URL, params)
        page = body.get('features', [])
        features.extend(page)
        print('  fetched zoning page at offset %d: %d features (total %d)' % (offset, len(page), len(features)))
        # Checkpointed every page, not just once at the end - this service returned real 503s ("wait timeout")
        # mid-fetch the first time this ran, and post_query's own retry budget isn't infinite; without this,
        # a crash past retry exhaustion loses every page already fetched instead of just the one in flight.
        with open(ZONING_CHECKPOINT_PATH, 'w') as f:
            json.dump(features, f)
        if len(page) < page_size:
            break
        offset += len(page)
        time.sleep(0.2)
    with open(ZONING_CHECKPOINT_PATH, 'w') as f:
        json.dump(features, f)
    return features


def fetch_assessor_batch(tlids):
    where = 'TLID IN (' + ','.join("'" + t.replace("'", "''") + "'" for t in tlids) + ')'
    body = post_query(PORTLAND_ASSESSOR_URL, {
        'where': where, 'outFields': 'TLID,A_T_SQFT', 'f': 'json',
    })
    result = {}
    for f in body.get('features', []):
        sqft = f['attributes'].get('A_T_SQFT')
        if sqft:
            result[f['attributes']['TLID']] = sqft
    return result


def main():
    print('Loading existing taxlot dataset...')
    taxlots = load_taxlot_dataset(TAXLOT_DATASET_PATH)
    print('  %d taxlots total' % len(taxlots))

    portland_res = [t for t in taxlots if t.get('city') == 'PORTLAND' and (t.get('zoneClass') or '').startswith(RESIDENTIAL_METRO_PREFIXES)]
    print('  %d Portland residential taxlots in scope' % len(portland_res))

    print('Fetching Portland zoning polygons...')
    zoning_features = fetch_portland_zoning_features()
    print('  %d zoning polygons' % len(zoning_features))

    print('Building spatial index over zoning polygons...')
    grid = {}
    for zf in zoning_features:
        geom = zf.get('geometry')
        if not geom or not geom.get('rings'):
            continue
        xs = [pt[0] for ring in geom['rings'] for pt in ring]
        ys = [pt[1] for ring in geom['rings'] for pt in ring]
        zf['_bbox'] = (min(xs), min(ys), max(xs), max(ys))
        for cell in grid_cells_for_bbox(zf['_bbox'], GRID_SIZE):
            grid.setdefault(cell, []).append(zf)

    print('Joining each taxlot to its real Portland zone code...')
    unmatched_zone = 0
    for i, t in enumerate(portland_res):
        lng, lat = t['lng'], t['lat']
        cell = (int(lng // GRID_SIZE), int(lat // GRID_SIZE))
        zone = None
        for zf in grid.get(cell, []):
            bx0, by0, bx1, by1 = zf['_bbox']
            if lng < bx0 or lng > bx1 or lat < by0 or lat > by1:
                continue
            if point_in_polygon(lng, lat, zf['geometry']['rings']):
                zone = zf['attributes']['ZONE']
                break
        t['portlandZoneClass'] = zone
        if zone is None:
            unmatched_zone += 1
        if (i + 1) % 40000 == 0:
            print('  joined %d / %d' % (i + 1, len(portland_res)))
    print('  unmatched (no zoning polygon found): %d / %d' % (unmatched_zone, len(portland_res)))

    # sqft only matters for the 5 single-dwelling zones RIP_SIXPLEX_MIN_SQFT covers - narrowed down AFTER the
    # real zone join above, not before, so a taxlot's own newly-looked-up zone code decides this, not its
    # (unrelated) Metro classification. Keeps the slow, real per-taxlot assessor fetch below scoped to only
    # the taxlots that could ever actually use its result.
    rip_eligible = [t for t in portland_res if t.get('portlandZoneClass') in RIP_SIXPLEX_MIN_SQFT]
    print('  %d / %d are in a RIP-eligible zone (R20/R10/R7/R5/R2.5) - only these need real assessor sqft' % (len(rip_eligible), len(portland_res)))

    print('Fetching real assessor square footage...')
    sqft_by_tlid = {}
    start_batch = 0
    if os.path.exists(ASSESSOR_CHECKPOINT_PATH):
        with open(ASSESSOR_CHECKPOINT_PATH) as f:
            saved = json.load(f)
        sqft_by_tlid = saved['sqft_by_tlid']
        start_batch = saved['completed_batches']
        print('  resuming from checkpoint: %d batches already fetched, %d real sqft values found so far' % (start_batch, len(sqft_by_tlid)))

    tlids = [t['tlid'] for t in rip_eligible]
    batches = [tlids[i:i + ASSESSOR_BATCH_SIZE] for i in range(0, len(tlids), ASSESSOR_BATCH_SIZE)]
    for i in range(start_batch, len(batches)):
        batch_result = fetch_assessor_batch(batches[i])
        sqft_by_tlid.update(batch_result)
        if (i + 1) % 10 == 0 or i + 1 == len(batches):
            print('  batch %d/%d (%d real sqft values found so far)' % (i + 1, len(batches), len(sqft_by_tlid)))
            with open(ASSESSOR_CHECKPOINT_PATH, 'w') as f:
                json.dump({'completed_batches': i + 1, 'sqft_by_tlid': sqft_by_tlid}, f)
        time.sleep(0.05)

    print('Computing real sqft (assessor value, or acres*43560 fallback)...')
    real_sqft_count = 0
    for t in rip_eligible:
        real = sqft_by_tlid.get(t['tlid'])
        if real:
            t['sqft'] = real
            real_sqft_count += 1
        else:
            t['sqft'] = round(t['acres'] * 43560, 1)
    print('  %d / %d taxlots got a real assessor sqft value (rest fell back to acres*43560)' % (real_sqft_count, len(rip_eligible)))

    # One-time cleanup: a previous version of this script wrote a ripMaxUnits field that nothing downstream
    # ever reads (index.html's own ripMaximumUnits and build_aggregation_layers.py's own rip_max_units both
    # recompute the same figure independently from sqft/zone at read time instead) - write_taxlot_dataset
    # derives its column list from whatever keys are actually present on each taxlot dict, so a stale field
    # from an old run stays in every future write unless explicitly dropped here.
    for t in taxlots:
        t.pop('ripMaxUnits', None)

    print('Writing updated taxlot dataset...')
    write_taxlot_dataset(taxlots, TAXLOT_DATASET_PATH)


if __name__ == '__main__':
    main()
