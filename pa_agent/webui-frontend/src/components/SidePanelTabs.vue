<script setup lang="ts">
/**
 * SidePanelTabs — tab strip that drives the right-hand side panel of the
 * trading terminal. Pure presentational + controlled: the parent owns the
 * active tab id via `v-model` and the child only renders + dispatches.
 *
 * Contract:
 *   <SidePanelTabs
 *     v-model="activeTab"
 *     :tabs="[{ id: 'stream', label: 'AI 流' }, ...]"
 *     aria-label="侧栏视图"
 *   />
 *
 *   - `modelValue` is the id of the currently active tab.
 *   - The component emits `update:modelValue` (standard v-model wiring) on
 *     click, Enter, Space, ArrowLeft, ArrowRight, Home, End.
 *   - Implements the WAI-ARIA tabs pattern: role=tablist, role=tab,
 *     aria-selected, aria-controls, roving tabindex.
 *   - Falls back to the first tab when `modelValue` does not match any
 *     tab id (defensive default for late v-model binding).
 *
 * Tokens used (all from src/styles/tokens.css):
 *   --surface-1, --surface-2, --surface-3, --fg, --fg-2, --fg-3,
 *   --border, --border-2, --accent, --accent-2, --danger,
 *   --font-body, --font-mono
 */
import { computed, nextTick, ref, watch } from 'vue';

export interface SidePanelTab {
  /** Stable id used for v-model wiring. */
  id: string;
  /** Visible label, plain text. */
  label: string;
  /** Optional decorative glyph; rendered aria-hidden. */
  icon?: string;
  /** Optional badge count (e.g. unread items); rendered as a pill. */
  badge?: number;
  /** Disables selection when true. */
  disabled?: boolean;
}

const props = withDefaults(
  defineProps<{
    tabs: SidePanelTab[];
    modelValue: string;
    ariaLabel?: string;
  }>(),
  { ariaLabel: '侧栏视图' },
);

const emit = defineEmits<{
  (e: 'update:modelValue', id: string): void;
}>();

const tablistEl = ref<HTMLElement | null>(null);

// Resolved active id — clamps to the first non-disabled tab when the
// incoming modelValue does not match. This keeps the UI deterministic
// even if a parent passes a stale id.
const activeId = computed<string>(() => {
  const match = props.tabs.find((t) => t.id === props.modelValue);
  if (match && !match.disabled) return match.id;
  const first = props.tabs.find((t) => !t.disabled);
  return first ? first.id : '';
});

const tabIndexFor = (id: string): 0 | -1 => (id === activeId.value ? 0 : -1);

function selectTab(id: string, tab: SidePanelTab): void {
  if (tab.disabled) return;
  if (id === activeId.value) return;
  emit('update:modelValue', id);
}

function focusTab(id: string): void {
  void nextTick(() => {
    // CSS.escape is not always present in jsdom; degrade gracefully.
    const escape = (s: string): string => {
      const cssEscape = (globalThis as { CSS?: { escape?: (v: string) => string } })
        .CSS?.escape;
      return cssEscape ? cssEscape(s) : s.replace(/(["\\])/g, '\\$1');
    };
    const el = tablistEl.value?.querySelector<HTMLElement>(
      `[data-tab-id="${escape(id)}"]`,
    );
    el?.focus();
  });
}

function move(delta: 1 | -1, from: string): void {
  const n = props.tabs.length;
  if (n === 0) return;
  const idx = props.tabs.findIndex((t) => t.id === from);
  if (idx < 0) return;
  for (let step = 1; step <= n; step += 1) {
    const next = (idx + delta * step + n) % n;
    const candidate = props.tabs[next];
    if (candidate && !candidate.disabled) {
      emit('update:modelValue', candidate.id);
      focusTab(candidate.id);
      return;
    }
  }
}

function onKeydown(ev: KeyboardEvent, current: SidePanelTab): void {
  switch (ev.key) {
    case 'ArrowRight':
      ev.preventDefault();
      move(1, current.id);
      break;
    case 'ArrowLeft':
      ev.preventDefault();
      move(-1, current.id);
      break;
    case 'Home':
      ev.preventDefault();
      {
        const first = props.tabs.find((t) => !t.disabled);
        if (first) {
          emit('update:modelValue', first.id);
          focusTab(first.id);
        }
      }
      break;
    case 'End':
      ev.preventDefault();
      {
        const last = [...props.tabs].reverse().find((t) => !t.disabled);
        if (last) {
          emit('update:modelValue', last.id);
          focusTab(last.id);
        }
      }
      break;
    case 'Enter':
    case ' ':
      ev.preventDefault();
      selectTab(current.id, current);
      break;
  }
}

// Keep DOM focus in sync if the parent mutates modelValue programmatically.
watch(
  () => props.modelValue,
  () => {
    // No-op: watchers above already keep activeId derived; focus is moved
    // explicitly via move()/Home/End.
  },
);
</script>

<template>
  <div
    ref="tablistEl"
    class="side-panel-tabs"
    data-testid="side-panel-tabs"
    :data-active="activeId"
    role="tablist"
    :aria-label="ariaLabel"
  >
    <button
      v-for="tab in tabs"
      :key="tab.id"
      type="button"
      role="tab"
      class="side-panel-tab"
      :class="{
        'is-active': tab.id === activeId,
        'is-disabled': !!tab.disabled,
      }"
      :data-testid="`side-panel-tab-${tab.id}`"
      :data-tab-id="tab.id"
      :data-active="tab.id === activeId ? 'true' : 'false'"
      :aria-selected="tab.id === activeId ? 'true' : 'false'"
      :aria-controls="`side-panel-pane-${tab.id}`"
      :aria-disabled="tab.disabled ? 'true' : 'false'"
      :tabindex="tabIndexFor(tab.id)"
      :title="tab.label"
      @click="selectTab(tab.id, tab)"
      @keydown="onKeydown($event, tab)"
    >
      <span
        v-if="tab.icon"
        class="side-panel-tab-icon"
        aria-hidden="true"
      >{{ tab.icon }}</span>
      <span class="side-panel-tab-label">{{ tab.label }}</span>
      <span
        v-if="typeof tab.badge === 'number' && tab.badge > 0"
        class="side-panel-tab-badge"
        :data-testid="`side-panel-tab-badge-${tab.id}`"
        :aria-label="`${tab.badge} 项`"
      >{{ tab.badge > 99 ? '99+' : tab.badge }}</span>
    </button>
  </div>
</template>

<style scoped>
.side-panel-tabs {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 4px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-family: var(--font-body);
}

.side-panel-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 12px;
  border-radius: 6px;
  background: transparent;
  color: var(--fg-2);
  border: 1px solid transparent;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  white-space: nowrap;
  transition:
    background 120ms ease,
    color 120ms ease,
    border-color 120ms ease;
}

.side-panel-tab:hover {
  background: var(--surface-2);
  color: var(--fg);
}

.side-panel-tab:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.side-panel-tab.is-active {
  background: var(--surface-3);
  color: var(--fg);
  border-color: var(--border-2);
}

.side-panel-tab.is-active:hover {
  background: var(--surface-3);
}

.side-panel-tab.is-disabled {
  color: var(--fg-3);
  cursor: not-allowed;
  opacity: 0.6;
}

.side-panel-tab-icon {
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1;
}

.side-panel-tab-label {
  letter-spacing: 0.01em;
}

.side-panel-tab-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  border-radius: 9px;
  background: var(--accent);
  color: var(--bg);
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
}

.side-panel-tab.is-disabled .side-panel-tab-badge {
  background: var(--fg-3);
  color: var(--bg);
}
</style>
