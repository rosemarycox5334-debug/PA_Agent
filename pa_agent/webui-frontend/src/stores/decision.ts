/**
 * Decision store captures the latest Stage-2 decision payload and reasoning
 * text. It also tracks the run lifecycle so the UI can disable submit buttons
 * while an analysis is in flight.
 */
import { computed, reactive } from 'vue';

export interface DecisionPayload {
  order_type?: string;
  order_direction?: string;
  entry_price?: number | null;
  take_profit_price?: number | null;
  stop_loss_price?: number | null;
  confidence?: number;
  key_breakout?: number | null;
  resistance?: number | null;
  support_zone?: number | null;
  support?: number | null;
  gate_trace?: Array<Record<string, unknown>>;
  decision_trace?: Array<Record<string, unknown>>;
  strategy_files_needed?: string[];
  next_bar_prediction?: {
    probabilities?: {
      bullish?: number;
      bearish?: number;
      neutral?: number;
    };
  };
  probabilities?: {
    bullish?: number;
    bearish?: number;
    neutral?: number;
  };
  elapsed_s?: number;
  elapsed_seconds?: number;
  duration_s?: number;
  [key: string]: unknown;
}

export const decisionStore = reactive({
  decision: null as DecisionPayload | null,
  reasoningText: '',
  reasoningLines: [] as string[],
  rawJson: '',
  analyzing: false,
  flowStep: 0,
  runId: null as string | null,
  error: null as string | null,
  startedAt: null as number | null,
  elapsedSeconds: null as number | null,

  reset(): void {
    this.decision = null;
    this.reasoningText = '';
    this.reasoningLines = [];
    this.rawJson = '';
    this.runId = null;
    this.error = null;
    this.startedAt = null;
    this.elapsedSeconds = null;
    this.flowStep = 0;
  },

  setDecision(decision: DecisionPayload | null): void {
    this.decision = decision;
    this.rawJson = JSON.stringify(decision ?? { status: 'waiting' }, null, 2);
  },

  appendReasoning(text: string): void {
    this.reasoningText += text;
    this.reasoningLines = this.reasoningText
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l.length > 0);
  },

  beginRun(runId: string | null = null): void {
    this.reset();
    this.analyzing = true;
    this.runId = runId;
    this.startedAt = Date.now();
  },

  endRun(): void {
    this.analyzing = false;
    if (this.startedAt) {
      this.elapsedSeconds = (Date.now() - this.startedAt) / 1000;
    }
  },

  fail(message: string): void {
    this.analyzing = false;
    this.error = message;
    if (this.startedAt) {
      this.elapsedSeconds = (Date.now() - this.startedAt) / 1000;
    }
  },
});

export const decisionPillClass = computed(() => {
  const d = decisionStore.decision?.order_direction ?? '';
  if (d.includes('多')) return 'green';
  if (d.includes('空')) return 'red';
  return 'amber';
});

export const mergedTrace = computed(() => {
  const gate = (decisionStore.decision?.gate_trace ?? []).map((t) => ({
    ...t,
    phase: 'gate' as const,
  }));
  const dec = (decisionStore.decision?.decision_trace ?? []).map((t) => ({
    ...t,
    phase: 'decision' as const,
  }));
  return [...gate, ...dec];
});

export const promptFiles = computed(() => {
  const list = decisionStore.decision?.strategy_files_needed ?? [];
  return Array.from(new Set(list));
});
