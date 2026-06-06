/**
 * Debug store — captures the most recent AI turns exchanged with the model
 * (Stage1, Stage2, Followup-N, Incremental) and the validation/exception
 * metadata that accompanies them.
 *
 * Mirrors the responsibilities of `pa_agent.gui.debug_widget.DebugWidget`
 * for the new Vue 3 SPA: every turn carries the system prompt, the user
 * prompt, the raw HTTP response, and the validation result / exception
 * classification.  A turn is therefore a self-contained bundle that
 * `DebugExceptionBundle.vue` can render without re-fetching.
 *
 * Consumers should mutate this store via the action methods so that the
 * loading / error state stays consistent.  Direct mutation of `turns` is
 * allowed for tests (the bundle reads `debugStore.turns` reactively).
 */
import { computed, reactive } from 'vue';
import { api } from '@/api/client';

export type TurnKind = 'stage1' | 'stage2' | 'followup' | 'incremental' | 'unknown';

export type ExceptionClass =
  | 'none'
  | 'network'
  | 'auth'
  | 'rate_limit'
  | 'schema'
  | 'timeout'
  | 'validation'
  | 'unknown';

export interface TurnException {
  /** High-level category that drives the icon & colour in the bundle UI. */
  klass: ExceptionClass;
  /** Human-readable message — typically the `validation_info` payload. */
  message: string;
  /** Optional short tag rendered as a pill (e.g. RETRY, MUTED). */
  tag?: string;
  /** Free-form stacktrace / retry attempts / extra context. */
  details?: string;
}

export interface TurnTraceNode {
  id: string;
  title: string;
  outcome?: string;
  phase?: 'gate' | 'decision';
}

export interface DebugTurn {
  /** Stable identifier used for v-for keys and selection. */
  id: string;
  /** Display label, e.g. "Stage1", "Stage2", "Followup-1". */
  label: string;
  kind: TurnKind;
  /** Wall-clock timestamp the turn completed (epoch millis). */
  ts: number;
  /** Stage prefix used for colouring and the timeline. */
  stage: '1' | '2' | 'followup' | 'incremental';
  /** System prompt sent to the model. */
  system_prompt: string;
  /** User prompt / market brief sent to the model. */
  user_prompt: string;
  /** Raw response payload (HTTP status, headers, body, reasoning_content, content). */
  raw_response: Record<string, unknown>;
  /** Validation result string. */
  validation_info: string;
  /** Optional exception / retry metadata. */
  exception: TurnException;
  /** Optional decision-flow nodes associated with the turn. */
  trace?: TurnTraceNode[];
  /** Optional run id that owns the turn. */
  run_id?: string;
}

interface RawTurn {
  label?: string;
  kind?: string;
  ts?: number;
  stage?: string;
  system_prompt?: string;
  user_prompt?: string;
  raw_response?: Record<string, unknown>;
  validation_info?: string;
  exception?: Partial<TurnException>;
  trace?: TurnTraceNode[];
  run_id?: string;
  id?: string;
}

const KIND_FROM_LABEL: Array<[RegExp, TurnKind]> = [
  [/^stage1$/i, 'stage1'],
  [/^stage2$/i, 'stage2'],
  [/^followup/i, 'followup'],
  [/^incremental/i, 'incremental'],
];

function coerceKind(label: string | undefined, hint: string | undefined): TurnKind {
  const text = `${label ?? ''} ${hint ?? ''}`.toLowerCase();
  for (const [pattern, kind] of KIND_FROM_LABEL) {
    if (pattern.test(text)) return kind;
  }
  return 'unknown';
}

function coerceStage(label: string | undefined, hint: string | undefined): DebugTurn['stage'] {
  const text = `${label ?? ''} ${hint ?? ''}`.toLowerCase();
  if (text.includes('stage1')) return '1';
  if (text.includes('stage2')) return '2';
  if (text.includes('followup')) return 'followup';
  if (text.includes('incremental')) return 'incremental';
  return 'followup';
}

