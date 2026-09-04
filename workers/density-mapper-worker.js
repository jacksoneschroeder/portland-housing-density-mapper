// Serves the R2-hosted taxlot dataset(s) for portland-housing-density-mapper.html - same tod-pitch-data
// bucket the main TOD-PITCH tool uses (read-only from here, via its own binding below). Only Portland's own
// key (portland_taxlot_density_data.json) is actually shared with that tool - TOD-PITCH's own worker owns and
// regenerates the ORIGINAL taxlot_density_data.json key under this same bucket; that key is left completely
// alone here (never renamed/deleted, and this Worker no longer reads it), since deleting or renaming it out
// from under TOD-PITCH would break a separate, live tool - this app's own R2 object under the new prefixed
// key is a duplicate upload, not a rename of the shared one. See workers/taxlot-worker.js's own comment for
// why this can't just be a static asset: the gzip taxlot dataset is tens of MB, over Cloudflare Workers'
// 25MiB static-asset limit. Only "/taxlot-data/*" ever reaches this code - see wrangler.jsonc's
// run_worker_first - every other request (the HTML file itself, the small runtime-data/ JSON files) is served
// directly as a static asset instead.
const R2_KEYS = {
    portland: 'portland_taxlot_density_data.json',
    salem: 'salem_taxlot_density_data.json',
};

export default {
    async fetch(request, env) {
        const match = /^\/taxlot-data\/([a-z]+)$/.exec(new URL(request.url).pathname);
        const key = match && R2_KEYS[match[1]];
        if (!key) {
            return new Response('Not found', { status: 404 });
        }
        const object = await env.TAXLOT_DATA_BUCKET.get(key);
        if (object === null) {
            return new Response('Not found: ' + key + ' in bucket tod-pitch-data', { status: 404 });
        }
        // Plain application/octet-stream, not Content-Encoding: gzip - see taxlot-worker.js's own comment on
        // why (relying on the browser to transparently decompress based on that header proved unreliable in
        // practice). The client (taxlot-parse-worker.js) always gunzips it itself via DecompressionStream.
        return new Response(object.body, {
            headers: {
                'Content-Type': 'application/octet-stream',
                'Cache-Control': 'public, max-age=3600',
            },
        });
    },
};
