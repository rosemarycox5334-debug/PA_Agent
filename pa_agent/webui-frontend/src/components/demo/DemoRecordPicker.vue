<script lang="ts">
/**
 * DemoRecordPicker — module-level exports.
 *
 * The component is split into a regular <script> (this block) and
 * <script setup>. The regular script carries the helper functions and
 * type/constant exports that the Vitest suite consumes; the setup block
 * owns the component's reactive state and template bindings.
 *
 * Vue 3 forbids `export` inside <script setup>, so value exports MUST
 * live here. Bindings declared here are visible to <script setup> as
 * module-level identifiers (no import needed) and are auto-exposed to
 * the template.
 */
import type { DataSnapshot } from '@/api/client';
import type { DecisionPayload } from '@/stores/decision';

export const DEMO_TIMEFRAMES = [
  'all',
  '1m',
  '5m',
  '15m',
  '30m',
  '1h',
  '4h',
  '1d',
] as const;
export type DemoTimeframe = (typeof DEMO_TIMEFRAMES)[number];

export type FlowPhase = 'gate' | 'decision' | 'terminal';
export type FlowBranch = 'up' | 'down' | 'neutral';

export interface VizNode {
  id: string;
  label: string;
  phase: FlowPhase;
  branch?: FlowBranch;
  x: number;
  y: number;
}

export interface VizEdge {
  from: string;
  to: string;
  branch?: FlowBranch;
}

export interface RecordDetail {
  filename: string;
  timestamp?: string;
  symbol?: string;
  timeframe?: string;
  bar_count?: number;
  notes?: string;
  bars?: DataSnapshot['bars'];
  indicators?: DataSnapshot['indicators'];
  kline_snapshot?: DataSnapshot;
  stage2_decision?: DecisionPayload;
}

function pickBranch(value: unknown): FlowBranch | undefined {
  if (value === 'up' || value === 'down' || value === 'neutral') {
    return value;
  }
  return undefined;
}

export function buildViz(
  detail: RecordDetail | null,
): { nodes: VizNode[]; edges: VizEdge[] } {
  const nodes: VizNode[] = [];
  const edges: VizEdge[] = [];
  if (!detail) return { nodes, edges };

  const decision = (detail.stage2_decision ?? {}) as DecisionPayload & {
    terminal?: Record<string, unknown>;
  };
  const gateList = (decision.gate_trace ?? []) as Array<Record<string, unknown>>;
  const decList = (decision.decision_trace ?? []) as Array<Record<string, unknown>>;

  // Gate phase nodes
  gateList.forEach((t, idx) => {
    const id = typeof t.id === 'string' ? t.id : `gate-${idx}`;
    const labelRaw = t.name ?? t.label ?? id;
    const label = typeof labelRaw === 'string' ? labelRaw : String(labelRaw);
    nodes.push({
      id,
      label,
      phase: 'gate',
      branch: pickBranch(t.branch),
      x: 0,
      y: 0,
    });
  });

  // Decision phase nodes
  decList.forEach((t, idx) => {
    const id = typeof t.id === 'string' ? t.id : `decision-${idx}`;
    const labelRaw = t.name ?? t.label ?? id;
    const label = typeof labelRaw === 'string' ? labelRaw : String(labelRaw);
    nodes.push({
      id,
      label,
      phase: 'decision',
      branch: pickBranch(t.branch),
      x: 0,
      y: 0,
    });
  });

  // Edges: explicit `next` (string | string[] | undefined) + implicit chain
  const allTraces = [...gateList, ...decList];
  allTraces.forEach((t, idx) => {
    const fromId =
      typeof t.id === 'string'
        ? t.id
        : idx < gateList.length
          ? `gate-${idx}`
          : `decision-${idx - gateList.length}`;
    const next = t.next;
    if (Array.isArray(next)) {
      for (const n of next as unknown[]) {
        if (typeof n === 'string') {
          edges.push({ from: fromId, to: n, branch: pickBranch(t.branch) });
        }
      }
    } else if (typeof next === 'string') {
      edges.push({ from: fromId, to: next, branch: pickBranch(t.branch) });
    } else if (idx < allTraces.length - 1) {
      const nextIdx = idx + 1;
      const toId =
        nextIdx < gateList.length
          ? typeof gateList[nextIdx].id === 'string'
            ? (gateList[nextIdx].id as string)
            : `gate-${nextIdx}`
          : typeof decList[nextIdx - gateList.length].id === 'string'
            ? (decList[nextIdx - gateList.length].id as string)
            : `decision-${nextIdx - gateList.length}`;
      edges.push({ from: fromId, to: toId, branch: pickBranch(t.branch) });
    }
  });

  // Terminal node
  const terminal = (decision as Record<string, unknown>).terminal;
  if (terminal && typeof terminal === 'object') {
    const t = terminal as Record<string, unknown>;
    const id = typeof t.id === 'string' ? t.id : 'terminal';
    const labelRaw = t.label ?? t.action ?? '终局';
    const label = typeof labelRaw === 'string' ? labelRaw : String(labelRaw);
    const branch = pickBranch(t.branch);
    nodes.push({ id, label, phase: 'terminal', branch, x: 0, y: 0 });
    if (nodes.length >= 2) {
      const last = nodes[nodes.length - 2] as VizNode;
      const exists = edges.find((e) => e.from === last.id && e.to === id);
      if (!exists) edges.push({ from: last.id, to: id, branch });
    }
  }

  return { nodes, edges };
}

