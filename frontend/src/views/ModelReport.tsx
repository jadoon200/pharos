import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type ScoreResult } from '../api'

// Build a demo track: a straight transit or a zig-zag anomaly (client-side, for the live scorer).
function makeTrack(zigzag: boolean): Array<Record<string, number>> {
  let lat = 1.2
  let lon = 103.8
  let heading = 90
  const pts: Array<Record<string, number>> = []
  for (let i = 0; i < 20; i++) {
    pts.push({ lat, lon, sog: 11 })
    heading += zigzag ? (i % 2 === 0 ? 60 : -55) : 0
    const h = (heading * Math.PI) / 180
    lat += Math.cos(h) / 111.19
    lon += Math.sin(h) / (111.19 * Math.cos((lat * Math.PI) / 180))
  }
  return pts
}

export default function ModelReport() {
  const stats = useQuery({ queryKey: ['stats'], queryFn: api.stats })
  const [result, setResult] = useState<ScoreResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function score(zigzag: boolean) {
    setBusy(true)
    setErr(null)
    try {
      setResult(await api.scoreTrack(makeTrack(zigzag)))
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const byDet = stats.data?.incidents_by_detector ?? {}

  return (
    <div className="grid" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(300px,0.8fr)' }}>
      <div className="grid" style={{ alignContent: 'start' }}>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Detection ensemble — per-threat coverage</h3>
          <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
            Five detectors, each covering a maritime threat no single model does. Live counts from
            the current corpus.
          </p>
          <table>
            <thead>
              <tr><th>Detector</th><th>Threat</th><th>Incidents</th></tr>
            </thead>
            <tbody>
              <tr><td>gap</td><td>dark ship / AIS gap</td><td>{byDet.gap ?? 0}</td></tr>
              <tr><td>rendezvous</td><td>ship-to-ship transfer</td><td>{byDet.rendezvous ?? 0}</td></tr>
              <tr><td>loiter</td><td>loitering / zone incursion</td><td>{byDet.loiter ?? 0}</td></tr>
              <tr><td>spoof</td><td>impossible kinematics</td><td>{byDet.spoof ?? 0}</td></tr>
              <tr><td>anomaly</td><td>trajectory deviation</td><td>{byDet.anomaly ?? 0}</td></tr>
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>Honest evaluation</h3>
          <ul style={{ fontSize: 13, lineHeight: 1.7, paddingLeft: 18 }}>
            <li>
              <strong>Cross-region generalization (the headline).</strong> The trajectory-anomaly
              model learns <em>route shape</em> from heading-invariant, region-agnostic features, so a
              model trained on one waterway still separates anomalies off a different coast. That
              train-A / test-B AUC is the maritime analogue of SENTINEL's cross-network transfer.
            </li>
            <li>
              <strong>Per-threat coverage.</strong> Each detector owns a different threat; the
              composite covers what no single detector does.
            </li>
            <li>
              <strong>The coverage confound, handled not hidden.</strong> An apparent dark ship is
              often a benign AIS receiver-coverage gap. A calibration <em>trap</em> (a long benign
              silence with near-zero displacement) must not be flagged, every incident carries an AIS
              reliability grade, and Global Fishing Watch gap events are the real-world cross-check.
            </li>
            <li className="muted">
              Offline gold-set precision/recall are near-perfect <em>by construction</em> (synthetic,
              separable events) — the claim is the methodology and the real-data validation path, not
              the synthetic number. See <code>docs/EVAL.md</code>.
            </li>
          </ul>
        </div>
      </div>

      <div className="card" style={{ alignSelf: 'start' }}>
        <h3 style={{ marginTop: 0 }}>Score a track</h3>
        <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
          Run the live anomaly model on a track shape (read-only inference). A zig-zag route
          reconstructs worse than a straight transit.
        </p>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="action" disabled={busy} onClick={() => score(false)}>
            Straight transit
          </button>
          <button className="action" disabled={busy} onClick={() => score(true)}>
            Zig-zag route
          </button>
        </div>
        {err && <p style={{ color: 'var(--red)', fontSize: 12 }}>{err}</p>}
        {result && !result.error && (
          <table style={{ marginTop: 12 }}>
            <tbody>
              <tr>
                <td className="muted">anomaly score</td>
                <td><strong>{result.anomaly_score}</strong></td>
              </tr>
              <tr><td className="muted">reconstruction error</td><td>{result.reconstruction_error}</td></tr>
              <tr><td className="muted">threshold</td><td>{result.threshold}</td></tr>
              <tr>
                <td className="muted">flagged?</td>
                <td className={result.is_anomalous ? 'sev-high' : 'muted'}>
                  {result.is_anomalous ? 'anomalous' : 'within normal pattern-of-life'}
                </td>
              </tr>
            </tbody>
          </table>
        )}
        {result?.error && <p style={{ color: 'var(--red)', fontSize: 12 }}>{result.error}</p>}
        <p className="muted" style={{ fontSize: 11 }}>
          Inference inspects only the submitted track — it never fetches a URL, so the API stays
          effectively read-only.
        </p>
      </div>
    </div>
  )
}
