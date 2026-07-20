/** Typed client for the PHAROS read-only maritime-domain-awareness API. */

// Same-origin in a production build (FastAPI serves this SPA and the API from one host);
// localhost:8000 in dev (`make ui` on :5173 talks to `make api` on :8000). VITE_API_URL overrides.
const BASE = import.meta.env.VITE_API_URL ?? (import.meta.env.PROD ? '' : 'http://localhost:8000')
export const SNAPSHOT_BASE =
  import.meta.env.VITE_SNAPSHOT_URL ??
  'https://raw.githubusercontent.com/jadoon200/pharos/snapshots/'

export interface Health {
  status: string
  version: string
}

export interface Stats {
  vessels: number
  positions: number
  tracks: number
  incidents: number
  zones: number
  incidents_by_detector: Record<string, number>
}

export interface Incident {
  incident_id: string
  mmsi: string
  detector: string
  incident_type: string
  score: number
  severity: string
  reliability: string // NATO-Admiralty AIS-confidence grade A-F
  ts_start: string
  ts_end: string | null
  lat: number | null
  lon: number | null
  zone_id: string | null
  counterpart_mmsi: string | null
  techniques: string[] | null
  region: string | null
  evidence?: Record<string, unknown>
}

/** A composite per-vessel maritime-threat rollup (the fusion unit). */
export interface Threat {
  mmsi: string
  name: string | null
  ship_type: string | null
  flag: string | null
  risk: number
  severity: string
  reliability: string
  detectors: string[]
  techniques: string[]
  incident_count: number
  incident_ids: string[]
  counterparts: string[]
  zones: string[]
  lat: number | null
  lon: number | null
  first_ts: string | null
  last_ts: string | null
  components: {
    max_score: number
    detector_count: number
    diversity: number
    best_reliability: string
    sensitive_zone: boolean
  }
}

export interface Vessel {
  mmsi: string
  name: string | null
  ship_type: string | null
  flag: string | null
  first_seen: string | null
  last_seen: string | null
}

export interface GeoFeature {
  type: 'Feature'
  geometry: { type: string; coordinates: number[][] | number[][][] }
  properties: Record<string, unknown>
}

export interface GeoJSON {
  type: 'FeatureCollection'
  features: GeoFeature[]
}

export interface ScoreResult {
  anomaly_score: number
  reconstruction_error: number
  threshold: number
  is_anomalous: boolean
  points_scored: number
  error?: string
}

export interface SnapshotCommon {
  generated_at: string
  source: { ais: string; gfw: string }
  freshness: string
  human_review_disclaimer: string
}

export interface SnapshotStatus extends SnapshotCommon {
  mode: 'live' | 'delayed' | 'collector offline'
  last_source_report_at: string | null
  last_processing_at: string | null
  observed_hours: number
  calendar_hours: number
  current_outage: { reason_category: string; age_minutes: number } | null
  run_count: number
  frozen_artifact_sha256: string
}

export interface SnapshotStats extends SnapshotCommon {
  vessels: number
  accepted_reports: number
  tracks: number
  incidents: number
  incidents_by_detector: Record<string, number>
  observed_hours: number
  observed_vessel_hours: number
  alert_rate_per_100_observed_vessel_hours: number | null
  reconnect_count: number
  outage_count: number
  outage_hours: number
  labels_by_source: Record<string, number>
  review_progress: {
    tracks: { reviewed: number; target: number }
    pharos_alerts: { reviewed: number; target: number }
  }
}

export interface SnapshotModel extends SnapshotCommon {
  artifact_source: string
  sha256: string
  parameter_count: number
  hidden_size: number
  threshold: number | null
  training_window: string
  normalization_provenance: string
  freeze_date: string
  model_card: string
}

export interface SnapshotEvaluations extends SnapshotCommon {
  evaluation: Record<string, unknown>
}

export interface SnapshotIncidents extends SnapshotCommon {
  incidents: Array<Record<string, unknown>>
}

export interface SnapshotTracks extends SnapshotCommon, GeoJSON {
  delay_floor_minutes: number
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`${path} → ${res.status}`)
  return res.json() as Promise<T>
}

export async function getSnapshot<T extends SnapshotCommon>(file: string): Promise<T> {
  const base = SNAPSHOT_BASE.endsWith('/') ? SNAPSHOT_BASE : `${SNAPSHOT_BASE}/`
  const res = await fetch(`${base}${file}`)
  if (!res.ok) throw new Error(`${file} → ${res.status}`)
  const payload = (await res.json()) as Partial<T>
  if (
    typeof payload !== 'object' ||
    typeof payload.generated_at !== 'string' ||
    typeof payload.freshness !== 'string' ||
    typeof payload.source !== 'object'
  ) {
    throw new Error(`${file} → invalid snapshot envelope`)
  }
  return payload as T
}

export const api = {
  health: () => get<Health>('/health'),
  stats: () => get<Stats>('/stats'),
  incidents: (params: { detector?: string; region?: string } = {}) => {
    const q = new URLSearchParams(params as Record<string, string>).toString()
    return get<Incident[]>(`/incidents${q ? `?${q}` : ''}`)
  },
  incident: (id: string) => get<Incident>(`/incidents/${id}`),
  maritimePicture: (region?: string) =>
    get<Threat[]>(`/maritime-picture${region ? `?region=${region}` : ''}`),
  zones: () => get<GeoJSON>('/zones'),
  tracks: (region?: string) => get<GeoJSON>(`/tracks${region ? `?region=${region}` : ''}`),
  vessels: () => get<Vessel[]>('/vessels'),
  vesselTrack: (mmsi: string) => get<GeoFeature>(`/vessels/${mmsi}/track`),
  scoreTrack: async (points: Array<Record<string, number>>): Promise<ScoreResult> => {
    const res = await fetch(`${BASE}/score-track`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ points }),
    })
    if (res.status === 429) throw new Error('rate limit exceeded — slow down')
    if (res.status === 503) throw new Error('server busy — try again')
    if (!res.ok) throw new Error(`score-track → ${res.status}`)
    return res.json() as Promise<ScoreResult>
  },
}

export const snapshotApi = {
  status: () => getSnapshot<SnapshotStatus>('status.json'),
  stats: () => getSnapshot<SnapshotStats>('stats.json'),
  tracks: () => getSnapshot<SnapshotTracks>('tracks.json'),
  incidents: () => getSnapshot<SnapshotIncidents>('incidents.json'),
  evaluations: () => getSnapshot<SnapshotEvaluations>('evaluations.json'),
  model: () => getSnapshot<SnapshotModel>('model.json'),
}

export const SEVERITY_CLASS: Record<string, string> = {
  low: 'sev-low',
  moderate: 'sev-moderate',
  high: 'sev-high',
  critical: 'sev-critical',
}
