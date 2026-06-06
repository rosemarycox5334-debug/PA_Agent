/**
 * StreamPhaseHeader — vitest unit tests.
 *
 * Covers the seven state branches of the phase label computed, the
 * tone (data-tone) attribute that drives dot color, and the four
 * per-stage counters (阶段一 / 阶段二 / 追问 / 总计). Each test mounts
 * the SFC against the real reactive stores, so the assertions exercise
 * the same reactivity surface that AIStreamPanel relies on.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import StreamPhaseHeader from './StreamPhaseHeader.vue';
import { streamStore, type StreamMessage } from '@/stores/stream';
import { decisionStore } from '@/stores/decision';
import { settingsStore } from '@/stores/settings';

function resetStores(): void {
  streamStore.reset();
  decisionStore.reset();
  settingsStore.tokenPct = 0;
  settingsStore.tokenText = '0% · 0 / 2,000,000';
  settingsStore.setAppState('idle');
}

function pushMsg(stage: StreamMessage['stage']): void {
  streamStore.push({
    title: `t-${stage}`,
    text: 'x',
    time: '00:00:00',
    stage,
  });
}

describe('StreamPhaseHeader.vue', () => {
  beforeEach(resetStores);
  afterEach(resetStores);

  it('renders the idle placeholder when no analysis has run', () => {
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-label"]').text()).toBe('等待分析…');
    expect(wrapper.get('[data-testid="phase-header"]').attributes('data-tone')).toBe('idle');
    expect(wrapper.get('[data-testid="phase-header"]').attributes('data-analyzing')).toBe('false');
  });

  it('shows the preheat label while analyzing with flowStep=0', () => {
    decisionStore.beginRun('run-preheat');
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-label"]').text()).toBe('阶段-1 · 预热中…');
    expect(wrapper.get('[data-testid="phase-dot"]').classes()).toContain('tone-running');
  });

  it('shows the stage-1 label while analyzing with flowStep=1', () => {
    decisionStore.beginRun('run-s1');
    decisionStore.flowStep = 1;
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-label"]').text()).toBe('阶段一 · 市场诊断中…');
  });

  it('shows the stage-2 label while analyzing with flowStep=2', () => {
    decisionStore.beginRun('run-s2');
    decisionStore.flowStep = 2;
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-label"]').text()).toBe('阶段二 · 交易决策中…');
  });

  it('shows the finalizing label while analyzing with flowStep>=3', () => {
    decisionStore.beginRun('run-finalize');
    decisionStore.flowStep = 3;
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-label"]').text()).toBe('收尾中…');
  });

  it('switches to "分析完成" once a decision is stored', () => {
    decisionStore.beginRun('run-done');
    decisionStore.flowStep = 4;
    decisionStore.setDecision({ order_direction: '观望' });
    decisionStore.endRun();
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-label"]').text()).toBe('分析完成');
    expect(wrapper.get('[data-testid="phase-dot"]').classes()).toContain('tone-success');
  });

  it('prefers the error label when decisionStore.error is set, even after a decision', () => {
    decisionStore.setDecision({ order_direction: '观望' });
    decisionStore.fail('阶段二超时');
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-label"]').text()).toBe('出错 · 阶段二超时');
    expect(wrapper.get('[data-testid="phase-dot"]').classes()).toContain('tone-error');
    expect(wrapper.get('[data-testid="phase-header"]').attributes('data-error')).toBe('true');
  });

  it('counts stage messages per channel and renders them in the stats line', () => {
    pushMsg('1');
    pushMsg('1');
    pushMsg('2');
    pushMsg('followup');
    pushMsg('followup');
    pushMsg('followup');
    const wrapper = mount(StreamPhaseHeader);
    const stats = wrapper.get('[data-testid="phase-stats"]').text();
    expect(stats).toContain('阶段一 2');
    expect(stats).toContain('阶段二 1');
    expect(stats).toContain('追问 3');
    expect(stats).toContain('总计 6');
  });

  it('does not count messages with no stage towards per-channel counters (but keeps total)', () => {
    streamStore.push({ title: 'meta', text: 'meta', time: '00:00:00' });
    const wrapper = mount(StreamPhaseHeader);
    const stats = wrapper.get('[data-testid="phase-stats"]').text();
    expect(stats).toContain('阶段一 0');
    expect(stats).toContain('总计 1');
  });

  it('uses CSS custom properties from tokens.css for color and font', () => {
    // jsdom does not expose the SFC's <style scoped> block via
    // wrapper.html() or getComputedStyle (it lives in a separate CSS
    // module). To make this assertion robust across environments we
    // read the source file directly and confirm the SFC references
    // the design tokens.
    const { readFileSync } = require('node:fs') as typeof import('node:fs');
    const { fileURLToPath } = require('node:url') as typeof import('node:url');
    const { resolve, dirname } = require('node:path') as typeof import('node:path');
    const sfcPath = resolve(dirname(fileURLToPath(import.meta.url)), 'StreamPhaseHeader.vue');
    const source = readFileSync(sfcPath, 'utf8');
    for (const token of ['--surface-1', '--fg', '--border', '--font-body', '--font-mono']) {
      expect(source).toContain(`var(${token})`);
    }
  });

  it('reflects tone changes after a store mutation (reactivity)', async () => {
    const wrapper = mount(StreamPhaseHeader);
    expect(wrapper.get('[data-testid="phase-header"]').attributes('data-tone')).toBe('idle');
    decisionStore.beginRun('run-reactivity');
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="phase-header"]').attributes('data-tone')).toBe('running');
    decisionStore.fail('boom');
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="phase-header"]').attributes('data-tone')).toBe('error');
  });
});
