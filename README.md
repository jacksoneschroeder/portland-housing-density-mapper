# Portland Housing Density Mapper

A web tool for visualizing Portland's housing density by taxlot, Portland zoning polygon, census block, or census tract. You can run the tool live now on Cloudflare, or clone the repo to run it locally and modify the source.

## Run the tool live now

https://portland-housing-density-mapper.jacksonschroeder.workers.dev

## Modify the source

Running `git clone` includes a snapshot of all the spatial data necessary to run the tool. Serve the repo root over HTTP and open `index.html` to see your changes - e.g.:

```
python3 -m http.server 8000
```

This snapshot was last updated on 2026-08-22 and therefore may not be current. If you want live spatial data, you'll have to regenerate the data in `runtime-data/` with `scripts/`, in this order:

1. `mkdir "Non-essential data"`
2. `python3 scripts/build_mpa_boundary.py` - builds the Metropolitan Planning Area boundary polygon
   (`runtime-data/mpa_boundary.json`) that every later step clips to.
3. `python3 scripts/build_taxlot_dataset.py` - fetches every taxlot and zoning polygon in the region, clips to
   the MPA boundary, spatially joins each taxlot to its zoning class. Writes
   `runtime-data/taxlot_density_data.json(.gz)`.
4. `python3 scripts/fetch_taxlot_addresses.py` - adds the address to each taxlot.
5. `python3 scripts/fetch_taxlot_housing_units.py` - adds the existing housing unit count to each taxlot.
6. `python3 scripts/add_portland_rip_density.py` - calculates the maximum units allowed per taxlot according to Portland's Residential Infill Project (Portland City Code 33.110.265).
7. `python3 scripts/build_aggregation_layers.py` - builds the data for the aggregate areas (that is, Portland zoning polygons, census blocks, and census tracts).

Steps 3-6 each take a while and checkpoint their progress into the `Non-essential data/` directory so a killed/crashed run resumes instead of starting over. Checkpoints are gitignored.
