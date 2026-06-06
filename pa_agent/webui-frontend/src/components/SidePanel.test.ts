/**
 * SidePanel — vitest unit tests.
 *
 * Coverage:
 *  - Renders the section root with data-testid="side-panel" and the
 *    <SidePanelTabs> tablist with one button per `tabs` prop entry.
 *  - Forwards modelValue/ariaLabel down to <SidePanelTabs> and re-emits
 *    update:modelValue upward (controlled v-model wiring).
 *  - Resolves the active tab to the first non-disabled entry when
 *    modelValue is empty / unknown / points at a disabled tab.
 *  - Renders one role=tabpanel wrapper per tab id; only the active
 *    pane is visible (the others carry `hidden`).
 *  - Slots keyed by tab id land in the matching pane wrapper; panes
 *    without a slot render the empty-state placeholder.
 *  - The `loading` prop sets aria-busy on the root and renders the
 *    loading indicator; non-loading hides it.
 *  - Exposes aria-labelledby on each pane that points at the tab's
 *    data-tab-id, closing the WAI-ARIA tabs/panel contract.
 *  - Uses CSS custom properties from tokens.css — no hardcoded hex in
 *    the SFC <style> block.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mount } from '@vue/test-utils';
import SidePanel, { type SidePanelTab } from './SidePanel.vue';
import SidePanelTabs from './SidePanelTabs.vue';

const __dirname = dirname(fileURLToPath(import.meta.url));
const sfcPath = resolve(__dirname, 'SidePanel.vue');

function readSfc(): string {
  return readFileSync(sfcPath, 'utf-8');
}

const SAMPLE_TABS: SidePanelTab[] = [
  { id: 'stream', label: 'AI 流', icon: '~' },
  { id: 'debug', label: '调试', badge: 3 },
  { id: 'history', label: '历史' },
  { id: 'locked', label: '实验', disabled: true },
];

interface MountOpts {
  modelValue?: string;
  loading?: boolean;
  ariaLabel?: string;
  tabs?: SidePanelTab[];
  withSlots?: boolean;
}

function makeWrapper(opts: MountOpts = {}) {
  const {
    modelValue = 'stream',
    loading = false,
    ariaLabel,
    tabs = SAMPLE_TABS,
    withSlots = true,
  } = opts;

  const slots: Record<string, string> = {};
  if (withSlots) {
    slots.stream = '<p data-testid="slot-stream">stream content</p>';
    slots.debug = '<p data-testid="slot-debug">debug content</p>';
    // Note: 'history' intentionally has no slot to exercise the empty
    // placeholder. 'locked' also has no slot (and is disabled).
  }

  return mount(SidePanel, {
    props: {
      tabs,
      modelValue,
      loading,
      ...(ariaLabel ? { ariaLabel } : {}),
    },
    slots,
  });
}

describe('SidePanel.vue', () => {
  beforeEach(() => {
    // No global stores — SidePanel is fully controlled via v-model.
  });
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('renders the section root and embeds a SidePanelTabs tablist', () => {
    const wrapper = makeWrapper();
    const root = wrapper.find('[data-testid="side-panel"]');
    expect(root.exists()).toBe(true);
    expect(root.element.tagName).toBe('SECTION');

    const tabsRoot = wrapper.find('[data-testid="side-panel-tabs"]');
    expect(tabsRoot.exists()).toBe(true);
    expect(tabsRoot.attributes('role')).toBe('tablist');

    for (const tab of SAMPLE_TABS) {
      expect(wrapper.find(`[data-testid="side-panel-tab-${tab.id}"]`).exists()).toBe(true);
    }
  });

  it('exposes the active tab id via data-active on the root', () => {
    const wrapper = makeWrapper({ modelValue: 'debug' });
    expect(wrapper.get('[data-testid="side-panel"]').attributes('data-active'))
      .toBe('debug');
  });

  it('forwards ariaLabel down to SidePanelTabs', () => {
    const wrapper = makeWrapper({ ariaLabel: 'Trading side panel' });
    expect(wrapper.get('[data-testid="side-panel-tabs"]').attributes('aria-label'))
      .toBe('Trading side panel');
  });

  it('uses the default ariaLabel when none is provided', () => {
    const wrapper = makeWrapper();
    expect(wrapper.get('[data-testid="side-panel-tabs"]').attributes('aria-label'))
      .toBe('侧栏视图');
  });

  it('falls back to the first non-disabled tab when modelValue is empty', () => {
    const wrapper = makeWrapper({ modelValue: '' });
    expect(wrapper.get('[data-testid="side-panel"]').attributes('data-active'))
      .toBe('stream');
  });

  it('falls back to the first non-disabled tab when modelValue is unknown', () => {
    const wrapper = makeWrapper({ modelValue: 'nope' });
    expect(wrapper.get('[data-testid="side-panel"]').attributes('data-active'))
      .toBe('stream');
  });

  it('skips a disabled tab when computing the fallback active id', () => {
    const tabs: SidePanelTab[] = [
      { id: 'locked', label: 'X', disabled: true },
      { id: 'first', label: 'A' },
      { id: 'second', label: 'B' },
    ];
    const wrapper = mount(SidePanel, {
      props: { tabs, modelValue: 'locked' },
    });
    expect(wrapper.get('[data-testid="side-panel"]').attributes('data-active'))
      .toBe('first');
  });

  it('re-emits update:modelValue when a tab is clicked', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    await wrapper.get('[data-testid="side-panel-tab-debug"]').trigger('click');
    const events = wrapper.emitted('update:modelValue');
    expect(events).toBeTruthy();
    expect(events!.at(-1)).toEqual(['debug']);
  });

  it('does not re-emit when the already-active tab is clicked', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    await wrapper.get('[data-testid="side-panel-tab-stream"]').trigger('click');
    expect(wrapper.emitted('update:modelValue') ?? []).toEqual([]);
  });

  it('skips disabled tabs even when a click is dispatched', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    await wrapper.get('[data-testid="side-panel-tab-locked"]').trigger('click');
    const events = wrapper.emitted('update:modelValue') ?? [];
    expect(events).toEqual([]);
  });

  it('renders one role=tabpanel wrapper per tab id', () => {
    const wrapper = makeWrapper();
    for (const tab of SAMPLE_TABS) {
      const pane = wrapper.find(`[data-testid="side-panel-pane-${tab.id}"]`);
      expect(pane.exists(), `pane for ${tab.id} should exist`).toBe(true);
      expect(pane.attributes('role')).toBe('tabpanel');
      expect(pane.attributes('id')).toBe(`side-panel-pane-${tab.id}`);
    }
  });

  it('marks only the active pane as visible (hidden on the rest)', () => {
    const wrapper = makeWrapper({ modelValue: 'debug' });
    const active = wrapper.get('[data-testid="side-panel-pane-debug"]');
    expect(active.attributes('hidden')).toBeUndefined();
    expect(active.attributes('data-active')).toBe('true');

    const stream = wrapper.get('[data-testid="side-panel-pane-stream"]');
    expect(stream.attributes('hidden')).toBe('');
    expect(stream.attributes('data-active')).toBe('false');

    const history = wrapper.get('[data-testid="side-panel-pane-history"]');
    expect(history.attributes('hidden')).toBe('');
  });

  it('wires each pane aria-labelledby to its tab id', () => {
    const wrapper = makeWrapper();
    for (const tab of SAMPLE_TABS) {
      const pane = wrapper.get(`[data-testid="side-panel-pane-${tab.id}"]`);
      expect(pane.attributes('aria-labelledby')).toBe(`side-panel-tab-${tab.id}`);
    }
  });

  it('renders the matching slot content in the active pane', () => {
    const wrapper = makeWrapper({ modelValue: 'debug' });
    // Debug slot content should be inside the debug pane.
    const debugPane = wrapper.get('[data-testid="side-panel-pane-debug"]');
    expect(debugPane.find('[data-testid="slot-debug"]').exists()).toBe(true);
    // Stream pane is hidden, so the slot content should not be in
    // document visible state.
    const streamPane = wrapper.get('[data-testid="side-panel-pane-stream"]');
    expect(streamPane.attributes('hidden')).toBe('');
  });

  it('renders the empty-state placeholder for panes without a slot', () => {
    const wrapper = makeWrapper({ modelValue: 'history' });
    const historyPane = wrapper.get('[data-testid="side-panel-pane-history"]');
    expect(historyPane.find('[data-testid="side-panel-empty-history"]').exists()).toBe(true);
    expect(historyPane.find('[data-testid="side-panel-empty-history"]').text())
      .toBe('该面板暂无内容');
  });

  it('flags data-provided=false on panes without a matching slot', () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    const streamPane = wrapper.get('[data-testid="side-panel-pane-stream"]');
    expect(streamPane.attributes('data-provided')).toBe('true');
    const historyPane = wrapper.get('[data-testid="side-panel-pane-history"]');
    expect(historyPane.attributes('data-provided')).toBe('false');
  });

  it('toggles aria-busy and the loading indicator via the loading prop', () => {
    const idle = makeWrapper({ loading: false });
    expect(idle.get('[data-testid="side-panel"]').attributes('aria-busy')).toBe('false');
    expect(idle.find('[data-testid="side-panel-loading"]').exists()).toBe(false);

    const busy = makeWrapper({ loading: true });
    expect(busy.get('[data-testid="side-panel"]').attributes('aria-busy')).toBe('true');
    const loader = busy.get('[data-testid="side-panel-loading"]');
    expect(loader.exists()).toBe(true);
    expect(loader.text()).toBe('···');
  });

  it('updates the visible pane reactively when modelValue changes', async () => {
    const wrapper = makeWrapper({ modelValue: 'stream' });
    expect(wrapper.get('[data-testid="side-panel-pane-stream"]').attributes('hidden'))
      .toBeUndefined();
    await wrapper.setProps({ modelValue: 'debug' });
    expect(wrapper.get('[data-testid="side-panel-pane-stream"]').attributes('hidden'))
      .toBe('');
    expect(wrapper.get('[data-testid="side-panel-pane-debug"]').attributes('hidden'))
      .toBeUndefined();
  });

  it('embeds the live SidePanelTabs component (not a stub)', () => {
    // Sanity check: the rendered child is a real SidePanelTabs instance,
    // not a stand-in. We assert by checking that the global attribute
    // contract (role=tablist, aria-selected) is produced.
    const wrapper = makeWrapper({ modelValue: 'debug' });
    const tabsRoot = wrapper.get('[data-testid="side-panel-tabs"]');
    expect(tabsRoot.attributes('role')).toBe('tablist');
    const active = wrapper.get('[data-testid="side-panel-tab-debug"]');
    expect(active.attributes('aria-selected')).toBe('true');
    // Reference the imported component to keep tree-shaking honest in
    // downstream bundles and to catch accidental file renames.
    expect(typeof SidePanelTabs).toBe('object');
  });

  it('uses CSS custom properties from tokens.css — no hardcoded hex in <style>', () => {
    const sfc = readSfc();
    const styleMatch = sfc.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, 'a <style> block should be present in the SFC').toBeTruthy();
    const styleBlock = styleMatch![0];
    for (const token of [
      'var(--surface-1)',
      'var(--surface-2)',
      'var(--bg)',
      'var(--fg)',
      'var(--fg-3)',
      'var(--border)',
      'var(--accent)',
      'var(--font-body)',
      'var(--font-mono)',
    ]) {
      expect(styleBlock, `expected ${token} in <style>`).toContain(token);
    }
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'SidePanel.vue <style> contains a hardcoded hex color; use a token from tokens.css',
    ).toBe(false);
  });
});
