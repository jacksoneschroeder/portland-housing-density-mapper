# Oregon DensiDwell

A web tool for visualizing Oregon cities' housing density by taxlot, zoning polygon, census block, or census tract - existing density alongside the maximum density each zone allows. Currently covers Portland and Salem. You can run the tool live now on Cloudflare, or clone the repo to run it locally or modify the program.

## Run the tool live now

https://densidwell.jacksonschroeder.workers.dev

## Run the tool locally, with spatial data from my snapshot

Running `git clone` includes a snapshot of all the spatial data necessary to run the tool. Serve the repo root over HTTP and open `index.html` with a command such as `python3 -m http.server 8000`. My snapshot was last updated on 2026-08-22 and therefore may not be current.

## Run the tool locally, with spatial data from live and official sources

If you want live spatial data, you'll have to regenerate the data in `runtime-data/` with `scripts/`, in this order:

1. `mkdir "Non-essential data"`
2. `python3 scripts/build_mpa_boundary.py` - builds the Metropolitan Planning Area boundary polygon
   (`runtime-data/mpa_boundary.json`) that every later step clips to.
3. `python3 scripts/build_city_boundaries.py` - fetches incorporated city limits from Metro's
   `City_Limits_poly` layer (`runtime-data/city_boundaries.json`), used by `build_aggregation_layers.py`
   below to find Portland's boundary.
4. `python3 scripts/build_taxlot_dataset.py` - fetches every taxlot and zoning polygon in the region, clips to
   the MPA boundary, spatially joins each taxlot to its zoning class. Writes
   `runtime-data/taxlot_density_data.json(.gz)`.
5. `python3 scripts/fetch_taxlot_addresses.py` - adds the address to each taxlot.
6. `python3 scripts/fetch_taxlot_housing_units.py` - adds the existing housing unit count to each taxlot.
7. `python3 scripts/add_portland_rip_density.py` - calculates the maximum units allowed per taxlot according to Portland's Residential Infill Project (Portland City Code 33.110.265).
8. `python3 scripts/build_aggregation_layers.py` - builds the data for the aggregate areas (that is, Portland zoning polygons, census blocks, and census tracts).

Steps 4–8 each take a while and checkpoint their progress into the `Non-essential data/` directory so a killed/crashed run resumes instead of starting over. Checkpoints are gitignored.
