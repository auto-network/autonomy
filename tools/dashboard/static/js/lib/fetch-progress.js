/**
 * Fetch with streaming progress tracking.
 *
 * Usage:
 *   const data = await fetchWithProgress('/api/big-payload', (received, total) => {
 *       this.loadPercent = Math.round(received / total * 100);
 *       this.loadedMB = (received / 1048576).toFixed(1);
 *       this.totalMB = (total / 1048576).toFixed(1);
 *   });
 *
 * Falls back to plain fetch().json() if Content-Length is missing or
 * ReadableStream is unsupported (older browsers).
 *
 * @param {string} url - Fetch URL
 * @param {function(number, number)|null} onProgress - callback(receivedBytes, totalBytes)
 * @returns {Promise<any>} Parsed JSON response
 */
window.fetchWithProgress = async function(url, onProgress) {
    var response = await fetch(url);

    var total = parseInt(response.headers.get('Content-Length'), 10);
    if (!total || !response.body || !response.body.getReader) {
        return response.json();
    }

    var reader = response.body.getReader();
    var received = 0;
    var chunks = [];

    while (true) {
        var result = await reader.read();
        if (result.done) break;
        chunks.push(result.value);
        received += result.value.length;
        if (onProgress) onProgress(received, total);
    }

    // Concatenate Uint8Array chunks and decode
    var body = new Uint8Array(received);
    var offset = 0;
    for (var i = 0; i < chunks.length; i++) {
        body.set(chunks[i], offset);
        offset += chunks[i].length;
    }

    return JSON.parse(new TextDecoder().decode(body));
};
