<script setup lang="ts">
/**
 * Read-only display of the latest Stage-2 decision, reasoning text, merged
 * gate/decision trace, prompt files, and raw JSON payload.
 */
import { decisionStore, decisionPillClass, mergedTrace, promptFiles } from '@/stores/decision';

type TraceItem = {
  phase?: 'gate' | 'decision';
  node_id?: string;
  answer?: string;
  bar_range?: string;
  reason?: string;
};

function formatPrice(value: unknown): string {
  if (value === null || value === undefined || value === '' || value === '--') return '--';
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const decimals = Math.abs(num) >= 1000 ? 2 : 4;
  return num.toFixed(decimals).replace(/\.?0+$/, '');
}

function answerClass(ans: unknown): string {
  const answer = String(ans ?? '').split('，')[0];
  if (answer === '是') return 'trace-answer-yes';
  if (answer === '否') return 'trace-answer-no';
  return 'trace-answer-neutral';
}

function traceItem(item: Record<string, unknown>): TraceItem {
  return item as TraceItem;
}
</script>

<template>
  <div class="decision-view">
    <section class="section">
      <div class="section-title">
        <span>决策摘要</span>
        <span v-if="decisionStore.decision" class="confidence-badge" :class="decisionPillClass">
          {{ decisionStore.decision.order_direction || 'Wait' }}
          <span v-if="decisionStore.decision.confidence" class="badge-pct">
            {{ decisionStore.decision.confidence }}%
          </span>
        </span>
        <span v-else class="pill" :class="decisionPillClass">
          Wait
        </span>
      </div>
      <div v-if="decisionStore.decision" class="decision-list">
        <div class="decision-row">
          <span class="key">订单类型</span>
          <span class="val">{{ decisionStore.decision.order_type }}</span>
        </div>
        <div class="decision-row">
          <span class="key">交易方向</span>
          <span class="val" :class="decisionPillClass">
            {{ decisionStore.decision.order_direction }}
          </span>
        </div>
        <div class="decision-row">
          <span class="key">入场价格</span>
          <span class="val">{{ formatPrice(decisionStore.decision.entry_price) }}</span>
        </div>
        <div class="decision-row">
          <span class="key">止盈价格</span>
          <span class="val up">{{ formatPrice(decisionStore.decision.take_profit_price) }}</span>
        </div>
        <div class="decision-row">
          <span class="key">止损价格</span>
          <span class="val down">{{ formatPrice(decisionStore.decision.stop_loss_price) }}</span>
        </div>
      </div>
      <div v-else class="empty">暂无决策，请先在交易终端提交一次分析</div>
    </section>

    <section class="section">
      <div class="section-title">
        <span>分析过程</span>
        <span class="pill blue">AI</span>
      </div>
      <ul v-if="decisionStore.reasoningLines.length" class="reasoning-list">
        <li v-for="(line, i) in decisionStore.reasoningLines" :key="i">{{ line }}</li>
      </ul>
      <div v-else class="empty">暂无推理文本</div>
    </section>

    <section class="section">
      <div class="section-title">
        <span>路径回放</span>
      </div>
      <table v-if="mergedTrace.length" class="trace-table">
        <thead>
          <tr>
            <th>步</th>
            <th>阶段</th>
            <th>节点</th>
            <th>回答</th>
            <th>K线依据</th>
            <th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, i) in mergedTrace" :key="i">
            <td>{{ i + 1 }}</td>
            <td>{{ traceItem(item).phase === 'gate' ? '闸门' : '策略' }}</td>
            <td>{{ traceItem(item).node_id || '--' }}</td>
            <td :class="answerClass(traceItem(item).answer)">
              {{ traceItem(item).answer || '--' }}
            </td>
            <td>{{ traceItem(item).bar_range || '--' }}</td>
            <td>{{ traceItem(item).reason || '--' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无决策树数据</div>
    </section>

    <section class="section">
      <div class="section-title">
        <span>提示词文件</span>
      </div>
      <ul v-if="promptFiles.length" class="prompt-list">
        <li v-for="(f, i) in promptFiles" :key="i" class="prompt-item">
          <span class="prompt-index">{{ i + 1 }}</span>
          <span class="prompt-name">{{ f }}</span>
        </li>
      </ul>
      <div v-else class="empty">暂无提示词数据</div>
    </section>

    <section class="section">
      <div class="section-title"><span>原始 JSON</span></div>
      <pre class="code-block">{{ decisionStore.rawJson }}</pre>
    </section>
  </div>
</template>

<style scoped>
.decision-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
  overflow: auto;
}
.section {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
}
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  color: var(--fg);
  margin-bottom: 10px;
}
.decision-list {
  display: grid;
  gap: 6px;
}
.decision-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 6px 10px;
  background: var(--surface-2);
  border-radius: 6px;
}
.decision-row .key {
  color: var(--fg-2);
}
.decision-row .val {
  font-family: var(--font-mono);
}
.val.up {
  color: var(--chart-up);
}
.val.down {
  color: var(--chart-down);
}
.empty {
  color: var(--fg-3);
  font-size: 12px;
  padding: 12px;
}
.reasoning-list {
  list-style: none;
  display: grid;
  gap: 6px;
  font-size: 12px;
  color: var(--fg-2);
  max-height: 280px;
  overflow: auto;
}
.reasoning-list li {
  padding: 6px 8px;
  background: var(--surface-2);
  border-radius: 6px;
  font-family: var(--font-mono);
}
.trace-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.trace-table th,
.trace-table td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}
.trace-table th {
  color: var(--fg-3);
  font-weight: 500;
}
.trace-answer-yes {
  color: var(--chart-up);
}
.trace-answer-no {
  color: var(--chart-down);
}
.trace-answer-neutral {
  color: var(--fg-2);
}
.prompt-list {
  list-style: none;
  display: grid;
  gap: 4px;
}
.prompt-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 8px;
  background: var(--surface-2);
  border-radius: 6px;
  font-size: 12px;
}
.prompt-index {
  width: 20px;
  height: 20px;
  display: inline-grid;
  place-items: center;
  background: var(--surface-3);
  border-radius: 4px;
  font-family: var(--font-mono);
  color: var(--fg-3);
}
.code-block {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--fg-2);
  overflow: auto;
  max-height: 360px;
}
</style>
