// Serves the R2-hosted taxlot dataset for portland-housing-density-mapper.html - same tod-pitch-data bucket
// the main TOD-PITCH tool uses (read-only from here, via its own binding below), reused rather than
// duplicated since it's the exact same file. See workers/taxlot-worker.js's own comment for why this can't
// just be a static asset: the gzip taxlot dataset is ~70MB, over Cloudflare Workers' 25MiB static-asset
// limit. Only "/taxlot-data" ever reaches this code - see wrangler-density-mapper.jsonc's run_worker_first -
// every other request (the HTML file itself, the small runtime-data/ JSON files) is served directly as a
// static asset instead.
export default {
    async fetch(request, env) {
        if (new URL(request.url).pathname !== '/taxlot-data') {
            return new Response('Not found', { status: 404 });
        }
        const object = await env.TAXLOT_DATA_BUCKET.get('taxlot_density_data.json');
        if (object === null) {
            return new Response('Not found: taxlot_density_data.json in bucket tod-pitch-data', { status: 404 });
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
