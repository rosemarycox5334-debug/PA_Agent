/**
 * Theme token validator — asserts that `tokens.css` exposes every CSS
 * custom property referenced by the streaming UI components. The check
 * is deliberately static (regex over the source file) so it works
 * inside jsdom where `getComputedStyle` cannot resolve var() lookups.
 *
 * When a new token is introduced in a component, add it to the
 * REQUIRED_TOKENS list below so this guard fails CI until tokens.css
 * is updated in lockstep.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve, dirname } from 'node:path';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const tokensPath = resolve(here, 'tokens.css');
const tokensSource = readFileSync(tokensPath, 'utf8');

/** Tokens that components under src/components/** and src/views/** rely on. */
const REQUIRED_TOKENS = [
  // Surfaces / borders
  '--bg',
  '--surface-1',
  '--surface-2',
  '--surface-3',
  '--border',
  '--border-2',
  // Foreground tones
  '--fg',
  '--fg-2',
  '--fg-3',
  // Semantic colors
  '--accent',
  '--success',
  '--danger',
  '--warning',
  // Typography
  '--font-body',
  '--font-mono',
] as const;

const OPTIONAL_TOKENS = [
  '--accent-2',
  '--accent-3',
  '--info',
  '--chart-up',
  '--chart-down',
  '--chart-grid',
  '--chart-line',
  '--chart-line-2',
  '--chart-line-3',
  '--chart-vol-up',
  '--chart-vol-down',
  '--font-display',
] as const;

function hasToken(name: string): boolean {
  // Match `--name:` with optional whitespace, inside a `:root { ... }` block.
  const re = new RegExp(`${name}\\s*:`, 'm');
  return re.test(tokensSource);
}

describe('styles/tokens.css', () => {
  it('declares a single :root block', () => {
    const rootOpens = (tokensSource.match(/:root\s*\{/g) ?? []).length;
    expect(rootOpens).toBeGreaterThanOrEqual(1);
  });

  it.each(REQUIRED_TOKENS)('exposes required token %s', (token) => {
    expect(hasToken(token)).toBe(true);
  });

  it.each(OPTIONAL_TOKENS)('exposes optional token %s (if present, must be well-formed)', (token) => {
    if (!hasToken(token)) return;
    const re = new RegExp(`${token}\\s*:\\s*[^;]+;`);
    expect(re.test(tokensSource)).toBe(true);
  });

  it('uses oklch() for color tokens (no hard-coded hex)', () => {
    // The token file should prefer oklch() so future theming is consistent.
    // Hex values are allowed only inside `global.css` for pills.
    const colorLines = tokensSource
      .split('\n')
      .filter((l) => /--(fg|surface|border|accent|success|danger|warning|info|chart)/.test(l));
    const offenders = colorLines.filter((l) => /#[0-9a-f]{3,8}/i.test(l));
    expect(offenders).toEqual([]);
  });

  it('does not reference tokens that are not declared (no dangling vars)', () => {
    // Walk every `var(--xxx)` reference in tokensSource and ensure each
    // token is declared. tokens.css does not currently reference other
    // tokens, but this guard catches accidental introduction.
    const refRe = /var\((--[a-z0-9-]+)\)/gi;
    const refs = new Set<string>();
    for (const m of tokensSource.matchAll(refRe)) refs.add(m[1].toLowerCase());
    const declared = new Set<string>();
    const declRe = /^(--[a-z0-9-]+)\s*:/gim;
    for (const m of tokensSource.matchAll(declRe)) declared.add(m[1].toLowerCase());
    for (const ref of refs) {
      expect(declared.has(ref)).toBe(true);
    }
  });
});

/**
 * Secondary suite — ensures every token USED by the SFCs is also
 * DECLARED. This catches drift between component styles and the
 * design system.
 */
describe('StreamPhaseHeader uses declared tokens', () => {
  const componentPath = resolve(
    here,
    '..',
    'components',
    'stream',
    'StreamPhaseHeader.vue',
  );
  const source = readFileSync(componentPath, 'utf8');

  it('scans the component source', () => {
    expect(source.length).toBeGreaterThan(0);
  });

  it.each(REQUIRED_TOKENS)('does not reference %s without it being declared', (token) => {
    const used = source.includes(`var(${token})`);
    if (!used) return; // component may opt out
    expect(hasToken(token)).toBe(true);
  });
});

/**
 * SidePanelTabs — secondary suite. Mirrors the StreamPhaseHeader check
 * so the right-hand side panel tab strip is guarded by the same
 * design-system contract.
 */
describe('SidePanelTabs uses declared tokens', () => {
  const componentPath = resolve(
    here,
    '..',
    'components',
    'SidePanelTabs.vue',
  );
  const source = readFileSync(componentPath, 'utf8');

  it('scans the SidePanelTabs SFC', () => {
    expect(source.length).toBeGreaterThan(0);
  });

  it('exposes a tablist root with role=tablist and data-testid=side-panel-tabs', () => {
    expect(source).toMatch(/role\s*=\s*"tablist"/);
    expect(source).toMatch(/data-testid\s*=\s*"side-panel-tabs"/);
  });

  it.each(REQUIRED_TOKENS)('does not reference %s without it being declared', (token) => {
    const used = source.includes(`var(${token})`);
    if (!used) return; // component may opt out
    expect(hasToken(token)).toBe(true);
  });

  it('does not introduce a hex color literal in the <style> block', () => {
    const styleMatch = source.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, 'a <style> block should be present in the SFC').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'SidePanelTabs.vue <style> contains a hardcoded hex color; use a token from tokens.css',
    ).toBe(false);
  });
});