export function layoutViz(
  nodes: VizNode[],
  width: number,
  height: number,
  padX = 32,
  padY = 24,
): void {
  if (nodes.length === 0) return;
  const usableW = Math.max(1, width - padX * 2);
  const stepX = nodes.length > 1 ? usableW / (nodes.length - 1) : 0;
  const midY = padY + (height - padY * 2) / 2;
  nodes.forEach((n, i) => {
    n.x = padX + (nodes.length > 1 ? i * stepX : usableW / 2);
    n.y = midY;
  });
}
</script>

<script setup lang="ts">
/**
 * DemoRecordPicker.vue
 *
 * Vue 3 port of the PyQt "演示记录选择器" panel. Lists pending analysis
 * records fetched from `GET /api/records`, supports search + timeframe
 * filtering, server-side random 演示 picks, and drives a Canvas 2D
 * decision-flow visualization that mirrors `pa_agent/gui/decision_flow_viz.py`.
 *
 * On pick the component:
 *  - hydrates `frameStore` (kline snapshot) so the chart panel re-renders
 *  - calls `decisionStore.setDecision(...)` with the embedded stage-2 decision
 *  - emits `picked` so parent views can navigate / refresh
 *
 * The Canvas 2D viz renders branch-coloured bezier edges between phase-coded
 * glass-card nodes built from `stage2_decision.gate_trace` /
 * `stage2_decision.decision_trace` / `stage2_decision.terminal`. All colors
 * come from theme tokens (no hardcoded hex in the scoped style block);
 * the canvas re-draws on:
 *  - record selection change
 *  - canvas resize (ResizeObserver)
 *  - explicit prop mutations (width/height/records)
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api, type RecordListItem } from '@/api/client';
import { frameStore } from '@/stores/frame';
import { decisionStore } from '@/stores/decision';

const props = withDefaults(
  defineProps<{
    autoPlay?: boolean;
    width?: number;
    height?: number;
    pageSize?: number;
  }>(),
  {
    autoPlay: false,
    width: 520,
    height: 220,
    pageSize: 30,
  },
);

const emit = defineEmits<{
  (e: 'picked', payload: { record: RecordListItem; detail: RecordDetail }): void;
  (e: 'select', payload: { record: RecordListItem }): void;
  (e: 'error', message: string): void;
  (e: 'refreshed', count: number): void;
}>();

const search = ref('');
const timeframe = ref<DemoTimeframe>('all');
const loading = ref(false);
const picking = ref(false);
const error = ref<string | null>(null);
const records = ref<RecordListItem[]>([]);
const selected = ref<RecordListItem | null>(null);
const selectedDetail = ref<RecordDetail | null>(null);
const canvasRef = ref<HTMLCanvasElement | null>(null);
let resizeObserver: ResizeObserver | null = null;

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase();
  const tf = timeframe.value;
  return records.value.filter((r) => {
    if (tf !== 'all' && r.timeframe !== tf) return false;
    if (!q) return true;
    return (
      r.symbol.toLowerCase().includes(q) ||
      r.filename.toLowerCase().includes(q) ||
      r.timestamp.toLowerCase().includes(q)
    );
  });
});

const hasSelection = computed(() => selected.value !== null);

onMounted(async () => {
  await refresh();
  if (typeof ResizeObserver !== 'undefined' && canvasRef.value) {
    resizeObserver = new ResizeObserver(() => draw());
    resizeObserver.observe(canvasRef.value);
  }
  // Auto play: kick off a random pick on first mount if requested.
  if (props.autoPlay && records.value.length > 0) {
    void pickRandom();
  }
  draw();
});

onBeforeUnmount(() => {
  if (resizeObserver) {
    resizeObserver.disconnect();
    resizeObserver = null;
  }
});

watch(filtered, () => draw());
watch(
  () => [props.width, props.height],
  () => draw(),
);
watch(selectedDetail, () => nextTick(draw));

async function refresh(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const resp = await api.fetchRecords();
    const list = (resp?.records ?? []) as RecordListItem[];
    records.value = list;
    emit('refreshed', list.length);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    emit('error', error.value);
  } finally {
    loading.value = false;
  }
}

async function pickRandom(): Promise<void> {
  if (records.value.length === 0) {
    error.value = '暂无可演示记录';
    return;
  }
  picking.value = true;
  error.value = null;
  try {
    // Server-side random pick: fetch a synthetic filename; backend should
    // return a RecordDetail with embedded kline_snapshot + stage2_decision.
    const detail = (await api.fetchRecord('__random__')) as unknown as RecordDetail | null;
    if (!detail || !detail.filename) {
      error.value = '随机选取失败：服务端未返回记录';
      emit('error', error.value);
      return;
    }
    const listItem: RecordListItem = {
      filename: detail.filename,
      timestamp: detail.timestamp ?? new Date().toISOString(),
      symbol: detail.symbol ?? '',
      timeframe: detail.timeframe ?? '1m',
      bar_count: detail.bar_count ?? 0,
    };
    hydrate(listItem, detail);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    emit('error', error.value);
  } finally {
    picking.value = false;
  }
}

async function pickRecord(item: RecordListItem): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const detail = (await api.fetchRecord(item.filename)) as unknown as RecordDetail | null;
    if (!detail) {
      const message = `记录不存在：${item.filename}`;
      error.value = message;
      emit('error', message);
      return;
    }
    hydrate(item, detail);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    emit('error', error.value);
  } finally {
    loading.value = false;
  }
}

function hydrate(item: RecordListItem, detail: RecordDetail): void {
  selected.value = item;
  selectedDetail.value = detail;
  // Hydrate the frame store with the kline snapshot so the chart canvas
  // re-renders against the picked symbol/timeframe/bars.
  const snap: DataSnapshot | null =
    detail.kline_snapshot ??
    (detail.bars
      ? {
          symbol: item.symbol,
          timeframe: item.timeframe,
          bars: detail.bars,
          indicators: detail.indicators ?? {},
        }
      : null);
  if (snap) {
    frameStore.setSnapshot(snap);
  }
  // Hydrate the decision store so the AIStreamPanel picks up the trace.
  if (detail.stage2_decision) {
    decisionStore.setDecision(detail.stage2_decision);
  }
  emit('select', { record: item });
  emit('picked', { record: item, detail });
  nextTick(draw);
}

function readToken(name: string, fallback: string): string {
  if (typeof window === 'undefined' || typeof getComputedStyle === 'undefined') {
    return fallback;
  }
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return raw || fallback;
}

function branchColor(branch: FlowBranch | undefined): string {
  if (branch === 'up') return readToken('--chart-up', '#22c55e');
  if (branch === 'down') return readToken('--chart-down', '#ef4444');
  if (branch === 'neutral') return readToken('--chart-line', '#f0c674');
  return readToken('--fg-3', '#8b94a3');
}

function phaseColor(phase: FlowPhase): string {
  if (phase === 'gate') return readToken('--accent-3', '#7aa2ff');
  if (phase === 'decision') return readToken('--accent', '#22c55e');
  return readToken('--accent-2', '#ff7a59');
}

function draw(): void {
  const canvas = canvasRef.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  // High-DPI sizing
  const rect = canvas.getBoundingClientRect();
  const cssW = props.width || Math.max(1, Math.round(rect.width));
  const cssH = props.height || Math.max(1, Math.round(rect.height));
  const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
  if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  // Glass-card background
  ctx.fillStyle = readToken('--surface-1', '#1a1d24');
  ctx.strokeStyle = readToken('--border', '#2a2f38');
  ctx.lineWidth = 1;
  ctx.fillRect(0.5, 0.5, cssW - 1, cssH - 1);
  ctx.strokeRect(0.5, 0.5, cssW - 1, cssH - 1);

  const { nodes, edges } = buildViz(selectedDetail.value);
  if (nodes.length === 0) {
    ctx.fillStyle = readToken('--fg-3', '#8b94a3');
    ctx.font = '12px var(--font-mono, monospace)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('请先选择一条演示记录以查看决策流', cssW / 2, cssH / 2);
    return;
  }

  layoutViz(nodes, cssW, cssH);
  const nodeW = 96;
  const nodeH = 36;

  // Edges (bezier) — branch-coloured
  for (const edge of edges) {
    const from = nodes.find((n) => n.id === edge.from);
    const to = nodes.find((n) => n.id === edge.to);
    if (!from || !to) continue;
    ctx.strokeStyle = branchColor(edge.branch ?? from.branch);
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    const x1 = from.x;
    const y1 = from.y;
    const x2 = to.x;
    const y2 = to.y;
    const cpx = x1 + (x2 - x1) * 0.5;
    ctx.moveTo(x1, y1);
    ctx.bezierCurveTo(cpx, y1, cpx, y2, x2, y2);
    ctx.stroke();
  }

  // Glass-card nodes — phase-coloured
  for (const node of nodes) {
    const x = node.x - nodeW / 2;
    const y = node.y - nodeH / 2;
    ctx.fillStyle = readToken('--surface-2', '#222632');
    ctx.strokeStyle = phaseColor(node.phase);
    ctx.lineWidth = 1.25;
    const r = 6;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + nodeW - r, y);
    ctx.quadraticCurveTo(x + nodeW, y, x + nodeW, y + r);
    ctx.lineTo(x + nodeW, y + nodeH - r);
    ctx.quadraticCurveTo(x + nodeW, y + nodeH, x + nodeW - r, y + nodeH);
    ctx.lineTo(x + r, y + nodeH);
    ctx.quadraticCurveTo(x, y + nodeH, x, y + nodeH - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // Phase dot
    ctx.fillStyle = phaseColor(node.phase);
    ctx.beginPath();
    ctx.arc(x + 10, y + nodeH / 2, 3, 0, Math.PI * 2);
    ctx.fill();

    // Label
    ctx.fillStyle = readToken('--fg', '#e8eaef');
    ctx.font = '11px var(--font-body, sans-serif)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    const label = node.label.length > 12 ? node.label.slice(0, 12) + '…' : node.label;
    ctx.fillText(label, x + 18, y + nodeH / 2);
  }
}

defineExpose({ refresh, draw, filtered, selected, selectedDetail, hydrate, buildViz });
</script>

<template>
  <section class="demo-picker" data-testid="demo-picker">
    <header class="picker-header" data-testid="picker-header">
      <div class="filters">
        <input
          v-model="search"
          class="search"
          type="search"
          placeholder="搜索 symbol / 文件名 / 时间戳"
          aria-label="搜索演示记录"
          data-testid="search-input"
        />
        <select
          v-model="timeframe"
          class="tf"
          aria-label="按周期过滤"
          data-testid="timeframe-select"
        >
          <option v-for="tf in DEMO_TIMEFRAMES" :key="tf" :value="tf">
            {{ tf === 'all' ? '全部周期' : tf }}
          </option>
        </select>
      </div>
      <div class="actions">
        <button
          type="button"
          class="btn-refresh"
          :disabled="loading"
          data-testid="refresh-btn"
          @click="refresh"
        >
          刷新
        </button>
        <button
          type="button"
          class="btn-pick"
          :disabled="picking || records.length === 0"
          data-testid="pick-random-btn"
          @click="pickRandom"
        >
          随机演示
        </button>
      </div>
    </header>

    <p v-if="error" class="error" role="alert" data-testid="error">{{ error }}</p>

    <div class="body">
      <ul
        v-if="filtered.length > 0"
        class="record-list"
        data-testid="record-list"
        role="listbox"
        aria-label="演示记录"
      >
        <li
          v-for="r in filtered"
          :key="r.filename"
          class="record-item"
          :class="{ active: selected?.filename === r.filename }"
          :data-testid="`record-${r.filename}`"
          role="option"
          :aria-selected="selected?.filename === r.filename"
          @click="pickRecord(r)"
        >
          <div class="row1">
            <span class="symbol">{{ r.symbol }}</span>
            <span class="tf-pill">{{ r.timeframe }}</span>
          </div>
          <div class="row2">
            <span class="filename">{{ r.filename }}</span>
            <span class="bar-count">{{ r.bar_count }} 根</span>
          </div>
          <time class="ts">{{ r.timestamp }}</time>
        </li>
      </ul>
      <p v-else class="empty" data-testid="empty">
        {{ records.length === 0 ? '尚无演示记录' : '无匹配记录' }}
      </p>

      <div class="viz-wrap" data-testid="viz-wrap">
        <div class="viz-title" data-testid="viz-title">
          {{ hasSelection ? `${selected?.symbol} · ${selected?.timeframe}` : '决策流可视化' }}
        </div>
        <canvas
          ref="canvasRef"
          class="viz-canvas"
          :width="width"
          :height="height"
          :style="{ width: `${width}px`, height: `${height}px` }"
          data-testid="viz-canvas"
          aria-hidden="true"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.demo-picker {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12px;
  color: var(--fg-2);
}
.picker-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  justify-content: space-between;
}
.filters {
  display: flex;
  gap: 6px;
  flex: 1 1 auto;
  min-width: 0;
}
.search,
.tf {
  background: var(--surface-2);
  border: 1px solid var(--border-2);
  color: var(--fg);
  border-radius: 6px;
  padding: 4px 8px;
  font-family: var(--font-body);
  font-size: 12px;
}
.search {
  flex: 1 1 auto;
  min-width: 120px;
}
.search:focus,
.tf:focus {
  outline: 1px solid var(--accent-3);
  border-color: var(--accent-3);
}
.actions {
  display: flex;
  gap: 6px;
}
.btn-refresh,
.btn-pick {
  background: var(--surface-2);
  border: 1px solid var(--border-2);
  color: var(--fg);
  border-radius: 6px;
  padding: 4px 10px;
  font-family: var(--font-body);
  font-size: 12px;
  cursor: pointer;
}
.btn-pick {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--surface-1);
  font-weight: 600;
}
.btn-refresh:disabled,
.btn-pick:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.error {
  margin: 0;
  padding: 6px 8px;
  background: var(--surface-2);
  border-left: 3px solid var(--danger);
  border-radius: 4px;
  color: var(--danger);
  font-family: var(--font-mono);
  font-size: 11px;
}
.body {
  display: grid;
  grid-template-columns: minmax(160px, 1fr) auto;
  gap: 10px;
  align-items: start;
}
.record-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 220px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
}
.record-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  color: var(--fg-2);
}
.record-item:hover {
  background: var(--surface-3);
}
.record-item.active {
  background: var(--surface-3);
  border-left: 3px solid var(--accent);
  color: var(--fg);
}
.row1 {
  display: flex;
  align-items: center;
  gap: 6px;
  justify-content: space-between;
}
.symbol {
  font-weight: 600;
  color: var(--fg);
}
.tf-pill {
  background: var(--surface-4);
  color: var(--fg-2);
  border-radius: 999px;
  padding: 0 6px;
  font-size: 10px;
  font-family: var(--font-mono);
}
.row2 {
  display: flex;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--fg-3);
  justify-content: space-between;
}
.ts {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--fg-3);
}
.empty {
  margin: 0;
  padding: 12px;
  text-align: center;
  color: var(--fg-3);
  font-size: 11px;
  border: 1px dashed var(--border-2);
  border-radius: 6px;
}
.viz-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}
.viz-title {
  font-size: 11px;
  color: var(--fg-2);
  font-family: var(--font-mono);
}
.viz-canvas {
  display: block;
  border-radius: 6px;
  background: var(--surface-2);
  border: 1px solid var(--border);
}
</style>
