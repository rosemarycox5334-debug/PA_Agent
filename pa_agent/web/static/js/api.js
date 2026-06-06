const API_BASE = "";

async function errorMessage(res) {
    try {
        const data = await res.json();
        if (data.detail) return data.detail;
        if (data.message) return data.message;
    } catch (_) {
        // Fall back to the HTTP status below.
    }
    return `HTTP ${res.status}`;
}

export async function fetchSnapshot() {
    const res = await fetch(`${API_BASE}/api/data/snapshot`);
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}

export async function submitAnalysis(barCount = 80, stance = "balanced", incremental = false, incrementalNewBars = null) {
    const payload = { bar_count: barCount, stance: stance, incremental: incremental };
    if (incremental && incrementalNewBars != null) {
        payload.incremental_new_bars = incrementalNewBars;
    }
    const res = await fetch(`${API_BASE}/api/analysis/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await errorMessage(res));
    return res;
}

export async function fetchSettings() {
    const res = await fetch(`${API_BASE}/api/settings`);
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}

export async function saveSettings(payload) {
    const res = await fetch(`${API_BASE}/api/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}

export async function fetchLedger() {
    const res = await fetch(`${API_BASE}/api/ledger`);
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}

export async function submitFollowup(text) {
    const res = await fetch(`${API_BASE}/api/analysis/followup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
    });
    if (!res.ok) throw new Error(await errorMessage(res));
    return res;
}

export async function fetchDebugTurns() {
    const res = await fetch(`${API_BASE}/api/debug/turns`);
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}

export async function fetchRecords() {
    const res = await fetch(`${API_BASE}/api/records`);
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}

export async function fetchRecord(filename) {
    const res = await fetch(`${API_BASE}/api/records/${encodeURIComponent(filename)}`);
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}

export async function fetchSources() {
    const res = await fetch(`${API_BASE}/api/data/sources`);
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
}
