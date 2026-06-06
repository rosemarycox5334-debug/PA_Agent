/**
 * Theme token validation — guards against the new SPA drifting from the
 * legacy palette. Two contracts are enforced:
 *
 * 1. Every token referenced from `StreamContextWarning.vue` exists in
 *    `src/styles/tokens.css`. New components added to the stream panel
 *    are expected to follow the same convention; an unrecognised
 *    `--name` means a missing token.
 *
 * 2. `tokens.css` itself contains the full set of "core" tokens (bg, fg,
 *    accent, semantic colors, chart palette) so that any component, not
 *    just this one, can rely on them.
 *
 * If you add a token, update both lists. If you remove one, make sure
 * no .vue file still references it.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(__dirname, '..', '..');
const tokensPath = resolve(projectRoot, 'src/styles/tokens.css');
const streamComponentsDir = resolve(projectRoot, 'src/components/stream');
const contextWarningPath = resolve(streamComponentsDir, 'StreamContextWarning.vue');
const charStatsPath = resolve(streamComponentsDir, 'StreamCharStats.vue');
const debugBundlePath = resolve(
  projectRoot,
  'src/components/debug/DebugExceptionBundle.vue',
);
const demoLauncherPath = resolve(
  projectRoot,
  'src/components/DemoModeLauncher.vue',
);
const appHeaderPath = resolve(
  projectRoot,
  'src/components/AppHeader.vue',
);
const autoIncrementalBadgePath = resolve(
  projectRoot,
  'src/components/AutoIncrementalBadge.vue',
);
const demoPickerPath = resolve(
  projectRoot,
  'src/components/demo/DemoRecordPicker.vue',
);

function readSource(path: string): string {
  return readFileSync(path, 'utf-8');
}

function extractTokenNames(css: string): Set<string> {
  const names = new Set<string>();
  // Match `--name:` (definition) and `var(--name)` (consumption).
  const re = /--([a-z0-9-]+)\s*:/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css)) !== null) {
    names.add(m[1].toLowerCase());
  }
  return names;
}

function extractVarReferences(source: string): Set<string> {
  const names = new Set<string>();
  const re = /var\(\s*--([a-z0-9-]+)\s*(?:,[^)]*)?\)/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(source)) !== null) {
    names.add(m[1].toLowerCase());
  }
  // Components that paint to a Canvas pull tokens through a
  // `readToken('--name', fallback)` helper instead of `var()`. Capture
  // those too so the asserted vocabulary is the full surface.
  const reCanvas = /readToken\(\s*['"]--([a-z0-9-]+)['"]/gi;
  while ((m = reCanvas.exec(source)) !== null) {
    names.add(m[1].toLowerCase());
  }
  return names;
}

describe('Theme tokens', () => {
  const tokens = readSource(tokensPath);
  const tokenNames = extractTokenNames(tokens);

  it('tokens.css defines the full core palette', () => {
    const required = [
      'bg',
      'surface-1',
      'surface-2',
      'surface-3',
      'surface-4',
      'fg',
      'fg-2',
      'fg-3',
      'border',
      'border-2',
      'accent',
      'accent-2',
      'accent-3',
      'success',
      'danger',
      'warning',
      'info',
      'chart-up',
      'chart-down',
      'chart-grid',
      'chart-line',
      'chart-line-2',
      'chart-line-3',
      'chart-vol-up',
      'chart-vol-down',
    ];
    for (const t of required) {
      expect(tokenNames.has(t), `token --${t} should be defined in tokens.css`).toBe(true);
    }
  });

  it('StreamContextWarning.vue only references tokens that exist in tokens.css', () => {
    const component = readSource(contextWarningPath);
    const referenced = extractVarReferences(component);
    // Some entries are read via getComputedStyle as fallbacks; make sure the
    // declared set is a strict subset of the file's token vocabulary.
    for (const name of referenced) {
      expect(
        tokenNames.has(name),
        `StreamContextWarning.vue references --${name} which is not declared in tokens.css`,
      ).toBe(true);
    }
    // And the component should reference at least the most important tokens
    // — guards against accidental removal of theming.
    const expected = ['--surface-1', '--border', '--fg', '--accent', '--warning', '--danger'];
    for (const t of expected) {
      expect(referenced.has(t.replace('--', '')), `expected ${t} to be used`).toBe(true);
    }
  });

  it('StreamContextWarning.vue does not hardcode hex colors in template styles', () => {
    const component = readSource(contextWarningPath);
    // Allow hex literals inside <script> for the canvas fallback shim, but
    // not in the <style> block — colors there MUST come from tokens.
    const styleMatch = component.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, '<style> block should exist').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'found a hardcoded hex color in <style>; use a token from tokens.css',
    ).toBe(false);
  });

  it('StreamCharStats.vue only references tokens that exist in tokens.css', () => {
    const component = readSource(charStatsPath);
    const referenced = extractVarReferences(component);
    for (const name of referenced) {
      expect(
        tokenNames.has(name),
        `StreamCharStats.vue references --${name} which is not declared in tokens.css`,
      ).toBe(true);
    }
    // Char stats should at minimum pull colors, border, surface, and the
    // body/mono fonts from the design system.
    const expected = [
      '--surface-1',
      '--fg',
      '--fg-2',
      '--fg-3',
      '--border',
      '--border-2',
      '--font-body',
      '--font-mono',
    ];
    for (const t of expected) {
      expect(referenced.has(t.replace('--', '')), `expected ${t} to be used`).toBe(true);
    }
  });

  it('StreamCharStats.vue does not hardcode hex colors in template styles', () => {
    const component = readSource(charStatsPath);
    const styleMatch = component.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, '<style> block should exist').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'StreamCharStats.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });

  it('DebugExceptionBundle.vue only references tokens that exist in tokens.css', () => {
    const component = readSource(debugBundlePath);
    const referenced = extractVarReferences(component);
    for (const name of referenced) {
      expect(
        tokenNames.has(name),
        `DebugExceptionBundle.vue references --${name} which is not declared in tokens.css`,
      ).toBe(true);
    }
    // The debug bundle is wired into the dark stream surface, so it MUST
    // pull in the major chrome tokens plus the chart/exception palette
    // — guards against accidentally hard-coding any of these.
    const expected = [
      '--surface-1',
      '--surface-2',
      '--surface-3',
      '--fg',
      '--fg-2',
      '--fg-3',
      '--border',
      '--border-2',
      '--accent',
      '--accent-3',
      '--success',
      '--warning',
      '--danger',
      '--info',
      '--chart-line',
      '--chart-line-2',
      '--font-body',
      '--font-mono',
    ];
    for (const t of expected) {
      expect(
        referenced.has(t.replace('--', '')),
        `expected ${t} to be used in DebugExceptionBundle.vue`,
      ).toBe(true);
    }
  });

  it('DebugExceptionBundle.vue does not hardcode hex colors in template styles', () => {
    const component = readSource(debugBundlePath);
    const styleMatch = component.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, '<style> block should exist').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'DebugExceptionBundle.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });

  it('DemoModeLauncher.vue only references tokens that exist in tokens.css', () => {
    const component = readSource(demoLauncherPath);
    const referenced = extractVarReferences(component);
    for (const name of referenced) {
      expect(
        tokenNames.has(name),
        `DemoModeLauncher.vue references --${name} which is not declared in tokens.css`,
      ).toBe(true);
    }
    // The pill is a chrome element; it must at least consume surface / border /
    // fg / font tokens, plus the semantic tones for the on + running states.
    const expected = [
      '--surface-2',
      '--surface-3',
      '--border',
      '--fg',
      '--fg-2',
      '--warning',
      '--info',
      '--bg',
      '--font-body',
    ];
    for (const t of expected) {
      expect(
        referenced.has(t.replace('--', '')),
        `expected ${t} to be used in DemoModeLauncher.vue`,
      ).toBe(true);
    }
  });

  it('DemoModeLauncher.vue does not hardcode hex colors in template styles', () => {
    const component = readSource(demoLauncherPath);
    const styleMatch = component.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, '<style> block should exist').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'DemoModeLauncher.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });

  it('AutoIncrementalBadge.vue only references tokens that exist in tokens.css', () => {
    const component = readSource(autoIncrementalBadgePath);
    const referenced = extractVarReferences(component);
    for (const name of referenced) {
      expect(
        tokenNames.has(name),
        `AutoIncrementalBadge.vue references --${name} which is not declared in tokens.css`,
      ).toBe(true);
    }
    // The badge is a chrome pill — must consume surfaces, fg, border, accent,
    // and body font, plus the bg that the on-state text paints onto.
    const expected = [
      '--surface-2',
      '--surface-3',
      '--fg',
      '--fg-2',
      '--fg-3',
      '--border',
      '--accent',
      '--accent-2',
      '--bg',
      '--font-body',
    ];
    for (const t of expected) {
      expect(
        referenced.has(t.replace('--', '')),
        `expected ${t} to be used in AutoIncrementalBadge.vue`,
      ).toBe(true);
    }
  });

  it('AutoIncrementalBadge.vue does not hardcode hex colors in template styles', () => {
    const component = readSource(autoIncrementalBadgePath);
    const styleMatch = component.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, '<style> block should exist').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'AutoIncrementalBadge.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });

  it('AppHeader.vue only references tokens that exist in tokens.css', () => {
    const component = readSource(appHeaderPath);
    const referenced = extractVarReferences(component);
    for (const name of referenced) {
      expect(
        tokenNames.has(name),
        `AppHeader.vue references --${name} which is not declared in tokens.css`,
      ).toBe(true);
    }
    // The header is the topmost chrome strip — must pull in every surface
    // tier, the fg ladder, the border, the semantic success/danger tones
    // for the status dot, and both body + mono fonts (the subtitle is mono).
    const expected = [
      '--surface-1',
      '--surface-2',
      '--surface-3',
      '--fg',
      '--fg-2',
      '--fg-3',
      '--border',
      '--success',
      '--danger',
      '--font-body',
      '--font-mono',
    ];
    for (const t of expected) {
      expect(
        referenced.has(t.replace('--', '')),
        `expected ${t} to be used in AppHeader.vue`,
      ).toBe(true);
    }
  });

  it('AppHeader.vue does not hardcode hex colors in template styles', () => {
    const component = readSource(appHeaderPath);
    const styleMatch = component.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, '<style> block should exist').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'AppHeader.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });

  it('DemoRecordPicker.vue only references tokens that exist in tokens.css', () => {
    const component = readSource(demoPickerPath);
    const referenced = extractVarReferences(component);
    // The set is a strict subset of the file's vocabulary — including
    // tokens that the Canvas 2D viz reads via the readToken() helper.
    for (const name of referenced) {
      expect(
        tokenNames.has(name),
        `DemoRecordPicker.vue references --${name} which is not declared in tokens.css`,
      ).toBe(true);
    }
    // The picker renders a chrome list (surfaces + border + fg ladder) AND
    // drives a Canvas 2D viz that consumes the phase/branch palette. Make
    // sure the major tokens for both surfaces are present.
    const expected = [
      '--surface-1',
      '--surface-2',
      '--surface-3',
      '--surface-4',
      '--fg',
      '--fg-2',
      '--fg-3',
      '--border',
      '--border-2',
      '--accent',
      '--accent-3',
      '--danger',
      '--chart-up',
      '--chart-down',
      '--chart-line',
      '--font-body',
      '--font-mono',
    ];
    for (const t of expected) {
      expect(
        referenced.has(t.replace('--', '')),
        `expected ${t} to be used in DemoRecordPicker.vue`,
      ).toBe(true);
    }
  });

  it('DemoRecordPicker.vue does not hardcode hex colors in template styles', () => {
    const component = readSource(demoPickerPath);
    const styleMatch = component.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, '<style> block should exist').toBeTruthy();
    const styleBlock = styleMatch![0];
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'DemoRecordPicker.vue <style> contains a hardcoded hex color; use tokens.css',
    ).toBe(false);
  });
});
