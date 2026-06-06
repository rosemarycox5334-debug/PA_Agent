/**
 * DemoRecordPicker.vue — unit tests
 *
 * Coverage:
 *  - Renders search input, timeframe select, refresh and 随机演示 buttons.
 *  - Filters the record list by search query and timeframe.
 *  - Calls `api.fetchRecords` on mount; emits `refreshed` with the count.
 *  - Random pick invokes `api.fetchRecord('__random__')`, hydrates
 *    `frameStore` + `decisionStore`, and emits `picked`.
 *  - Manual record click fetches detail, hydrates stores, emits `picked`.
 *  - Surfaces errors via the `error` event + template role=alert.
 *  - The Canvas 2D viz uses theme tokens; draws empty-state text when no
 *    selection, otherwise draws bezier edges + glass-card nodes.
 *  - ResizeObserver is attached on mount and disconnected on unmount.
 *  - `buildViz` extracts gate/decision/terminal nodes + edges from
 *    stage2_decision; `layoutViz` distributes nodes across the canvas width.
 *
 * Theme token coverage is asserted in `src/test/tokens.spec.ts`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils';
import { nextTick } from 'vue';
import DemoRecordPicker, {
  buildViz,
  layoutViz,
  type RecordDetail,
  type VizNode,
} from '@/components/demo/DemoRecordPicker.vue';
import { api, type RecordListItem } from '@/api/client';
import { frameStore } from '@/stores/frame';
import { decisionStore } from '@/stores/decision';

// ------------------------------------------------------------------
// API mocking — vitest module mocks
// ------------------------------------------------------------------
vi.mock('@/api/client', () => {
  return {
    api: {
      fetchRecords: vi.fn(),
      fetchRecord: vi.fn(),
      fetchSnapshot: vi.fn(),
      submitAnalysis: vi.fn(),
      fetchSettings: vi.fn(),
      saveSettings: vi.fn(),
      fetchLedger: vi.fn(),
      submitFollowup: vi.fn(),
      fetchDebugTurns: vi.fn(),
    },
  };
});

const mockedApi = vi.mocked(api, { deep: true });

type CtxStub = {
  setTransform: ReturnType<typeof vi.fn>;
  clearRect: ReturnType<typeof vi.fn>;
  beginPath: ReturnType<typeof vi.fn>;
  closePath: ReturnType<typeof vi.fn>;
  moveTo: ReturnType<typeof vi.fn>;
  lineTo: ReturnType<typeof vi.fn>;
  stroke: ReturnType<typeof vi.fn>;
  fill: ReturnType<typeof vi.fn>;
  arc: ReturnType<typeof vi.fn>;
  fillRect: ReturnType<typeof vi.fn>;
  strokeRect: ReturnType<typeof vi.fn>;
  bezierCurveTo: ReturnType<typeof vi.fn>;
  quadraticCurveTo: ReturnType<typeof vi.fn>;
  fillText: ReturnType<typeof vi.fn>;
  strokeStyle: string | null;
  fillStyle: string | null;
  lineWidth: number;
  lineJoin: string;
  setLineDash: ReturnType<typeof vi.fn>;
  textAlign: string;
  textBaseline: string;
  font: string;
};

function makeCtxStub(): CtxStub {
  return {
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    arc: vi.fn(),
    fillRect: vi.fn(),
    strokeRect: vi.fn(),
    bezierCurveTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    fillText: vi.fn(),
    strokeStyle: null,
    fillStyle: null,
    lineWidth: 0,
    lineJoin: '',
    setLineDash: vi.fn(),
    textAlign: 'left',
    textBaseline: 'top',
    font: '',
  };
}

function stubCanvasContext(ctx: CtxStub): { restore: () => void } {
  const original = HTMLCanvasElement.prototype.getContext;
  // Use defineProperty to override even if jsdom marked the method
  // non-writable on this prototype revision.
  Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
    configurable: true,
    writable: true,
    enumerable: true,
    value: vi.fn(() => ctx),
  });
  return {
    restore: () => {
      Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
        configurable: true,
        writable: true,
        enumerable: true,
        value: original,
      });
    },
  };
}

const SAMPLE_RECORDS: RecordListItem[] = [
  { filename: 'AAPL_5m_2024-01-02.json', timestamp: '2024-01-02T09:30:00Z', symbol: 'AAPL', timeframe: '5m', bar_count: 60 },
  { filename: 'AAPL_1h_2024-01-02.json', timestamp: '2024-01-02T09:00:00Z', symbol: 'AAPL', timeframe: '1h', bar_count: 24 },
  { filename: 'TSLA_15m_2024-02-10.json', timestamp: '2024-02-10T13:15:00Z', symbol: 'TSLA', timeframe: '15m', bar_count: 96 },
  { filename: 'BTC_1d_2024-03-01.json', timestamp: '2024-03-01T00:00:00Z', symbol: 'BTC', timeframe: '1d', bar_count: 30 },
];

const SAMPLE_DETAIL: RecordDetail = {
  filename: 'AAPL_5m_2024-01-02.json',
  symbol: 'AAPL',
  timeframe: '5m',
  bar_count: 60,
  timestamp: '2024-01-02T09:30:00Z',
  bars: [
    { seq: 1, ts_open: 1704190200000, open: 100, high: 102, low: 99, close: 101, volume: 1000, closed: true },
    { seq: 2, ts_open: 1704190500000, open: 101, high: 103, low: 100, close: 102, volume: 900, closed: true },
  ],
  indicators: { ema20: [100, 101] },
  stage2_decision: {
    order_direction: '做多',
    entry_price: 101.5,
    take_profit_price: 105,
    stop_loss_price: 99,
    confidence: 0.82,
    gate_trace: [
      { id: 'g1', name: '趋势', branch: 'up' },
      { id: 'g2', name: '动量', branch: 'up' },
    ],
    decision_trace: [
      { id: 'd1', name: '突破', branch: 'up' },
    ],
    terminal: { id: 't1', label: '入场: 做多', branch: 'up' },
  } as RecordDetail['stage2_decision'],
};

/**
 * Mount the picker with default mocks. The returned helpers include a
 * `settle()` function that flushes all pending microtasks so async
 * `refresh()` / `pickRecord()` / `pickRandom()` calls complete before
 * assertions. This is the right primitive for jsdom + Vue 3 — `nextTick`
 * alone is insufficient because it only drains Vue's reactive queue,
 * not arbitrary user-land Promises (e.g. the mocked `fetchRecords`).
 */
