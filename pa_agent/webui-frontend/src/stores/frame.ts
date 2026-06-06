/**
 * Frame store — holds the latest kline snapshot, the live SSE subscription
 * handle, and computed helpers used by the chart panel.
 *
 * The store is intentionally decoupled from the chart canvas: the chart
 * component subscribes via Vue's reactivity and re-renders on change.
 */
import { computed, reactive } from 'vue';
import { api, type DataSnapshot } from '@/api/client';

export interface FormingCountdown {
  text: string;
  remainingSec: number;
}

const tfMinutes: Record<string, number> = {
  '1m': 1,
  '5m': 5,
  '15m': 15,
  '30m': 30,
  '1h': 60,
  '4h': 240,
  '1d': 1440,
};

export const frameStore = reactive({
  snapshot: null as DataSnapshot | null,
  loading: false,
  error: null as string | null,
  isLive: true,
  cleanupStream: null as null | (() => void),
  eventSource: null as EventSource | null,

  async refresh(): Promise<DataSnapshot> {
    this.loading = true;
    this.error = null;
    try {
      const snap = await api.fetchSnapshot();
      this.snapshot = snap;
      return snap;
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
      throw err;
    } finally {
      this.loading = false;
    }
  },

  setSnapshot(snap: DataSnapshot): void {
    this.snapshot = snap;
  },

  startLive(onUpdate: (snap: DataSnapshot) => void): void {
    this.stopLive();
    if (typeof EventSource === 'undefined') {
      console.warn('[frame] EventSource is not available; live updates disabled.');
      return;
    }
    const es = new EventSource('/api/data/stream');
    es.onmessage = (ev: MessageEvent<string>) => {
      try {
        const data = JSON.parse(ev.data) as DataSnapshot;
        this.snapshot = data;
        onUpdate(data);
      } catch (err) {
        console.warn('[frame] failed to parse stream payload:', err);
      }
    };
    es.onerror = () => {
      // Browser will auto-reconnect; just surface to the console for now.
      console.warn('[frame] SSE connection error');
    };
    this.eventSource = es;
    this.isLive = true;
  },

  stopLive(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    this.isLive = false;
  },
});

export const barCount = computed(() => frameStore.snapshot?.bars?.length ?? 0);
export const lastClose = computed(() => frameStore.snapshot?.bars?.[0]?.close ?? null);
export const lastEma20 = computed(() => {
  const ema = frameStore.snapshot?.indicators?.ema20;
  if (!ema || ema.length === 0) return null;
  const v = ema[ema.length - 1];
  return typeof v === 'number' ? v : null;
});

export const formingCountdown = computed<FormingCountdown>(() => {
  const snap = frameStore.snapshot;
  const bar = snap?.bars?.[0];
  if (!bar || bar.closed) return { text: '', remainingSec: 0 };
  const tf = snap?.timeframe ?? '1m';
  const periodMin = tfMinutes[tf] ?? 1;
  const nextClose = bar.ts_open + periodMin * 60 * 1000;
  const remaining = Math.max(0, nextClose - Date.now());
  const sec = Math.ceil(remaining / 1000);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return {
    text: `${m}:${String(s).padStart(2, '0')}`,
    remainingSec: sec,
  };
});
