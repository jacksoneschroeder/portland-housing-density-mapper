// Browser Web Worker (runs in the visitor's own browser, on its own thread) - NOT the same thing as
// taxlot-worker.js (a Cloudflare Worker, server-side edge code that streams the file out of R2). This one's
// only job is to fetch the taxlot dataset and JSON.parse() it off the page's main thread: that parse is a
// single synchronous call with no way to yield partway through, so running it on the main thread would
// freeze the whole page - repaints, the progress bar itself, everything - for however long it takes. Off
// the main thread, the tab stays fully responsive the entire time.
//
// Tries `url` first, falls back to `fallbackUrl` if that fails (mirrors index.html's own primary/local
// fallback logic, since /taxlot-data - the deployed Cloudflare Worker route - only resolves on the actual
// deployment, not a local dev server). Both URLs must be root-relative (leading "/") rather than plain
// relative paths - fetch() inside a Worker resolves a relative URL against the WORKER SCRIPT's own
// location (workers/taxlot-parse-worker.js), not the page's, so a plain "runtime-data/..." path here would
// resolve to workers/runtime-data/... instead of the real file at the site root.
//
// Both URLs serve real gzip-compressed bytes (taxlot_density_data.json.gz, uploaded to R2 under the
// taxlot-worker.js route, or the same file locally) - this always decompresses via DecompressionStream, a
// native browser API (no library needed), rather than relying on a Content-Encoding: gzip response header
// and the browser's own transparent decompression. taxlot-worker.js deliberately doesn't set that header -
// see its own comment on why that turned out to be unreliable behind this site's Cloudflare Access setup.
//
// The decompressed JSON itself is a compact columnar format ({fields: [...], rows: [[...], ...]}) rather
// than one object per taxlot repeating every field name 571k+ times - see write_taxlot_dataset() in
// scripts/build_taxlot_dataset.py for the full reasoning (repeated key names were 60% of this file's own
// byte size, which used to push the decompressed JSON past the ~537MB hard ceiling Chrome's V8 places on a
// single JS string - Blob.text() below silently returned an empty string past that point, with no error
// thrown, which is what actually broke taxlot loading before this format existed). rowsToObjects() below
// reconstructs the named-object array immediately after parsing, so every consumer of this worker's `data`
// message (index.html, and this worker's own postMessage contract) is completely unaffected by the format
// change - the reconstruction is real per-taxlot object allocation (unavoidable, that's what index.html
// actually needs), but it's fast compared to the fetch/decompress/parse this already does.
// True on a plain local dev server (127.0.0.1/localhost) - just adds a reassuring note onto the warning
// below so the 404 this route always produces locally (it only resolves once actually deployed behind the
// Cloudflare Worker) doesn't read as a real bug.
function isLocalDevServer() {
    return ['localhost', '127.0.0.1'].indexOf(self.location.hostname) !== -1;
}

self.onmessage = function(e) {
    var url = e.data.url;
    var fallbackUrl = e.data.fallbackUrl;
    fetchAndParse(url).catch(function(err) {
        console.warn('Primary taxlot dataset fetch (' + url + ') failed, falling back to local file' + (isLocalDevServer() ? ' (expected here - this route only exists once deployed behind the Cloudflare Worker)' : '') + ':', err);
        return fetchAndParse(fallbackUrl);
    }).then(function(data) {
        postMessage({ type: 'done', data: data });
    }).catch(function(err) {
        postMessage({ type: 'error', message: String(err) });
    });
};

// Byte-level progress via a counting TransformStream. Content-Length reflects the compressed bytes actually transferred over the wire,
// so the counting TransformStream below sits BEFORE the DecompressionStream and counts raw compressed
// bytes as they arrive - counting post-decompression bytes instead (as an earlier version of this did)
// would race ahead of the real transfer, since decompressed output is ~3x larger than what's actually been
// received, and then sit stuck at the 99% clamp for however much of the real download was still in flight.
function fetchAndParse(url) {
    return fetch(url).then(function(response) {
        if (!response.ok) throw new Error('Fetch of ' + url + ' failed: ' + response.status + ' ' + response.statusText);
        var contentLength = response.headers.get('content-length');
        var total = contentLength ? parseInt(contentLength, 10) : null;
        var loaded = 0;
        var countingStream = new TransformStream({
            transform: function(chunk, controller) {
                loaded += chunk.length;
                postMessage({ type: 'progress', fraction: total ? Math.min(loaded / total, 0.99) : null });
                controller.enqueue(chunk);
            },
        });
        var body = response.body.pipeThrough(countingStream).pipeThrough(new DecompressionStream('gzip'));
        var reader = body.getReader();
        var chunks = [];
        function pump() {
            return reader.read().then(function(result) {
                if (result.done) return;
                chunks.push(result.value);
                return pump();
            });
        }
        return pump().then(function() {
            postMessage({ type: 'progress', fraction: 1 });
            return new Blob(chunks).text();
        });
    }).then(function(text) { return rowsToObjects(JSON.parse(text)); });
}

// {fields: ['tlid', 'acres', ...], rows: [[val, val, ...], ...]} -> [{tlid: val, acres: val, ...}, ...] -
// see this file's own top comment for why the wire format is columnar instead of one object per taxlot.
function rowsToObjects(parsed) {
    var fields = parsed.fields;
    var rows = parsed.rows;
    var taxlots = new Array(rows.length);
    for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        var t = {};
        for (var j = 0; j < fields.length; j++) t[fields[j]] = row[j];
        taxlots[i] = t;
    }
    return taxlots;
}
