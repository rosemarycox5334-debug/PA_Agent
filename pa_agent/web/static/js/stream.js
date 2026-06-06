const API_BASE = "";

export function createStream(onFrame) {
    const es = new EventSource(`${API_BASE}/api/stream`);
    es.addEventListener("kline_frame", (e) => {
        try {
            const frame = JSON.parse(e.data);
            onFrame(frame);
        } catch (err) {
            console.warn("Malformed SSE frame, ignoring:", err);
        }
    });
    es.onerror = () => console.warn("SSE error, reconnecting...");
    return () => es.close();
}
