<script setup lang="ts">
/**
 * DemoModeLauncher — header pill that opens the demo-mode modal. The
 * "demo" flag is purely UI state today: when ON, the next analysis
 * submission would replay a canned decision payload. The launcher only
 * toggles `uiStore.demoMode` / `demoRunning` and emits a toast; the
 * actual replay pipeline lives in a separate module.
 *
 * Reads/writes:
 *  - uiStore.demoMode (boolean flag)
 *  - uiStore.demoRunning (transient, true while a replay is in flight)
 *  - uiStore.openModal('demo-launcher') on click
 */
import { computed } from 'vue';
import { uiStore } from '@/stores/ui';

const isOn = computed(() => uiStore.demoMode);
const isRunning = computed(() => uiStore.demoRunning);
const label = computed(() => {
  if (isRunning.value) return '演示运行中…';
  if (isOn.value) return '演示模式 · 开';
  return '演示模式';
});
const ariaPressed = computed(() => (isOn.value ? 'true' : 'false'));

function launch(): void {
  if (isRunning.value) return;
  uiStore.setDemoMode(!isOn.value);
  uiStore.pushToast(
    isOn.value ? '演示模式已开启' : '演示模式已关闭',
    isOn.value ? 'success' : 'info',
  );
  // Open the launcher modal so the user can pick a canned scenario.
  uiStore.openModal('demo-launcher');
}
</script>

<template>
  <button
    type="button"
    class="demo-launcher"
    :data-state="isOn ? 'on' : 'off'"
    :data-running="isRunning ? 'true' : 'false'"
    data-testid="demo-launcher"
    :aria-pressed="ariaPressed"
    :title="isOn ? '点击关闭演示模式' : '点击开启演示模式'"
    @click="launch"
  >
    <span class="icon" aria-hidden="true">▶</span>
    <span class="label" data-testid="demo-launcher-label">{{ label }}</span>
  </button>
</template>

<style scoped>
.demo-launcher {
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
.demo-launcher:hover {
  background: var(--surface-3);
  color: var(--fg);
}
.demo-launcher[data-state='on'] {
  background: var(--warning);
  color: var(--bg);
  border-color: var(--warning);
}
.demo-launcher[data-running='true'] {
  background: var(--info);
  color: var(--bg);
  border-color: var(--info);
  cursor: progress;
}
.icon {
  font-size: 10px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 12px;
}
.label {
  font-weight: 500;
  white-space: nowrap;
}
</style>
