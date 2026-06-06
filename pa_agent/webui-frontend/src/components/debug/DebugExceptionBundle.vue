<script setup lang="ts">
/**
 * DebugExceptionBundle.vue
 *
 * Surfaces the debug/exception bundle captured by `stores/debug.ts`.  The
 * component is a faithful port of the PyQt `pa_agent.gui.debug_widget.DebugWidget`
 * to the Vue 3 SPA:
 *
 *   - On the left, a scrollable list of all turns captured in the current
 *     session (Stage1, Stage2, Followup-N, Incremental).  Each row exposes
 *     its label, stage pill, timestamp and exception badge.
 *   - On the right, four read-only text blocks (system prompt, user prompt,
 *     raw response, validation info) plus a top-level bundle summary.
 *   - A Canvas 2D decision-flow visualisation that maps each turn's
 *     `trace` nodes onto a horizontal timeline — using the same colour
 *     tokens as `KLineChartCanvas` so the two visuals feel like one.
 *   - A copy-to-clipboard + JSON export toolbar so users can grab the
 *     full bundle without copy-pasting from each block.
 *
 * The component never owns transport state: it reads from the store and
 * delegates refresh/clear back to the same store.  This keeps the
 * ValidationDebugDialog dialog in charge of when to fetch.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  debugStore,
  exceptionCounts,
  hasFailures,
  selectedTurn,
  type DebugTurn,
  type ExceptionClass,
} from '@/stores/debug';
import { settingsStore } from '@/stores/settings';

const STAGE_LABELS: Record<DebugTurn['stage'], string> = {
  '1': '阶段一',
  '2': '阶段二',
  followup: '追问',
  incremental: '增量',
};

const EXCEPTION_LABELS: Record<ExceptionClass, string> = {
  none: '正常',
  network: '网络异常',
  auth: '鉴权失败',
  rate_limit: '限流',
  schema: '结构错误',
  timeout: '超时',
  validation: '校验失败',
  unknown: '未知异常',
};

const EXCEPTION_TOKENS: Record<ExceptionClass, string> = {
  none: '--success',
  network: '--warning',
  auth: '--danger',
  rate_limit: '--warning',
  schema: '--danger',
  timeout: '--warning',
  validation: '--danger',
  unknown: '--danger',
};

// ---- Local state ---------------------------------------------------------

const turnList = ref<HTMLElement | null>(null);
const flowCanvas = ref<HTMLCanvasElement | null>(null);
const flowHoverIndex = ref<number>(-1);
const copyState = ref<'idle' | 'copied' | 'failed'>('idle');
let copyResetTimer: number | null = null;

// ---- Computed views ------------------------------------------------------

const turns = computed<DebugTurn[]>(() => debugStore.turns);
const totalCount = computed<number>(() => turns.value.length);
const failureCount = computed<number>(() => {
  return turns.value.filter((t) => t.exception.klass !== 'none').length;
});

const summary = computed<string>(() => {
  if (totalCount.value === 0) return '暂无调试轮次';
  if (failureCount.value === 0) return `${totalCount.value} 轮次 · 全部正常`;
  return `${totalCount.value} 轮次 · ${failureCount.value} 次异常`;
});

const exceptionSummary = computed<string>(() => {
  const counts = exceptionCounts.value;
  const parts: string[] = [];
  for (const [klass, label] of Object.entries(EXCEPTION_LABELS) as Array<[ExceptionClass, string]>) {
    const c = counts[klass] ?? 0;
    if (c > 0) parts.push(`${label} ${c}`);
  }
  return parts.join(' · ');
});

const turnBadgeClass = (turn: DebugTurn): string => {
  return `badge badge--${turn.exception.klass}`;
};

const turnBadgeLabel = (turn: DebugTurn): string => {
  if (turn.exception.klass === 'none') return 'OK';
  return turn.exception.tag ?? EXCEPTION_LABELS[turn.exception.klass];
};

const formatTime = (ts: number): string => {
  if (!ts) return '--:--:--';
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

const formattedRaw = computed<string>(() => {
  const turn = selectedTurn.value;
  if (!turn) return '';
  try {
    return JSON.stringify(turn.raw_response ?? {}, null, 2);
  } catch {
    return String(turn.raw_response ?? '');
  }
});

const formattedTrace = computed<string>(() => {
  const turn = selectedTurn.value;
  if (!turn || !turn.trace || turn.trace.length === 0) return '无决策流节点';
  return turn.trace
    .map((node, idx) => `#${idx + 1} [${node.phase ?? 'decision'}] ${node.title}${node.outcome ? ` → ${node.outcome}` : ''}`)
    .join('\n');
});

const traceNodes = computed(() => selectedTurn.value?.trace ?? []);

// ---- Lifecycle -----------------------------------------------------------

onMounted(() => {
  // Best-effort initial fetch.  ValidationDebugDialog may have already
  // populated the store; the refresh() is a no-op for the empty state when
  // the API returns 404 (we just leave the list empty).
  if (debugStore.turns.length === 0 && !debugStore.loading) {
    void debugStore.refresh().catch(() => {
      // swallow — error already captured into debugStore.error
    });
  }
  scheduleDraw();
});

onBeforeUnmount(() => {
  if (copyResetTimer !== null) {
    window.clearTimeout(copyResetTimer);
    copyResetTimer = null;
  }
});

watch(
  [traceNodes, flowCanvas, () => selectedTurn.value?.id],
  () => {
    scheduleDraw();
  },
  { deep: true, flush: 'post' },
);

watch(
  () => debugStore.selectedId,
  () => {
    flowHoverIndex.value = -1;
    void scrollSelectedIntoView();
  },
);

async function scrollSelectedIntoView(): Promise<void> {
  await nextTick();
  const root = turnList.value;
  if (!root) return;
  const el = root.querySelector<HTMLElement>(`[data-turn-id="${debugStore.selectedId}"]`);
  if (el && typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ block: 'nearest' });
  }
}

// ---- Actions -------------------------------------------------------------

async function copyBundle(): Promise<void> {
  const turn = selectedTurn.value;
  if (!turn) return;
  const payload = {
    label: turn.label,
    kind: turn.kind,
    stage: turn.stage,
    ts: turn.ts,
    run_id: turn.run_id ?? null,
    system_prompt: turn.system_prompt,
    user_prompt: turn.user_prompt,
    raw_response: turn.raw_response,
    validation_info: turn.validation_info,
    exception: turn.exception,
    trace: turn.trace ?? [],
  };
  const text = JSON.stringify(payload, null, 2);
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    copyState.value = 'copied';
  } catch {
    copyState.value = 'failed';
  } finally {
    if (copyResetTimer !== null) window.clearTimeout(copyResetTimer);
    copyResetTimer = window.setTimeout(() => {
      copyState.value = 'idle';
    }, 1800);
  }
}

function downloadBundle(): void {
  const turn = selectedTurn.value;
  if (!turn) return;
  const blob = new Blob(
    [JSON.stringify(turn, null, 2)],
    { type: 'application/json' },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${turn.label.replace(/[^a-z0-9_-]+/gi, '-')}-${turn.ts}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function refreshBundle(): void {
  void debugStore.refresh().catch(() => {
    /* error already stored on debugStore */
  });
}

