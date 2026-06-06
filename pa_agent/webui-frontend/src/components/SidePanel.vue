<script setup lang="ts">
/**
 * SidePanel — right-hand side panel container for the trading terminal.
 *
 * Composes:
 *   - <SidePanelTabs>  → tab strip driving the active pane (controlled via v-model)
 *   - tab panes        → rendered through named slots keyed by tab id; only the
 *                        active pane is visible at a time.
 *
 * Contract:
 *   <SidePanel
 *     v-model="activeTab"
 *     :tabs="[
 *       { id: 'stream',  label: 'AI 流' },
 *       { id: 'debug',   label: '调试', badge: 3 },
 *       { id: 'history', label: '历史' },
 *     ]"
 *     aria-label="侧栏视图"
 *   >
 *     <template #stream><AIStreamPanel /></template>
 *     <template #debug>…</template>
 *     <template #history>…</template>
 *   </SidePanel>
 *
 *   - `modelValue` is the id of the currently active tab.
 *   - `tabs` is forwarded to <SidePanelTabs>; the panel itself never
 *     mutates the order or composition of tabs.
 *   - Slots that match a tab id are rendered as `role="tabpanel"` panels;
 *     only the active one is visible (`hidden` attribute on the rest).
 *   - The container is fully controlled: clicking a tab emits
 *     `update:modelValue` and the parent decides what the new id is.
 *
 * Tokens used (all from src/styles/tokens.css):
 *   --bg, --surface-1, --surface-2, --surface-3, --surface-4,
 *   --fg, --fg-2, --fg-3,
 *   --border, --border-2,
 *   --accent, --accent-2, --accent-3,
 *   --success, --danger, --warning, --info,
 *   --font-body, --font-mono
 */
import { computed, useSlots } from 'vue';
import SidePanelTabs, { type SidePanelTab } from './SidePanelTabs.vue';

const props = withDefaults(
  defineProps<{
    tabs: SidePanelTab[];
    modelValue: string;
    ariaLabel?: string;
    /** When true, the panel renders a "loading" overlay instead of the active pane. */
    loading?: boolean;
  }>(),
  { ariaLabel: '侧栏视图', loading: false },
);

const emit = defineEmits<{
  (e: 'update:modelValue', id: string): void;
}>();

const slots = useSlots();

/**
 * Pane ids that actually have a matching slot. Pane wrappers still render
 * for every tab id (so the slot contract is uniform), but the wrapper
 * becomes empty when no slot was provided. This keeps ARIA wiring stable
 * even when the parent renders the panel incrementally.
 */
const providedPaneIds = computed<Set<string>>(() => {
  const ids = new Set<string>();
  for (const t of props.tabs) {
    if (slots[t.id]) ids.add(t.id);
  }
  return ids;
});

const activeId = computed<string>(() => {
  const match = props.tabs.find((t) => t.id === props.modelValue);
  if (match && !match.disabled) return match.id;
  const first = props.tabs.find((t) => !t.disabled);
  return first ? first.id : '';
});

function onTabUpdate(id: string): void {
  emit('update:modelValue', id);
}

function paneIsVisible(tabId: string): boolean {
  return tabId === activeId.value;
}
</script>

<template>
  <section
    class="side-panel"
    data-testid="side-panel"
    :data-active="activeId"
    :aria-busy="loading ? 'true' : 'false'"
    aria-label="侧栏面板"
  >
    <header class="side-panel-header" data-testid="side-panel-header">
      <SidePanelTabs
        :tabs="tabs"
        :model-value="modelValue"
        :aria-label="ariaLabel"
        @update:model-value="onTabUpdate"
      />
      <div
        v-if="loading"
        class="side-panel-loading"
        data-testid="side-panel-loading"
        aria-hidden="true"
      >···</div>
    </header>

    <div
      v-for="tab in tabs"
      v-show="paneIsVisible(tab.id)"
      :key="tab.id"
      class="side-panel-pane"
      :data-testid="`side-panel-pane-${tab.id}`"
      :data-active="tab.id === activeId ? 'true' : 'false'"
      :data-provided="providedPaneIds.has(tab.id) ? 'true' : 'false'"
      :id="`side-panel-pane-${tab.id}`"
      role="tabpanel"
      :aria-labelledby="`side-panel-tab-${tab.id}`"
      :hidden="tab.id !== activeId"
    >
      <slot v-if="providedPaneIds.has(tab.id)" :name="tab.id" />
      <p
        v-else
        class="side-panel-empty"
        :data-testid="`side-panel-empty-${tab.id}`"
      >该面板暂无内容</p>
    </div>
  </section>
</template>

<style scoped>
.side-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--surface-1);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-family: var(--font-body);
  overflow: hidden;
}

.side-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--border);
}

.side-panel-loading {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent);
  letter-spacing: 0.05em;
  padding: 0 4px;
}

.side-panel-pane {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 10px 12px;
  background: var(--bg);
}

.side-panel-pane[hidden] {
  display: none;
}

.side-panel-pane[data-active='true'] {
  background: var(--bg);
}

.side-panel-pane[data-provided='false'] {
  display: flex;
  align-items: center;
  justify-content: center;
}

.side-panel-empty {
  margin: 0;
  color: var(--fg-3);
  font-size: 12px;
  font-family: var(--font-mono);
}

.side-panel[data-active=''] .side-panel-pane {
  display: none;
}
</style>
