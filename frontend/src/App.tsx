import { useState } from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { api, snapshotApi } from './api'
import { resolveSnapshotMode } from './snapshot'
import MaritimePicture from './views/MaritimePicture'
import Incidents from './views/Incidents'
import ModelReport from './views/ModelReport'
import Pilot from './views/Pilot'

const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
})

type Tab = 'picture' | 'incidents' | 'model' | 'pilot'

const TABS: { id: Tab; label: string }[] = [
  { id: 'picture', label: 'Maritime Picture' },
  { id: 'incidents', label: 'Incidents' },
  { id: 'model', label: 'Model Report' },
  { id: 'pilot', label: 'Singapore Pilot' },
]

function StatusPill() {
  const { data, isError } = useQuery({ queryKey: ['health'], queryFn: api.health })
  if (isError) return <span className="status-pill">API offline</span>
  if (!data) return <span className="status-pill">…</span>
  return <span className="status-pill ok">● API v{data.version}</span>
}

function SnapshotBanner() {
  const status = useQuery({
    queryKey: ['snapshot', 'status'],
    queryFn: snapshotApi.status,
    refetchInterval: 60_000,
    retry: 1,
  })
  const mode = resolveSnapshotMode(status.isError ? null : status.data)
  return (
    <div className={`snapshot-banner mode-${mode.kind}`}>
      <strong>{mode.label}</strong>
      <span>generated_at: {mode.generatedAt ?? 'unavailable'}</span>
    </div>
  )
}

function Shell() {
  const [tab, setTab] = useState<Tab>('picture')
  return (
    <>
      <header className="masthead">
        <div className="brand">
          PHAROS
          <small>maritime domain awareness · GEOINT</small>
        </div>
        <StatusPill />
        <SnapshotBanner />
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`tab ${tab === t.id ? 'active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {tab === 'picture' && <MaritimePicture />}
        {tab === 'incidents' && <Incidents />}
        {tab === 'model' && <ModelReport />}
        {tab === 'pilot' && <Pilot />}
      </main>
    </>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={client}>
      <Shell />
    </QueryClientProvider>
  )
}
