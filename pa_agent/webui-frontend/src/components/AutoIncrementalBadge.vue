<script setup lang="ts">
/**
 * AutoIncrementalBadge — header pill that toggles the auto-incremental
 * analysis flag. When ON, the next chart refresh will trigger an
 * incremental run via streamStore.submitAnalysis({ incremental: true }).
 *
 * Reads/writes:
 *  - uiStore.autoIncremental (boolean flag)
 *
 * Visuals follow the rest of the chrome: pill shape, color pulled from
 * --accent / --surface-3, hover lifts the surface to --surface-4. All
 * colors come from tokens.css so a future theme switch adapts the badge
 * without code changes.
 */
import { computed } from 'vue';
import { uiStore } from '@/stores/ui';

const isOn = computed(() => uiStore.autoIncremental);
const ariaPressed = computed(() => (isOn.value ? 'true' : 'false'));

function toggle(): void {
  uiStore.setAutoIncremental(!isOn.value);
  uiStore.pushToast(
    isOn.value ? '已开启自动增量分析' : '已关闭自动增量分析',
    isOn.value ? 'success' : 'info',
  );
}
</script>

<template>
  <button
    type="button"
    class="auto-incremental-badge"
    :data-state="isOn ? 'on' : 'off'"
    :data-testid="'auto-incremental-badge'"
    :aria-pressed="ariaPressed"
    :title="isOn ? '点击关闭自动增量' : '点击开启自动增量'"
    @click="toggle"
  >
    <span class="dot" data-testid="auto-incremental-dot" aria-hidden="true" />
    <span class="label" data-testid="auto-incremental-label">
      {{ isOn ? '自动增量 · 开' : '自动增量' }}
    </span>
  </button>
</template>

<style scoped>
.auto-incremental-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border-radius: 13px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--fg-2);
  font-family: var(--font-body);
  font-size: 12px;
  cursor: pointer;
  transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
}
.auto-incremental-badge:hover {
  background: var(--surface-3);
  color: var(--fg);
}
.auto-incremental-badge[data-state='on'] {
  background: var(--accent);
  color: var(--bg);
  border-color: var(--accent);
}
.auto-incremental-badge[data-state='on']:hover {
  background: var(--accent-2);
  border-color: var(--accent-2);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fg-3);
  transition: background 120ms ease;
}
.auto-incremental-badge[data-state='on'] .dot {
  background: var(--bg);
  box-shadow: 0 0 6px var(--accent);
}
.label {
  font-weight: 500;
  letter-spacing: 0.01em;
  white-space: nowrap;
}
</style>
