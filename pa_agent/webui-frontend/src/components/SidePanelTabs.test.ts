/**
 * SidePanelTabs — vitest unit tests.
 *
 * Coverage:
 *  - Renders the tablist and one tab button per `tabs` prop entry.
 *  - Applies the correct role/aria-selected/tabindex trio per tab.
 *  - Emits `update:modelValue` on click and ignores clicks on disabled tabs.
 *  - Reflects the active tab id through data-active on the root and on the
 *    individual buttons.
 *  - Falls back to the first non-disabled tab when the incoming
 *    modelValue is empty / unknown.
 *  - ArrowLeft / ArrowRight / Home / End keyboard nav updates modelValue
 *    and moves DOM focus.
 *  - Enter and Space activate the focused tab.
 *  - Renders the optional icon (aria-hidden) and badge.
 *  - Uses CSS custom properties from tokens.css (no hardcoded hex).
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mount } from '@vue/test-utils';
import SidePanelTabs, { type SidePanelTab } from './SidePanelTabs.vue';

const __dirname = dirname(fileURLToPath(import.meta.url));
const sfcPath = resolve(__dirname, 'SidePanelTabs.vue');

function readSfc(): string {
  return readFileSync(sfcPath, 'utf-8');
}

const SAMPLE_TABS: SidePanelTab[] = [
  { id: 'stream', label: 'AI 流', icon: '~' },
  { id: 'debug', label: '调试', badge: 3 },
  { id: 'history', label: '历史', icon: '#' },
  { id: 'locked', label: '实验', disabled: true },
];

function makeWrapper(propsOverride: Partial<InstanceType<typeof SidePanelTabs>['$props']> = {}) {
  return mount(SidePanelTabs, {
    props: {
      tabs: SAMPLE_TABS,
      modelValue: 'stream',
      ...propsOverride,
    },
  });
}

describe('SidePanelTabs.vue', () => {
  beforeEach(() => {
    // No global stores to reset — component is fully controlled.
  });
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('renders the tablist root and one tab button per entry', () => {
    const wrapper = makeWrapper();
    const root = wrapper.get('[data-testid="side-panel-tabs"]');
    expect(root.exists()).toBe(true);
    expect(root.attributes('role')).toBe('tablist');
    expect(root.attributes('aria-label')).toBe('侧栏视图');

    for (const tab of SAMPLE_TABS) {
      const btn = wrapper.get(`[data-testid="side-panel-tab-${tab.id}"]`);
      expect(btn.exists()).toBe(true);
      expect(btn.attributes('role')).toBe('tab');
    }
  });

  it('uses the provided ariaLabel prop when passed', () => {
    const wrapper = makeWrapper({ ariaLabel: 'Trading side panel' });
    expect(wrapper.get('[data-testid="side-panel-tabs"]').attributes('aria-label'))
      .toBe('Trading side panel');
  });

  it('marks the active tab with aria-selected=true and tabindex=0, others tabindex=-1', () => {
    const wrapper = makeWrapper({ modelValue: 'debug' });
    const active = wrapper.get('[data-testid="side-panel-tab-debug"]');
    expect(active.attributes('aria-selected')).toBe('true');
    expect(active.attributes('tabindex')).toBe('0');
    expect(active.attributes('data-active')).toBe('true');

    const inactive = wrapper.get('[data-testid="side-panel-tab-stream"]');
    expect(inactive.attributes('aria-selected')).toBe('false');
    expect(inactive.attributes('tabindex')).toBe('-1');
    expect(inactive.attributes('data-active')).toBe('false');
  });

  it('exposes the active id on the tablist via data-active', () => {
    const wrapper = makeWrapper({ modelValue: 'history' });
    expect(wrapper.get('[data-testid="side-panel-tabs"]').attributes('data-active'))
      .toBe('history');
  });

  it('emits update:modelValue on click and skips disabled tabs', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    await wrapper.get('[data-testid="side-panel-tab-debug"]').trigger('click');
    const events = wrapper.emitted('update:modelValue');
    expect(events).toBeTruthy();
    expect(events![events!.length - 1]).toEqual(['debug']);

    // Clicking the disabled tab must not emit.
    await wrapper.get('[data-testid="side-panel-tab-locked"]').trigger('click');
    const all = wrapper.emitted('update:modelValue')!;
    expect(all[all.length - 1]).toEqual(['debug']);
  });

  it('does not re-emit when clicking the already-active tab', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    await wrapper.get('[data-testid="side-panel-tab-stream"]').trigger('click');
    const events = wrapper.emitted('update:modelValue');
    expect(events ?? []).toEqual([]);
  });

  it('falls back to the first non-disabled tab when modelValue is empty', () => {
    const wrapper = makeWrapper({ modelValue: '' });
    expect(wrapper.get('[data-testid="side-panel-tabs"]').attributes('data-active'))
      .toBe('stream');
    expect(wrapper.get('[data-testid="side-panel-tab-stream"]').attributes('aria-selected'))
      .toBe('true');
  });

  it('falls back to the first non-disabled tab when modelValue is unknown', () => {
    const wrapper = makeWrapper({ modelValue: 'nope' });
    expect(wrapper.get('[data-testid="side-panel-tabs"]').attributes('data-active'))
      .toBe('stream');
  });

  it('skips a disabled tab when computing the fallback', () => {
    const tabs: SidePanelTab[] = [
      { id: 'locked', label: 'X', disabled: true },
      { id: 'first', label: 'A' },
      { id: 'second', label: 'B' },
    ];
    const wrapper = mount(SidePanelTabs, {
      props: { tabs, modelValue: 'locked' },
    });
    // Active should be 'first' (the first non-disabled), even though the
    // incoming modelValue ('locked') points at a disabled entry.
    expect(wrapper.get('[data-testid="side-panel-tabs"]').attributes('data-active'))
      .toBe('first');
  });

  it('renders the icon (aria-hidden) when provided', () => {
    const wrapper = makeWrapper();
    const stream = wrapper.get('[data-testid="side-panel-tab-stream"]');
    const html = stream.html();
    expect(html).toContain('side-panel-tab-icon');
    // The icon text itself is rendered in a span that is aria-hidden.
    expect(html).toContain('aria-hidden="true"');
  });

  it('renders the badge count when provided and skips zero', () => {
    const wrapper = makeWrapper();
    const debugBadge = wrapper.get('[data-testid="side-panel-tab-badge-debug"]');
    expect(debugBadge.text()).toBe('3');
    expect(debugBadge.attributes('aria-label')).toBe('3 项');

    // Tabs without a badge should not have a badge node.
    const html = wrapper.get('[data-testid="side-panel-tab-stream"]').html();
    expect(html).not.toContain('side-panel-tab-badge-stream');
  });

  it('caps the badge label at "99+"', () => {
    const tabs: SidePanelTab[] = [{ id: 'busy', label: 'Busy', badge: 150 }];
    const wrapper = mount(SidePanelTabs, {
      props: { tabs, modelValue: 'busy' },
    });
    expect(wrapper.get('[data-testid="side-panel-tab-badge-busy"]').text()).toBe('99+');
  });

  it('ArrowRight moves to the next non-disabled tab and emits update:modelValue', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    const stream = wrapper.get('[data-testid="side-panel-tab-stream"]');
    await stream.trigger('keydown', { key: 'ArrowRight' });
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['debug']);
  });

  it('ArrowLeft moves to the previous non-disabled tab and emits update:modelValue', async () => {
    const wrapper = makeWrapper({ modelValue: 'debug' });
    const debug = wrapper.get('[data-testid="side-panel-tab-debug"]');
    await debug.trigger('keydown', { key: 'ArrowLeft' });
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['stream']);
  });

  it('skips disabled tabs in arrow navigation', async () => {
    const tabs: SidePanelTab[] = [
      { id: 'a', label: 'A' },
      { id: 'b', label: 'B', disabled: true },
      { id: 'c', label: 'C' },
    ];
    const wrapper = mount(SidePanelTabs, {
      props: { tabs, modelValue: 'a' },
    });
    const a = wrapper.get('[data-testid="side-panel-tab-a"]');
    await a.trigger('keydown', { key: 'ArrowRight' });
    // b is disabled → should jump straight to c.
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['c']);
  });

  it('Home jumps to the first non-disabled tab, End to the last', async () => {
    const wrapper = makeWrapper({ modelValue: 'debug' });
    const debug = wrapper.get('[data-testid="side-panel-tab-debug"]');
    await debug.trigger('keydown', { key: 'End' });
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['history']);
    await debug.trigger('keydown', { key: 'Home' });
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['stream']);
  });

  it('Enter and Space activate the focused tab', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    const debug = wrapper.get('[data-testid="side-panel-tab-debug"]');
    await debug.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['debug']);
    await debug.trigger('keydown', { key: ' ' });
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['debug']);
  });

  it('marks the active tab with a CSS class for the .is-active selector', () => {
    const wrapper = makeWrapper({ modelValue: 'debug' });
    expect(wrapper.get('[data-testid="side-panel-tab-debug"]').classes()).toContain('is-active');
    expect(wrapper.get('[data-testid="side-panel-tab-stream"]').classes()).not.toContain('is-active');
  });

  it('exposes aria-controls that points at the tab pane', () => {
    const wrapper = makeWrapper();
    expect(wrapper.get('[data-testid="side-panel-tab-stream"]').attributes('aria-controls'))
      .toBe('side-panel-pane-stream');
  });

  it('uses CSS custom properties from tokens.css — no hardcoded hex in <style>', () => {
    const sfc = readSfc();
    const styleMatch = sfc.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, 'a <style> block should be present in the SFC').toBeTruthy();
    const styleBlock = styleMatch![0];
    for (const token of [
      'var(--surface-1)',
      'var(--surface-2)',
      'var(--surface-3)',
      'var(--fg)',
      'var(--fg-2)',
      'var(--fg-3)',
      'var(--border)',
      'var(--border-2)',
      'var(--accent)',
      'var(--font-body)',
      'var(--font-mono)',
    ]) {
      expect(styleBlock, `expected ${token} in <style>`).toContain(token);
    }
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'SidePanelTabs.vue <style> contains a hardcoded hex color; use a token from tokens.css',
    ).toBe(false);
  });
});
