import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CircleMarker, MapContainer, Polygon, Polyline, TileLayer, Tooltip } from 'react-leaflet'
import { api, SEVERITY_CLASS, type Threat } from '../api'

const SEV_COLOR: Record<string, string> = {
  low: '#64748b',
  moderate: '#fbbf24',
  high: '#fb923c',
  critical: '#f87171',
}

// GeoJSON is [lon, lat]; Leaflet wants [lat, lon].
type LatLng = [number, number]
const flip = (c: number[]): LatLng => [c[1], c[0]]

export default function MaritimePicture() {
  const zones = useQuery({ queryKey: ['zones'], queryFn: api.zones })
  const tracks = useQuery({ queryKey: ['tracks'], queryFn: () => api.tracks() })
  const threats = useQuery({ queryKey: ['picture'], queryFn: () => api.maritimePicture() })
  const [selected, setSelected] = useState<Threat | null>(null)

  const center: LatLng = [1.2, 103.85]

  return (
    <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 2fr) minmax(280px, 1fr)' }}>
      <div className="card" style={{ padding: 6 }}>
        <MapContainer center={center} zoom={11} style={{ height: '72vh', minHeight: 460 }}>
          <TileLayer
            attribution='&copy; OpenStreetMap'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          {/* Reference zones */}
          {zones.data?.features.map((f, i) => {
            const ring = (f.geometry.coordinates as number[][][])[0].map(flip)
            const sensitive = f.properties.sensitive as boolean
            return (
              <Polygon
                key={`z${i}`}
                positions={ring}
                pathOptions={{
                  color: sensitive ? '#f87171' : '#2dd4bf',
                  weight: 1,
                  fillOpacity: 0.06,
                  dashArray: sensitive ? undefined : '4',
                }}
              >
                <Tooltip>{f.properties.name as string}</Tooltip>
              </Polygon>
            )
          })}
          {/* Vessel tracks */}
          {tracks.data?.features.map((f, i) => (
            <Polyline
              key={`t${i}`}
              positions={(f.geometry.coordinates as number[][]).map(flip)}
              pathOptions={{ color: '#2dd4bf', weight: 1, opacity: 0.35 }}
            />
          ))}
          {/* Threat markers */}
          {threats.data
            ?.filter((t) => t.lat != null && t.lon != null)
            .map((t) => (
              <CircleMarker
                key={t.mmsi}
                center={[t.lat as number, t.lon as number]}
                radius={6 + t.risk * 10}
                pathOptions={{
                  color: SEV_COLOR[t.severity],
                  fillColor: SEV_COLOR[t.severity],
                  fillOpacity: 0.7,
                  weight: selected?.mmsi === t.mmsi ? 3 : 1,
                }}
                eventHandlers={{ click: () => setSelected(t) }}
              >
                <Tooltip>
                  {t.name ?? t.mmsi} · risk {t.risk.toFixed(2)} · {t.detectors.join(', ')}
                </Tooltip>
              </CircleMarker>
            ))}
        </MapContainer>
        <div className="muted" style={{ fontSize: 11, padding: '6px 8px' }}>
          <span className="legend-dot" style={{ background: '#f87171' }} /> critical
          <span className="legend-dot" style={{ background: '#fb923c', marginLeft: 12 }} /> high
          <span className="legend-dot" style={{ background: '#fbbf24', marginLeft: 12 }} /> moderate
          &nbsp;·&nbsp; dashed teal = reference zone, solid red = sensitive watch zone
        </div>
      </div>

      <div className="grid" style={{ alignContent: 'start' }}>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Threat picture</h3>
          <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
            {threats.data?.length ?? '…'} vessels at risk, riskiest first. Click a marker or a row.
          </p>
          <div style={{ maxHeight: selected ? 180 : 420, overflowY: 'auto' }}>
            <table>
              <tbody>
                {threats.data?.map((t) => (
                  <tr
                    key={t.mmsi}
                    className="clickable"
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelected(t)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        setSelected(t)
                      }
                    }}
                    style={{ outline: selected?.mmsi === t.mmsi ? '1px solid var(--teal)' : undefined }}
                  >
                    <td>
                      <strong>{t.name ?? t.mmsi}</strong>
                      <div className="muted" style={{ fontSize: 11 }}>{t.detectors.join(' · ')}</div>
                    </td>
                    <td style={{ width: 92 }}>
                      <div className={SEVERITY_CLASS[t.severity]} style={{ fontSize: 11 }}>
                        {t.severity}
                      </div>
                      <div className="risk-bar">
                        <span style={{ width: `${t.risk * 100}%` }} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {selected && <ThreatDetail threat={selected} />}
      </div>
    </div>
  )
}

