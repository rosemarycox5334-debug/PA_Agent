/**
 * Settings store mirrors the backend SettingsResponse and exposes computed
 * helpers used by the header, status pill, progress bar, and settings view.
 */
import { computed, reactive } from 'vue';
import { api, type SettingsResponse } from '@/api/client';

export interface SettingsState {
  provider_model: string;
  provider_base_url: string;
  provider_api_key: string;
  provider_thinking: boolean;
  provider_reasoning_effort: string;
  provider_context_window: number;
  analysis_bar_count: number;
  refresh_interval_ms: number;
  auto_resume_chart_after_analysis: boolean;
  context_warning_threshold_pct: number;
  stream_pane_font_pt: number;
  chart_seq_label_font_pt: number;
  incremental_max_new_bars: number;
  decision_stance: string;
  rqdata_license_key: string;
  decision_flow_auto_play: boolean;
  decision_flow_play_seconds: number;
  decision_flow_default_zoom_pct: number;
  last_symbol: string;
  last_timeframe: string;
  last_data_source: string;
  last_tradingview_exchange: string;
}

type AppState = 'idle' | 'analyzing' | 'done' | 'error';

const empty: SettingsState = {
  provider_model: '',
  provider_base_url: '',
  provider_api_key: '',
  provider_thinking: true,
  provider_reasoning_effort: 'max',
  provider_context_window: 2_000_000,
  analysis_bar_count: 100,
  refresh_interval_ms: 1000,
  auto_resume_chart_after_analysis: false,
  context_warning_threshold_pct: 80,
  stream_pane_font_pt: 11,
  chart_seq_label_font_pt: 7,
  incremental_max_new_bars: 10,
  decision_stance: 'balanced',
  rqdata_license_key: '',
  decision_flow_auto_play: true,
  decision_flow_play_seconds: 50,
  decision_flow_default_zoom_pct: 500,
  last_symbol: '',
  last_timeframe: '',
  last_data_source: 'mt5',
  last_tradingview_exchange: '',
};

const settingsBase = reactive({
  state: { ...empty } as SettingsState,
  loading: false,
  error: null as string | null,
  tokenPct: 0,
  tokenText: '0% · 0 / 2,000,000',
  appState: 'idle' as AppState,

  async refresh(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      const data: SettingsResponse = await api.fetchSettings();
      this.hydrate(data);
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
      throw err;
    } finally {
      this.loading = false;
    }
  },

  hydrate(data: SettingsResponse): void {
    const p = data.provider ?? {};
    const g = data.general ?? {};
    this.state.provider_model = p.model ?? '';
    this.state.provider_base_url = p.base_url ?? '';
    this.state.provider_api_key = p.api_key === '***' ? '' : p.api_key ?? '';
    this.state.provider_thinking = p.thinking ?? true;
    this.state.provider_reasoning_effort = p.reasoning_effort ?? 'max';
    this.state.provider_context_window = p.context_window ?? 2_000_000;
    this.state.analysis_bar_count = g.analysis_bar_count ?? 100;
    this.state.refresh_interval_ms = g.refresh_interval_ms ?? 1000;
    this.state.auto_resume_chart_after_analysis = g.auto_resume_chart_after_analysis ?? false;
    this.state.context_warning_threshold_pct = g.context_warning_threshold_pct ?? 80;
    this.state.stream_pane_font_pt = g.stream_pane_font_pt ?? 11;
    this.state.chart_seq_label_font_pt = g.chart_seq_label_font_pt ?? 7;
    this.state.incremental_max_new_bars = g.incremental_max_new_bars ?? 10;
    this.state.decision_stance = g.decision_stance ?? 'balanced';
    this.state.rqdata_license_key = g.rqdata_license_key === '***' ? '' : g.rqdata_license_key ?? '';
    this.state.decision_flow_auto_play = g.decision_flow_auto_play ?? true;
    this.state.decision_flow_play_seconds = g.decision_flow_play_seconds ?? 50;
    this.state.decision_flow_default_zoom_pct = g.decision_flow_default_zoom_pct ?? 500;
    this.state.last_symbol = g.last_symbol ?? '';
    this.state.last_timeframe = g.last_timeframe ?? '';
    this.state.last_data_source = g.last_data_source ?? 'mt5';
    this.state.last_tradingview_exchange = g.last_tradingview_exchange ?? '';
  },

  async save(): Promise<void> {
    const s = this.state;
    await api.saveSettings({
      provider: {
        model: s.provider_model,
        base_url: s.provider_base_url,
        api_key: s.provider_api_key || '***',
        thinking: s.provider_thinking,
        reasoning_effort: s.provider_reasoning_effort,
        context_window: s.provider_context_window,
      },
      general: {
        analysis_bar_count: s.analysis_bar_count,
        refresh_interval_ms: s.refresh_interval_ms,
        auto_resume_chart_after_analysis: s.auto_resume_chart_after_analysis,
        context_warning_threshold_pct: s.context_warning_threshold_pct,
        stream_pane_font_pt: s.stream_pane_font_pt,
        chart_seq_label_font_pt: s.chart_seq_label_font_pt,
        incremental_max_new_bars: s.incremental_max_new_bars,
        decision_stance: s.decision_stance,
        rqdata_license_key: s.rqdata_license_key || '***',
        decision_flow_auto_play: s.decision_flow_auto_play,
        decision_flow_play_seconds: s.decision_flow_play_seconds,
        decision_flow_default_zoom_pct: s.decision_flow_default_zoom_pct,
        last_symbol: s.last_symbol,
        last_timeframe: s.last_timeframe,
        last_data_source: s.last_data_source,
        last_tradingview_exchange: s.last_tradingview_exchange,
      },
    });
  },

  updateTokenUsage(pct: number, used: number, window: number): void {
    this.tokenPct = pct;
    this.tokenText = `${pct}% · ${used} / ${window}`;
  },

  setAppState(state: AppState): void {
    this.appState = state;
  },
});

export const modelLabel = computed(
  () => settingsBase.state.provider_model || '未配置模型',
);

export const statusText = computed(() => {
  switch (settingsBase.appState) {
    case 'error':
      return '数据异常 · 请检查配置';
    case 'analyzing':
      return `AI 分析中 · ${settingsBase.state.last_symbol} ${settingsBase.state.last_timeframe}`;
    case 'done':
      return `分析完成 · ${settingsBase.state.last_symbol} ${settingsBase.state.last_timeframe}`;
    default:
      return `等待 · ${settingsBase.state.last_symbol || '--'} ${settingsBase.state.last_timeframe || '--'}`;
  }
});

export const statusDotClass = computed(() => {
  if (settingsBase.appState === 'error') return 'offline';
  if (settingsBase.state.last_data_source) return 'online';
  return 'unknown';
});

export const progressClass = computed(() => {
  const pct = settingsBase.tokenPct;
  if (pct >= 80) return 'danger';
  if (pct >= 60) return 'warn';
  return '';
});

export const settingsStore = settingsBase as typeof settingsBase & {
  readonly modelLabel: string;
  readonly statusText: string;
  readonly statusDotClass: string;
  readonly progressClass: string;
};

Object.defineProperties(settingsStore, {
  modelLabel: { get: () => modelLabel.value },
  statusText: { get: () => statusText.value },
  statusDotClass: { get: () => statusDotClass.value },
  progressClass: { get: () => progressClass.value },
});
