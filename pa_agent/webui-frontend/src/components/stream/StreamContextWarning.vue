<script setup lang="ts">
/**
 * StreamContextWarning — context window usage warning panel with a Canvas 2D
 * trend chart. Mirrors the PyQt AIStreamPanel context bar but in idiomatic
 * Vue 3 Composition API form. All colors come from theme tokens (tokens.css)
 * so the component re-themes correctly when the host switches palettes.
 *
 * The component is intentionally "dumb": it owns no transport, no store, and
 * no polling — callers feed it the latest snapshot via props and watch the
 * reactive `history` array to redraw the canvas. This keeps the unit-test
 * surface small (props in, canvas API calls out).
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

export interface ContextSample {
  /** Epoch milliseconds — used to label the x-axis ticks. */
  t: number;
  /** Percentage 0-100 of the model context window. */
  pct: number;
}

const props = withDefaults(
  defineProps<{
    /** Latest usage snapshot, percentage 0-100. */
    currentPct: number;
    /** Tokens consumed in the current run. */
    used: number;
    /** Model context window size, in tokens. */
    window: number;
    /** Threshold percentage (0-100) at which the warning escalates. */
    thresholdPct?: number;
    /** Historical samples rendered in the trend chart, oldest first. */
    history?: ContextSample[];
    /** Optional fixed pixel width for the canvas. */
    width?: number;
    /** Optional fixed pixel height for the canvas. */
    height?: number;
  }>(),
  {
    thresholdPct: 80,
    history: () => [],
    width: 220,
    height: 56,
  },
);

const emit = defineEmits<{
  (e: 'level', level: ContextLevel): void;
}>();

export type ContextLevel = 'safe' | 'warn' | 'danger';

const canvasRef = ref<HTMLCanvasElement | null>(null);
let resizeObserver: ResizeObserver | null = null;

const safeMax = computed(() => Math.max(1, props.thresholdPct - 20));

const level = computed<ContextLevel>(() => {
  const pct = props.currentPct;
  if (pct >= props.thresholdPct) return 'danger';
  if (pct >= safeMax.value) return 'warn';
  return 'safe';
});

const formatted = computed(() => {
  const usedFmt = props.used.toLocaleString('en-US');
  const winFmt = props.window.toLocaleString('en-US');
  return {
    pctText: `${props.currentPct.toFixed(1)}%`,
    usageText: `${usedFmt} / ${winFmt}`,
    label:
      level.value === 'danger'
        ? '上下文窗口即将耗尽'
        : level.value === 'warn'
          ? '上下文使用率偏高'
          : '上下文使用率正常',
  };
});

const fillStyle = computed(() =>
  level.value === 'danger'
    ? 'var(--danger)'
    : level.value === 'warn'
      ? 'var(--warning)'
      : 'var(--accent)',
);

const trackStyle = computed(() => 'var(--surface-3)');

watch(
  () => props.currentPct,
  (next) => {
    const lvl: ContextLevel =
      next >= props.thresholdPct ? 'danger' : next >= safeMax.value ? 'warn' : 'safe';
    emit('level', lvl);
  },
  { immediate: true },
);

watch(
  () => [props.history.length, props.currentPct, level.value, props.width, props.height],
  () => drawChart(),
);

onMounted(() => {
  drawChart();
  if (typeof ResizeObserver !== 'undefined' && canvasRef.value) {
    resizeObserver = new ResizeObserver(() => drawChart());
    resizeObserver.observe(canvasRef.value);
  }
});

onBeforeUnmount(() => {
  if (resizeObserver) {
    resizeObserver.disconnect();
    resizeObserver = null;
  }
});

function readToken(name: string, fallback: string): string {
  if (typeof window === 'undefined' || typeof getComputedStyle === 'undefined') {
    return fallback;
  }
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return raw || fallback;
}

