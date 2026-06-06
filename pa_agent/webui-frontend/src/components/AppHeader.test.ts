/**
 * AppHeader — vitest unit tests.
 *
 * Coverage:
 *  - Renders all the chrome elements (brand, model pill, data-source
 *    pill, action buttons).
 *  - Reflects settingsStore state for model and data source.
 *  - Reflects the status dot class from settingsStore.statusDotClass.
 *  - Sidebar toggle dispatches uiStore.toggleSidebar() and reflects
 *    uiStore.sidebarOpen via the data-sidebar-open attribute.
 *  - Settings shortcut opens the settings modal (uiStore.openModal).
 *  - AutoIncrementalBadge + DemoModeLauncher children are rendered and
 *    their state propagates from the store.
 *  - Uses CSS custom properties from tokens.css (no hardcoded hex).
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mount } from '@vue/test-utils';
import AppHeader from './AppHeader.vue';
import { settingsStore } from '@/stores/settings';
import { uiStore } from '@/stores/ui';

const __dirname = dirname(fileURLToPath(import.meta.url));
const sfcPath = resolve(__dirname, 'AppHeader.vue');

function readSfc(): string {
  return readFileSync(sfcPath, 'utf-8');
}

function resetStores(): void {
  settingsStore.state.provider_model = '';
  settingsStore.state.last_data_source = 'mt5';
  settingsStore.appState = 'idle';
  uiStore.setSidebar(true);
  uiStore.closeModal();
  uiStore.setAutoIncremental(false);
  uiStore.setDemoMode(false);
  uiStore.setDemoRunning(false);
  uiStore.clearToast();
}

describe('AppHeader.vue', () => {
  beforeEach(resetStores);
  afterEach(resetStores);

  it('renders the brand mark, pills, and action buttons', () => {
    const wrapper = mount(AppHeader);
    expect(wrapper.find('[data-testid="app-header"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="app-header-brand"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="app-header-model-pill"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="app-header-source-pill"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="app-header-actions"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="app-header-settings-btn"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="app-header-sidebar-toggle"]').exists()).toBe(true);
  });

  it('falls back to a Chinese placeholder when the model is not configured', () => {
    settingsStore.state.provider_model = '';
    const wrapper = mount(AppHeader);
    expect(wrapper.get('[data-testid="app-header-model-pill"]').text()).toBe('未配置模型');
  });

  it('surfaces the configured model label', () => {
    settingsStore.state.provider_model = 'gpt-4o-mini';
    const wrapper = mount(AppHeader);
    expect(wrapper.get('[data-testid="app-header-model-pill"]').text()).toBe('gpt-4o-mini');
  });

  it('surfaces the configured data source', () => {
    settingsStore.state.last_data_source = 'tv';
    const wrapper = mount(AppHeader);
    expect(wrapper.get('[data-testid="app-header-source-pill"]').text()).toBe('tv');
  });

  it('reflects the status dot class from settingsStore.statusDotClass', () => {
    // When no data source is configured the dot should be "unknown".
    settingsStore.state.last_data_source = '';
    settingsStore.appState = 'idle';
    const wrapper = mount(AppHeader);
    const dot = wrapper.get('[data-testid="app-header-status-dot"]');
    expect(dot.classes()).toContain('unknown');

    // When a data source is configured the dot is "online".
    settingsStore.state.last_data_source = 'mt5';
    return wrapper.vm.$nextTick().then(() => {
      const refreshed = mount(AppHeader);
      expect(refreshed.get('[data-testid="app-header-status-dot"]').classes()).toContain('online');
    });
  });

  it('reflects the error state via the "offline" status dot class', () => {
    settingsStore.appState = 'error';
    settingsStore.state.last_data_source = 'mt5';
    const wrapper = mount(AppHeader);
    expect(wrapper.get('[data-testid="app-header-status-dot"]').classes()).toContain('offline');
  });

  it('toggles the sidebar flag when the sidebar button is clicked', async () => {
    const wrapper = mount(AppHeader);
    expect(wrapper.get('[data-testid="app-header"]').attributes('data-sidebar-open')).toBe('true');
    expect(wrapper.get('[data-testid="app-header-sidebar-toggle"]').attributes('aria-expanded')).toBe('true');
    await wrapper.get('[data-testid="app-header-sidebar-toggle"]').trigger('click');
    expect(uiStore.sidebarOpen).toBe(false);
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="app-header"]').attributes('data-sidebar-open')).toBe('false');
    expect(wrapper.get('[data-testid="app-header-sidebar-toggle"]').attributes('aria-expanded')).toBe('false');
  });

  it('opens the settings modal when the settings button is clicked', async () => {
    const wrapper = mount(AppHeader);
    await wrapper.get('[data-testid="app-header-settings-btn"]').trigger('click');
    expect(uiStore.modal).toBe('settings');
  });

  it('renders the AutoIncrementalBadge and DemoModeLauncher children', () => {
    const wrapper = mount(AppHeader);
    expect(wrapper.find('[data-testid="auto-incremental-badge"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="demo-launcher"]').exists()).toBe(true);
  });

  it('propagates the auto-incremental flag from the store to the child badge', async () => {
    const wrapper = mount(AppHeader);
    expect(wrapper.get('[data-testid="auto-incremental-badge"]').attributes('data-state')).toBe('off');
    uiStore.setAutoIncremental(true);
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="auto-incremental-badge"]').attributes('data-state')).toBe('on');
  });

  it('propagates the demo mode flag from the store to the child launcher', async () => {
    const wrapper = mount(AppHeader);
    expect(wrapper.get('[data-testid="demo-launcher"]').attributes('data-state')).toBe('off');
    uiStore.setDemoMode(true);
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="demo-launcher"]').attributes('data-state')).toBe('on');
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
      'var(--success)',
      'var(--danger)',
      'var(--font-body)',
      'var(--font-mono)',
    ]) {
      expect(styleBlock, `expected ${token} in <style>`).toContain(token);
    }
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'AppHeader.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });
});
