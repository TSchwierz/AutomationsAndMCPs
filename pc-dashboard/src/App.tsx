import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  fetchCurrent,
  fetchMeasurements,
  fetchSettings,
  fetchStats,
  getApiBase,
  updateSettings,
  type CurrentResponse,
  type Measurement,
  type Settings,
  type StatsResponse,
} from './api'
import { ClimateChart } from './ClimateChart'
import './App.css'

type RangeKey = '24h' | '7d' | '30d'

function rangeStart(key: RangeKey): Date {
  const now = new Date()
  const ms =
    key === '24h' ? 24 * 3600_000 : key === '7d' ? 7 * 24 * 3600_000 : 30 * 24 * 3600_000
  return new Date(now.getTime() - ms)
}

function fmt(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

export default function App() {
  const [range, setRange] = useState<RangeKey>('24h')
  const [current, setCurrent] = useState<CurrentResponse | null>(null)
  const [measurements, setMeasurements] = useState<Measurement[]>([])
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [form, setForm] = useState<Partial<Settings>>({})
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    const from = rangeStart(range).toISOString()
    const to = new Date().toISOString()
    try {
      const [cur, hist, st, set] = await Promise.all([
        fetchCurrent(),
        fetchMeasurements(from, to),
        fetchStats(from, to),
        fetchSettings(),
      ])
      setCurrent(cur)
      setMeasurements(hist.measurements)
      setStats(st)
      setSettings(set)
      setForm({
        humidity_min: set.humidity_min,
        humidity_max: set.humidity_max,
        temp_min: set.temp_min,
        temp_max: set.temp_max,
        sustain_minutes: set.sustain_minutes,
        alert_cooldown_minutes: set.alert_cooldown_minutes,
        ntfy_server: set.ntfy_server,
        ntfy_topic: set.ntfy_topic,
        sample_interval_seconds: set.sample_interval_seconds,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [range])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => void load(), 30_000)
    return () => window.clearInterval(id)
  }, [load])

  const overall = current?.status?.overall ?? 'unknown'
  const statusLabel = useMemo(() => {
    if (overall === 'in_band') return 'In target band'
    if (overall === 'out_of_band') return 'Out of band'
    return 'No data yet'
  }, [overall])

  async function onSave(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    setSavedAt(null)
    try {
      const updated = await updateSettings({
        humidity_min: Number(form.humidity_min),
        humidity_max: Number(form.humidity_max),
        temp_min: Number(form.temp_min),
        temp_max: Number(form.temp_max),
        sustain_minutes: Number(form.sustain_minutes),
        alert_cooldown_minutes: Number(form.alert_cooldown_minutes),
        ntfy_server: String(form.ntfy_server ?? ''),
        ntfy_topic: String(form.ntfy_topic ?? ''),
        sample_interval_seconds: Number(form.sample_interval_seconds),
      })
      setSettings(updated)
      setSavedAt(new Date().toLocaleTimeString())
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const m = current?.measurement

  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">Studio Climate Monitor</p>
        <h1>Room climate</h1>
        <p className="lede">
          Mold prevention and PLA storage — live readings from the Pi on your LAN.
        </p>
        <p className="api-hint">API: {getApiBase()}</p>
      </header>

      {error && <div className="banner error">{error}</div>}

      <section className="status-row">
        <article className={`card status ${overall}`}>
          <h2>Now</h2>
          <p className="big">
            {m ? `${fmt(m.temperature_c)}°C` : '—'}
            <span className="sep">/</span>
            {m ? `${fmt(m.humidity_pct)}%` : '—'}
          </p>
          <p className="meta">{statusLabel}</p>
          {m && <p className="meta">Updated {new Date(m.ts).toLocaleString()}</p>}
        </article>

        <article className="card">
          <h2>Range stats</h2>
          <ul className="stats">
            <li>
              Samples <strong>{stats?.count ?? 0}</strong>
            </li>
            <li>
              Temp <strong>{fmt(stats?.temperature_c.min)}–{fmt(stats?.temperature_c.max)}°C</strong>
              <span className="avg">avg {fmt(stats?.temperature_c.avg)}</span>
            </li>
            <li>
              RH <strong>{fmt(stats?.humidity_pct.min)}–{fmt(stats?.humidity_pct.max)}%</strong>
              <span className="avg">avg {fmt(stats?.humidity_pct.avg)}</span>
            </li>
          </ul>
        </article>
      </section>

      <section className="card chart-card">
        <div className="chart-toolbar">
          <h2>Temperature & humidity</h2>
          <div className="range-toggle" role="group" aria-label="Time range">
            {(['24h', '7d', '30d'] as RangeKey[]).map((key) => (
              <button
                key={key}
                type="button"
                className={range === key ? 'active' : ''}
                onClick={() => setRange(key)}
              >
                {key}
              </button>
            ))}
          </div>
        </div>
        <ClimateChart
          measurements={measurements}
          humidityMin={settings?.humidity_min ?? 40}
          humidityMax={settings?.humidity_max ?? 55}
          tempMin={settings?.temp_min ?? 18}
          tempMax={settings?.temp_max ?? 24}
        />
      </section>

      <section className="card">
        <h2>Target bands</h2>
        <p className="lede-sm">
          Adjust remotely on the Pi. Alerts fire after the sustain window via ntfy.
        </p>
        <form className="settings-form" onSubmit={onSave}>
          <label>
            Humidity min %
            <input
              type="number"
              step="0.1"
              value={form.humidity_min ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, humidity_min: Number(e.target.value) }))}
              required
            />
          </label>
          <label>
            Humidity max %
            <input
              type="number"
              step="0.1"
              value={form.humidity_max ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, humidity_max: Number(e.target.value) }))}
              required
            />
          </label>
          <label>
            Temp min °C
            <input
              type="number"
              step="0.1"
              value={form.temp_min ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, temp_min: Number(e.target.value) }))}
              required
            />
          </label>
          <label>
            Temp max °C
            <input
              type="number"
              step="0.1"
              value={form.temp_max ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, temp_max: Number(e.target.value) }))}
              required
            />
          </label>
          <label>
            Sustain minutes
            <input
              type="number"
              min={1}
              value={form.sustain_minutes ?? ''}
              onChange={(e) =>
                setForm((f) => ({ ...f, sustain_minutes: Number(e.target.value) }))
              }
              required
            />
          </label>
          <label>
            Alert cooldown min
            <input
              type="number"
              min={1}
              value={form.alert_cooldown_minutes ?? ''}
              onChange={(e) =>
                setForm((f) => ({ ...f, alert_cooldown_minutes: Number(e.target.value) }))
              }
              required
            />
          </label>
          <label className="wide">
            ntfy server
            <input
              type="url"
              value={form.ntfy_server ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, ntfy_server: e.target.value }))}
            />
          </label>
          <label className="wide">
            ntfy topic
            <input
              type="text"
              value={form.ntfy_topic ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, ntfy_topic: e.target.value }))}
              placeholder="your-private-topic"
            />
          </label>
          <label>
            Sample interval s
            <input
              type="number"
              min={2}
              value={form.sample_interval_seconds ?? ''}
              onChange={(e) =>
                setForm((f) => ({ ...f, sample_interval_seconds: Number(e.target.value) }))
              }
              required
            />
          </label>
          <div className="form-actions">
            <button type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Save bands'}
            </button>
            {savedAt && <span className="saved">Saved at {savedAt}</span>}
          </div>
        </form>
      </section>
    </div>
  )
}
