/**
 * StreamCharStats — vitest unit tests.
 *
 * Coverage:
 *  - Renders zero state when the stream is empty.
 *  - Sums message text length per stage (阶段一 / 阶段二 / 追问).
 *  - Grand total = sum across all messages including those with no stage.
 *  - Updates reactively when the store mutates after mount.
 *  - Locale-formats the per-bucket char count via toLocaleString.
 *  - Reflects the new totals in the `data-total-*` attributes used by tests.
 *  - Uses CSS custom properties from tokens.css (no hardcoded hex).
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mount } from '@vue/test-utils';
import StreamCharStats from './StreamCharStats.vue';
import { streamStore, type StreamMessage } from '@/stores/stream';

const __dirname = dirname(fileURLToPath(import.meta.url));
const sfcPath = resolve(__dirname, 'StreamCharStats.vue');

function readSfc(): string {
  return readFileSync(sfcPath, 'utf-8');
}

function resetStores(): void {
  streamStore.reset();
}

function pushMsg(stage: StreamMessage['stage'], text: string): void {
  streamStore.push({
    title: `t-${stage ?? 'na'}-${text.length}`,
    text,
    time: '00:00:00',
    stage,
  });
}

describe('StreamCharStats.vue', () => {
  beforeEach(resetStores);
  afterEach(resetStores);

  it('renders all buckets at zero and zero total when the store is empty', () => {
    const wrapper = mount(StreamCharStats);
    const root = wrapper.get('[data-testid="char-stats"]');
    expect(root.attributes('data-total-messages')).toBe('0');
    expect(root.attributes('data-total-chars')).toBe('0');
    expect(root.get('[data-testid="stat-stage-1-chars"]').text()).toBe('0');
    expect(root.get('[data-testid="stat-stage-2-chars"]').text()).toBe('0');
    expect(root.get('[data-testid="stat-followup-chars"]').text()).toBe('0');
    expect(root.get('[data-testid="stat-total-chars"]').text()).toBe('0');
    expect(root.get('[data-testid="stat-total-messages"]').text()).toBe('0 条');
  });

  it('counts characters per stage based on text.length', () => {
    // '阶段一内容' is 5 chars; '阶段二更长的内容' is 8 chars; 'a'.repeat(120) is 120.
    pushMsg('1', '阶段一内容');
    pushMsg('1', 'abcd');
    pushMsg('2', '阶段二更长的内容');
    pushMsg('followup', 'a'.repeat(120));
    const wrapper = mount(StreamCharStats);
    const root = wrapper.get('[data-testid="char-stats"]');
    expect(root.get('[data-testid="stat-stage-1-chars"]').text()).toBe('9');
    expect(root.get('[data-testid="stat-stage-2-chars"]').text()).toBe('8');
    expect(root.get('[data-testid="stat-followup-chars"]').text()).toBe('120');
    expect(root.get('[data-testid="stat-total-chars"]').text()).toBe('137');
    expect(root.get('[data-testid="stat-total-messages"]').text()).toBe('4 条');
    expect(root.attributes('data-total-chars')).toBe('137');
  });

  it('includes messages without a stage in the grand total but not in any bucket', () => {
    pushMsg(undefined as unknown as StreamMessage['stage'], 'meta-info');
    pushMsg('1', 'one');
    const wrapper = mount(StreamCharStats);
    const root = wrapper.get('[data-testid="char-stats"]');
    // Untagged message: not counted in any named stage.
    expect(root.get('[data-testid="stat-stage-1-chars"]').text()).toBe('3');
    expect(root.get('[data-testid="stat-stage-2-chars"]').text()).toBe('0');
    expect(root.get('[data-testid="stat-followup-chars"]').text()).toBe('0');
    // Grand total includes the untagged message.
    expect(root.get('[data-testid="stat-total-chars"]').text()).toBe('12');
    expect(root.get('[data-testid="stat-total-messages"]').text()).toBe('2 条');
  });

  it('locale-formats counts above 1000 with thousands separators', () => {
    // toLocaleString in jsdom uses 'en-US' by default and emits commas.
    pushMsg('1', 'x'.repeat(1234));
    const wrapper = mount(StreamCharStats);
    expect(wrapper.get('[data-testid="stat-stage-1-chars"]').text()).toBe('1,234');
    expect(wrapper.get('[data-testid="stat-total-chars"]').text()).toBe('1,234');
  });

  it('reacts to new messages pushed after mount', async () => {
    const wrapper = mount(StreamCharStats);
    expect(wrapper.get('[data-testid="stat-total-chars"]').text()).toBe('0');
    pushMsg('1', 'first');
    pushMsg('followup', 'second-message');
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="stat-stage-1-chars"]').text()).toBe('5');
    // 'second-message' is 14 characters: s e c o n d - m e s s a g e
    expect(wrapper.get('[data-testid="stat-followup-chars"]').text()).toBe('14');
    expect(wrapper.get('[data-testid="stat-total-chars"]').text()).toBe('19');
    expect(wrapper.get('[data-testid="stat-total-messages"]').text()).toBe('2 条');
  });

  it('updates when the store is reset back to empty', async () => {
    pushMsg('2', 'x'.repeat(7));
    const wrapper = mount(StreamCharStats);
    expect(wrapper.get('[data-testid="stat-total-chars"]').text()).toBe('7');
    streamStore.reset();
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-testid="stat-total-chars"]').text()).toBe('0');
    expect(wrapper.get('[data-testid="stat-total-messages"]').text()).toBe('0 条');
  });

  it('emits an aria-label that includes both the total messages and chars', () => {
    pushMsg('1', 'a');
    pushMsg('2', 'bc');
    pushMsg('followup', 'def');
    const wrapper = mount(StreamCharStats);
    const root = wrapper.get('[data-testid="char-stats"]');
    const aria = root.attributes('aria-label') ?? '';
    expect(aria).toContain('3');
    expect(aria).toContain('6');
    expect(aria).toContain('字符统计');
  });

  it('renders the three named stages with a stable, ordered testid surface', () => {
    const wrapper = mount(StreamCharStats);
    const html = wrapper.html();
    expect(html).toContain('data-testid="stat-stage-1"');
    expect(html).toContain('data-testid="stat-stage-2"');
    expect(html).toContain('data-testid="stat-followup"');
    expect(html).toContain('data-testid="stat-total"');
    // The order in DOM is 阶段一 → 阶段二 → 追问 → 总计.
    const idxS1 = html.indexOf('stat-stage-1');
    const idxS2 = html.indexOf('stat-stage-2');
    const idxFollowup = html.indexOf('stat-followup');
    const idxTotal = html.indexOf('stat-total"');
    expect(idxS1).toBeGreaterThan(-1);
    expect(idxS1).toBeLessThan(idxS2);
    expect(idxS2).toBeLessThan(idxFollowup);
    expect(idxFollowup).toBeLessThan(idxTotal);
  });

  it('uses CSS custom properties from tokens.css — no hardcoded hex in <style>', () => {
    const sfc = readSfc();
    // Read the SFC source directly: the comment in StreamPhaseHeader.test
    // already explained that jsdom can't resolve var() lookups, and the
    // reliable way to assert on token references is to read the .vue
    // file's <style> block. The theme-token spec in src/test/tokens.spec.ts
    // also uses this approach.
    const styleMatch = sfc.match(/<style[\s\S]*?<\/style>/);
    expect(styleMatch, 'a <style> block should be present in the SFC').toBeTruthy();
    const styleBlock = styleMatch![0];
    // Sanity-check the major tokens are referenced in the SFC's style block.
    for (const token of [
      'var(--surface-1)',
      'var(--fg)',
      'var(--fg-2)',
      'var(--fg-3)',
      'var(--border)',
      'var(--border-2)',
      'var(--font-body)',
      'var(--font-mono)',
    ]) {
      expect(styleBlock, `expected ${token} in <style>`).toContain(token);
    }
    // No hex literal should appear inside the <style> block.
    expect(
      /#[0-9a-fA-F]{3,8}\b/.test(styleBlock),
      'StreamCharStats.vue <style> contains a hardcoded hex color; use a token from tokens.css',
    ).toBe(false);
    // Sanity: the SFC still mounts cleanly.
    const wrapper = mount(StreamCharStats);
    expect(wrapper.get('[data-testid="char-stats"]').exists()).toBe(true);
  });
});
