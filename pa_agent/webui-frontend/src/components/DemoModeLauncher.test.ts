/**
 * DemoModeLauncher unit tests — exercise the header pill that toggles
 * the demo-mode flag and opens the `demo-launcher` modal. The component
 * is purely presentational: it reads from `uiStore` and dispatches
 * `setDemoMode` / `pushToast` / `openModal` on click. We mutate the
 * store directly (the same way AppHeader / SettingsView would) and
 * assert on the rendered DOM + side effects on the store.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import DemoModeLauncher from './DemoModeLauncher.vue';
import { uiStore } from '@/stores/ui';

function resetUiStore(): void {
  uiStore.demoMode = false;
  uiStore.demoRunning = false;
  uiStore.modal = null;
  uiStore.lastToast = null;
  uiStore.sidebarOpen = true;
  uiStore.autoIncremental = false;
}

describe('DemoModeLauncher', () => {
  beforeEach(() => {
    resetUiStore();
  });

  it('renders the off-state pill with the default label', () => {
    const wrapper = mount(DemoModeLauncher);
    const btn = wrapper.find('[data-testid="demo-launcher"]');
    expect(btn.exists()).toBe(true);
    expect(btn.attributes('data-state')).toBe('off');
    expect(btn.attributes('data-running')).toBe('false');
    expect(btn.attributes('aria-pressed')).toBe('false');
    expect(wrapper.find('[data-testid="demo-launcher-label"]').text()).toBe('演示模式');
  });

  it('reflects demoMode=true with the on label and pressed state', async () => {
    uiStore.demoMode = true;
    const wrapper = mount(DemoModeLauncher);
    await wrapper.vm.$nextTick();
    const btn = wrapper.find('[data-testid="demo-launcher"]');
    expect(btn.attributes('data-state')).toBe('on');
    expect(btn.attributes('aria-pressed')).toBe('true');
    expect(wrapper.find('[data-testid="demo-launcher-label"]').text()).toBe('演示模式 · 开');
  });

  it('shows the running label and data-running flag while demoRunning is true', async () => {
    uiStore.demoMode = true;
    uiStore.demoRunning = true;
    const wrapper = mount(DemoModeLauncher);
    await wrapper.vm.$nextTick();
    const btn = wrapper.find('[data-testid="demo-launcher"]');
    expect(btn.attributes('data-running')).toBe('true');
    expect(btn.attributes('data-state')).toBe('on');
    expect(wrapper.find('[data-testid="demo-launcher-label"]').text()).toBe('演示运行中…');
  });

  it('clicking from off → on flips demoMode, pushes a success toast, and opens the demo-launcher modal', async () => {
    const wrapper = mount(DemoModeLauncher);
    const btn = wrapper.find('[data-testid="demo-launcher"]');

    expect(uiStore.demoMode).toBe(false);
    await btn.trigger('click');
    expect(uiStore.demoMode).toBe(true);
    expect(uiStore.modal).toBe('demo-launcher');
    expect(uiStore.lastToast).toBeTruthy();
    expect(uiStore.lastToast?.text).toBe('演示模式已开启');
    expect(uiStore.lastToast?.tone).toBe('success');
  });

  it('clicking from on → off flips demoMode back, pushes an info toast, and still opens the modal', async () => {
    uiStore.demoMode = true;
    const wrapper = mount(DemoModeLauncher);
    const btn = wrapper.find('[data-testid="demo-launcher"]');
    await wrapper.vm.$nextTick();

    await btn.trigger('click');
    expect(uiStore.demoMode).toBe(false);
    // setDemoMode(false) should also clear demoRunning as a side effect
    expect(uiStore.demoRunning).toBe(false);
    expect(uiStore.modal).toBe('demo-launcher');
    expect(uiStore.lastToast?.text).toBe('演示模式已关闭');
    expect(uiStore.lastToast?.tone).toBe('info');
  });

  it('ignores clicks while a demo is running (no toggle, no toast, no modal change)', async () => {
    uiStore.demoRunning = true;
    const wrapper = mount(DemoModeLauncher);
    const btn = wrapper.find('[data-testid="demo-launcher"]');
    await wrapper.vm.$nextTick();

    await btn.trigger('click');
    // The handler must early-return — demoMode stays at its current value
    expect(uiStore.demoMode).toBe(false);
    // Toast is NOT pushed and the modal must NOT be opened
    expect(uiStore.lastToast).toBeNull();
    expect(uiStore.modal).toBeNull();
  });

  it('updates the title attribute to reflect the next action', async () => {
    const wrapper = mount(DemoModeLauncher);
    const btn = wrapper.find('[data-testid="demo-launcher"]');
    expect(btn.attributes('title')).toBe('点击开启演示模式');

    uiStore.demoMode = true;
    await wrapper.vm.$nextTick();
    expect(btn.attributes('title')).toBe('点击关闭演示模式');
  });

  it('renders a single visible icon glyph with an aria-hidden decoration', () => {
    const wrapper = mount(DemoModeLauncher);
    const icon = wrapper.find('.icon');
    expect(icon.exists()).toBe(true);
    expect(icon.attributes('aria-hidden')).toBe('true');
    expect(icon.text()).toBe('▶');
  });
});
