<script setup lang="ts">
/**
 * Records view — list and replay historical analysis records. Mirrors the
 * "演示" tab in the legacy build.
 */
import { onMounted, ref } from 'vue';
import { api, type RecordListItem } from '@/api/client';
import { frameStore } from '@/stores/frame';
import { decisionStore } from '@/stores/decision';

const records = ref<RecordListItem[]>([]);
const loading = ref(false);
const current = ref<Record<string, unknown> | null>(null);

async function refresh(): Promise<void> {
  loading.value = true;
  try {
    const data = await api.fetchRecords();
    records.value = data.records ?? [];
  } catch (err) {
    console.error('[records] refresh failed:', err);
  } finally {
    loading.value = false;
  }
}

async function load(filename: string): Promise<void> {
  loading.value = true;
  try {
    const data = await api.fetchRecord(filename);
    current.value = data;
    const kline = data['kline_data'] as Array<Record<string, unknown>> | undefined;
    const meta = data['meta'] as { symbol?: string; timeframe?: string } | undefined;
    if (kline && meta) {
      frameStore.setSnapshot({
        symbol: meta.symbol ?? '',
        timeframe: meta.timeframe ?? '',
        bars: kline as never,
        indicators: (data['kline_indicators'] as never) ?? {},
      });
    }
    const decision = data['stage2_decision'];
    if (decision) {
      decisionStore.setDecision(decision as never);
    }
  } catch (err) {
    console.error('[records] load failed:', err);
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <div class="records-view">
    <div class="toolbar">
      <button class="button secondary" type="button" :disabled="loading" @click="refresh">
        {{ loading ? '加载中...' : '刷新列表' }}
      </button>
      <span class="muted">共 {{ records.length }} 条记录</span>
    </div>
    <div v-if="records.length" class="records-list">
      <div
        v-for="rec in records"
        :key="rec.filename"
        class="record-item"
        :class="{ active: current?.['filename'] === rec.filename }"
        @click="load(rec.filename)"
      >
        <span class="time">{{ rec.timestamp?.slice(0, 16)?.replace('T', ' ') }}</span>
        <span class="symbol">{{ rec.symbol }} · {{ rec.timeframe }}</span>
        <span class="bars">{{ rec.bar_count }} 根</span>
      </div>
    </div>
    <div v-else class="empty">{{ loading ? '加载中...' : '暂无记录' }}</div>
  </div>
</template>

<style scoped>
.records-view {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  overflow: auto;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.muted {
  color: var(--fg-3);
  font-size: 12px;
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
.records-list {
  display: grid;
  gap: 6px;
}
.record-item {
  display: grid;
  grid-template-columns: 160px 1fr 80px;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.record-item:hover {
  background: var(--surface-2);
}
.record-item.active {
  border-color: var(--accent);
}
.record-item .time {
  font-family: var(--font-mono);
  color: var(--fg-2);
}
.record-item .symbol {
  color: var(--fg);
}
.record-item .bars {
  color: var(--fg-3);
  font-family: var(--font-mono);
  text-align: right;
}
.empty {
  color: var(--fg-3);
  font-size: 12px;
  padding: 24px;
  text-align: center;
}
</style>
