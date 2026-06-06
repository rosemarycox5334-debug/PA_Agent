<script setup lang="ts">
/**
 * Settings view — full editor for the settings store. Save action calls
 * settingsStore.save(), which posts the partial payload to /api/settings.
 */
import { ref } from 'vue';
import { settingsStore } from '@/stores/settings';

const showApiKey = ref(false);
const showRqKey = ref(false);
const saving = ref(false);
const message = ref<{ kind: 'ok' | 'warn'; text: string } | null>(null);

async function save(): Promise<void> {
  saving.value = true;
  message.value = null;
  try {
    await settingsStore.save();
    message.value = { kind: 'ok', text: '已保存' };
  } catch (err) {
    message.value = {
      kind: 'warn',
      text: err instanceof Error ? err.message : String(err),
    };
  } finally {
    saving.value = false;
  }
}

async function reload(): Promise<void> {
  saving.value = true;
  try {
    await settingsStore.refresh();
    message.value = { kind: 'ok', text: '已重新加载' };
  } catch (err) {
    message.value = {
      kind: 'warn',
      text: err instanceof Error ? err.message : String(err),
    };
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="settings-view">
    <div class="card">
      <h3>AI 提供商</h3>
      <div class="grid">
        <div class="field">
          <label>模型</label>
          <input v-model="settingsStore.state.provider_model" />
        </div>
        <div class="field">
          <label>Base URL</label>
          <input v-model="settingsStore.state.provider_base_url" />
        </div>
        <div class="field">
          <label>API Key</label>
          <div class="row">
            <input
              :type="showApiKey ? 'text' : 'password'"
              v-model="settingsStore.state.provider_api_key"
              placeholder="*** 表示不修改"
            />
            <button type="button" @click="showApiKey = !showApiKey">
              {{ showApiKey ? '隐藏' : '显示' }}
            </button>
          </div>
        </div>
        <div class="field row">
          <label>启用 Thinking</label>
          <input
            type="checkbox"
            :checked="settingsStore.state.provider_thinking"
            @change="settingsStore.state.provider_thinking = ($event.target as HTMLInputElement).checked"
          />
        </div>
        <div class="field">
          <label>Reasoning Effort</label>
          <select v-model="settingsStore.state.provider_reasoning_effort">
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
            <option value="max">max</option>
          </select>
        </div>
        <div class="field">
          <label>Context Window</label>
          <input
            type="number"
            v-model.number="settingsStore.state.provider_context_window"
            min="1000"
            max="2000000"
            step="1000"
          />
        </div>
      </div>
    </div>

    <div class="card">
      <h3>通用设置</h3>
      <div class="grid">
        <div class="field">
          <label>分析 K 线数</label>
          <input
            type="number"
            v-model.number="settingsStore.state.analysis_bar_count"
            min="2"
            max="5000"
          />
        </div>
        <div class="field">
          <label>刷新间隔 (ms)</label>
          <input
            type="number"
            v-model.number="settingsStore.state.refresh_interval_ms"
            min="100"
            max="10000"
          />
        </div>
        <div class="field row">
          <label>分析后自动恢复图表</label>
          <input
            type="checkbox"
            :checked="settingsStore.state.auto_resume_chart_after_analysis"
            @change="settingsStore.state.auto_resume_chart_after_analysis = ($event.target as HTMLInputElement).checked"
          />
        </div>
        <div class="field">
          <label>上下文警告阈值 (%)</label>
          <input
            type="number"
            v-model.number="settingsStore.state.context_warning_threshold_pct"
            min="1"
            max="100"
          />
        </div>
        <div class="field">
          <label>流式面板字体 (pt)</label>
          <input
            type="number"
            v-model.number="settingsStore.state.stream_pane_font_pt"
            min="8"
            max="28"
          />
        </div>
        <div class="field">
          <label>增量最大新 K 线数</label>
          <input
            type="number"
            v-model.number="settingsStore.state.incremental_max_new_bars"
            min="0"
            max="500"
          />
        </div>
        <div class="field">
          <label>决策立场</label>
          <select v-model="settingsStore.state.decision_stance">
            <option value="conservative">保守 (conservative)</option>
            <option value="balanced">均衡 (balanced)</option>
            <option value="aggressive">激进 (aggressive)</option>
            <option value="extreme_aggressive">极端激进 (extreme_aggressive)</option>
          </select>
        </div>
        <div class="field">
          <label>RQData License Key</label>
          <div class="row">
            <input
              :type="showRqKey ? 'text' : 'password'"
              v-model="settingsStore.state.rqdata_license_key"
              placeholder="*** 表示不修改"
            />
            <button type="button" @click="showRqKey = !showRqKey">
              {{ showRqKey ? '隐藏' : '显示' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>数据源</h3>
      <div class="grid">
        <div class="field">
          <label>品种</label>
          <input v-model="settingsStore.state.last_symbol" />
        </div>
        <div class="field">
          <label>周期</label>
          <input v-model="settingsStore.state.last_timeframe" />
        </div>
        <div class="field">
          <label>数据源</label>
          <select v-model="settingsStore.state.last_data_source">
            <option value="mt5">MT5</option>
            <option value="rqdata">RQData</option>
            <option value="tradingview">TradingView</option>
            <option value="akshare">AKShare</option>
          </select>
        </div>
        <div class="field">
          <label>TradingView 交易所</label>
          <select v-model="settingsStore.state.last_tradingview_exchange">
            <option value="">（自动探测）</option>
            <option value="OANDA">OANDA</option>
            <option value="SSE">SSE</option>
            <option value="SZSE">SZSE</option>
            <option value="HKEX">HKEX</option>
            <option value="SP">SP</option>
            <option value="NYSE">NYSE</option>
            <option value="NASDAQ">NASDAQ</option>
          </select>
        </div>
      </div>
    </div>

    <div class="actions">
      <button class="button secondary" type="button" :disabled="saving" @click="reload">
        重新加载
      </button>
      <button class="button primary" type="button" :disabled="saving" @click="save">
        {{ saving ? '保存中...' : '保存' }}
      </button>
      <span v-if="message" class="message" :class="message.kind">{{ message.text }}</span>
    </div>
  </div>
</template>

<style scoped>
.settings-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
  overflow: auto;
}
.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.card h3 {
  font-size: 13px;
  margin-bottom: 12px;
  color: var(--accent-3);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field.row {
  flex-direction: row;
  align-items: center;
  gap: 8px;
}
.field label {
  font-size: 11px;
  color: var(--fg-3);
}
.field input,
.field select {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  height: 32px;
  padding: 0 10px;
  color: var(--fg);
  font-size: 12px;
  font-family: var(--font-mono);
  outline: none;
}
.row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.row input {
  flex: 1;
}
.row button {
  height: 32px;
  padding: 0 10px;
  border-radius: 6px;
  border: 1px solid var(--border-2);
  background: var(--surface-2);
  color: var(--fg);
  cursor: pointer;
  font-size: 12px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: flex-end;
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
.message {
  font-size: 12px;
  font-family: var(--font-mono);
}
.message.ok {
  color: var(--success);
}
.message.warn {
  color: var(--warning);
}
</style>
