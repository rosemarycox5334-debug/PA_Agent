/**
 * ui store — vitest unit tests.
 *
 * Coverage:
 *  - Initial state matches the documented contract.
 *  - openModal / closeModal mutate the active modal slot.
 *  - toggleSidebar / setSidebar flip the sidebar flag.
 *  - setDemoMode / setDemoRunning maintain the invariant that turning
 *    demo mode off also clears the running flag.
 *  - setAutoIncremental mutates the auto-incremental flag.
 *  - pushToast emits a monotonically-increasing id and respects tone.
 *  - Computed accessors (isModalOpen, isDemoActive, headerPillText) read
 *    as plain property accessors on the store.
 *  - reset() via re-import is not provided (the store is a singleton),
 *    so each test uses a dedicated `reset()` helper to restore defaults.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { uiStore } from './ui';

function reset(): void {
  uiStore.sidebarOpen = true;
  uiStore.modal = null;
  uiStore.demoMode = false;
  uiStore.demoRunning = false;
  uiStore.autoIncremental = false;
  uiStore.lastToast = null;
  uiStore.toastTtlMs = 2400;
}

describe('stores/ui.ts', () => {
  beforeEach(reset);
  afterEach(reset);

  it('exposes the documented initial state', () => {
    // After reset() the store matches the initial defaults.
    expect(uiStore.sidebarOpen).toBe(true);
    expect(uiStore.modal).toBeNull();
    expect(uiStore.demoMode).toBe(false);
    expect(uiStore.demoRunning).toBe(false);
    expect(uiStore.autoIncremental).toBe(false);
    expect(uiStore.lastToast).toBeNull();
    expect(uiStore.toastTtlMs).toBeGreaterThan(0);
  });

  it('openModal() sets the active modal id; closeModal() clears it', () => {
    uiStore.openModal('settings');
    expect(uiStore.modal).toBe('settings');
    uiStore.openModal('demo-launcher');
    expect(uiStore.modal).toBe('demo-launcher');
    uiStore.closeModal();
    expect(uiStore.modal).toBeNull();
  });

  it('toggleSidebar() flips the sidebar flag both ways', () => {
    expect(uiStore.sidebarOpen).toBe(true);
    uiStore.toggleSidebar();
    expect(uiStore.sidebarOpen).toBe(false);
    uiStore.toggleSidebar();
    expect(uiStore.sidebarOpen).toBe(true);
  });

  it('setSidebar(open) overrides the current flag', () => {
    uiStore.setSidebar(false);
    expect(uiStore.sidebarOpen).toBe(false);
    uiStore.setSidebar(true);
    expect(uiStore.sidebarOpen).toBe(true);
  });

  it('turning demo mode off also clears demoRunning (invariant)', () => {
    uiStore.setDemoMode(true);
    uiStore.setDemoRunning(true);
    expect(uiStore.demoMode).toBe(true);
    expect(uiStore.demoRunning).toBe(true);
    uiStore.setDemoMode(false);
    expect(uiStore.demoMode).toBe(false);
    expect(uiStore.demoRunning).toBe(false);
  });

  it('turning demo mode on does not auto-start a running replay', () => {
    uiStore.setDemoMode(true);
    expect(uiStore.demoRunning).toBe(false);
  });

  it('setDemoRunning() updates the running flag independently of demo mode', () => {
    uiStore.setDemoRunning(true);
    expect(uiStore.demoRunning).toBe(true);
    expect(uiStore.demoMode).toBe(false);
    uiStore.setDemoRunning(false);
    expect(uiStore.demoRunning).toBe(false);
  });

  it('setAutoIncremental() toggles the auto-incremental flag', () => {
    uiStore.setAutoIncremental(true);
    expect(uiStore.autoIncremental).toBe(true);
    uiStore.setAutoIncremental(false);
    expect(uiStore.autoIncremental).toBe(false);
  });

  it('pushToast() emits a toast with a monotonically-increasing id', () => {
    uiStore.pushToast('first', 'info');
    const first = uiStore.lastToast;
    expect(first).not.toBeNull();
    expect(first!.text).toBe('first');
    expect(first!.tone).toBe('info');

    uiStore.pushToast('second', 'success');
    const second = uiStore.lastToast!;
    expect(second.id).toBeGreaterThan(first!.id);
    expect(second.text).toBe('second');
    expect(second.tone).toBe('success');
  });

  it('pushToast() defaults tone to "info" when not provided', () => {
    uiStore.pushToast('hi');
    expect(uiStore.lastToast?.tone).toBe('info');
  });

  it('clearToast() removes the active toast', () => {
    uiStore.pushToast('x');
    expect(uiStore.lastToast).not.toBeNull();
    uiStore.clearToast();
    expect(uiStore.lastToast).toBeNull();
  });

  describe('computed accessors attached via defineProperties', () => {
    it('isModalOpen reflects whether a modal is active', () => {
      expect(uiStore.isModalOpen).toBe(false);
      uiStore.openModal('validation-debug');
      expect(uiStore.isModalOpen).toBe(true);
      uiStore.closeModal();
      expect(uiStore.isModalOpen).toBe(false);
    });

    it('isDemoActive is true when demo is on or a replay is running', () => {
      expect(uiStore.isDemoActive).toBe(false);
      uiStore.setDemoMode(true);
      expect(uiStore.isDemoActive).toBe(true);
      uiStore.setDemoMode(false);
      expect(uiStore.isDemoActive).toBe(false);
      uiStore.setDemoRunning(true);
      expect(uiStore.isDemoActive).toBe(true);
    });

    it('headerPillText surfaces the highest-priority label first', () => {
      expect(uiStore.headerPillText).toBeNull();
      uiStore.setAutoIncremental(true);
      expect(uiStore.headerPillText).toBe('自动增量 · 开');
      uiStore.setDemoMode(true);
      // Demo mode label outranks the auto-incremental pill.
      expect(uiStore.headerPillText).toBe('演示模式');
      uiStore.setDemoRunning(true);
      // A running replay outranks the static "demo mode" label.
      expect(uiStore.headerPillText).toBe('演示运行中…');
      // Tearing down the running replay reveals the static demo label
      // (we use setDemoRunning directly, since setDemoMode(false) is
      // documented to also clear demoRunning as a side-effect).
      uiStore.setDemoRunning(false);
      expect(uiStore.headerPillText).toBe('演示模式');
      uiStore.setDemoMode(false);
      expect(uiStore.headerPillText).toBe('自动增量 · 开');
      uiStore.setAutoIncremental(false);
      expect(uiStore.headerPillText).toBeNull();
    });
  });
});