function clearBundle(): void {
  debugStore.reset();
}

function selectTurn(turn: DebugTurn): void {
  debugStore.select(turn.id);
}

// ---- Decision-flow canvas (matches KLineChartCanvas styling) -------------

interface NodeRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

let nodeRects: NodeRect[] = [];
let drawRaf: number | null = null;

function scheduleDraw(): void {
  if (typeof window === 'undefined') return;
  if (drawRaf !== null) return;
  drawRaf = window.requestAnimationFrame(() => {
    drawRaf = null;
    drawFlow();
  });
}

function readToken(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function drawFlow(): void {
  const canvas = flowCanvas.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 320;
  const cssHeight = canvas.clientHeight || 160;
  if (canvas.width !== cssWidth * dpr || canvas.height !== cssHeight * dpr) {
    canvas.width = Math.max(1, Math.floor(cssWidth * dpr));
    canvas.height = Math.max(1, Math.floor(cssHeight * dpr));
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  ctx.fillStyle = readToken('--surface-1', '#0f1419');
  ctx.fillRect(0, 0, cssWidth, cssHeight);

  const nodes = traceNodes.value;
  if (nodes.length === 0) {
    drawEmptyState(ctx, cssWidth, cssHeight);
    return;
  }

  nodeRects = layoutNodes(nodes, cssWidth, cssHeight);
  drawConnectors(ctx, nodeRects);
  nodes.forEach((node, i) => {
    if (!nodeRects[i]) return;
    drawNode(ctx, nodeRects[i], node, i === flowHoverIndex.value);
  });
}

function drawEmptyState(ctx: CanvasRenderingContext2D, w: number, h: number): void {
  ctx.fillStyle = readToken('--fg-3', '#8b95a1');
  ctx.font = '11px var(--font-mono), monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('决策流 · 等待节点', w / 2, h / 2);
  nodeRects = [];
}

function layoutNodes(
  nodes: Array<{ id: string; title: string; phase?: string }>,
  w: number,
  h: number,
): NodeRect[] {
  const pad = 12;
  const nodeH = 30;
  const gap = 6;
  const maxNodes = Math.max(1, Math.floor((h - pad * 2) / (nodeH + gap)));
  const visible = nodes.slice(-maxNodes);
  const out: NodeRect[] = [];
  const x = pad;
  let y = pad;
  for (const _n of visible) {
    void _n;
    out.push({ x, y, w: w - pad * 2, h: nodeH });
    y += nodeH + gap;
  }
  return out;
}

function drawConnectors(ctx: CanvasRenderingContext2D, rects: NodeRect[]): void {
  if (rects.length < 2) return;
  ctx.strokeStyle = readToken('--chart-line', '#c9a227');
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  for (let i = 1; i < rects.length; i += 1) {
    const a = rects[i - 1];
    const b = rects[i];
    const x0 = a.x + a.w / 2;
    const y0 = a.y + a.h;
    const x1 = b.x + b.w / 2;
    const y1 = b.y;
    ctx.moveTo(x0, y0);
    ctx.lineTo(x0, y0 + 3);
    ctx.lineTo(x1, y1 - 3);
    ctx.lineTo(x1, y1);
  }
  ctx.stroke();
}

function drawNode(
  ctx: CanvasRenderingContext2D,
  rect: NodeRect,
  node: { title: string; outcome?: string; phase?: string },
  hovered: boolean,
): void {
  const phase = node.phase ?? 'decision';
  const border =
    phase === 'gate'
      ? readToken('--chart-line-2', '#79c0ff')
      : readToken('--chart-line', '#c9a227');
  const fill = hovered
    ? readToken('--surface-3', '#1b2230')
    : readToken('--surface-2', '#161b22');

  ctx.fillStyle = fill;
  ctx.strokeStyle = border;
  ctx.lineWidth = 1.2;
  roundRect(ctx, rect.x, rect.y, rect.w, rect.h, 6);
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = border;
  ctx.font = '10px var(--font-mono), monospace';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(truncate(node.title, 48), rect.x + 10, rect.y + rect.h / 2);

  if (node.outcome) {
    ctx.fillStyle = readToken('--fg-3', '#8b95a1');
    ctx.textAlign = 'right';
    ctx.fillText(truncate(node.outcome, 24), rect.x + rect.w - 10, rect.y + rect.h / 2);
  }
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return `${s.slice(0, Math.max(0, max - 1))}…`;
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  ctx.lineTo(x + rr, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
  ctx.lineTo(x, y + rr);
  ctx.quadraticCurveTo(x, y, x + rr, y);
  ctx.closePath();
}

function onCanvasMouseMove(ev: MouseEvent): void {
  const c = flowCanvas.value;
  if (!c) return;
  const rect = c.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const y = ev.clientY - rect.top;
  const idx = hitTestNode(x, y);
  if (idx !== flowHoverIndex.value) {
    flowHoverIndex.value = idx;
    scheduleDraw();
  }
  c.style.cursor = idx >= 0 ? 'pointer' : 'default';
}

function onCanvasMouseLeave(): void {
  flowHoverIndex.value = -1;
  scheduleDraw();
}

function hitTestNode(x: number, y: number): number {
  for (let i = 0; i < nodeRects.length; i += 1) {
    const r = nodeRects[i];
    if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return i;
  }
  return -1;
}

// ---- Helpers -------------------------------------------------------------

const exceptionTokenFor = (klass: ExceptionClass): string => EXCEPTION_TOKENS[klass];
const stageLabel = (s: DebugTurn['stage']): string => STAGE_LABELS[s] ?? s;
const apiKey = computed<string>(() => settingsStore.state.provider_api_key || '');
</script>

<template>
  <div
    class="debug-bundle"
    :class="{ 'has-failures': hasFailures }"
    data-testid="debug-bundle"
  >
    <header class="debug-bundle-header">
      <div class="debug-bundle-title">
        <span class="debug-bundle-icon" aria-hidden="true">🪪</span>
        <div class="debug-bundle-meta">
          <h3>调试异常包</h3>
          <p class="debug-bundle-summary" data-testid="bundle-summary">{{ summary }}</p>
        </div>
      </div>
      <div class="debug-bundle-actions">
        <button
          type="button"
          class="btn"
          data-testid="refresh-btn"
          :disabled="debugStore.loading"
          @click="refreshBundle"
        >
          {{ debugStore.loading ? '刷新中…' : '刷新' }}
        </button>
        <button
          type="button"
          class="btn"
          data-testid="copy-btn"
          :disabled="!selectedTurn"
          @click="copyBundle"
        >
          {{
            copyState === 'copied'
              ? '已复制'
              : copyState === 'failed'
                ? '复制失败'
                : '复制'
          }}
        </button>
        <button
          type="button"
          class="btn"
          data-testid="export-btn"
          :disabled="!selectedTurn"
          @click="downloadBundle"
        >
          导出
        </button>
        <button
          type="button"
          class="btn btn--ghost"
          data-testid="clear-btn"
          :disabled="turns.length === 0"
          @click="clearBundle"
        >
          清空
        </button>
      </div>
    </header>

    <p
      v-if="exceptionSummary"
      class="debug-bundle-exceptions"
      data-testid="exception-summary"
    >
      异常统计 · {{ exceptionSummary }}
    </p>
    <p
      v-if="debugStore.error"
      class="debug-bundle-error"
      role="alert"
      data-testid="bundle-error"
    >
      {{ debugStore.error }}
    </p>

    <div class="debug-bundle-body">
      <aside class="debug-bundle-list" ref="turnList" data-testid="turn-list">
        <div v-if="turns.length === 0" class="debug-bundle-empty" data-testid="empty-state">
          <p>暂无轮次。运行分析后此处会显示 Stage1 / Stage2 / 追问的完整调试数据。</p>
        </div>
        <button
          v-for="turn in turns"
          :key="turn.id"
          type="button"
          :class="['turn-item', { active: turn.id === debugStore.selectedId }]"
          :data-turn-id="turn.id"
          :data-testid="`turn-${turn.id}`"
          @click="selectTurn(turn)"
        >
          <div class="turn-item-row">
            <span :class="['stage-pill', `stage-pill--${turn.stage}`]">
              {{ stageLabel(turn.stage) }}
            </span>
            <span class="turn-item-label">{{ turn.label }}</span>
            <span :class="turnBadgeClass(turn)">{{ turnBadgeLabel(turn) }}</span>
          </div>
          <div class="turn-item-sub">
            <span>{{ formatTime(turn.ts) }}</span>
            <span v-if="turn.run_id" class="turn-item-run">run {{ turn.run_id.slice(0, 8) }}</span>
          </div>
          <p v-if="turn.exception.message" class="turn-item-msg">
            {{ truncate(turn.exception.message, 96) }}
          </p>
        </button>
      </aside>

      <section class="debug-bundle-detail" data-testid="bundle-detail">
        <div
          v-if="!selectedTurn"
          class="debug-bundle-placeholder"
          data-testid="detail-placeholder"
        >
          请选择左侧轮次查看详情。
        </div>
        <template v-else>
          <div class="detail-block">
            <div class="detail-block-head">
              <span class="detail-block-title">System Prompt</span>
              <span class="detail-block-tag">只读</span>
            </div>
            <pre class="detail-block-pre" data-testid="detail-system">{{ selectedTurn.system_prompt || '（空）' }}</pre>
          </div>
          <div class="detail-block">
            <div class="detail-block-head">
              <span class="detail-block-title">User Prompt</span>
              <span class="detail-block-tag">只读</span>
            </div>
            <pre class="detail-block-pre" data-testid="detail-user">{{ selectedTurn.user_prompt || '（空）' }}</pre>
          </div>
          <div class="detail-block">
            <div class="detail-block-head">
              <span class="detail-block-title">Raw Response</span>
              <span class="detail-block-tag">JSON</span>
            </div>
            <pre class="detail-block-pre" data-testid="detail-raw">{{ formattedRaw }}</pre>
          </div>
          <div class="detail-block">
            <div class="detail-block-head">
              <span class="detail-block-title">Validation / 异常分类</span>
              <span
                class="detail-block-tag"
                data-testid="detail-validation-tag"
                :style="{ color: `var(${exceptionTokenFor(selectedTurn.exception.klass)})` }"
              >
                {{ EXCEPTION_LABELS[selectedTurn.exception.klass] }}
              </span>
            </div>
            <pre class="detail-block-pre" data-testid="detail-validation">{{ selectedTurn.validation_info || '（无）' }}</pre>
            <p
              v-if="selectedTurn.exception.details"
              class="detail-block-extra"
              data-testid="detail-exception-details"
            >
              {{ selectedTurn.exception.details }}
            </p>
          </div>

          <div class="detail-flow">
            <div class="detail-flow-head">
              <span class="detail-block-title">决策流可视化</span>
              <span class="detail-block-tag" data-testid="trace-node-count">{{ traceNodes.length }} 节点</span>
            </div>
            <canvas
              ref="flowCanvas"
              class="detail-flow-canvas"
              data-testid="flow-canvas"
              @mousemove="onCanvasMouseMove"
              @mouseleave="onCanvasMouseLeave"
            />
            <pre class="detail-block-pre detail-block-pre--mono" data-testid="detail-trace">{{ formattedTrace }}</pre>
          </div>

          <p v-if="apiKey" class="debug-bundle-disclaimer">
            复制 / 导出时会保留 API Key；调试面板已在上方隐藏展示。
          </p>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.debug-bundle {
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  color: var(--fg);
  font-family: var(--font-body);
  min-height: 0;
}
.debug-bundle.has-failures {
  border-color: var(--danger);
}
.debug-bundle-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.debug-bundle-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.debug-bundle-icon {
  font-size: 20px;
}
.debug-bundle-meta h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--fg);
}
.debug-bundle-summary {
  margin: 0;
  font-size: 11px;
  color: var(--fg-2);
  font-family: var(--font-mono);
}
.debug-bundle-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.debug-bundle-actions .btn {
  font-family: var(--font-body);
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid var(--border-2);
  background: var(--surface-2);
  color: var(--fg);
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.debug-bundle-actions .btn:hover:not(:disabled) {
  background: var(--surface-3);
  border-color: var(--accent);
}
.debug-bundle-actions .btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.debug-bundle-actions .btn--ghost {
  background: transparent;
  color: var(--fg-2);
}
.debug-bundle-exceptions {
  margin: 0;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--warning);
}
.debug-bundle-error {
  margin: 0;
  padding: 6px 10px;
  font-size: 11px;
  border: 1px solid var(--danger);
  background: var(--surface-2);
  color: var(--danger);
  border-radius: 4px;
  font-family: var(--font-mono);
}
.debug-bundle-body {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(0, 2.2fr);
  gap: 12px;
  min-height: 360px;
}
.debug-bundle-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow: auto;
  max-height: 480px;
  padding-right: 4px;
}
.debug-bundle-empty {
  padding: 12px;
  border: 1px dashed var(--border-2);
  border-radius: 6px;
  color: var(--fg-3);
  font-size: 11px;
  font-family: var(--font-mono);
  text-align: center;
}
.turn-item {
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
  cursor: pointer;
  font-family: var(--font-body);
  color: var(--fg);
  transition: border-color 0.15s ease, background 0.15s ease;
}
.turn-item:hover {
  border-color: var(--accent-3);
}
.turn-item.active {
  border-color: var(--accent);
  background: var(--surface-3);
}
.turn-item-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.turn-item-label {
  flex: 1 1 auto;
  font-size: 12px;
  font-weight: 600;
}
.turn-item-sub {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--fg-3);
  font-family: var(--font-mono);
}
.turn-item-msg {
  margin: 0;
  font-size: 10.5px;
  color: var(--fg-2);
  font-family: var(--font-mono);
  word-break: break-word;
}
.stage-pill {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10px;
  font-family: var(--font-mono);
  background: var(--surface-3);
  color: var(--fg-2);
  border: 1px solid var(--border-2);
}
.stage-pill--1 { color: var(--info); border-color: var(--info); }
.stage-pill--2 { color: var(--success); border-color: var(--success); }
.stage-pill--followup { color: var(--accent-3); border-color: var(--accent-3); }
.stage-pill--incremental { color: var(--warning); border-color: var(--warning); }
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-family: var(--font-mono);
  background: var(--surface-3);
  color: var(--fg-2);
  border: 1px solid var(--border-2);
}
.badge--none { color: var(--success); border-color: var(--success); }
.badge--network, .badge--timeout, .badge--rate_limit { color: var(--warning); border-color: var(--warning); }
.badge--auth, .badge--schema, .badge--validation, .badge--unknown { color: var(--danger); border-color: var(--danger); }
.debug-bundle-detail {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.debug-bundle-placeholder {
  border: 1px dashed var(--border-2);
  border-radius: 6px;
  padding: 24px;
  text-align: center;
  font-size: 12px;
  color: var(--fg-3);
  font-family: var(--font-mono);
}
.detail-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.detail-block-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.detail-block-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--fg-2);
  font-family: var(--font-mono);
}
.detail-block-tag {
  font-size: 10px;
  color: var(--fg-3);
  font-family: var(--font-mono);
}
.detail-block-pre {
  margin: 0;
  padding: 8px 10px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.5;
  max-height: 180px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
.detail-block-pre--mono {
  max-height: 90px;
}
.detail-block-extra {
  margin: 0;
  padding: 4px 8px;
  font-size: 10.5px;
  font-family: var(--font-mono);
  color: var(--fg-3);
  background: var(--surface-2);
  border-left: 2px solid var(--warning);
  border-radius: 0 4px 4px 0;
}
.detail-flow {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.detail-flow-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.detail-flow-canvas {
  width: 100%;
  height: 160px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-1);
  display: block;
}
.debug-bundle-disclaimer {
  margin: 0;
  font-size: 10.5px;
  color: var(--fg-3);
  font-family: var(--font-mono);
}
@media (max-width: 720px) {
  .debug-bundle-body {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
