/**
 * UI store — global chrome state shared between the application header,
 * the side panel, and modal launchers. Mirrors the conventions of
 * `stores/settings.ts`: a single `reactive` object exported as `uiStore`,
 * plus a few computed helpers pinned onto the store so SFCs can read
 * them as plain property accessors (uiStore.sidebarOpen, etc).
 *
 * Intentionally narrow scope:
 *  - sidebar collapsed / expanded flag (drives AppHeader toggle)
 *  - active modal id (null when no modal is open)
 *  - demo mode flag + launcher state (DemoModeLauncher subscribes)
 *  - auto-incremental toggle (AutoIncrementalBadge subscribes)
 *  - last toast (transient, auto-cleared after `toastTtlMs`)
 *
 * No DOM access. The store is the source of truth; the components
 * only render + dispatch. This keeps every SFC testable in isolation
 * by mutating the store directly.
 */
import { computed, reactive } from 'vue';

export type ModalId =
  | null
  | 'settings'
  | 'validation-debug'
  | 'tv-blocked'
  | 'demo-launcher';

export interface ToastPayload {
  id: number;
  text: string;
  tone: 'info' | 'success' | 'warn' | 'error';
}

const DEFAULT_TOAST_TTL_MS = 2400;
let toastSeq = 0;

export interface UiState {
  sidebarOpen: boolean;
  modal: ModalId;
  demoMode: boolean;
  demoRunning: boolean;
  autoIncremental: boolean;
  lastToast: ToastPayload | null;
  toastTtlMs: number;
}

export const uiStore = reactive<UiState & {
  openModal(id: Exclude<ModalId, null>): void;
  closeModal(): void;
  toggleSidebar(): void;
  setSidebar(open: boolean): void;
  setDemoMode(on: boolean): void;
  setDemoRunning(running: boolean): void;
  setAutoIncremental(on: boolean): void;
  pushToast(text: string, tone?: ToastPayload['tone']): void;
  clearToast(): void;
}>({
  sidebarOpen: true,
  modal: null,
  demoMode: false,
  demoRunning: false,
  autoIncremental: false,
  lastToast: null,
  toastTtlMs: DEFAULT_TOAST_TTL_MS,

  openModal(id) {
    this.modal = id;
  },

  closeModal() {
    this.modal = null;
  },

  toggleSidebar() {
    this.sidebarOpen = !this.sidebarOpen;
  },

  setSidebar(open) {
    this.sidebarOpen = open;
  },

  setDemoMode(on) {
    this.demoMode = on;
    if (!on) this.demoRunning = false;
  },

  setDemoRunning(running) {
    this.demoRunning = running;
  },

  setAutoIncremental(on) {
    this.autoIncremental = on;
  },

  pushToast(text, tone = 'info') {
    toastSeq += 1;
    this.lastToast = { id: toastSeq, text, tone };
  },

  clearToast() {
    this.lastToast = null;
  },
});

/**
 * Computed accessors attached to the reactive store via defineProperties
 * so consumers can use `uiStore.isModalOpen` without unwrapping a ref.
 */
export const isModalOpen = computed(() => uiStore.modal !== null);
export const isDemoActive = computed(() => uiStore.demoMode || uiStore.demoRunning);
export const headerPillText = computed(() => {
  if (uiStore.demoRunning) return '演示运行中…';
  if (uiStore.demoMode) return '演示模式';
  if (uiStore.autoIncremental) return '自动增量 · 开';
  return null;
});

Object.defineProperties(uiStore, {
  isModalOpen: { get: () => isModalOpen.value },
  isDemoActive: { get: () => isDemoActive.value },
  headerPillText: { get: () => headerPillText.value },
});
