/**
 * Global test setup — installs matchMedia (jsdom does not provide it),
 * which @vueuse/core and other libs touch during mount, and stubs the
 * minimal browser APIs the stream store expects to find.
 */
import { vi } from 'vitest';

if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

// TextEncoder/TextDecoder exist in jsdom, but stub for safety in case
// the test environment ever swaps to happy-dom.
if (typeof globalThis.TextEncoder === 'undefined') {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  globalThis.TextEncoder = require('util').TextEncoder;
  globalThis.TextDecoder = require('util').TextDecoder;
}
