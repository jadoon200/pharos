import type { SnapshotStatus } from './api'

export type DisplayMode = 'live' | 'delayed' | 'offline' | 'demo'

export interface ResolvedMode {
  kind: DisplayMode
  label: string
  generatedAt: string | null
  ageMinutes: number | null
}

export function resolveSnapshotMode(
  status: SnapshotStatus | null | undefined,
  nowMs = Date.now(),
): ResolvedMode {
  if (!status || typeof status.generated_at !== 'string') {
    return {
      kind: 'demo',
      label: 'demo fallback — synthetic data',
      generatedAt: null,
      ageMinutes: null,
    }
  }
  const generatedMs = Date.parse(status.generated_at)
  if (!Number.isFinite(generatedMs) || generatedMs > nowMs + 60_000) {
    return {
      kind: 'demo',
      label: 'demo fallback — synthetic data',
      generatedAt: null,
      ageMinutes: null,
    }
  }
  const ageMinutes = Math.max(0, (nowMs - generatedMs) / 60_000)
  if (ageMinutes < 10) {
    return { kind: 'live', label: 'live', generatedAt: status.generated_at, ageMinutes }
  }
  if (ageMinutes <= 30) {
    return {
      kind: 'delayed',
      label: `delayed — ${Math.floor(ageMinutes)} min old`,
      generatedAt: status.generated_at,
      ageMinutes,
    }
  }
  return {
    kind: 'offline',
    label: `collector offline — showing last snapshot ${Math.floor(ageMinutes)} min old`,
    generatedAt: status.generated_at,
    ageMinutes,
  }
}

export function evaluationMetricText(value: unknown): string {
  if (!value || typeof value !== 'object') return 'not estimable — no recorded metric'
  const metric = value as Record<string, unknown>
  if (metric.status !== 'estimated') {
    const reason = typeof metric.reason === 'string' ? metric.reason : 'insufficient data'
    const denominator = typeof metric.denominator === 'number' ? `; n=${metric.denominator}` : ''
    return `not estimable — ${reason}${denominator}`
  }
  if (typeof metric.roc_auc === 'number' && typeof metric.pr_auc === 'number') {
    const denominator = typeof metric.denominator === 'number' ? `; n=${metric.denominator}` : ''
    return `ROC ${metric.roc_auc.toFixed(3)}; PR ${metric.pr_auc.toFixed(3)}${denominator}`
  }
  const estimate = typeof metric.value === 'number' ? metric.value.toFixed(3) : 'recorded'
  const ci = Array.isArray(metric.ci95)
    ? `; 95% CI ${Number(metric.ci95[0]).toFixed(3)}–${Number(metric.ci95[1]).toFixed(3)}`
    : ''
  const denominator = typeof metric.denominator === 'number' ? `; n=${metric.denominator}` : ''
  return `${estimate}${ci}${denominator}`
}