function ThreatDetail({ threat: t }: { threat: Threat }) {
  const c = t.components
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>
        {t.name ?? t.mmsi}{' '}
        <span className={SEVERITY_CLASS[t.severity]} style={{ fontSize: 13 }}>
          {t.severity.toUpperCase()}
        </span>
      </h3>
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
        MMSI {t.mmsi}
        {t.flag ? ` · ${t.flag}` : ''}
        {t.ship_type ? ` · ${t.ship_type}` : ''}
      </div>

      {/* Evidence chain: detectors → techniques → zone → fused risk */}
      <div style={{ fontSize: 12, lineHeight: 1.9 }}>
        <div>
          <span className="muted">Detectors agree:</span>{' '}
          {t.detectors.map((d) => (
            <span key={d} className="pill detector">{d}</span>
          ))}
        </div>
        <div>
          <span className="muted">Techniques:</span>{' '}
          {t.techniques.map((x) => (
            <span key={x} className="pill">{x}</span>
          ))}
        </div>
        {t.zones.length > 0 && (
          <div>
            <span className="muted">Zone:</span> {t.zones.join(', ')}
          </div>
        )}
        {t.counterparts.length > 0 && (
          <div>
            <span className="muted">Counterpart vessel(s):</span> {t.counterparts.join(', ')}
          </div>
        )}
        <div>
          <span className="muted">AIS reliability:</span>{' '}
          <span className="grade">{t.reliability}</span>{' '}
          <span className="muted">(A completely reliable … F cannot be judged)</span>
        </div>
      </div>

      <div style={{ margin: '12px 0 4px' }}>
        {/* Spell the arithmetic out with the terms that actually multiply. The old caption
            named the three ideas but showed only their raw inputs, so multiplying what was
            on screen (1.00 × 0.333 × a letter grade) came nowhere near the headline. */}
        <div className="muted" style={{ fontSize: 11 }}>
          Composite risk = {c.max_score} × {c.corroboration_factor} (corroboration) ×{' '}
          {c.reliability_factor} (reliability)
          {c.sensitive_bonus > 0 && <> + {c.sensitive_bonus} (sensitive zone)</>} ={' '}
          {/* The unrounded value, so the line the reviewer checks is the line that closes —
              the two-decimal headline elsewhere would not. */}
          <b>{t.risk}</b>
        </div>
        <div className="risk-bar" style={{ height: 10 }}>
          <span style={{ width: `${t.risk * 100}%` }} />
        </div>
      </div>
      <table style={{ marginTop: 8 }}>
        <tbody>
          <tr><td className="muted">max detector score</td><td>{c.max_score}</td></tr>
          <tr><td className="muted">distinct detectors</td><td>{c.detector_count}</td></tr>
          <tr>
            <td className="muted">corroboration</td>
            <td>{c.diversity} → 0.7 + 0.3 × {c.diversity} = {c.corroboration_factor}</td>
          </tr>
          <tr>
            <td className="muted">best reliability</td>
            <td>
              <span className="grade">{c.best_reliability}</span> = {c.reliability_weight} → 0.6 +
              0.4 × {c.reliability_weight} = {c.reliability_factor}
            </td>
          </tr>
          <tr>
            <td className="muted">sensitive zone</td>
            <td>{c.sensitive_zone ? `yes → +${c.sensitive_bonus}` : 'no'}</td>
          </tr>
        </tbody>
      </table>
      <p className="muted" style={{ fontSize: 11, marginBottom: 0 }}>
        Human-review decision support — never an automated verdict. A gap may be a benign coverage loss.
      </p>
    </div>
  )
}
