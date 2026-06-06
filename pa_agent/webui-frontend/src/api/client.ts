/**
 * Unified fetch wrapper for the PA Agent backend.
 *
 * Every endpoint is mounted under `/api/...` on the FastAPI server. During
 * dev, Vite's dev-server proxy (see vite.config.ts) forwards these calls to
 * http://localhost:8080 so the SPA can keep a same-origin mental model.
 *
 * The wrapper is intentionally tiny: it handles JSON encoding, error
 * extraction, and a typed `stream()` helper for SSE. No external HTTP
 * library is required.
 */

const API_BASE = '/api';

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function extractError(res: Response): Promise<ApiError> {
  let payload: unknown = null;
  let message = `HTTP ${res.status}`;
  try {
    payload = await res.json();
    if (payload && typeof payload === 'object') {
      const obj = payload as { detail?: unknown; message?: unknown };
      if (typeof obj.detail === 'string') message = obj.detail;
      else if (typeof obj.message === 'string') message = obj.message;
    }
  } catch {
    // response had no JSON body — fall through with status-only message
  }
  return new ApiError(message, res.status, payload);
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export async function request<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = 'GET', body, headers = {}, signal } = options;
  const init: RequestInit = {
    method,
    headers: {
      Accept: 'application/json',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    signal,
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) throw await extractError(res);
  // 204 No Content
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

/**
 * Open a streaming response and return the underlying Response object.
 * Caller is responsible for iterating the body reader (e.g. SSE).
 */
export async function stream(path: string, options: RequestOptions = {}): Promise<Response> {
  const { method = 'POST', body, headers = {}, signal } = options;
  const init: RequestInit = {
    method,
    headers: {
      Accept: 'text/event-stream',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    signal,
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) throw await extractError(res);
  return res;
}

// ------------------------------------------------------------------
// Typed endpoint helpers — match the routers in pa_agent/web/api/*.
// ------------------------------------------------------------------

export interface SettingsResponse {
  provider: {
    model: string;
    base_url: string;
    api_key: string;
    thinking: boolean;
    reasoning_effort: string;
    context_window: number;
  };
  general: {
    analysis_bar_count: number;
    refresh_interval_ms: number;
    auto_resume_chart_after_analysis: boolean;
    context_warning_threshold_pct: number;
    stream_pane_font_pt: number;
    chart_seq_label_font_pt: number;
    incremental_max_new_bars: number;
    decision_stance: string;
    rqdata_license_key: string;
    decision_flow_auto_play: boolean;
    decision_flow_play_seconds: number;
    decision_flow_default_zoom_pct: number;
    last_symbol: string;
    last_timeframe: string;
    last_data_source: string;
    last_tradingview_exchange: string;
  };
}

export interface DataSnapshot {
  symbol: string;
  timeframe: string;
  bars: Array<{
    seq: number;
    ts_open: number;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    closed: boolean;
  }>;
  indicators: {
    ema10?: Array<number | null>;
    ema20?: Array<number | null>;
    ema60?: Array<number | null>;
    atr14?: Array<number | null>;
  };
}

export interface LedgerResponse {
  context_pct: number;
  context_used: number;
  context_window: number;
}

export interface RecordListItem {
  filename: string;
  timestamp: string;
  symbol: string;
  timeframe: string;
  bar_count: number;
}

export const api = {
  fetchSnapshot: () => request<DataSnapshot>('/data/snapshot'),
  submitAnalysis: (
    barCount: number,
    stance: string = 'balanced',
    incremental: boolean = false,
    incrementalNewBars: number | null = null,
  ) => {
    const payload: Record<string, unknown> = {
      bar_count: barCount,
      stance,
      incremental,
    };
    if (incremental && incrementalNewBars !== null) {
      payload.incremental_new_bars = incrementalNewBars;
    }
    return stream('/analysis/submit', { method: 'POST', body: payload });
  },
  fetchSettings: () => request<SettingsResponse>('/settings'),
  saveSettings: (payload: Partial<SettingsResponse>) =>
    request<SettingsResponse>('/settings', { method: 'POST', body: payload }),
  fetchLedger: () => request<LedgerResponse>('/ledger'),
  submitFollowup: (text: string) =>
    stream('/analysis/followup', { method: 'POST', body: { text } }),
  fetchDebugTurns: () =>
    request<Array<Record<string, unknown>> | { turns: Array<Record<string, unknown>> }>(
      '/debug/turns',
    ),
  fetchRecords: () =>
    request<{ records: RecordListItem[] }>('/records'),
  fetchRecord: (filename: string) =>
    request<Record<string, unknown>>(`/records/${encodeURIComponent(filename)}`),
};

export default api;
