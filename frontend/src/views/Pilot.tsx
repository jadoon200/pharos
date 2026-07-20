import { useQuery } from '@tanstack/react-query'
import { snapshotApi } from '../api'
import { evaluationMetricText } from '../snapshot'

const refetchInterval = 60_000

function Value({ children }: { children: React.ReactNode }) {
  return <strong style={{ color: 'var(--teal-bright)' }}>{children}</strong>
}

function MetricRow({ name, value }: { name: string; value: unknown }) {
  return (
    <tr>
      <td>{name}</td>
      <td>{evaluationMetricText(value)}</td>
    </tr>
  )
}

export default function Pilot() {
  const status = useQuery({
    queryKey: ['snapshot', 'status'],
    queryFn: snapshotApi.status,
    refetchInterval,
    retry: 1,
  })
  const stats = useQuery({
    queryKey: ['snapshot', 'stats'],
    queryFn: snapshotApi.stats,
    refetchInterval,
    retry: 1,
  })
  const model = useQuery({
    queryKey: ['snapshot', 'model'],
    queryFn: snapshotApi.model,
    refetchInterval,
    retry: 1,
  })
  const evaluations = useQuery({
    queryKey: ['snapshot', 'evaluations'],
    queryFn: snapshotApi.evaluations,
    refetchInterval,
    retry: 1,
  })

  if (status.isError || stats.isError || model.isError || evaluations.isError) {
    return (
      <div className="card pilot-fallback">
        <h2>Singapore Pilot</h2>
        <p>Demo fallback — synthetic data. The delayed pilot snapshot is unreachable or invalid.</p>
        <p className="muted">No pilot evaluation values are substituted from the baked demo.</p>
      </div>
    )
  }
  if (!status.data || !stats.data || !model.data || !evaluations.data) {
    return <div className="card">Loading delayed Singapore Pilot snapshot…</div>
  }

  const evaluation = evaluations.data.evaluation as Record<string, unknown>
  const metrics = (evaluation.metrics ?? {}) as Record<string, unknown>
  const limitations = Array.isArray(metrics.limitations) ? metrics.limitations : []
  const started = Date.parse('2026-07-16T02:47:51Z')
  const day = Math.max(
    0,
    Math.floor((Date.parse(status.data.generated_at) - started) / 86_400_000),
  )

  return (
    <div className="grid pilot-grid">
      <section className="card pilot-hero">
        <div>
          <span className="pill detector">SG-PILOT-v0 · day {day}</span>
          <h1>Singapore Strait live pilot</h1>
          <p>
            A frozen 804-parameter trajectory model and detector ensemble evaluated against
            independent events and blinded human review. Collection continues while labels,
            evaluation, and delayed publication remain off the scoring path.
          </p>
        </div>
        <div className="pilot-freeze">
          <span className="muted">Frozen artifact</span>
          <code>{model.data.sha256.slice(0, 16)}…</code>
          <small>threshold {model.data.threshold ?? 'unavailable'} · frozen {model.data.freeze_date}</small>
        </div>
      </section>

      <section className="card">
        <h3>Observed-time operations</h3>
        <div className="pilot-numbers">
          <div><Value>{status.data.observed_hours.toFixed(1)} h</Value><span>observed</span></div>
          <div><Value>{status.data.calendar_hours.toFixed(1)} h</Value><span>calendar</span></div>
          <div><Value>{stats.data.accepted_reports.toLocaleString()}</Value><span>reports</span></div>
          <div><Value>{stats.data.vessels.toLocaleString()}</Value><span>vessels</span></div>
          <div><Value>{stats.data.tracks.toLocaleString()}</Value><span>tracks</span></div>
          <div><Value>{stats.data.reconnect_count}</Value><span>reconnects</span></div>
        </div>
        <p className="muted">
          {stats.data.outage_count} recorded outages ({stats.data.outage_hours.toFixed(1)} h). Alert
          rate: {stats.data.alert_rate_per_100_observed_vessel_hours ?? 'not estimable'} per 100
          observed vessel-hours.
        </p>
      </section>

      <section className="card">
        <h3>Frozen model</h3>
        <table><tbody>
          <tr><td>parameters</td><td>{model.data.parameter_count}</td></tr>
          <tr><td>hidden size</td><td>{model.data.hidden_size}</td></tr>
          <tr><td>training window</td><td>{model.data.training_window}</td></tr>
          <tr><td>normalization</td><td>{model.data.normalization_provenance}</td></tr>
        </tbody></table>
      </section>

      <section className="card">
        <h3>Independent labels and review</h3>
        <table><tbody>
          {Object.entries(stats.data.labels_by_source).map(([source, count]) => (
            <tr key={source}><td>{source}</td><td>{count}</td></tr>
          ))}
          {Object.keys(stats.data.labels_by_source).length === 0 && (
            <tr><td>external labels</td><td>0 — recorded negative</td></tr>
          )}
          <tr><td>blinded tracks</td><td>{stats.data.review_progress.tracks.reviewed} / 200</td></tr>
          <tr><td>reviewed alerts</td><td>{stats.data.review_progress.pharos_alerts.reviewed} / 50</td></tr>
        </tbody></table>
      </section>

      <section className="card pilot-evaluation">
        <h3>Recorded evaluation</h3>
        <p className="muted">Pilot status: {String(evaluation.pilot_status ?? 'collecting')}</p>
        {metrics.precision_reviewed_alerts ? (
          <table><tbody>
            <MetricRow name="Reviewed-alert precision" value={metrics.precision_reviewed_alerts} />
            <MetricRow name="Official-event recall" value={metrics.external_official_recall} />
            <MetricRow name="GFW silver agreement" value={metrics.external_gfw_agreement} />
            <MetricRow name="Weighted ROC/PR" value={metrics.weighted_discrimination} />
          </tbody></table>
        ) : (
          <p>Collecting — no pilot evaluation yet. Existing validation is not relabelled as pilot evidence.</p>
        )}
        {limitations.length > 0 && (
          <div className="limitations">
            <strong>Recorded limitations</strong>
            {limitations.map((limitation) => <p key={String(limitation)}>{String(limitation)}</p>)}
          </div>
        )}
      </section>

      <section className="card honesty">
        <h3>Data honesty</h3>
        <p>
          AIS silence can mean receiver coverage loss, not vessel behaviour. AISStream is a beta
          feed with no SLA. Public geometry is delayed at least 15 minutes; candidates require
          human review and are never automated verdicts.
        </p>
        <p className="muted">{status.data.freshness} · {status.data.source.gfw}</p>
      </section>
    </div>
  )
}
