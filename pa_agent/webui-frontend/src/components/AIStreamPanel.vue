<script setup lang="ts">
/**
 * AIStreamPanel — Vue 3 port of pa_agent/gui/ai_stream_window.py.
 *
 * Renders the live streaming reasoning + content text, the per-stage header,
 * per-stage char stats, a token-usage bar, and a send/cancel + clear control.
 * Reads from `streamStore` and `decisionStore`; emits no events of its own
 * (parent view owns the SSE transport). The component is intentionally
 * presentational so it can be tested in isolation.
 */
import { computed } from 'vue';
import { streamStore, type StreamMessage } from '@/stores/stream';
import { decisionStore } from '@/stores/decision';
import { settingsStore } from '@/stores/settings';

const phaseLabel = computed(() => {
  const flow = decisionStore.flowStep;
  if (decisionStore.error) return `出错 · ${decisionStore.error}`;
  if (decisionStore.analyzing) {
    if (flow <= 0) return '阶段-1 · 预热中…';
    if (flow === 1) return '阶段一 · 市场诊断中…';
    if (flow === 2) return '阶段二 · 交易决策中…';
    return '收尾中…';
  }
  if (decisionStore.decision) return '分析完成';
  return '等待分析…';
});

const stageOneCount = computed(
  () => streamStore.messages.filter((m: StreamMessage) => m.stage === '1').length,
);
const stageTwoCount = computed(
  () => streamStore.messages.filter((m: StreamMessage) => m.stage === '2').length,
);
const followupCount = computed(
  () => streamStore.messages.filter((m: StreamMessage) => m.stage === 'followup').length,
);

function onCancel(): void {
  streamStore.cancel();
}

function onClear(): void {
  streamStore.reset();
  decisionStore.reset();
}
</script>

<template>
  <section class="ai-stream-panel" data-testid="ai-stream-panel">
    <header class="phase-header" data-testid="phase-header">
      <span class="phase-label" data-testid="phase-label">{{ phaseLabel }}</span>
      <span class="phase-stats" data-testid="phase-stats">
        阶段一 {{ stageOneCount }} · 阶段二 {{ stageTwoCount }} · 追问 {{ followupCount }}
      </span>
    </header>

    <div class="reasoning" data-testid="reasoning-pane">
      <pre class="reasoning-text" data-testid="reasoning-text">{{ decisionStore.reasoningText || '（暂无推理输出）' }}</pre>
    </div>

    <div class="messages" data-testid="message-list">
      <article
        v-for="(msg, idx) in streamStore.messages"
        :key="`${msg.time}-${idx}`"
        class="message"
        :data-stage="msg.stage ?? ''"
        :data-testid="`message-${idx}`"
      >
        <h4 class="message-title">{{ msg.title }}</h4>
        <time class="message-time">{{ msg.time }}</time>
        <pre class="message-body">{{ msg.text }}</pre>
      </article>
    </div>

    <footer class="controls" data-testid="controls">
      <div class="token-bar" data-testid="token-bar">
        <span class="token-label">上下文</span>
        <div class="token-progress" :data-state="settingsStore.tokenPct >= 80 ? 'danger' : settingsStore.tokenPct >= 60 ? 'warn' : 'ok'">
          <div class="token-progress-fill" :style="{ width: `${Math.min(100, Math.max(0, settingsStore.tokenPct))}%` }" />
        </div>
        <span class="token-text" data-testid="token-text">{{ settingsStore.tokenText }}</span>
      </div>
      <div class="buttons">
        <button
          type="button"
          class="btn-primary"
          data-testid="cancel-btn"
          :disabled="!streamStore.active"
          @click="onCancel"
        >
          停止
        </button>
        <button type="button" class="btn-secondary" data-testid="clear-btn" @click="onClear">
          清空
        </button>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.ai-stream-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  min-height: 0;
  background: var(--surface-1);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  font-family: var(--font-body);
}

.phase-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

.phase-label {
  font-weight: 600;
  font-size: 13px;
  color: var(--fg);
}

.phase-stats {
  font-size: 11px;
  color: var(--fg-3);
  font-family: var(--font-mono);
}

.reasoning {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
}

.reasoning-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  color: var(--fg);
}

.messages {
  max-height: 180px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.message {
  background: var(--surface-2);
  border: 1px solid var(--border-2);
  border-radius: 6px;
  padding: 8px 10px;
}

.message[data-stage='1'] {
  border-left: 3px solid var(--chart-line);
}
.message[data-stage='2'] {
  border-left: 3px solid var(--accent);
}
.message[data-stage='followup'] {
  border-left: 3px solid var(--accent-3);
}

.message-title {
  margin: 0 0 2px 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--fg);
}

.message-time {
  font-size: 10px;
  color: var(--fg-3);
  font-family: var(--font-mono);
  margin-left: 6px;
}

.message-body {
  margin: 4px 0 0 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--fg-2);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 80px;
  overflow: auto;
}

.controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}

.token-bar {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.token-label {
  font-size: 11px;
  color: var(--fg-3);
}

.token-progress {
  flex: 1;
  height: 8px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 999px;
  overflow: hidden;
}

.token-progress-fill {
  height: 100%;
  background: var(--accent);
  transition: width 200ms ease;
}

.token-progress[data-state='warn'] .token-progress-fill {
  background: var(--warning);
}
.token-progress[data-state='danger'] .token-progress-fill {
  background: var(--danger);
}

.token-text {
  font-size: 11px;
  color: var(--fg-2);
  font-family: var(--font-mono);
}

.buttons {
  display: inline-flex;
  gap: 6px;
}

.btn-primary,
.btn-secondary {
  height: 30px;
  padding: 0 12px;
  border-radius: 6px;
  border: 1px solid var(--border-2);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}

.btn-primary {
  background: var(--accent);
  color: #06210d;
  border-color: var(--accent);
  font-weight: 600;
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  background: var(--surface-2);
  color: var(--fg-3);
  border-color: var(--border-2);
}

.btn-secondary {
  background: var(--surface-2);
  color: var(--fg);
}
</style>