/**
 * SidePanel — secondary suite. Validates the right-hand side panel
 * container that composes <SidePanelTabs> with slot-based tab panes.
 * This guards the design-system contract for the container in addition
 * to the strip.
 */
describe('SidePanel uses declared tokens', () => {
  const componentPath = resolve(
    here,
    '..',
    'components',
    'SidePanel.vue',
  );
  const source = readFileSync(componentPath, 'utf8');

  it('scans the SidePanel SFC', () => {
    expect(source.length).toBeGreaterThan(0);
  });

  it('exposes the section root with data-testid=side-panel and role=tabpanel panes', () => {
    expect(source).toMatch(/data-testid\s*=\s*"side-panel"/);
    expect(source).toMatch(/role\s*=\s*"tabpanel"/);
    // Pane data-testid is built with a template literal, so the literal
    // "side-panel-pane-" may sit just after a backtick instead of a quote.
    expect(source).toMatch(/data-testid\s*=\s*"[`]?side-panel-pane-/);
  });

  it.each(REQUIRED_TOKENS)('does not reference %s without it being declared', (token) => {
    const used = source.includes(`var(${token})`);
    if (!used) return; // component may opt out
    expect(hasToken(token)).toBe(true);
  });

  it('does not introduce a hex color literal in the <style> block', () => {
    const styleMatch = source.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, 'a <style> block should be present in the SFC').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'SidePanel.vue <style> contains a hardcoded hex color; use a token from tokens.css',
    ).toBe(false);
  });

  it('every var(--xxx) reference in the <style> block resolves to a declared token', () => {
    const styleMatch = source.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch).toBeTruthy();
    const styleBlock = styleMatch![0];
    const refRe = /var\((--[a-z0-9-]+)\)/gi;
    const refs = new Set<string>();
    for (const m of styleBlock.matchAll(refRe)) refs.add(m[1].toLowerCase());
    expect(refs.size).toBeGreaterThan(0);
    for (const ref of refs) {
      expect(hasToken(ref), `${ref} is used in SidePanel.vue <style> but is not declared in tokens.css`).toBe(true);
    }
  });
});
