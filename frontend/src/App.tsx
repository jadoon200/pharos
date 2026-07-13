import { useState } from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { api } from './api'
import MaritimePicture from './views/MaritimePicture'
import Incidents from './views/Incidents'
import ModelReport from './views/ModelReport'

const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
})

type Tab = 'picture' | 'incidents' | 'model'

const TABS: { id: Tab; label: string }[] = [
  { id: 'picture', label: 'Maritime Picture' },
  { id: 'incidents', label: 'Incidents' },
  { id: 'model', label: 'Model Report' },
]

function StatusPill() {
  const { data, isError } = useQuery({ queryKey: ['health'], queryFn: api.health })
  if (isError) return <span className="status-pill">API offline</span>
  if (!data) return <span className="status-pill">…</span>
  return <span className="status-pill ok">● API v{data.version}</span>
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
