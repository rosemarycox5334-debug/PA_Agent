/**
 * Theme token validation — parse src/styles/tokens.css and assert that
 * the design system still exposes the contract the rest of the UI relies
 * on. Catches accidental renames or removals when someone tweaks the
 * tokens file, and documents the contract as executable spec.
 *
 * Pure string / regex based (no DOM) so it runs in any environment.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve, dirname } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const tokensPath = resolve(here, './tokens.css');
const css = readFileSync(tokensPath, 'utf8');

// Required tokens by category. Keys map to the var name. Values describe
// the value pattern (oklch / hex / rgba / font stack) we want to enforce.
const required = {
  // Surfaces
  '--bg': 'oklch',
  '--surface-1': 'oklch',
  '--surface-2': 'oklch',
  '--surface-3': 'oklch',
  '--surface-4': 'oklch',
  // Foreground
  '--fg': 'oklch',
  '--fg-2': 'oklch',
  '--fg-3': 'oklch',
  // Borders
  '--border': 'oklch',
  '--border-2': 'oklch',
  // Accent / semantic
  '--accent': 'oklch',
  '--accent-2': 'oklch',
  '--accent-3': 'oklch',
  '--success': 'oklch',
  '--danger': 'oklch',
  '--warning': 'oklch',
  '--info': 'oklch',
  // Charts
  '--chart-up': 'oklch',
  '--chart-down': 'oklch',
  '--chart-grid': 'oklch',
  '--chart-line': 'oklch',
  '--chart-line-2': 'oklch',
  '--chart-line-3': 'oklch',
  '--chart-vol-up': 'oklch',
  '--chart-vol-down': 'oklch',
  // Fonts
  '--font-display': 'font-stack',
  '--font-body': 'font-stack',
  '--font-mono': 'font-stack',
} as const;

function extractVar(cssText: string, name: string): string | null {
  // Tolerate flexible whitespace and trailing semicolons.
  const re = new RegExp(`${name}\\s*:\\s*([^;]+);`);
  const m = cssText.match(re);
  return m ? m[1].trim() : null;
}

describe('theme tokens — tokens.css', () => {
  it('exposes the :root block', () => {
    expect(css).toMatch(/:root\s*\{/);
  });

  for (const [name, kind] of Object.entries(required)) {
    it(`defines ${name} (${kind})`, () => {
      const value = extractVar(css, name);
      expect(value, `expected ${name} to be defined in tokens.css`).not.toBeNull();
      if (kind === 'oklch') {
        expect(value, `${name} should use oklch()`).toMatch(/^oklch\(/i);
      } else if (kind === 'font-stack') {
        // Font stacks always include at least one font family token
        expect(value, `${name} should be a non-empty font stack`).toBeTruthy();
        expect(value!.length).toBeGreaterThan(4);
      }
    });
  }

  it('all chart volume tokens use oklch with alpha (rgba style) for translucency', () => {
    const up = extractVar(css, '--chart-vol-up');
    const down = extractVar(css, '--chart-vol-down');
    expect(up).toMatch(/oklch\([^)]*\/\s*0?\.\d+/i);
    expect(down).toMatch(/oklch\([^)]*\/\s*0?\.\d+/i);
  });

  it('does not leave placeholder color literals (no #000, #fff fallback) in the root block', () => {
    // Match only the :root body (not the comment header).
    const rootMatch = css.match(/:root\s*\{([\s\S]*?)\}/);
    expect(rootMatch).not.toBeNull();
    const rootBody = rootMatch![1];
    expect(rootBody).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  it('exposes a monospace font stack for code/stream panes', () => {
    const mono = extractVar(css, '--font-mono')!;
    // Should mention at least one known monospace family.
    const knownMono = ['JetBrains Mono', 'IBM Plex Mono', 'Menlo', 'Consolas', 'ui-monospace', 'monospace'];
    expect(knownMono.some((f) => mono.includes(f))).toBe(true);
  });

  it('keeps the dark-first palette (bg lightness < 20%)', () => {
    const bg = extractVar(css, '--bg')!;
    const lightness = bg.match(/oklch\(\s*(\d+(?:\.\d+)?)%/i);
    expect(lightness).not.toBeNull();
    const l = parseFloat(lightness![1]);
    expect(l).toBeLessThan(20);
  });
});
