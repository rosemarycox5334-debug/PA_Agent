<script setup lang="ts">
/**
 * Terminal view — toolbar + chart + AI panel. Mirrors the layout of the
 * legacy index.html but is backed by the new typed stores. The chart
 * itself is intentionally minimal; the full canvas implementation lives
 * in a dedicated component to keep this view focused on composition.
 */
import { onMounted, onUnmounted, ref } from 'vue';
import { settingsStore } from '@/stores/settings';
import { frameStore, barCount, lastClose, lastEma20, formingCountdown } from '@/stores/frame';
import { decisionStore } from '@/stores/decision';
import { streamStore } from '@/stores/stream';

const isLive = ref(true);
const followupText = ref('');

let ledgerInterval: number | null = null;
let nowTimer: number | null = null;

async function fetchData(): Promise<void> {
  try {
    await settingsStore.save();
    await frameStore.refresh();
  } catch (err) {
    console.error('[terminal] fetchData failed:', err);
  }
}

async function submitAnalysis(): Promise<void> {
  try {
    await streamStore.submitAnalysis({ incremental: false });
  } catch (err) {
    console.error('[terminal] submitAnalysis failed:', err);
  }
}

async function submitIncremental(): Promise<void> {
  try {
    await streamStore.submitAnalysis({
      incremental: true,
      incrementalNewBars: settingsStore.state.incremental_max_new_bars || 10,
    });
  } catch (err) {
    console.error('[terminal] submitIncremental failed:', err);
  }
}

function toggleLive(): void {
  isLive.value = !isLive.value;
  if (isLive.value) {
    frameStore.startLive(() => {
      /* chart re-renders via reactive */
    });
  } else {
    frameStore.stopLive();
  }
}

async function sendFollowup(): Promise<void> {
  if (!followupText.value.trim()) return;
  const text = followupText.value;
  followupText.value = '';
  try {
    await streamStore.submitFollowup(text);
  } catch (err) {
    console.error('[terminal] followup failed:', err);
  }
}

onMounted(() => {
  if (isLive.value) {
    frameStore.startLive(() => undefined);
  }
  // Poll the ledger for context usage.
  async function tick(): Promise<void> {
    try {
      const { api } = await import('@/api/client');
      const data = await api.fetchLedger();
      settingsStore.updateTokenUsage(
        data.context_pct ?? 0,
        data.context_used ?? 0,
        data.context_window ?? 2_000_000,
      );
    } catch {
      // Non-fatal — keep the existing values.
    }
  }
  void tick();
  ledgerInterval = window.setInterval(tick, 2000);
  nowTimer = window.setInterval(() => {
    // Touch the reactive computed so countdown re-evaluates.
    void formingCountdown.value;
  }, 1000);
});

onUnmounted(() => {
  frameStore.stopLive();
  if (ledgerInterval) window.clearInterval(ledgerInterval);
  if (nowTimer) window.clearInterval(nowTimer);
  streamStore.cancel();
});
</script>

<template>
  <section class="workspace">
    <div class="toolbar">
      <div class="tool-group">
        <div class="field">
          <label>品种</label>
          <div class="select-like">
            <input v-model="settingsStore.state.last_symbol" placeholder="SA2609" />
          </div>
        </div>
        <div class="field">
          <label>周期</label>
          <div class="select-like">
            <input v-model="settingsStore.state.last_timeframe" placeholder="1h" />
          </div>
        </div>
        <button class="button secondary" type="button" @click="fetchData">获取数据</button>
      </div>
      <div class="tool-group" style="justify-content: flex-end;">
        <button
          class="button primary"
          type="button"
          :disabled="decisionStore.analyzing"
          @click="submitAnalysis"
        >
          {{ decisionStore.analyzing ? '分析中...' : '提交分析' }}
        </button>
        <button
          class="button secondary"
          type="button"
          :disabled="decisionStore.analyzing"
          @click="submitIncremental"
        >
          增量分析
        </button>
        <button class="button secondary" type="button" @click="toggleLive">
          {{ isLive ? '暂停更新' : '实时更新' }}
        </button>
      </div>
    </div>

    <div class="terminal-grid">
      <div class="chart-panel">
        <div class="panel-titlebar">
          <span class="panel-title">
            {{ frameStore.snapshot?.symbol || '---' }} ·
            {{ frameStore.snapshot?.timeframe || '--' }}
          </span>
          <span class="panel-meta">{{ barCount }} 根K线 · EMA20</span>
        </div>
        <div class="chart-summary">
          <div>Price {{ lastClose ?? '--' }}</div>
          <div>EMA20 {{ lastEma20 ?? '--' }}</div>
          <div v-if="!frameStore.snapshot?.bars?.[0]?.closed" class="form">
            ⏱ {{ formingCountdown.text }}
          </div>
        </div>
      </div>

      <div class="followup">
        <input v-model="followupText" placeholder="追问当前分析..." @keyup.enter="sendFollowup" />
        <div class="followup-actions">
          <button type="button" class="send" @click="sendFollowup">发送</button>
          <button type="button" @click="followupText = ''">清空</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.workspace {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
  height: 100%;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.tool-group {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.field {
  display: inline-flex;
  flex-direction: column;
  gap: 2px;
}
.field label {
  font-size: 11px;
  color: var(--fg-3);
}
.select-like {
  display: inline-flex;
  align-items: center;
  padding: 0 10px;
  height: 32px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 12px;
  min-width: 100px;
}
.select-like input {
  background: transparent;
  border: none;
  outline: none;
  width: 100%;
  color: inherit;
  font: inherit;
}
.button {
  height: 32px;
  padding: 0 12px;
  border-radius: 6px;
  border: 1px solid var(--border-2);
  background: var(--surface-2);
  color: var(--fg);
  cursor: pointer;
  font-size: 12px;
}
.button.primary {
  background: var(--accent);
  color: #06210d;
  border-color: var(--accent);
  font-weight: 600;
}
.button.secondary {
  background: var(--surface-2);
}
.button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.terminal-grid {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 12px;
  flex: 1;
  min-height: 0;
}
.chart-panel {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  min-height: 360px;
}
.panel-titlebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  margin-bottom: 8px;
  color: var(--fg-2);
}
.panel-title {
  color: var(--fg);
  font-weight: 600;
}
.chart-summary {
  display: flex;
  gap: 16px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--fg-2);
}
.form {
  color: var(--accent);
}
.followup {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.followup input {
  width: 100%;
  height: 36px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0 10px;
  color: var(--fg);
  font-size: 12px;
  outline: none;
}
.followup-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}
.followup-actions button {
  height: 28px;
  padding: 0 12px;
  border-radius: 6px;
  border: 1px solid var(--border-2);
  background: var(--surface-2);
  color: var(--fg);
  cursor: pointer;
  font-size: 12px;
}
.followup-actions .send {
  background: var(--accent);
  color: #06210d;
  border-color: var(--accent);
  font-weight: 600;
}
</style>
