<script setup lang="ts">
/**
 * AppHeader — top chrome bar. Pure presentational; reads all state from
 * the reactive stores. Composed of:
 *  - brand mark (PA Agent + status dot)
 *  - model + data-source pills (settingsStore)
 *  - AutoIncrementalBadge (auto-incremental toggle)
 *  - DemoModeLauncher (demo mode toggle)
 *  - settings shortcut (opens the settings modal)
 *  - sidebar collapse toggle
 *
 * Tokens used (all from src/styles/tokens.css):
 *  --bg, --surface-1, --surface-2, --surface-3, --fg, --fg-2, --fg-3,
 *  --border, --accent, --success, --danger, --warning, --info,
 *  --font-body, --font-mono
 */
import { computed } from 'vue';
import { settingsStore } from '@/stores/settings';
import { uiStore } from '@/stores/ui';
import AutoIncrementalBadge from './AutoIncrementalBadge.vue';
import DemoModeLauncher from './DemoModeLauncher.vue';

const modelLabel = computed(() => settingsStore.state.provider_model || '未配置模型');
const dataSource = computed(
  () => settingsStore.state.last_data_source || '--',
);
const sidebarOpen = computed(() => uiStore.sidebarOpen);

function openSettings(): void {
  uiStore.openModal('settings');
}

function toggleSidebar(): void {
  uiStore.toggleSidebar();
}
</script>

<template>
  <header
    class="app-header"
    data-testid="app-header"
    :data-sidebar-open="sidebarOpen ? 'true' : 'false'"
  >
    <div class="brand-wrap" data-testid="app-header-brand">
      <span
        class="status-dot"
        data-testid="app-header-status-dot"
        :class="settingsStore.statusDotClass"
        aria-hidden="true"
      />
      <div class="brand">
        <span class="brand-title">PA Agent</span>
        <span class="brand-subtitle">Trading Terminal</span>
      </div>
    </div>

    <div class="pill-row" data-testid="app-header-pill-row">
      <span
        class="pill"
        data-testid="app-header-model-pill"
        :title="`模型: ${modelLabel}`"
      >
        {{ modelLabel }}
      </span>
      <span
        class="pill"
        data-testid="app-header-source-pill"
        :title="`数据源: ${dataSource}`"
      >
        {{ dataSource }}
      </span>
    </div>

    <div class="actions" data-testid="app-header-actions">
      <AutoIncrementalBadge />
      <DemoModeLauncher />
      <button
        type="button"
        class="pill pill-button"
        data-testid="app-header-settings-btn"
        title="打开设置"
        @click="openSettings"
      >
        <span aria-hidden="true">⚙</span>
        <span class="sr-only">设置</span>
      </button>
      <button
        type="button"
        class="pill pill-button"
        data-testid="app-header-sidebar-toggle"
        :title="sidebarOpen ? '折叠侧栏' : '展开侧栏'"
        :aria-expanded="sidebarOpen ? 'true' : 'false'"
        @click="toggleSidebar"
      >
        <span aria-hidden="true">{{ sidebarOpen ? '«' : '»' }}</span>
        <span class="sr-only">侧栏</span>
      </button>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 44px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-1);
  color: var(--fg);
  font-family: var(--font-body);
}
.brand-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fg-3);
  flex-shrink: 0;
}
.status-dot.online { background: var(--success); }
.status-dot.offline { background: var(--danger); }
.status-dot.unknown { background: var(--fg-3); }

.brand {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}
.brand-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--fg);
}
.brand-subtitle {
  font-size: 11px;
  color: var(--fg-2);
  font-family: var(--font-mono);
  white-space: nowrap;
}

.pill-row {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-right: auto;
  flex-wrap: nowrap;
  min-width: 0;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 10px;
  border-radius: 13px;
  background: var(--surface-2);
  color: var(--fg-2);
  font-size: 12px;
  border: 1px solid var(--border);
  white-space: nowrap;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pill-button {
  cursor: pointer;
  transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
}
.pill-button:hover {
  background: var(--surface-3);
  color: var(--fg);
}

.actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
