<script setup lang="ts">
/**
 * StreamPhaseHeader — extracted from AIStreamPanel to keep the phase label
 * and per-stage counters in their own testable component. Drives its state
 * from `streamStore.messages`, `decisionStore.flowStep / analyzing / error /
 * decision`, and `settingsStore.appState`. All colors and fonts come from
 * CSS custom properties declared in `src/styles/tokens.css`, so the
 * component adapts to any future theme switch without re-style work.
 */
import { computed } from 'vue';
import { streamStore, type StreamMessage } from '@/stores/stream';
import { decisionStore } from '@/stores/decision';
import { settingsStore } from '@/stores/settings';

const phaseLabel = computed(() => {
  if (decisionStore.error) return `出错 · ${decisionStore.error}`;
  if (decisionStore.analyzing) {
    const flow = decisionStore.flowStep;
    if (flow <= 0) return '阶段-1 · 预热中…';
    if (flow === 1) return '阶段一 · 市场诊断中…';
    if (flow === 2) return '阶段二 · 交易决策中…';
    return '收尾中…';
  }
  if (decisionStore.decision) return '分析完成';
  return '等待分析…';
});

const phaseTone = computed<'idle' | 'running' | 'success' | 'error' | 'warn'>(() => {
  if (decisionStore.error) return 'error';
  if (decisionStore.analyzing) return 'running';
  if (decisionStore.decision) return 'success';
  if (settingsStore.appState === 'error') return 'error';
  return 'idle';
});

const stageOneCount = computed(
  () => streamStore.messages.filter((m: StreamMessage) => m.stage === '1').length,
);
const stageTwoCount = computed(
  () => streamStore.messages.filter((m: StreamMessage) => m.stage === '2').length,
);
const followupCount = computed(
  () => streamStore.messages.filter((m: StreamMessage) => m.stage === 'followup').length,
);

const totalMessages = computed(() => streamStore.messages.length);
</script>

<template>
  <header
    class="phase-header"
    data-testid="phase-header"
    :data-tone="phaseTone"
    :data-analyzing="decisionStore.analyzing ? 'true' : 'false'"
    :data-error="decisionStore.error ? 'true' : 'false'"
  >
    <span
      class="phase-dot"
      :class="`tone-${phaseTone}`"
      data-testid="phase-dot"
      aria-hidden="true"
    />
    <span class="phase-label" data-testid="phase-label">{{ phaseLabel }}</span>
    <span class="phase-stats" data-testid="phase-stats">
      <span class="stat" data-testid="stat-stage-1">
        阶段一 <strong>{{ stageOneCount }}</strong>
      </span>
      <span class="stat-sep" aria-hidden="true">·</span>
      <span class="stat" data-testid="stat-stage-2">
        阶段二 <strong>{{ stageTwoCount }}</strong>
      </span>
      <span class="stat-sep" aria-hidden="true">·</span>
      <span class="stat" data-testid="stat-followup">
        追问 <strong>{{ followupCount }}</strong>
      </span>
      <span class="stat-sep" aria-hidden="true">·</span>
      <span class="stat" data-testid="stat-total">
        总计 <strong>{{ totalMessages }}</strong>
      </span>
    </span>
  </header>
</template>

<style scoped>
.phase-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-1);
  color: var(--fg);
  font-family: var(--font-body);
}

.phase-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fg-3);
  flex-shrink: 0;
  transition: background 200ms ease, box-shadow 200ms ease;
}
.phase-dot.tone-running {
  background: var(--warning);
  box-shadow: 0 0 6px var(--warning);
}
.phase-dot.tone-success {
  background: var(--success);
  box-shadow: 0 0 6px var(--success);
}
.phase-dot.tone-error {
  background: var(--danger);
  box-shadow: 0 0 6px var(--danger);
}
.phase-dot.tone-idle {
  background: var(--fg-3);
}

.phase-label {
  font-weight: 600;
  font-size: 13px;
  color: var(--fg);
  margin-right: auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.phase-stats {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--fg-3);
  font-family: var(--font-mono);
  white-space: nowrap;
}

.stat strong {
  color: var(--fg);
  font-weight: 600;
  margin-left: 2px;
}

.stat-sep {
  color: var(--fg-3);
  opacity: 0.6;
}
</style>
