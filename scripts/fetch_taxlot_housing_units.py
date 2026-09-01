#!/usr/bin/env python3
# One-time enrichment: adds 'existingUnits' (dwelling unit count already on the parcel today) to every
# taxlot, fetched from Metro's own Housing inventory (services2.arcgis.com/.../Housing, layer 11 - the same
# regional GIS provider/account the taxlot/zoning/MAF data already comes from), joined by TLID exactly like
# fetch_taxlot_addresses.py joins MAF - not geocoding, a direct attribute join on Metro's own parcel ID.
#
# This replaces the old flat 'oldResDensity' assumption (a single citywide units/acre guess, see
# index.html's own climateAssumptions comment) with each taxlot's own real existing density
# (existingUnits / acres), used by the new per-taxlot delta-VMT formula. A taxlot can have zero Housing
# matches (vacant land, or a parcel that's never had a dwelling on it - existingUnits stays 0) or several
# (multiple buildings/ADUs on one parcel - existingUnits is their sum, since the whole parcel's existing
# density is what the new formula needs, not any one building's). A tiny number of Housing records carry a
# blank/whitespace TLID (unparcelled - confirmed via a live sample query) and are skipped, since they can't
# be joined to any specific taxlot.
#
# Run with: python3 scripts/fetch_taxlot_housing_units.py
# Reads/writes: runtime-data/taxlot_density_data.json (+ .gz)
# Must run AFTER fetch_taxlot_addresses.py has finished writing its own output - both scripts read/rewrite
# the same full dataset file, so running them concurrently would race and one run's output would clobber
# the other's.

import json
import os
import time

from build_taxlot_dataset import RUNTIME_DATA_DIR, NON_ESSENTIAL_DATA_DIR, post_query, load_taxlot_dataset, write_taxlot_dataset

HOUSING_URL = 'https://services2.arcgis.com/McQ0OlIABe29rJJy/arcgis/rest/services/Housing/FeatureServer/11/query'
TAXLOT_DATASET_PATH = os.path.join(RUNTIME_DATA_DIR, 'taxlot_density_data.json')
CHECKPOINT_PATH = os.path.join(NON_ESSENTIAL_DATA_DIR, 'taxlot_housing_units_checkpoint.json')
# Same batch size/reasoning as fetch_taxlot_addresses.py's own BATCH_SIZE - a live sample query found only
# ~1.4% of TLIDs have more than one Housing record, so 300 TLIDs/request stays comfortably under this
# service's own maxRecordCount (2000) without needing exceededTransferLimit pagination.
BATCH_SIZE = 300


def fetch_units_batch(tlids):
    where = 'TLID IN (' + ','.join("'" + t.replace("'", "''") + "'" for t in tlids) + ')'
    body = post_query(HOUSING_URL, {
        'where': where,
        'outFields': 'TLID,UNITS',
        'returnGeometry': 'false',
        'f': 'json',
    })
    result = {}
    for f in body.get('features', []):
        tlid = f['attributes']['TLID']
        if not tlid or not tlid.strip():
            continue  # Unparcelled Housing record - see this script's own header comment
        result[tlid] = result.get(tlid, 0) + (f['attributes'].get('UNITS') or 0)
    return result


def main():
    print('Loading existing taxlot dataset...')
    taxlots = load_taxlot_dataset(TAXLOT_DATASET_PATH)
    print('  %d taxlots' % len(taxlots))

    units_data = {}
    start_batch = 0
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH) as f:
            saved = json.load(f)
        units_data = saved['units_data']
        start_batch = saved['completed_batches']
        print('  resuming from checkpoint: %d batches already fetched, %d TLIDs with units so far' % (start_batch, len(units_data)))

    tlids = [t['tlid'] for t in taxlots]
    batches = [tlids[i:i + BATCH_SIZE] for i in range(0, len(tlids), BATCH_SIZE)]
    print('Fetching existing housing units from Metro Housing inventory (%d batches of up to %d TLIDs each)...' % (len(batches), BATCH_SIZE))

    for i in range(start_batch, len(batches)):
        batch_result = fetch_units_batch(batches[i])
        units_data.update(batch_result)
        if (i + 1) % 20 == 0 or i + 1 == len(batches):
            print('  batch %d/%d (%d TLIDs with units so far)' % (i + 1, len(batches), len(units_data)))
            with open(CHECKPOINT_PATH, 'w') as f:
                json.dump({'completed_batches': i + 1, 'units_data': units_data}, f)
        time.sleep(0.05)  # Same light pacing reasoning as fetch_taxlot_addresses.py - proactive, not just retry-driven

    matched = 0
    for t in taxlots:
        units = units_data.get(t['tlid'], 0)
        if units:
            matched += 1
        t['existingUnits'] = units
    print('Matched %d / %d taxlots to at least 1 existing housing unit (rest are vacant/non-residential, existingUnits=0)' % (matched, len(taxlots)))

    print('Writing updated taxlot dataset...')
    write_taxlot_dataset(taxlots, TAXLOT_DATASET_PATH)


if __name__ == '__main__':
    main()
