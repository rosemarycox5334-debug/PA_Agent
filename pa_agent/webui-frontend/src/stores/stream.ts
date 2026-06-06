/**
 * Stream store — manages the SSE reader lifecycle and dispatches typed
 * events to the decision store. Decouples transport from view code so
 * follow-up and incremental analysis can share the same plumbing.
 */
import { reactive } from 'vue';
import { api } from '@/api/client';
import { decisionStore, type DecisionPayload } from './decision';
import { settingsStore } from './settings';

export interface StreamMessage {
  title: string;
  text: string;
  time: string;
  stage?: '1' | '2' | 'followup' | 'incremental';
}

export const streamStore = reactive({
  messages: [] as StreamMessage[],
  active: false,
  controller: null as AbortController | null,

  reset(): void {
    this.messages = [];
  },

  push(msg: StreamMessage): void {
    this.messages.push(msg);
  },

  cancel(): void {
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
    this.active = false;
  },

  /**
   * Submit a full or incremental analysis and pipe events into stores.
   * @param incremental when true, requires a prior decision in the store
   */
  async submitAnalysis(opts: {
    incremental?: boolean;
    incrementalNewBars?: number | null;
  } = {}): Promise<void> {
    const { incremental = false, incrementalNewBars = null } = opts;
    if (decisionStore.analyzing) return;
    if (incremental && !decisionStore.decision) {
      throw new Error('请先完成一次完整分析');
    }

    decisionStore.beginRun();
    settingsStore.setAppState('analyzing');

    this.reset();
    this.active = true;
    this.controller = new AbortController();

    try {
      const res = await api.submitAnalysis(
        settingsStore.state.analysis_bar_count || 80,
        settingsStore.state.decision_stance || 'balanced',
        incremental,
        incrementalNewBars,
      );
      // Re-bind the abort signal to the response reader.
      const reader = res.body?.getReader();
      if (!reader) throw new Error('stream response missing body');
      const decoder = new TextDecoder();
      let buffer = '';
      // Read loop
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6)) as Record<string, unknown>;
            this.handleEvent(data, incremental);
          } catch (err) {
            console.warn('[stream] failed to parse SSE line:', err);
          }
        }
      }
    } catch (err) {
      decisionStore.fail(err instanceof Error ? err.message : String(err));
      settingsStore.setAppState('error');
      throw err;
    } finally {
      this.active = false;
      this.controller = null;
    }
  },

  async submitFollowup(text: string): Promise<void> {
    if (!text.trim()) return;
    const stamp = nowStamp();
    try {
      const res = await api.submitFollowup(text.trim());
      const reader = res.body?.getReader();
      if (!reader) throw new Error('followup response missing body');
      const decoder = new TextDecoder();
      let buffer = '';
      let replyText = '';
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6)) as Record<string, unknown>;
            const ev = data.event as string | undefined;
            if (ev === 'followup_reasoning' && typeof data.text === 'string') {
              decisionStore.appendReasoning(data.text);
            } else if (ev === 'followup_content' && typeof data.text === 'string') {
              replyText += data.text;
            } else if (ev === 'followup_reply' && typeof data.content === 'string') {
              replyText = data.content;
            } else if (ev === 'error') {
              console.warn('[stream] followup error:', data.message);
            } else if (ev === 'done') {
              if (replyText) {
                this.push({
                  title: `追问 · ${text.slice(0, 20)}`,
                  text: replyText,
                  time: stamp,
                  stage: 'followup',
                });
              }
            }
          } catch (err) {
            console.warn('[stream] followup parse error:', err);
          }
        }
      }
    } catch (err) {
      console.error('[stream] followup failed:', err);
      throw err;
    }
  },

  handleEvent(data: Record<string, unknown>, incremental: boolean): void {
    const ev = (data.event as string | undefined) ?? '';
    const stamp = nowStamp();
    switch (ev) {
      case 'stage1_started':
      case 'stage1_done':
        decisionStore.flowStep = 2;
        break;
      case 'stage2_started':
      case 'stage2_done':
        decisionStore.flowStep = 3;
        break;
      case 'record_saved':
        decisionStore.flowStep = 4;
        break;
      case 'stage1_failed':
      case 'stage2_failed':
        settingsStore.setAppState('error');
        decisionStore.fail((data.message as string) ?? '分析阶段失败');
        break;
      case 'cancelled':
        settingsStore.setAppState('idle');
        decisionStore.endRun();
        break;
      case 'error':
        settingsStore.setAppState('error');
        decisionStore.fail((data.message as string) ?? '分析出错');
        break;
      case 'done':
        decisionStore.endRun();
        settingsStore.setAppState('done');
        break;
      default:
        break;
    }

    if (
      ev === 'stage1_reasoning' ||
      ev === 'stage1_content' ||
      ev === 'stage2_reasoning' ||
      ev === 'stage2_content'
    ) {
      if (typeof data.text === 'string') decisionStore.appendReasoning(data.text);
    } else if (ev === 'stage1_result') {
      this.push({
        title: incremental ? '增量 · 市场诊断' : '阶段一 · 市场诊断',
        text: JSON.stringify(data),
        time: stamp,
        stage: incremental ? 'incremental' : '1',
      });
    } else if (ev === 'stage2_decision') {
      const decision = (data.decision ?? data) as DecisionPayload;
      decisionStore.setDecision(decision);
      this.push({
        title: incremental ? '增量 · 交易决策' : '阶段二 · 交易决策',
        text: JSON.stringify(data),
        time: stamp,
        stage: incremental ? 'incremental' : '2',
      });
    }
  },
});

function nowStamp(): string {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}
