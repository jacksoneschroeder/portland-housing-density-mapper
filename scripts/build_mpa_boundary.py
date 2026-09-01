#!/usr/bin/env python3
# The federally-required Metropolitan Planning Area (MPA) boundary - the real geography the VMT reduction
# goal is actually defined against (not the full three-county RLIS taxlot extent, which reaches well past
# it into rural/exurban land no transit-oriented bill could plausibly touch). The MPA is fully contained
# within both the tri-county regional housing goal area and the statewide emissions goal area, so clipping
# the taxlot dataset to it is strictly narrower/still valid for all three goals - see
# build_taxlot_dataset.py's own use of this file for the actual clip.
#
# One single feature (a polygon with a handful of small interior holes - real enclaves excluded from the
# federal MPA designation), queried directly from Oregon Metro's own hosted ArcGIS feature layer.
#
# Source: https://rlisdiscovery.oregonmetro.gov/datasets/f47be032fdad463882632f386dbcca28_0/explore
# (same ArcGIS org as this app's other Metro-sourced layers - see TAXLOT_SERVICE_URL/ZONING_SERVICE_URL in
# build_taxlot_dataset.py)
# Run with: python3 scripts/build_mpa_boundary.py
# Output:   runtime-data/mpa_boundary.json

import json
import math
import os
import urllib.parse
import urllib.request

SERVICE_URL = 'https://services2.arcgis.com/McQ0OlIABe29rJJy/arcgis/rest/services/Metropolitan_Planning_Area_MPA/FeatureServer/0/query'
OUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'runtime-data', 'mpa_boundary.json')

# Same tolerance/precision build_taxlot_dataset.py uses for parcel boundaries (compress_rings) - proven
# accurate enough there that no real taxlot near a boundary gets misjoined, and this boundary is used for
# exactly that kind of precision-sensitive point-in-polygon check (not just decoration), so it gets the same
# treatment rather than a looser one that might shift the line enough to misclassify a taxlot near the edge.
SIMPLIFY_TOLERANCE_METERS = 4
COORDINATE_DECIMALS = 5
METERS_PER_DEGREE = 111320  # Rough - fine at this precision, same approximation build_taxlot_dataset.py uses


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
    # Standard recursive Ramer-Douglas-Peucker - same algorithm/math as build_taxlot_dataset.py's own copy
    # (see that file's own comment on why each build script keeps its own copy rather than importing one
    # shared module).
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
        'outFields': 'MPA',
        'returnGeometry': 'true',
        'outSR': '4326',  # Plain lat/lng - matches every other geometry this app draws with, not the service's native Oregon State Plane
        'f': 'json',
    }
    url = SERVICE_URL + '?' + urllib.parse.urlencode(params)
    print('Querying %s ...' % SERVICE_URL)
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.load(resp)

    if 'error' in data:
        raise RuntimeError('ArcGIS query failed: %s' % data['error'])
    if len(data['features']) != 1:
        raise RuntimeError('Expected exactly one MPA feature, got %d' % len(data['features']))

    raw_rings = data['features'][0]['geometry']['rings']
    print('%d ring(s), %d total vertices before simplification' % (len(raw_rings), sum(len(r) for r in raw_rings)))
    rings = compress_rings(raw_rings, SIMPLIFY_TOLERANCE_METERS, COORDINATE_DECIMALS)
    print('%d total vertices after simplification' % sum(len(r) for r in rings))

    with open(OUT_PATH, 'w') as f:
        json.dump({'rings': rings}, f)
    print('Wrote %s' % OUT_PATH)


if __name__ == '__main__':
    main()
