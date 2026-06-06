<script setup lang="ts">
/**
 * StreamCharStats — per-stage character usage readout for the AI stream pane.
 *
 * Extracted from AIStreamPanel so the per-stage char counters live in a
 * testable surface of their own. The component is purely presentational:
 * it derives the numbers from `streamStore.messages` and renders them with
 * `data-testid` hooks for vitest. All colors, borders, and font families
 * come from CSS custom properties declared in `src/styles/tokens.css`,
 * so the panel adapts to any future theme switch without re-style work.
 *
 * Char totals are computed as the sum of `text.length` for messages whose
 * `stage` matches the bucket. Messages without a stage are still included
 * in the grand total but are not attributed to any named stage.
 */
import { computed } from 'vue';
import { streamStore, type StreamMessage } from '@/stores/stream';

interface StageBucket {
  key: '1' | '2' | 'followup';
  label: string;
  testid: string;
}

const BUCKETS: readonly StageBucket[] = [
  { key: '1', label: '阶段一', testid: 'stat-stage-1' },
  { key: '2', label: '阶段二', testid: 'stat-stage-2' },
  { key: 'followup', label: '追问', testid: 'stat-followup' },
] as const;

function charsFor(stage: StageBucket['key']): number {
  return streamStore.messages
    .filter((m: StreamMessage) => m.stage === stage)
    .reduce((acc: number, m: StreamMessage) => acc + (m.text?.length ?? 0), 0);
}

const totalChars = computed(() =>
  streamStore.messages.reduce(
    (acc: number, m: StreamMessage) => acc + (m.text?.length ?? 0),
    0,
  ),
);

const totalMessages = computed(() => streamStore.messages.length);

const rows = computed(() =>
  BUCKETS.map((b) => ({
    ...b,
    chars: charsFor(b.key),
  })),
);
</script>

<template>
  <section
    class="char-stats"
    data-testid="char-stats"
    :data-total-messages="totalMessages"
    :data-total-chars="totalChars"
    :aria-label="`字符统计 共 ${totalMessages} 条消息，${totalChars} 字符`"
  >
    <ul class="char-stats-list" data-testid="char-stats-list">
      <li
        v-for="row in rows"
        :key="row.key"
        class="char-stats-row"
        :data-testid="row.testid"
        :data-stage="row.key"
      >
        <span class="char-stats-label">{{ row.label }}</span>
        <strong class="char-stats-value" :data-testid="`${row.testid}-chars`">
          {{ row.chars.toLocaleString() }}
        </strong>
        <span class="char-stats-unit" aria-hidden="true">字</span>
      </li>
      <li class="char-stats-row char-stats-total" data-testid="stat-total">
        <span class="char-stats-label">总计</span>
        <strong class="char-stats-value" data-testid="stat-total-chars">
          {{ totalChars.toLocaleString() }}
        </strong>
        <span class="char-stats-unit" aria-hidden="true">字</span>
        <span class="char-stats-divider" aria-hidden="true">·</span>
        <span class="char-stats-msg" data-testid="stat-total-messages">
          {{ totalMessages }} 条
        </span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.char-stats {
  display: block;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-1);
  color: var(--fg);
  font-family: var(--font-body);
}

.char-stats-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 10px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--fg-3);
}

.char-stats-row {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  white-space: nowrap;
}

.char-stats-label {
  color: var(--fg-3);
}

.char-stats-value {
  color: var(--fg);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.char-stats-unit {
  color: var(--fg-3);
  font-size: 10px;
}

.char-stats-total {
  margin-left: auto;
  padding-left: 10px;
  border-left: 1px solid var(--border-2);
}

.char-stats-divider {
  color: var(--fg-3);
  opacity: 0.6;
}

.char-stats-msg {
  color: var(--fg-2);
  font-size: 10px;
}
</style>
