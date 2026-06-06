/**
 * StreamContextWarning — unit tests
 *
 * Coverage:
 *  - Renders the current percentage, used/window counts, and level label.
 *  - Escalates through `safe` → `warn` → `danger` as currentPct crosses the
 *    configured threshold and the safe/warn bands.
 *  - Emits a `level` event whenever currentPct changes, including the
 *    initial mount.
 *  - The Canvas 2D trend chart calls the expected context2d methods
 *    (beginPath / moveTo / lineTo / stroke / fill / arc) and is redrawn
 *    when the history array mutates.
 *  - Respects the default and override threshold props.
 *  - Cleans up the ResizeObserver on unmount.
 *
 * Theme token coverage is asserted in `src/test/tokens.spec.ts`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, type VueWrapper } from '@vue/test-utils';
import { nextTick } from 'vue';
import StreamContextWarning, {
  type ContextLevel,
  type ContextSample,
} from '@/components/stream/StreamContextWarning.vue';

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
  strokeStyle: string | null;
  fillStyle: string | null;
  lineWidth: number;
  lineJoin: string;
  setLineDash: ReturnType<typeof vi.fn>;
  save: ReturnType<typeof vi.fn>;
  restore: ReturnType<typeof vi.fn>;
  measureText: ReturnType<typeof vi.fn>;
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
    strokeStyle: null,
    fillStyle: null,
    lineWidth: 0,
    lineJoin: '',
    setLineDash: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    measureText: vi.fn(() => ({ width: 0 } as TextMetrics)),
  };
}

function stubCanvasContext(ctx: CtxStub): {
  restore: () => void;
} {
  // jsdom 24 ships a (mostly no-op) Canvas 2D context. We override the
  // prototype's `getContext` so every canvas instance returns our spy.
  // Saves/restores the original so other suites aren't affected.
  const original = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ctx) as unknown as HTMLCanvasElement['getContext'];
  return {
    restore: () => {
      HTMLCanvasElement.prototype.getContext = original;
    },
  };
}

function mountWarning(props: Record<string, unknown> = {}): {
  wrapper: VueWrapper;
  ctx: CtxStub;
  observe: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
  restore: () => void;
} {
  const ctx = makeCtxStub();
  const { restore } = stubCanvasContext(ctx);
  const observe = vi.fn();
  const disconnect = vi.fn();
  // jsdom does not ship ResizeObserver; install a minimal stub.
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

  const wrapper = mount(StreamContextWarning, {
    props: {
      currentPct: 30,
      used: 600_000,
      window: 2_000_000,
      thresholdPct: 80,
      history: [],
      ...props,
    },
    attachTo: document.body,
  });
  return { wrapper, ctx, observe, disconnect, restore };
}

describe('StreamContextWarning.vue', () => {
  let pendingRestore: Array<() => void> = [];
  afterEach(() => {
    pendingRestore.forEach((fn) => fn());
    pendingRestore = [];
    document.body.innerHTML = '';
  });
  function trackRestore(r: () => void): void {
    pendingRestore.push(r);
  }

  it('renders the current percentage, usage text, and safe label by default', async () => {
    const { wrapper, restore } = mountWarning({ currentPct: 30, used: 600_000, window: 2_000_000 });
    trackRestore(restore);
    await nextTick();
    expect(wrapper.find('[data-testid="bar-text"]').text()).toContain('30.0%');
    expect(wrapper.find('[data-testid="bar-text"]').text()).toContain('600,000 / 2,000,000');
    expect(wrapper.text()).toContain('上下文使用率正常');
    expect(wrapper.classes()).toContain('is-safe');
  });

  it('escalates to warn at safeMax and danger at threshold', async () => {
    const { wrapper, restore } = mountWarning({ currentPct: 60, thresholdPct: 80 });
    trackRestore(restore);
    await nextTick();
    expect(wrapper.classes()).toContain('is-warn');
    expect(wrapper.text()).toContain('上下文使用率偏高');

    await wrapper.setProps({ currentPct: 92 });
    await nextTick();
    expect(wrapper.classes()).toContain('is-danger');
    expect(wrapper.text()).toContain('上下文窗口即将耗尽');
  });

  it('uses the custom thresholdPct prop to drive level transitions', async () => {
    const { wrapper, restore } = mountWarning({ currentPct: 55, thresholdPct: 50 });
    trackRestore(restore);
    await nextTick();
    expect(wrapper.classes()).toContain('is-danger');
  });

  it('emits a level event on mount and whenever currentPct changes', async () => {
    const { wrapper, restore } = mountWarning({ currentPct: 30 });
    trackRestore(restore);
    const levels: ContextLevel[] = wrapper.emitted('level') as ContextLevel[][];
    expect(levels).toBeTruthy();
    expect(levels[0]?.[0]).toBe('safe');

    await wrapper.setProps({ currentPct: 70 });
    await nextTick();
    const next = wrapper.emitted('level') as ContextLevel[][];
    expect(next[next.length - 1]?.[0]).toBe('warn');

    await wrapper.setProps({ currentPct: 95 });
    await nextTick();
    const last = wrapper.emitted('level') as ContextLevel[][];
    expect(last[last.length - 1]?.[0]).toBe('danger');
  });

  it('draws the trend chart on the canvas context (axes, line, marker)', async () => {
    const samples: ContextSample[] = [
      { t: 1, pct: 10 },
      { t: 2, pct: 30 },
      { t: 3, pct: 55 },
      { t: 4, pct: 88 },
    ];
    const { ctx, restore } = mountWarning({
      currentPct: 88,
      history: samples,
      thresholdPct: 80,
    });
    trackRestore(restore);
    expect(ctx.beginPath).toHaveBeenCalled();
    expect(ctx.moveTo).toHaveBeenCalled();
    expect(ctx.lineTo).toHaveBeenCalled();
    expect(ctx.stroke).toHaveBeenCalled();
    expect(ctx.fill).toHaveBeenCalled();
    expect(ctx.arc).toHaveBeenCalled();
    expect(ctx.setLineDash).toHaveBeenCalled();
  });

  it('redraws the chart when history grows', async () => {
    const { wrapper, ctx, restore } = mountWarning({
      currentPct: 30,
      history: [{ t: 1, pct: 10 }],
    });
    trackRestore(restore);
    const baseline = ctx.beginPath.mock.calls.length;
    await wrapper.setProps({ history: [{ t: 1, pct: 10 }, { t: 2, pct: 35 }] });
    await nextTick();
    expect(ctx.beginPath.mock.calls.length).toBeGreaterThan(baseline);
  });

  it('redraws when the level changes (color swap path)', async () => {
    const { wrapper, ctx, restore } = mountWarning({ currentPct: 30 });
    trackRestore(restore);
    const before = ctx.beginPath.mock.calls.length;
    await wrapper.setProps({ currentPct: 95 });
    await nextTick();
    expect(ctx.beginPath.mock.calls.length).toBeGreaterThan(before);
  });

  it('attaches a ResizeObserver on mount and disconnects on unmount', () => {
    const { wrapper, observe, disconnect, restore } = mountWarning();
    trackRestore(restore);
    expect(observe).toHaveBeenCalled();
    wrapper.unmount();
    expect(disconnect).toHaveBeenCalled();
  });

  it('renders an aria-label with the current percentage', async () => {
    const { wrapper, restore } = mountWarning({ currentPct: 42.5 });
    trackRestore(restore);
    await nextTick();
    const root = wrapper.find('section.context-warning');
    expect(root.attributes('aria-label')).toBe('上下文使用率 42.5%');
  });

  it('uses an empty history by drawing a single point at the current percentage', () => {
    const { ctx, restore } = mountWarning({ currentPct: 65, history: [] });
    trackRestore(restore);
    expect(ctx.arc).toHaveBeenCalled();
  });
});

