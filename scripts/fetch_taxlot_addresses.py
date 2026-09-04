#!/usr/bin/env python3
# One-time enrichment: fetches each taxlot's real site address from Metro's own Master Address File (MAF,
# services2.arcgis.com/.../Master_Address_File_MAF - the same regional GIS provider the taxlot/zoning data
# itself comes from), joined by TLID - not a geocoding step at all, MAF already carries a real address point
# per address, distinct from a taxlot's own parcel-polygon centroid (confirmed empirically: X_COORD/Y_COORD
# on the taxlot service itself turned out to just BE that same centroid reprojected, not a separate point -
# MAF's own address points are the real thing). A taxlot can have zero MAF matches (vacant land, ROW,
# utility parcels - no building, no address) or several (multi-unit properties) - zero means no site point at
# all for that taxlot (siteLat/siteLng fall back to the parcel centroid, address stays unset), several just
# takes the first match, since picking one specific unit's address for a whole-parcel point isn't meaningfully
# more correct than picking another.
#
# Adds 'address' (display string, e.g. "13045 SE MEADEHILL AVE, HAPPY VALLEY"), 'city' (MAF's own JURIS_CITY,
# e.g. "HAPPY VALLEY" - stored as its own field, not just baked into 'address', so the app can group/filter
# by city without parsing it back out of a display string), and 'siteLat'/'siteLng' (the MAF point itself,
# WGS84) to every taxlot - siteLat/siteLng stay purely informational here (this tool has no walking-distance
# routing step), while lat/lng stay exactly as build_taxlot_dataset.py always computed them (the parcel
# centroid - what the map draws taxlots relative to, and what the zoning spatial join used).
#
# This is a trimmed fork of the same-named script in the TOD-PITCH repo, which also recomputes per-taxlot
# transit-distance (dist_X) fields from siteLat/siteLng - removed here since this tool never computes those
# fields in the first place (see build_taxlot_dataset.py's own header comment for why).
#
# Run with: python3 scripts/fetch_taxlot_addresses.py
# Reads/writes: runtime-data/portland_taxlot_density_data.json (+ .gz) - Portland only, see build_taxlot_dataset.py's load_taxlot_dataset()/write_taxlot_dataset() own comment on why Salem has no equivalent enrichment step.

import json
import os
import time

from build_taxlot_dataset import RUNTIME_DATA_DIR, NON_ESSENTIAL_DATA_DIR, post_query, load_taxlot_dataset, write_taxlot_dataset

MAF_URL = 'https://services2.arcgis.com/McQ0OlIABe29rJJy/arcgis/rest/services/Master_Address_File_MAF/FeatureServer/0/query'
TAXLOT_DATASET_PATH = os.path.join(RUNTIME_DATA_DIR, 'portland_taxlot_density_data.json')
CHECKPOINT_PATH = os.path.join(NON_ESSENTIAL_DATA_DIR, 'taxlot_addresses_checkpoint.json')
# ~300 TLIDs/request keeps the where-clause a safe size (tested empirically well under any real limit) while
# keeping total request count manageable (~1900 requests for 571k MPA-clipped taxlots) - MAF's own maxRecordCount (2000)
# isn't the limiting factor here (well under 300 TLIDs' worth of matches per batch in practice), TLID being
# a unique join key per request is.
BATCH_SIZE = 300


def fetch_address_batch(tlids):
    where = 'TLID IN (' + ','.join("'" + t.replace("'", "''") + "'" for t in tlids) + ')'
    body = post_query(MAF_URL, {
        'where': where,
        'outFields': 'TLID,ADDRESS,JURIS_CITY',
        'outSR': '4326',
        'returnGeometry': 'true',
        'f': 'json',
    })
    result = {}
    for f in body.get('features', []):
        tlid = f['attributes']['TLID']
        if tlid in result:
            continue  # First match per TLID only - see this script's own header comment
        addr = f['attributes'].get('ADDRESS')
        city = f['attributes'].get('JURIS_CITY')
        result[tlid] = {
            'address': (addr + ', ' + city) if addr and city else addr,
            'city': city,
            'lat': f['geometry']['y'],
            'lng': f['geometry']['x'],
        }
    return result


def main():
    print('Loading existing taxlot dataset...')
    taxlots = load_taxlot_dataset(TAXLOT_DATASET_PATH)
    print('  %d taxlots' % len(taxlots))

    site_data = {}
    start_batch = 0
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH) as f:
            saved = json.load(f)
        site_data = saved['site_data']
        start_batch = saved['completed_batches']
        print('  resuming from checkpoint: %d batches already fetched, %d addresses found so far' % (start_batch, len(site_data)))

    tlids = [t['tlid'] for t in taxlots]
    batches = [tlids[i:i + BATCH_SIZE] for i in range(0, len(tlids), BATCH_SIZE)]
    print('Fetching addresses from Metro Master Address File (%d batches of up to %d TLIDs each)...' % (len(batches), BATCH_SIZE))

    for i in range(start_batch, len(batches)):
        batch_result = fetch_address_batch(batches[i])
        site_data.update(batch_result)
        if (i + 1) % 20 == 0 or i + 1 == len(batches):
            print('  batch %d/%d (%d addresses found so far)' % (i + 1, len(batches), len(site_data)))
            with open(CHECKPOINT_PATH, 'w') as f:
                json.dump({'completed_batches': i + 1, 'site_data': site_data}, f)
        time.sleep(0.05)  # Same light pacing reasoning as fetch_all_features - proactive, not just retry-driven

    matched = 0
    for t in taxlots:
        site = site_data.get(t['tlid'])
        if site:
            matched += 1
            t['address'] = site['address']
            t['city'] = site['city']
            t['siteLat'] = site['lat']
            t['siteLng'] = site['lng']
        else:
            t['siteLat'] = t['lat']
            t['siteLng'] = t['lng']
    print('Matched %d / %d taxlots to a real MAF address (rest fall back to their own parcel centroid)' % (matched, len(taxlots)))

    print('Writing updated taxlot dataset...')
    write_taxlot_dataset(taxlots, TAXLOT_DATASET_PATH)


if __name__ == '__main__':
    main()