function mountPicker(props: Record<string, unknown> = {}): {
  wrapper: VueWrapper;
  ctx: CtxStub;
  observe: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
  restore: () => void;
  settle: () => Promise<void>;
} {
  const ctx = makeCtxStub();
  const { restore } = stubCanvasContext(ctx);
  const observe = vi.fn();
  const disconnect = vi.fn();
  class RO {
    cb: ResizeObserverCallback;
    constructor(cb: ResizeObserverCallback) {
      this.cb = cb;
    }
    observe = observe;
    unobserve = vi.fn();
    disconnect = disconnect;
  }
  (globalThis as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO;

  mockedApi.fetchRecords.mockResolvedValue({ records: SAMPLE_RECORDS });
  // fetchRecord is NOT stubbed here — individual tests set their own
  // expectations for it (random pick, manual click, null detail, error).
  // This avoids mountPicker silently overriding a test's mock setup.

  const wrapper = mount(DemoRecordPicker, {
    props: { width: 480, height: 220, ...props },
    attachTo: document.body,
  });
  return {
    wrapper,
    ctx,
    observe,
    disconnect,
    restore,
    settle: async () => {
      // Drain Vue's reactive queue + every queued microtask (mock
      // fetchRecords, watch jobs, ResizeObserver callbacks, etc).
      await flushPromises();
      await nextTick();
    },
  };
}

describe('DemoRecordPicker.vue', () => {
  let pendingRestore: Array<() => void> = [];
  afterEach(() => {
    pendingRestore.forEach((fn) => fn());
    pendingRestore = [];
    document.body.innerHTML = '';
    vi.clearAllMocks();
    // Reset stores between tests
    frameStore.setSnapshot({
      symbol: '',
      timeframe: '1m',
      bars: [],
      indicators: {},
    });
    decisionStore.setDecision(null);
  });
  function trackRestore(r: () => void): void {
    pendingRestore.push(r);
  }

  it('renders search, timeframe select, refresh, and 随机演示 buttons', async () => {
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    expect(wrapper.find('[data-testid="search-input"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="timeframe-select"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="refresh-btn"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pick-random-btn"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="viz-canvas"]').exists()).toBe(true);
  });

  it('fetches records on mount and emits `refreshed` with the count', async () => {
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    expect(mockedApi.fetchRecords).toHaveBeenCalledTimes(1);
    const events = wrapper.emitted('refreshed') as number[][];
    expect(events).toBeTruthy();
    expect(events[0]?.[0]).toBe(SAMPLE_RECORDS.length);
  });

  it('filters the list by search query (symbol match)', async () => {
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    const search = wrapper.find('[data-testid="search-input"]');
    await search.setValue('TSLA');
    await settle();
    const items = wrapper.findAll('.record-item');
    expect(items.length).toBe(1);
    expect(items[0]?.text()).toContain('TSLA');
  });

  it('filters the list by timeframe select', async () => {
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    const sel = wrapper.find('[data-testid="timeframe-select"]');
    await sel.setValue('1h');
    await settle();
    const items = wrapper.findAll('.record-item');
    expect(items.length).toBe(1);
    expect(items[0]?.text()).toContain('1h');
  });

  it('emits `error` and renders the message when fetchRecords rejects', async () => {
    mockedApi.fetchRecords.mockRejectedValueOnce(new Error('网络异常'));
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    const errorNode = wrapper.find('[data-testid="error"]');
    expect(errorNode.exists()).toBe(true);
    expect(errorNode.text()).toContain('网络异常');
    const errEvents = wrapper.emitted('error') as string[][];
    expect(errEvents).toBeTruthy();
    expect(errEvents[0]?.[0]).toBe('网络异常');
  });

  it('clicking a record hydrates frameStore + decisionStore and emits `picked`', async () => {
    mockedApi.fetchRecord.mockResolvedValueOnce(SAMPLE_DETAIL);
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    await wrapper.find('[data-testid="record-AAPL_5m_2024-01-02.json"]').trigger('click');
    await settle();
    expect(mockedApi.fetchRecord).toHaveBeenCalledWith('AAPL_5m_2024-01-02.json');
    expect(frameStore.snapshot?.symbol).toBe('AAPL');
    expect(frameStore.snapshot?.bars?.length).toBe(2);
    expect(decisionStore.decision?.order_direction).toBe('做多');
    const picked = wrapper.emitted('picked') as Array<
      Array<{ record: RecordListItem; detail: RecordDetail }>
    >;
    expect(picked).toBeTruthy();
    expect(picked[0]?.[0]?.record.filename).toBe('AAPL_5m_2024-01-02.json');
  });

  it('随机演示 button fetches __random__ and hydrates stores + emits `picked`', async () => {
    mockedApi.fetchRecord.mockResolvedValueOnce(SAMPLE_DETAIL);
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    await wrapper.find('[data-testid="pick-random-btn"]').trigger('click');
    await settle();
    expect(mockedApi.fetchRecord).toHaveBeenCalledWith('__random__');
    expect(frameStore.snapshot?.symbol).toBe('AAPL');
    expect(decisionStore.decision?.order_direction).toBe('做多');
    const picked = wrapper.emitted('picked') as Array<
      Array<{ record: RecordListItem; detail: RecordDetail }>
    >;
    expect(picked).toBeTruthy();
  });

  it('surfaces random-pick failure via `error` when server returns null', async () => {
    mockedApi.fetchRecord.mockResolvedValueOnce(null);
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    await wrapper.find('[data-testid="pick-random-btn"]').trigger('click');
    await settle();
    const errorNode = wrapper.find('[data-testid="error"]');
    expect(errorNode.exists()).toBe(true);
    expect(errorNode.text()).toContain('随机选取失败');
  });

  it('disables 随机演示 button when there are no records', async () => {
    mockedApi.fetchRecords.mockResolvedValueOnce({ records: [] });
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    const btn = wrapper.find('[data-testid="pick-random-btn"]');
    expect((btn.element as HTMLButtonElement).disabled).toBe(true);
  });

  it('draws the empty-state hint on the canvas when nothing is selected', async () => {
    const { ctx, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    expect(ctx.fillText).toHaveBeenCalled();
    const calls = ctx.fillText.mock.calls.map((c) => c[0] as string);
    expect(calls.some((c) => c.includes('请先选择一条演示记录'))).toBe(true);
  });

  it('draws bezier edges and glass-card nodes after a record is picked', async () => {
    mockedApi.fetchRecord.mockResolvedValueOnce(SAMPLE_DETAIL);
    const { wrapper, ctx, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    const beginBefore = ctx.beginPath.mock.calls.length;
    const bezierBefore = ctx.bezierCurveTo.mock.calls.length;
    const fillBefore = ctx.fill.mock.calls.length;
    await wrapper.find('[data-testid="record-AAPL_5m_2024-01-02.json"]').trigger('click');
    await settle();
    expect(ctx.beginPath.mock.calls.length).toBeGreaterThan(beginBefore);
    expect(ctx.bezierCurveTo.mock.calls.length).toBeGreaterThan(bezierBefore);
    expect(ctx.fill.mock.calls.length).toBeGreaterThan(fillBefore);
    expect(ctx.fillText).toHaveBeenCalled();
  });

  it('attaches a ResizeObserver on mount and disconnects on unmount', async () => {
    const { wrapper, observe, disconnect, restore, settle } = mountPicker();
    trackRestore(restore);
    await settle();
    expect(observe).toHaveBeenCalled();
    wrapper.unmount();
    expect(disconnect).toHaveBeenCalled();
  });

  it('emits `error` when manual record fetch throws', async () => {
    mockedApi.fetchRecord.mockImplementation(async (filename: string) => {
      if (filename === '__random__') return SAMPLE_DETAIL;
      throw new Error('记录不存在');
    });
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    await wrapper.find('[data-testid="record-TSLA_15m_2024-02-10.json"]').trigger('click');
    await settle();
    const errorNode = wrapper.find('[data-testid="error"]');
    expect(errorNode.exists()).toBe(true);
    expect(errorNode.text()).toContain('记录不存在');
  });

  it('uses the empty list state when filter matches nothing', async () => {
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    const search = wrapper.find('[data-testid="search-input"]');
    await search.setValue('XYZ-NO-MATCH');
    await settle();
    expect(wrapper.find('[data-testid="empty"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="empty"]').text()).toContain('无匹配记录');
  });

  it('displays the no-records message when the server returns an empty list', async () => {
    mockedApi.fetchRecords.mockResolvedValueOnce({ records: [] });
    const { wrapper, settle, restore } = mountPicker();
    trackRestore(restore);
    await settle();
    const empty = wrapper.find('[data-testid="empty"]');
    expect(empty.exists()).toBe(true);
    expect(empty.text()).toContain('尚无演示记录');
  });
});

describe('DemoRecordPicker — buildViz / layoutViz (pure helpers)', () => {
  it('returns empty arrays when no detail is supplied', () => {
    const { nodes, edges } = buildViz(null);
    expect(nodes).toEqual([]);
    expect(edges).toEqual([]);
  });

  it('builds gate / decision / terminal nodes with phase coding', () => {
    const { nodes, edges } = buildViz(SAMPLE_DETAIL);
    const phases = nodes.map((n) => n.phase);
    expect(phases).toEqual(['gate', 'gate', 'decision', 'terminal']);
    // Implicit chain edges: g1→g2, g2→d1, d1→t1
    expect(edges.length).toBeGreaterThanOrEqual(3);
    // Edges from gate trace inherit branch
    expect(edges[0]?.branch).toBe('up');
  });

  it('layoutViz distributes nodes horizontally', () => {
    const { nodes } = buildViz(SAMPLE_DETAIL);
    layoutViz(nodes, 400, 200);
    // X must be strictly increasing and Y equal for a horizontal flow
    for (let i = 1; i < nodes.length; i++) {
      const prev = nodes[i - 1] as VizNode;
      const cur = nodes[i] as VizNode;
      expect(cur.x).toBeGreaterThan(prev.x);
      expect(cur.y).toBe(prev.y);
    }
  });

  it('layoutViz is a no-op for an empty list', () => {
    const nodes: VizNode[] = [];
    layoutViz(nodes, 400, 200);
    expect(nodes.length).toBe(0);
  });

  it('builds an explicit edge when `next` is a string', () => {
    const detail: RecordDetail = {
      filename: 'X.json',
      stage2_decision: {
        gate_trace: [
          { id: 'a', name: 'A', branch: 'up', next: 'b' },
          { id: 'b', name: 'B', branch: 'up' },
        ],
        decision_trace: [],
      } as RecordDetail['stage2_decision'],
    };
    const { nodes, edges } = buildViz(detail);
    expect(nodes.map((n) => n.id)).toEqual(['a', 'b']);
    expect(edges.some((e) => e.from === 'a' && e.to === 'b')).toBe(true);
  });
});
