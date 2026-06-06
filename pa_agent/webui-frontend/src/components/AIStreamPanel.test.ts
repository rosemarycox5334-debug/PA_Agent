/**
 * AIStreamPanel unit tests — exercise the Vue 3 port of the PyQt
 * AIStreamPanel. The component is purely presentational: it reads from
 * streamStore + decisionStore + settingsStore and exposes cancel/clear
 * affordances. We mutate the shared stores directly (the same way the SSE
 * reader does) and assert on rendered DOM.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import AIStreamPanel from './AIStreamPanel.vue';
import { streamStore, type StreamMessage } from '@/stores/stream';
import { decisionStore } from '@/stores/decision';
import { settingsStore } from '@/stores/settings';

function resetStores(): void {
  streamStore.reset();
  streamStore.active = false;
  streamStore.controller = null;
  decisionStore.reset();
  settingsStore.appState = 'idle';
  settingsStore.tokenPct = 0;
  settingsStore.tokenText = '0% · 0 / 2,000,000';
}

describe('AIStreamPanel', () => {
  beforeEach(() => {
    resetStores();
  });

  it('renders the empty/idle state with a phase header', () => {
    const wrapper = mount(AIStreamPanel);
    expect(wrapper.find('[data-testid="ai-stream-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="phase-label"]').text()).toBe('等待分析…');
    expect(wrapper.find('[data-testid="reasoning-text"]').text()).toBe('（暂无推理输出）');
    expect(wrapper.find('[data-testid="phase-stats"]').text()).toContain('阶段一 0');
  });

  it('reflects the stage-1 / stage-2 / analyzing flow steps', async () => {
    const wrapper = mount(AIStreamPanel);
    decisionStore.beginRun('run-test-1');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-testid="phase-label"]').text()).toContain('预热中');

    decisionStore.flowStep = 1;
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-testid="phase-label"]').text()).toContain('阶段一');

    decisionStore.flowStep = 2;
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-testid="phase-label"]').text()).toContain('阶段二');
  });

  it('renders streamed messages with stage classes and counts them', async () => {
    const wrapper = mount(AIStreamPanel);
    const messages: StreamMessage[] = [
      { title: '阶段一 · 市场诊断', text: 'diag payload', time: '10:00:01', stage: '1' },
      { title: '阶段二 · 交易决策', text: 'decision payload', time: '10:00:05', stage: '2' },
      { title: '追问 · …', text: 'followup payload', time: '10:00:10', stage: 'followup' },
    ];
    for (const m of messages) streamStore.push(m);
    await wrapper.vm.$nextTick();

    const rendered = wrapper.findAll('article[data-testid^="message-"]');
    expect(rendered).toHaveLength(3);
    expect(rendered[0].attributes('data-stage')).toBe('1');
    expect(rendered[1].attributes('data-stage')).toBe('2');
    expect(rendered[2].attributes('data-stage')).toBe('followup');
    expect(wrapper.find('[data-testid="phase-stats"]').text()).toBe('阶段一 1 · 阶段二 1 · 追问 1');
  });

  it('mirrors reasoning text from the decision store', async () => {
    const wrapper = mount(AIStreamPanel);
    decisionStore.appendReasoning('第一步：识别关键支撑\n');
    decisionStore.appendReasoning('第二步：评估风险收益比');
    await wrapper.vm.$nextTick();
    const text = wrapper.find('[data-testid="reasoning-text"]').text();
    expect(text).toContain('第一步');
    expect(text).toContain('第二步');
  });

  it('shows the cancel button as enabled while streaming and triggers streamStore.cancel', async () => {
    const wrapper = mount(AIStreamPanel);
    streamStore.active = true;
    await wrapper.vm.$nextTick();
    const cancelBtn = wrapper.find('[data-testid="cancel-btn"]');
    expect((cancelBtn.element as HTMLButtonElement).disabled).toBe(false);

    // Spy on cancel() without relying on the real AbortController
    let cancelCalls = 0;
    const original = streamStore.cancel;
    streamStore.cancel = (): void => {
      cancelCalls += 1;
    };
    await cancelBtn.trigger('click');
    expect(cancelCalls).toBe(1);
    streamStore.cancel = original;
  });

  it('disables the cancel button when the stream is idle', async () => {
    const wrapper = mount(AIStreamPanel);
    streamStore.active = false;
    await wrapper.vm.$nextTick();
    const cancelBtn = wrapper.find('[data-testid="cancel-btn"]');
    expect((cancelBtn.element as HTMLButtonElement).disabled).toBe(true);
  });

  it('clear button resets the stores', async () => {
    const wrapper = mount(AIStreamPanel);
    streamStore.push({ title: '阶段一', text: 'x', time: '10:00:00', stage: '1' });
    decisionStore.appendReasoning('some reasoning');
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-testid="clear-btn"]').trigger('click');
    await wrapper.vm.$nextTick();

    expect(streamStore.messages).toHaveLength(0);
    expect(decisionStore.reasoningText).toBe('');
    expect(wrapper.find('[data-testid="reasoning-text"]').text()).toBe('（暂无推理输出）');
  });

  it('surfaces errors in the phase label', async () => {
    const wrapper = mount(AIStreamPanel);
    decisionStore.beginRun();
    decisionStore.fail('上游模型超时');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-testid="phase-label"]').text()).toContain('出错');
    expect(wrapper.find('[data-testid="phase-label"]').text()).toContain('上游模型超时');
  });

  it('marks the token progress bar as danger above 80%', async () => {
    const wrapper = mount(AIStreamPanel);
    settingsStore.updateTokenUsage(85, 1_700_000, 2_000_000);
    await wrapper.vm.$nextTick();
    const bar = wrapper.find('.token-progress');
    expect(bar.attributes('data-state')).toBe('danger');
    expect(wrapper.find('[data-testid="token-text"]').text()).toContain('85%');
  });

  it('marks the token progress bar as warn between 60% and 80%', async () => {
    const wrapper = mount(AIStreamPanel);
    settingsStore.updateTokenUsage(70, 1_400_000, 2_000_000);
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.token-progress').attributes('data-state')).toBe('warn');
  });
});
