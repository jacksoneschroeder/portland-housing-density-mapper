#!/usr/bin/env python3
# Incorporated city limits for the "Cities by impact" map view - queried directly from Oregon Metro's own
# hosted ArcGIS feature layer (same org as this app's other Metro-sourced layers - see
# TAXLOT_SERVICE_URL/ZONING_SERVICE_URL in build_taxlot_dataset.py, and build_mpa_boundary.py for the same
# fetch/simplify pattern this script follows). CITYNAME here is Title Case ("West Linn") - index.html joins
# it against each taxlot's own 'city' field (MAF's JURIS_CITY, all-caps - see fetch_taxlot_addresses.py)
# case-insensitively, not by assuming they already match exactly.
#
# Every incorporated city in the layer is kept, not just ones inside the MPA/taxlot dataset's own clipped
# extent - a handful (Vancouver, Camas, Washougal, ...) are Washington cities included for regional context
# and will simply never have a matching 'city' value in the (Oregon-only) taxlot dataset, so they never
# render on this app's own map; keeping them in the dataset is simpler than re-deriving which subset to drop
# and never wrong either way, since nothing looks them up by anything other than name.
#
# Source: https://rlisdiscovery.oregonmetro.gov/datasets/City_Limits_poly/explore
# Run with: python3 scripts/build_city_boundaries.py
# Output:   runtime-data/city_boundaries.json

import json
import math
import os
import urllib.parse
import urllib.request

SERVICE_URL = 'https://services2.arcgis.com/McQ0OlIABe29rJJy/arcgis/rest/services/City_Limits_poly/FeatureServer/0/query'
OUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'runtime-data', 'city_boundaries.json')

# Purely decorative/click-target geometry (a filled map layer plus point-in-polygon for "which city was
# clicked"), not a precision boundary like build_mpa_boundary.py's own MPA polygon - same tolerance/decimals
# anyway since they're already proven safe and there's no reason to tune a second value for a looser need.
SIMPLIFY_TOLERANCE_METERS = 4
COORDINATE_DECIMALS = 5
METERS_PER_DEGREE = 111320


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


def main():
    params = {
        'where': '1=1',
        'outFields': 'CITYNAME',
        'returnGeometry': 'true',
        'outSR': '4326',
        'f': 'json',
    }
    url = SERVICE_URL + '?' + urllib.parse.urlencode(params)
    print('Querying %s ...' % SERVICE_URL)
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.load(resp)

    if 'error' in data:
        raise RuntimeError('ArcGIS query failed: %s' % data['error'])

    cities = []
    total_before, total_after = 0, 0
    for f in data['features']:
        name = f['attributes']['CITYNAME']
        raw_rings = f['geometry']['rings']
        total_before += sum(len(r) for r in raw_rings)
        rings = compress_rings(raw_rings, SIMPLIFY_TOLERANCE_METERS, COORDINATE_DECIMALS)
        total_after += sum(len(r) for r in rings)
        cities.append({'name': name, 'rings': rings})
    cities.sort(key=lambda c: c['name'])

    print('%d cities, %d total vertices before simplification, %d after' % (len(cities), total_before, total_after))
    with open(OUT_PATH, 'w') as f:
        json.dump({'cities': cities}, f)
    print('Wrote %s' % OUT_PATH)


if __name__ == '__main__':
    main()