function drawChart(): void {
  const canvas = canvasRef.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  // Handle high-DPI rendering — pull intrinsic size from the bounding rect
  // so a ResizeObserver-driven width change redraws at the new density.
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

  // Theme tokens — fallback hex matches oklch values from tokens.css
  // so SSR/test environments without stylesheets still render meaningfully.
  const grid = readToken('--chart-grid', '#2a2f38');
  const line = readToken(
    level.value === 'danger'
      ? '--chart-line-3'
      : level.value === 'warn'
        ? '--chart-line'
        : '--chart-line-2',
    level.value === 'danger' ? '#ff7a59' : level.value === 'warn' ? '#f0c674' : '#7aa2ff',
  );
  const fill = readToken('--chart-line-2', '#7aa2ff');

  // Background grid: 4 horizontal bands at 25/50/75/100%.
  ctx.strokeStyle = grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 4; i++) {
    const y = Math.round((cssH - 1) * (i / 4)) + 0.5;
    ctx.moveTo(0, y);
    ctx.lineTo(cssW, y);
  }
  ctx.stroke();

  // Threshold line.
  const thresholdY = cssH - (Math.min(100, props.thresholdPct) / 100) * (cssH - 1);
  ctx.strokeStyle = readToken('--warning', '#f0c674');
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(0, thresholdY);
  ctx.lineTo(cssW, thresholdY);
  ctx.stroke();
  ctx.setLineDash([]);

  const samples = props.history.length ? props.history : [{ t: Date.now(), pct: props.currentPct }];
  if (samples.length === 0) return;

  // Build the path; left-pad with a single leading zero so the line always
  // touches the left edge for a single-sample history.
  const stepX = samples.length > 1 ? cssW / (samples.length - 1) : 0;
  const points: Array<[number, number]> = samples.map((s, i) => {
    const x = samples.length > 1 ? i * stepX : 0;
    const clamped = Math.max(0, Math.min(100, s.pct));
    const y = cssH - (clamped / 100) * (cssH - 1);
    return [x, y];
  });

  // Filled area under the curve for visual weight. Use beginPath/fill/closePath
  // rather than Path2D because jsdom (the test environment) does not ship
  // Path2D. The visual output is identical in real browsers.
  ctx.fillStyle = withAlpha(fill, 0.18);
  ctx.beginPath();
  ctx.moveTo(points[0]?.[0] ?? 0, cssH);
  for (const [x, y] of points) ctx.lineTo(x, y);
  ctx.lineTo(points[points.length - 1]?.[0] ?? 0, cssH);
  ctx.closePath();
  ctx.fill();

  // Foreground line.
  ctx.strokeStyle = line;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  points.forEach(([x, y], idx) => {
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Final value marker.
  const last = points[points.length - 1];
  if (last) {
    ctx.fillStyle = line;
    ctx.beginPath();
    ctx.arc(last[0], last[1], 2.5, 0, Math.PI * 2);
    ctx.fill();
  }
}

function withAlpha(color: string, alpha: number): string {
  // Minimal hex/oklch alpha shim — Canvas 2D accepts CSS color strings and
  // modern Chromium/Firefox parse #rrggbb / rgb(...) alpha-merged at draw
  // time. We rebuild as rgba for the two common hex forms so older jsdom
  // fallbacks don't drop the fill.
  if (color.startsWith('#') && (color.length === 7 || color.length === 4)) {
    let r = 0;
    let g = 0;
    let b = 0;
    if (color.length === 7) {
      r = parseInt(color.slice(1, 3), 16);
      g = parseInt(color.slice(3, 5), 16);
      b = parseInt(color.slice(5, 7), 16);
    } else {
      r = parseInt(color[1] + color[1], 16);
      g = parseInt(color[2] + color[2], 16);
      b = parseInt(color[3] + color[3], 16);
    }
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  return color;
}

defineExpose({ level, formatted, redraw: drawChart });
</script>

<template>
  <section
    class="context-warning"
    :class="`is-${level}`"
    role="status"
    :aria-label="`上下文使用率 ${formatted.pctText}`"
  >
    <header class="header">
      <span class="label">上下文</span>
      <span class="pill" :class="`pill-${level}`">{{ formatted.label }}</span>
    </header>
    <div class="bar">
      <div class="track" :style="{ background: trackStyle }">
        <div
          class="fill"
          :style="{ width: `${Math.min(100, currentPct)}%`, background: fillStyle }"
        />
        <div
          class="threshold-mark"
          :style="{ left: `${Math.min(100, thresholdPct)}%` }"
          aria-hidden="true"
        />
      </div>
      <span class="bar-text" data-testid="bar-text">{{ formatted.pctText }} · {{ formatted.usageText }}</span>
    </div>
    <canvas
      ref="canvasRef"
      class="trend"
      :width="width"
      :height="height"
      :style="{ width: `${width}px`, height: `${height}px` }"
      data-testid="trend-canvas"
      aria-hidden="true"
    />
  </section>
</template>

<style scoped>
.context-warning {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12px;
  color: var(--fg-2);
}
.context-warning.is-warn {
  border-color: var(--warning);
}
.context-warning.is-danger {
  border-color: var(--danger);
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.label {
  color: var(--fg);
  font-weight: 600;
  font-size: 12px;
}
.pill {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--border-2);
  background: var(--surface-2);
  color: var(--fg-2);
  font-size: 11px;
  white-space: nowrap;
}
.pill-safe {
  color: var(--success);
  border-color: var(--success);
  background: var(--surface-2);
}
.pill-warn {
  color: var(--warning);
  border-color: var(--warning);
}
.pill-danger {
  color: var(--danger);
  border-color: var(--danger);
}
.bar {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.track {
  position: relative;
  width: 100%;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
}
.fill {
  position: absolute;
  inset: 0 auto 0 0;
  border-radius: 4px;
  transition: width 0.2s ease;
}
.threshold-mark {
  position: absolute;
  top: -2px;
  bottom: -2px;
  width: 1px;
  background: var(--warning);
  opacity: 0.7;
}
.bar-text {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--fg-2);
}
.trend {
  display: block;
  border-radius: 4px;
  background: var(--surface-2);
}
</style>
