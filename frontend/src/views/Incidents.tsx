import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, SEVERITY_CLASS, type Incident } from '../api'

const DETECTORS = ['', 'gap', 'rendezvous', 'loiter', 'spoof', 'anomaly']
const DETECTOR_LABEL: Record<string, string> = {
  '': 'all detectors',
  gap: 'dark ship / AIS gap',
  rendezvous: 'ship-to-ship',
  loiter: 'loitering',
  spoof: 'spoofing',
  anomaly: 'trajectory anomaly',
}

export default function Incidents() {
  const [detector, setDetector] = useState('')
  const incidents = useQuery({
    queryKey: ['incidents', detector],
    queryFn: () => api.incidents(detector ? { detector } : {}),
  })
  const [open, setOpen] = useState<Incident | null>(null)

  return (
    <div className="grid" style={{ gridTemplateColumns: open ? '2fr 1fr' : '1fr' }}>
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>Incident feed</h3>
          <select
            value={detector}
            onChange={(e) => setDetector(e.target.value)}
            style={{ marginLeft: 'auto', width: 'auto' }}
          >
            {DETECTORS.map((d) => (
              <option key={d} value={d}>
                {DETECTOR_LABEL[d]}
              </option>
            ))}
          </select>
        </div>
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Vessel</th>
              <th>Score</th>
              <th>Severity</th>
              <th>Rel.</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {incidents.data?.map((i) => (
              <tr key={i.incident_id} className="clickable" onClick={() => setOpen(i)}>
                <td>
                  {i.incident_type}
                  <div className="muted" style={{ fontSize: 11 }}>{i.detector}</div>
                </td>
                <td>{i.mmsi}{i.counterpart_mmsi ? ` ⇄ ${i.counterpart_mmsi}` : ''}</td>
                <td>{i.score.toFixed(2)}</td>
                <td className={SEVERITY_CLASS[i.severity]}>{i.severity}</td>
                <td className="grade">{i.reliability}</td>
                <td className="muted" style={{ fontSize: 11 }}>
                  {new Date(i.ts_start).toISOString().slice(0, 16).replace('T', ' ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {incidents.data?.length === 0 && <p className="muted">No incidents for this filter.</p>}
      </div>

      {open && <IncidentDetail id={open.incident_id} onClose={() => setOpen(null)} />}
    </div>
  )
}

function IncidentDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ['incident', id], queryFn: () => api.incident(id) })
  const i = q.data
  return (
    <div className="card">
      <div style={{ display: 'flex', alignItems: 'center' }}>
        <h3 style={{ margin: 0 }}>{i?.incident_type ?? '…'}</h3>
        <button className="tab" style={{ marginLeft: 'auto' }} onClick={onClose}>
          close
        </button>
      </div>
      {i && (
        <>
          <div className="muted" style={{ fontSize: 12, margin: '6px 0' }}>
            {i.detector} · MMSI {i.mmsi} · score {i.score} ·{' '}
            <span className={SEVERITY_CLASS[i.severity]}>{i.severity}</span> · reliability{' '}
            <span className="grade">{i.reliability}</span>
          </div>
          <div style={{ marginBottom: 8 }}>
            {(i.techniques ?? []).map((t) => (
              <span key={t} className="pill detector">{t}</span>
            ))}
          </div>
          <h4 style={{ marginBottom: 4 }}>Evidence</h4>
          <table>
            <tbody>
              {Object.entries(i.evidence ?? {}).map(([k, v]) => (
                <tr key={k}>
                  <td className="muted">{k}</td>
                  <td>{String(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 11 }}>
            Human-review decision support only, never an automated verdict.
          </p>
        </>
      )}
    </div>
  )
}
