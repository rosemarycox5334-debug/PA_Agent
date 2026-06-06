<script setup lang="ts">
/**
 * Root shell. Hosts the top header, side tab navigation, and the router-view
 * that renders the active view. State for the app lives in the dedicated
 * stores under src/stores/ — this component only wires lifecycle hooks and
 * exposes the tab list to the router.
 */
import { onMounted, ref } from 'vue';
import { settingsStore } from '@/stores/settings';

const tabs = ref<Array<{ id: string; label: string; icon: string }>>([
  { id: 'terminal', label: '交易终端', icon: '📊' },
  { id: 'decision', label: '决策面板', icon: '🎯' },
  { id: 'records', label: '历史记录', icon: '🗂' },
  { id: 'settings', label: '设置', icon: '⚙' },
]);

const currentTab = ref<string>('terminal');

onMounted(async () => {
  // Best-effort initial settings fetch — failures are non-fatal since the
  // settings modal can recover the connection on its own.
  try {
    await settingsStore.refresh();
  } catch (err) {
    console.warn('[App] initial settings refresh failed:', err);
  }
});
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="brand-wrap">
        <span class="status-dot" :class="settingsStore.statusDotClass" />
        <div class="brand">PA Agent</div>
      </div>
      <div class="brand-subtitle">Trading Terminal</div>
      <span class="pill green">{{ settingsStore.modelLabel }} ▾</span>
      <span class="pill" @click="currentTab = 'settings'" style="cursor: pointer;">
        ⚙ 设置
      </span>
    </header>

    <nav class="tab-bar" aria-label="主导航">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        type="button"
        class="tab-button"
        :class="{ active: currentTab === tab.id }"
        @click="currentTab = tab.id"
      >
        <span class="tab-icon">{{ tab.icon }}</span>
        <span>{{ tab.label }}</span>
      </button>
    </nav>

    <main class="app-main">
      <component :is="currentTab" v-if="false" />
      <!-- RouterView reserved for future per-route layouts -->
      <router-view v-slot="{ Component }">
        <component :is="Component" :tab="currentTab" />
      </router-view>
    </main>

    <footer class="status-bar">
      <span>
        <span class="status-dot" :class="settingsStore.statusDotClass" />
        {{ settingsStore.statusText }}
      </span>
      <span class="status-right">
        <span class="pill">{{ settingsStore.state.last_data_source || '--' }}</span>
        <span style="margin-left: 12px;">上下文</span>
        <span class="progress" :title="settingsStore.tokenText">
          <span class="progress-track">
            <span
              class="progress-fill"
              :class="settingsStore.progressClass"
              :style="{ width: settingsStore.tokenPct + '%' }"
            />
          </span>
          <span>{{ settingsStore.tokenText }}</span>
        </span>
      </span>
    </footer>
  </div>
</template>

<style scoped>
.app-shell {
  min-width: 1180px;
  min-height: 100vh;
  display: grid;
  grid-template-rows: 44px 44px 1fr 28px;
  background: var(--bg);
  color: var(--fg);
}
.app-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-1);
}
.brand-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.brand-subtitle {
  color: var(--fg-2);
  font-size: 12px;
  margin-right: auto;
}
.tab-bar {
  display: flex;
  gap: 4px;
  padding: 0 16px;
  align-items: center;
  background: var(--surface-1);
  border-bottom: 1px solid var(--border);
}
.tab-button {
  height: 32px;
  padding: 0 12px;
  background: transparent;
  border: none;
  color: var(--fg-2);
  font-size: 12px;
  font-family: var(--font-body);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 6px;
}
.tab-button:hover {
  background: var(--surface-2);
  color: var(--fg);
}
.tab-button.active {
  background: var(--surface-3);
  color: var(--fg);
}
.tab-icon {
  font-size: 14px;
}
.app-main {
  padding: 16px;
  overflow: auto;
  background: var(--bg);
}
.status-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  background: var(--surface-1);
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--fg-2);
}
.status-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.progress {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 11px;
}
.progress-track {
  width: 120px;
  height: 6px;
  background: var(--surface-3);
  border-radius: 3px;
  overflow: hidden;
  display: inline-block;
}
.progress-fill {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.2s ease;
}
.progress-fill.warn {
  background: var(--warning);
}
.progress-fill.danger {
  background: var(--danger);
}
</style>
