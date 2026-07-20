import { afterEach, describe, expect, it, vi } from 'vitest'
import { getSnapshot, type SnapshotStatus } from './api'
import { evaluationMetricText, resolveSnapshotMode } from './snapshot'

const NOW = Date.parse('2026-07-20T12:00:00Z')

afterEach(() => vi.unstubAllGlobals())

function status(ageMinutes: number): SnapshotStatus {
  return {
    generated_at: new Date(NOW - ageMinutes * 60_000).toISOString(),
    source: { ais: 'fixture', gfw: 'fixture' },
    freshness: 'fixture',
    human_review_disclaimer: 'fixture',
    mode: 'live',
    last_source_report_at: null,
    last_processing_at: null,
    observed_hours: 1,
    calendar_hours: 1,
    current_outage: null,
    run_count: 1,
    frozen_artifact_sha256: 'a'.repeat(64),
  }
}

describe('resolveSnapshotMode', () => {
  it('resolves all four honest display modes and boundaries', () => {
    expect(resolveSnapshotMode(status(9.99), NOW).kind).toBe('live')
    expect(resolveSnapshotMode(status(10), NOW).kind).toBe('delayed')
    expect(resolveSnapshotMode(status(30), NOW).kind).toBe('delayed')
    expect(resolveSnapshotMode(status(30.01), NOW).kind).toBe('offline')
    expect(resolveSnapshotMode(null, NOW).kind).toBe('demo')
  })

  it('rejects invalid and future generated timestamps', () => {
    const invalid = { ...status(1), generated_at: 'not-a-date' }
    expect(resolveSnapshotMode(invalid, NOW).kind).toBe('demo')
    expect(resolveSnapshotMode(status(-2), NOW).kind).toBe('demo')
  })
})

describe('evaluation rendering', () => {
  it('preserves not-estimable reasons and denominators', () => {
    expect(
      evaluationMetricText({ status: 'not estimable', reason: 'fewer than 5 positives', denominator: 3 }),
    ).toBe('not estimable — fewer than 5 positives; n=3')
  })

  it('renders recorded intervals and denominators', () => {
    expect(
      evaluationMetricText({ status: 'estimated', value: 0.5, ci95: [0.2, 0.8], denominator: 10 }),
    ).toBe('0.500; 95% CI 0.200–0.800; n=10')
  })
})

describe('snapshot fetch parsing', () => {
  it('accepts a valid common envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(status(1)))))
    await expect(getSnapshot<SnapshotStatus>('status.json')).resolves.toMatchObject({ mode: 'live' })
  })

  it('rejects an invalid common envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ mode: 'live' }))))
    await expect(getSnapshot<SnapshotStatus>('status.json')).rejects.toThrow(
      'invalid snapshot envelope',
    )
  })
})
