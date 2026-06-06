/**
 * DebugExceptionBundle — vitest unit tests.
 *
 * Coverage:
 *  - Renders an empty-state placeholder when the store has no turns and
 *    keeps the action buttons disabled where appropriate.
 *  - Renders one turn per row with stage pill, label, exception badge,
 *    and the truncated exception message.
 *  - Selecting a turn populates the four read-only blocks (system
 *    prompt, user prompt, raw response, validation info) and toggles the
 *    `active` class on the corresponding row.
 *  - The exception summary counts each ExceptionClass and the
 *    has-failures border lights up whenever any turn is non-`none`.
 *  - `复制` proxies to navigator.clipboard.writeText when available, and
 *    falls back to a textarea + execCommand when it is not.
 *  - `导出` triggers a Blob anchor click.
 *  - `刷新` delegates to debugStore.refresh().
 *  - `清空` calls debugStore.reset() and re-renders the empty state.
 *  - The decision-flow canvas issues the expected Canvas 2D calls when
 *    the selected turn has `trace` nodes (and also paints the empty
 *    state when there are none).
 *  - The first mount triggers a best-effort debugStore.refresh().
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, type VueWrapper } from '@vue/test-utils';
import { nextTick } from 'vue';
import DebugExceptionBundle from './DebugExceptionBundle.vue';
import { debugStore, type DebugTurn } from '@/stores/debug';
import { settingsStore } from '@/stores/settings';

type CtxStub = {
  setTransform: ReturnType<typeof vi.fn>;
  clearRect: ReturnType<typeof vi.fn>;
  beginPath: ReturnType<typeof vi.fn>;
  closePath: ReturnType<typeof vi.fn>;
  moveTo: ReturnType<typeof vi.fn>;
  lineTo: ReturnType<typeof vi.fn>;
  quadraticCurveTo: ReturnType<typeof vi.fn>;
  stroke: ReturnType<typeof vi.fn>;
  fill: ReturnType<typeof vi.fn>;
  arc: ReturnType<typeof vi.fn>;
  fillRect: ReturnType<typeof vi.fn>;
  fillText: ReturnType<typeof vi.fn>;
  strokeStyle: string | null;
  fillStyle: string | null;
  lineWidth: number;
  font: string;
  textAlign: string;
  textBaseline: string;
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
    quadraticCurveTo: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    arc: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    strokeStyle: null,
    fillStyle: null,
    lineWidth: 0,
    font: '',
    textAlign: '',
    textBaseline: '',
    setLineDash: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    measureText: vi.fn(() => ({ width: 0 } as TextMetrics)),
  };
}

function stubCanvasContext(ctx: CtxStub): { restore: () => void } {
  const original = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = vi.fn(
    () => ctx,
  ) as unknown as HTMLCanvasElement['getContext'];
  return {
    restore: () => {
      HTMLCanvasElement.prototype.getContext = original;
    },
  };
}

function stubRaf(): { restore: () => void } {
  const originalRaf = window.requestAnimationFrame;
  const originalCancel = window.cancelAnimationFrame;
  window.requestAnimationFrame = ((cb: FrameRequestCallback): number => {
    cb(0);
    return 1;
  }) as typeof window.requestAnimationFrame;
  window.cancelAnimationFrame = vi.fn() as unknown as typeof window.cancelAnimationFrame;
  return {
    restore: () => {
      window.requestAnimationFrame = originalRaf;
      window.cancelAnimationFrame = originalCancel;
    },
  };
}

function makeTurn(overrides: Partial<DebugTurn> = {}): DebugTurn {
  return {
    id: overrides.id ?? `turn-${Math.random().toString(36).slice(2, 8)}`,
    label: overrides.label ?? 'Stage1',
    kind: overrides.kind ?? 'stage1',
    ts: overrides.ts ?? Date.UTC(2026, 5, 5, 10, 30, 15),
    stage: overrides.stage ?? '1',
    system_prompt: overrides.system_prompt ?? 'sys',
    user_prompt: overrides.user_prompt ?? 'usr',
    raw_response: overrides.raw_response ?? { status: 200 },
    validation_info: overrides.validation_info ?? '',
    exception: overrides.exception ?? { klass: 'none', message: '' },
    trace: overrides.trace ?? [],
    run_id: overrides.run_id,
  };
}

function resetStores(): void {
  debugStore.reset();
  debugStore.loading = false;
  debugStore.error = null;
  debugStore.lastFetchedAt = 0;
  settingsStore.state.provider_api_key = '';
}

describe('DebugExceptionBundle.vue', () => {
  let pendingRestore: Array<() => void> = [];

  beforeEach(() => {
    resetStores();
  });

  afterEach(() => {
    pendingRestore.forEach((fn) => fn());
    pendingRestore = [];
    document.body.innerHTML = '';
    resetStores();
  });

  function trackRestore(r: () => void): void {
    pendingRestore.push(r);
  }

  function mountBundle(): { wrapper: VueWrapper; ctx: CtxStub } {
    const ctx = makeCtxStub();
    const { restore: restoreCanvas } = stubCanvasContext(ctx);
    const { restore: restoreRaf } = stubRaf();
    trackRestore(restoreCanvas);
    trackRestore(restoreRaf);
    // Stub refresh to avoid hitting the backend during mount.
    const originalRefresh = debugStore.refresh.bind(debugStore);
    debugStore.refresh = (async () => {
      /* no-op for tests */
    }) as typeof debugStore.refresh;
    trackRestore(() => {
      debugStore.refresh = originalRefresh;
    });
    const wrapper = mount(DebugExceptionBundle, { attachTo: document.body });
    return { wrapper, ctx };
  }

  it('renders the empty state when the store has no turns', () => {
    const { wrapper } = mountBundle();
    expect(wrapper.find('[data-testid="debug-bundle"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="bundle-summary"]').text()).toBe('暂无调试轮次');
    expect(wrapper.find('[data-testid="detail-placeholder"]').exists()).toBe(true);
    expect(
      (wrapper.find('[data-testid="copy-btn"]').element as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (wrapper.find('[data-testid="export-btn"]').element as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (wrapper.find('[data-testid="clear-btn"]').element as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it('best-effort refreshes the store on first mount', () => {
    const spy = vi.fn(async () => {
      /* no-op */
    });
    const originalRefresh = debugStore.refresh.bind(debugStore);
    debugStore.refresh = spy as typeof debugStore.refresh;
    trackRestore(() => {
      debugStore.refresh = originalRefresh;
    });
    const ctx = makeCtxStub();
    const { restore: restoreCanvas } = stubCanvasContext(ctx);
    const { restore: restoreRaf } = stubRaf();
    trackRestore(restoreCanvas);
    trackRestore(restoreRaf);
    mount(DebugExceptionBundle, { attachTo: document.body });
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('renders one turn per row with stage pill, label, and badge', async () => {
    debugStore.pushTurn(
      makeTurn({ id: 's1', label: 'Stage1', stage: '1' }),
    );
    debugStore.pushTurn(
      makeTurn({
        id: 'fu1',
        label: 'Followup-1',
        stage: 'followup',
        kind: 'followup',
        exception: { klass: 'timeout', message: '上游模型超时' },
      }),
    );
    const { wrapper } = mountBundle();
    await nextTick();
    const rows = wrapper.findAll('[data-turn-id]');
    expect(rows).toHaveLength(2);
    expect(rows[0].text()).toContain('阶段一');
    expect(rows[0].text()).toContain('Stage1');
    expect(rows[0].text()).toContain('OK');
    expect(rows[1].text()).toContain('追问');
    expect(rows[1].text()).toContain('Followup-1');
    expect(rows[1].text()).toContain('超时');
    expect(rows[1].text()).toContain('上游模型超时');
  });

  it('selecting a turn populates the four read-only detail blocks', async () => {
    const turn = makeTurn({
      id: 'sel',
      label: 'Stage2',
      kind: 'stage2',
      stage: '2',
      system_prompt: 'SYSTEM PROMPT BODY',
      user_prompt: 'USER PROMPT BODY',
      raw_response: { status: 200, body: { ok: true } },
      validation_info: 'schema mismatch on field action',
      exception: { klass: 'validation', message: 'validation failed' },
    });
    debugStore.pushTurn(turn);
    debugStore.select('sel');
    const { wrapper } = mountBundle();
    await nextTick();
    expect(wrapper.find('[data-testid="detail-system"]').text()).toBe('SYSTEM PROMPT BODY');
    expect(wrapper.find('[data-testid="detail-user"]').text()).toBe('USER PROMPT BODY');
    const raw = wrapper.find('[data-testid="detail-raw"]').text();
    expect(raw).toContain('"status": 200');
    expect(raw).toContain('"ok": true');
    expect(wrapper.find('[data-testid="detail-validation"]').text()).toBe('schema mismatch on field action');
    expect(wrapper.find('[data-testid="detail-validation-tag"]').text()).toBe('校验失败');
    expect(wrapper.find('[data-turn-id="sel"]').classes()).toContain('active');
  });

  it('clicking a row updates the selection and highlights the row', async () => {
    debugStore.pushTurn(makeTurn({ id: 'a', label: 'Stage1', stage: '1' }));
    debugStore.pushTurn(
      makeTurn({
        id: 'b',
        label: 'Stage2',
        kind: 'stage2',
        stage: '2',
        system_prompt: 'B-SYS',
      }),
    );
    const { wrapper } = mountBundle();
    await nextTick();
    await wrapper.find('[data-turn-id="b"]').trigger('click');
    await nextTick();
    expect(debugStore.selectedId).toBe('b');
    expect(wrapper.find('[data-testid="detail-system"]').text()).toBe('B-SYS');
    expect(wrapper.find('[data-turn-id="b"]').classes()).toContain('active');
    expect(wrapper.find('[data-turn-id="a"]').classes()).not.toContain('active');
  });

  it('exception summary aggregates each ExceptionClass and lights the failure border', async () => {
    debugStore.pushTurn(makeTurn({ id: 'ok', exception: { klass: 'none', message: '' } }));
    debugStore.pushTurn(
      makeTurn({
        id: 'net',
        exception: { klass: 'network', message: 'connection reset' },
      }),
    );
    debugStore.pushTurn(
      makeTurn({
        id: 'val',
        exception: { klass: 'validation', message: 'bad payload' },
      }),
    );
    const { wrapper } = mountBundle();
    await nextTick();
    const summary = wrapper.find('[data-testid="exception-summary"]').text();
    expect(summary).toContain('正常 1');
    expect(summary).toContain('网络异常 1');
    expect(summary).toContain('校验失败 1');
    expect(wrapper.find('[data-testid="debug-bundle"]').classes()).toContain('has-failures');
    expect(wrapper.find('[data-testid="bundle-summary"]').text()).toBe('3 轮次 · 2 次异常');
  });

  it('shows "全部正常" summary when every turn is healthy and omits the failures border', async () => {
    debugStore.pushTurn(makeTurn({ id: 'a' }));
    debugStore.pushTurn(makeTurn({ id: 'b', label: 'Stage2', stage: '2', kind: 'stage2' }));
    const { wrapper } = mountBundle();
    await nextTick();
    expect(wrapper.find('[data-testid="bundle-summary"]').text()).toBe('2 轮次 · 全部正常');
    expect(wrapper.find('[data-testid="debug-bundle"]').classes()).not.toContain('has-failures');
    expect(wrapper.find('[data-testid="exception-summary"]').exists()).toBe(true);
  });

  it('copy uses navigator.clipboard.writeText when available', async () => {
    debugStore.pushTurn(makeTurn({ id: 'copy', label: 'Stage1' }));
    debugStore.select('copy');
    const write = vi.fn(async () => {
      /* no-op */
    });
    const originalClipboard = (navigator as { clipboard?: Clipboard }).clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: write },
      configurable: true,
    });
    trackRestore(() => {
      if (originalClipboard) {
        Object.defineProperty(navigator, 'clipboard', {
          value: originalClipboard,
          configurable: true,
        });
      } else {
        Object.defineProperty(navigator, 'clipboard', {
          value: undefined,
          configurable: true,
        });
      }
    });
    const { wrapper } = mountBundle();
    await nextTick();
    await wrapper.find('[data-testid="copy-btn"]').trigger('click');
    await nextTick();
    expect(write).toHaveBeenCalledTimes(1);
    const payload = JSON.parse(write.mock.calls[0][0] as string);
    expect(payload.label).toBe('Stage1');
    expect(payload.stage).toBe('1');
    expect(payload.raw_response).toEqual({ status: 200 });
    expect(wrapper.find('[data-testid="copy-btn"]').text()).toBe('已复制');
  });

  it('copy falls back to textarea + execCommand when clipboard is missing', async () => {
    debugStore.pushTurn(makeTurn({ id: 'fb', label: 'Stage1' }));
    debugStore.select('fb');
    const originalClipboard = (navigator as { clipboard?: Clipboard }).clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
    });
    trackRestore(() => {
      Object.defineProperty(navigator, 'clipboard', {
        value: originalClipboard,
        configurable: true,
      });
    });
    const execSpy = vi.fn(() => true);
    const originalExec = document.execCommand;
    document.execCommand = execSpy as unknown as typeof document.execCommand;
    trackRestore(() => {
      document.execCommand = originalExec;
    });
    const { wrapper } = mountBundle();
    await nextTick();
    await wrapper.find('[data-testid="copy-btn"]').trigger('click');
    await nextTick();
    expect(execSpy).toHaveBeenCalledWith('copy');
    expect(wrapper.find('[data-testid="copy-btn"]').text()).toBe('已复制');
  });

  it('export creates a Blob URL and triggers a download anchor click', async () => {
    debugStore.pushTurn(makeTurn({ id: 'exp', label: 'Stage1', ts: 1717584000000 }));
    debugStore.select('exp');
    const createUrl = vi.fn(() => 'blob:test-url');
    const revokeUrl = vi.fn();
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    URL.createObjectURL = createUrl as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = revokeUrl as unknown as typeof URL.revokeObjectURL;
    trackRestore(() => {
      URL.createObjectURL = originalCreate;
      URL.revokeObjectURL = originalRevoke;
    });
    const clickSpy = vi.fn();
    const originalClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = clickSpy as unknown as typeof HTMLAnchorElement.prototype.click;
    trackRestore(() => {
      HTMLAnchorElement.prototype.click = originalClick;
    });
    const { wrapper } = mountBundle();
    await nextTick();
    await wrapper.find('[data-testid="export-btn"]').trigger('click');
    expect(createUrl).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeUrl).toHaveBeenCalledWith('blob:test-url');
  });

  it('refresh button calls debugStore.refresh()', async () => {
    const spy = vi.fn(async () => {
      /* no-op */
    });
    const originalRefresh = debugStore.refresh.bind(debugStore);
    debugStore.refresh = spy as typeof debugStore.refresh;
    trackRestore(() => {
      debugStore.refresh = originalRefresh;
    });
    const ctx = makeCtxStub();
    const { restore: restoreCanvas } = stubCanvasContext(ctx);
    const { restore: restoreRaf } = stubRaf();
    trackRestore(restoreCanvas);
    trackRestore(restoreRaf);
    const wrapper = mount(DebugExceptionBundle, { attachTo: document.body });
    // First call is the on-mount best-effort refresh.
    expect(spy).toHaveBeenCalledTimes(1);
    await wrapper.find('[data-testid="refresh-btn"]').trigger('click');
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it('clear button resets the store and re-renders the empty state', async () => {
    debugStore.pushTurn(makeTurn({ id: 'r' }));
    const { wrapper } = mountBundle();
    await nextTick();
    expect(wrapper.findAll('[data-turn-id]')).toHaveLength(1);
    await wrapper.find('[data-testid="clear-btn"]').trigger('click');
    await nextTick();
    expect(debugStore.turns).toHaveLength(0);
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true);
  });

  it('renders the bundle error banner when debugStore.error is set', async () => {
    debugStore.error = 'HTTP 503 from /api/debug/turns';
    const { wrapper } = mountBundle();
    await nextTick();
    expect(wrapper.find('[data-testid="bundle-error"]').text()).toBe('HTTP 503 from /api/debug/turns');
  });

  it('paints the canvas empty state when the selected turn has no trace nodes', async () => {
    debugStore.pushTurn(makeTurn({ id: 'noflow', trace: [] }));
    debugStore.select('noflow');
    const { wrapper, ctx } = mountBundle();
    await nextTick();
    expect(wrapper.find('[data-testid="trace-node-count"]').text()).toBe('0 节点');
    expect(ctx.fillText).toHaveBeenCalled();
    const calls = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(calls).toContain('决策流 · 等待节点');
    expect(wrapper.find('[data-testid="detail-trace"]').text()).toBe('无决策流节点');
  });

  it('draws connectors and node rectangles when the selected turn has trace nodes', async () => {
    debugStore.pushTurn(
      makeTurn({
        id: 'flow',
        trace: [
          { id: 'g1', title: 'Gate · 数据完整性', phase: 'gate', outcome: 'pass' },
          { id: 'd1', title: 'Decision · 趋势判断', phase: 'decision', outcome: 'long' },
          { id: 'd2', title: 'Decision · 风险评估', phase: 'decision' },
        ],
      }),
    );
    debugStore.select('flow');
    const { wrapper, ctx } = mountBundle();
    await nextTick();
    expect(wrapper.find('[data-testid="trace-node-count"]').text()).toBe('3 节点');
    expect(ctx.beginPath).toHaveBeenCalled();
    expect(ctx.moveTo).toHaveBeenCalled();
    expect(ctx.lineTo).toHaveBeenCalled();
    expect(ctx.stroke).toHaveBeenCalled();
    expect(ctx.fill).toHaveBeenCalled();
    // Node titles should be rendered via fillText.
    const titles = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(titles.some((t) => String(t).includes('Gate'))).toBe(true);
    expect(titles.some((t) => String(t).includes('Decision · 趋势判断'))).toBe(true);
    // Trace string is also surfaced in the textual fallback.
    const trace = wrapper.find('[data-testid="detail-trace"]').text();
    expect(trace).toContain('#1 [gate] Gate · 数据完整性 → pass');
    expect(trace).toContain('#2 [decision] Decision · 趋势判断 → long');
    expect(trace).toContain('#3 [decision] Decision · 风险评估');
  });

  it('renders the api-key disclaimer when settings store has a key set', async () => {
    settingsStore.state.provider_api_key = 'sk-test';
    debugStore.pushTurn(makeTurn({ id: 'k' }));
    const { wrapper } = mountBundle();
    await nextTick();
    expect(wrapper.text()).toContain('复制 / 导出时会保留 API Key');
  });
});
