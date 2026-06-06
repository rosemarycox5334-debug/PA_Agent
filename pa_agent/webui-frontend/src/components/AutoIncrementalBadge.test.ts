/**
 * AutoIncrementalBadge — vitest unit tests.
 *
 * Coverage:
 *  - Renders the off-state by default.
 *  - Reflects the uiStore.autoIncremental flag (data-state attribute).
 *  - Clicking the badge flips the flag and pushes a toast.
 *  - aria-pressed tracks the boolean state.
 *  - Uses CSS custom properties from tokens.css (no hardcoded hex).
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mount } from '@vue/test-utils';
import AutoIncrementalBadge from './AutoIncrementalBadge.vue';
import { uiStore } from '@/stores/ui';

const __dirname = dirname(fileURLToPath(import.meta.url));
const sfcPath = resolve(__dirname, 'AutoIncrementalBadge.vue');

function readSfc(): string {
  return readFileSync(sfcPath, 'utf-8');
}

function resetStores(): void {
  uiStore.setAutoIncremental(false);
  uiStore.clearToast();
}

describe('AutoIncrementalBadge.vue', () => {
  beforeEach(resetStores);
  afterEach(resetStores);

  it('renders the off state by default', () => {
    const wrapper = mount(AutoIncrementalBadge);
    const root = wrapper.get('[data-testid="auto-incremental-badge"]');
    expect(root.attributes('data-state')).toBe('off');
    expect(root.attributes('aria-pressed')).toBe('false');
    expect(wrapper.get('[data-testid="auto-incremental-label"]').text()).toBe('自动增量');
  });

  it('reflects the on state when uiStore.autoIncremental is true', async () => {
    uiStore.setAutoIncremental(true);
    const wrapper = mount(AutoIncrementalBadge);
    const root = wrapper.get('[data-testid="auto-incremental-badge"]');
    expect(root.attributes('data-state')).toBe('on');
    expect(root.attributes('aria-pressed')).toBe('true');
    expect(wrapper.get('[data-testid="auto-incremental-label"]').text()).toBe('自动增量 · 开');
  });

  it('clicking flips the flag from off to on and pushes a success toast', async () => {
    const wrapper = mount(AutoIncrementalBadge);
    await wrapper.get('[data-testid="auto-incremental-badge"]').trigger('click');
    expect(uiStore.autoIncremental).toBe(true);
    expect(uiStore.lastToast?.tone).toBe('success');
    expect(uiStore.lastToast?.text).toContain('开启');
  });

  it('clicking again flips the flag from on to off and pushes an info toast', async () => {
    uiStore.setAutoIncremental(true);
    const wrapper = mount(AutoIncrementalBadge);
    await wrapper.get('[data-testid="auto-incremental-badge"]').trigger('click');
    expect(uiStore.autoIncremental).toBe(false);
    expect(uiStore.lastToast?.tone).toBe('info');
    expect(uiStore.lastToast?.text).toContain('关闭');
  });

  it('updates reactively when the store flag changes after mount', async () => {
    const wrapper = mount(AutoIncrementalBadge);
    expect(wrapper.get('[data-testid="auto-incremental-badge"]').attributes('data-state')).toBe('off');
    uiStore.setAutoIncremental(true);
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="auto-incremental-badge"]').attributes('data-state')).toBe('on');
    expect(wrapper.get('[data-testid="auto-incremental-label"]').text()).toBe('自动增量 · 开');
  });

  it('uses CSS custom properties from tokens.css — no hardcoded hex in <style>', () => {
    const sfc = readSfc();
    const styleMatch = sfc.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, 'a <style> block should be present in the SFC').toBeTruthy();
    const styleBlock = styleMatch![0];
    for (const token of [
      'var(--surface-2)',
      'var(--surface-3)',
      'var(--fg)',
      'var(--fg-2)',
      'var(--fg-3)',
      'var(--border)',
      'var(--accent)',
      'var(--accent-2)',
      'var(--bg)',
    ]) {
      expect(styleBlock, `expected ${token} in <style>`).toContain(token);
    }
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'AutoIncrementalBadge.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });
});