function classifyException(raw: Partial<TurnException> | undefined): TurnException {
  if (!raw) return { klass: 'none', message: '' };
  const klass = (raw.klass ?? 'unknown') as ExceptionClass;
  return {
    klass,
    message: raw.message ?? '',
    tag: raw.tag,
    details: raw.details,
  };
}

function normaliseTurn(raw: RawTurn, index: number): DebugTurn {
  const label = String(raw.label ?? `Turn-${index + 1}`);
  const kind = coerceKind(raw.kind ?? raw.label, raw.stage);
  const stage = coerceStage(raw.label, raw.stage);
  return {
    id: raw.id ?? `${label}-${raw.ts ?? index}`,
    label,
    kind,
    ts: typeof raw.ts === 'number' ? raw.ts : Date.now(),
    stage,
    system_prompt: raw.system_prompt ?? '',
    user_prompt: raw.user_prompt ?? '',
    raw_response: raw.raw_response ?? {},
    validation_info: raw.validation_info ?? '',
    exception: classifyException(raw.exception),
    trace: Array.isArray(raw.trace) ? raw.trace : [],
    run_id: raw.run_id,
  };
}

export const debugStore = reactive({
  turns: [] as DebugTurn[],
  loading: false,
  error: null as string | null,
  selectedId: null as string | null,
  bundleExpanded: false,

  /** Last fetch result — used by the bundle header to show source/range. */
  lastFetchedAt: 0 as number,
  /** Set by ValidationDebugDialog to opt into automatic refreshes. */
  autoRefresh: false,

  async refresh(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      const payload = await api.fetchDebugTurns();
      const list: RawTurn[] = Array.isArray(payload)
        ? payload
        : Array.isArray((payload as { turns?: RawTurn[] })?.turns)
          ? (payload as { turns: RawTurn[] }).turns
          : [];
      this.turns = list.map((turn, idx) => normaliseTurn(turn, idx));
      this.lastFetchedAt = Date.now();
      if (
        this.selectedId === null ||
        !this.turns.some((t) => t.id === this.selectedId)
      ) {
        this.selectedId = this.turns[0]?.id ?? null;
      }
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
      throw err;
    } finally {
      this.loading = false;
    }
  },

  reset(): void {
    this.turns = [];
    this.selectedId = null;
    this.error = null;
    this.bundleExpanded = false;
  },

  select(id: string | null): void {
    this.selectedId = id;
  },

  toggleBundle(): void {
    this.bundleExpanded = !this.bundleExpanded;
  },

  setAutoRefresh(value: boolean): void {
    this.autoRefresh = value;
  },

  /** Test helper — inject a normalised turn directly. */
  pushTurn(turn: DebugTurn): void {
    this.turns = [...this.turns, turn];
    if (this.selectedId === null) this.selectedId = turn.id;
  },

  /** Test helper — accept a raw, partial payload and normalise it. */
  ingestRaw(raw: RawTurn): DebugTurn {
    const turn = normaliseTurn(raw, this.turns.length);
    this.pushTurn(turn);
    return turn;
  },
});

export const selectedTurn = computed<DebugTurn | null>(() => {
  const id = debugStore.selectedId;
  if (!id) return debugStore.turns[0] ?? null;
  return debugStore.turns.find((t) => t.id === id) ?? null;
});

export const exceptionCounts = computed<Record<ExceptionClass, number>>(() => {
  const out: Record<ExceptionClass, number> = {
    none: 0,
    network: 0,
    auth: 0,
    rate_limit: 0,
    schema: 0,
    timeout: 0,
    validation: 0,
    unknown: 0,
  };
  for (const turn of debugStore.turns) {
    out[turn.exception.klass] = (out[turn.exception.klass] ?? 0) + 1;
  }
  return out;
});

export const hasFailures = computed<boolean>(() => {
  return debugStore.turns.some((t) => t.exception.klass !== 'none');
});
