import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  fetchCurrent,
  fetchMeasurements,
  fetchOutside,
  fetchOutsideMeasurements,
  fetchSettings,
  fetchStats,
  getApiBase,
  updateSettings,
  type CurrentResponse,
  type Measurement,
  type OutsideReading,
  type OutsideResponse,
  type Settings,
  type StatsResponse,
} from './api'
import { ClimateChart } from './ClimateChart'
import { WeatherIcon } from './WeatherIcon'
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

function hourLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function ageLabel(seconds: number | null): string {
  if (seconds == null) return 'never'
  if (seconds < 90) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  return hours === 1 ? '1 h ago' : `${hours} h ago`
}

export default function App() {
  const [range, setRange] = useState<RangeKey>('24h')
  const [current, setCurrent] = useState<CurrentResponse | null>(null)
  const [measurements, setMeasurements] = useState<Measurement[]>([])
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [outside, setOutside] = useState<OutsideResponse | null>(null)
  const [outsideHistory, setOutsideHistory] = useState<OutsideReading[]>([])
  const [form, setForm] = useState<Partial<Settings>>({})
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    const from = rangeStart(range).toISOString()
    const to = new Date().toISOString()
    try {
      // Outside data is optional: a Pi with weather disabled (or an older
      // build) should not take the rest of the dashboard down with it.
      const [cur, hist, st, set, out, outHist] = await Promise.all([
        fetchCurrent(),
        fetchMeasurements(from, to),
        fetchStats(from, to),
        fetchSettings(),
        fetchOutside().catch(() => null),
        fetchOutsideMeasurements(from, to).catch(() => null),
      ])
      setCurrent(cur)
      setMeasurements(hist.measurements)
      setStats(st)
      setSettings(set)
      setOutside(out)
      setOutsideHistory(outHist?.measurements ?? [])
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
        quiet_hours_enabled: set.quiet_hours_enabled,
        quiet_hours_start: set.quiet_hours_start,
        quiet_hours_end: set.quiet_hours_end,
        quiet_hours_timezone: set.quiet_hours_timezone,
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
        quiet_hours_enabled: Boolean(form.quiet_hours_enabled),
        quiet_hours_start: String(form.quiet_hours_start ?? '23:00'),
        quiet_hours_end: String(form.quiet_hours_end ?? '08:00'),
        quiet_hours_timezone: String(form.quiet_hours_timezone ?? ''),
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
  const outsideReading = outside?.reading
  const ventilation = outside?.comparison.ventilation
  const forecast = outside?.forecast
  const summary = forecast?.summary

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
          {outside?.comparison.indoor_dew_point_c != null && (
            <p className="meta">
              Dew point {fmt(outside.comparison.indoor_dew_point_c)}°C
            </p>
          )}
        </article>

        {outside?.enabled && (
          <article className="card status outside">
            <h2>
              Outside
              {outsideReading && (
                <WeatherIcon
                  condition={outsideReading.condition}
                  label={outsideReading.condition_label}
                  className="head-icon"
                />
              )}
            </h2>
            {outsideReading ? (
              <>
                <p className="big">
                  {fmt(outsideReading.temperature_c)}°C
                  <span className="sep">/</span>
                  {fmt(outsideReading.humidity_pct, 0)}%
                </p>
                <p className="meta">
                  {outsideReading.condition_label}
                  {outsideReading.wind_label
                    ? ` · ${outsideReading.wind_label} ${fmt(outsideReading.wind_kph, 0)} km/h`
                    : ''}
                  {' · dew point '}
                  {fmt(outsideReading.dew_point_c)}°C
                </p>
                <p className="meta">
                  {ageLabel(outside.age_seconds)} · averaged over{' '}
                  {outsideReading.provider_count}{' '}
                  {outsideReading.provider_count === 1 ? 'service' : 'services'}
                  {outside.stale ? ' · stale' : ''}
                </p>
                {ventilation && ventilation.action !== 'unknown' && (
                  <p className={`vent ${ventilation.action}`}>{ventilation.reason}</p>
                )}
              </>
            ) : (
              <>
                <p className="big">—</p>
                <p className="meta">Waiting for the first weather poll.</p>
              </>
            )}
          </article>
        )}

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

      {summary && forecast && forecast.points.length > 0 && (
        <section className="card forecast-card">
          <div className="chart-toolbar">
            <h2>Next {forecast.window_hours} hours outside</h2>
            {forecast.fetched_at && (
              <span className="forecast-meta">
                forecast from {hourLabel(forecast.fetched_at)}
              </span>
            )}
          </div>
          <p className="forecast-headline">
            <WeatherIcon
              condition={summary.condition_peak}
              label={summary.condition_peak_label}
              className="headline-icon"
            />
            {summary.headline}
          </p>
          <ul className="forecast-strip">
            {forecast.points.map((point) => (
              <li
                key={point.ts}
                className={
                  point.ts === summary.change_at ? 'forecast-chip change' : 'forecast-chip'
                }
              >
                <span className="chip-time">{hourLabel(point.ts)}</span>
                <WeatherIcon condition={point.condition} label={point.condition_label} />
                <span className="chip-temp">{fmt(point.temperature_c, 0)}°C</span>
                <span className="chip-cond">{point.condition_label}</span>
                <span className="chip-sub">
                  {point.precipitation_probability != null &&
                  point.precipitation_probability >= 5
                    ? `${Math.round(point.precipitation_probability)}% rain`
                    : `${fmt(point.humidity_pct, 0)}% RH`}
                </span>
              </li>
            ))}
          </ul>
          <p className="chart-note">
            Averaged over {outside?.providers.join(', ')} at {outside?.location.latitude},{' '}
            {outside?.location.longitude}
          </p>
        </section>
      )}

      <section className="card chart-card">
        <div className="chart-toolbar">
          <h2>{outsideHistory.length > 0 ? 'Inside vs outside' : 'Temperature & humidity'}</h2>
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
          outside={outsideHistory}
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
          <label className="wide toggle">
            <input
              type="checkbox"
              checked={Boolean(form.quiet_hours_enabled)}
              onChange={(e) =>
                setForm((f) => ({ ...f, quiet_hours_enabled: e.target.checked }))
              }
            />
            Quiet hours — hold back notifications overnight
          </label>
          <label>
            Quiet from
            <input
              type="time"
              value={form.quiet_hours_start ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, quiet_hours_start: e.target.value }))}
              disabled={!form.quiet_hours_enabled}
              required
            />
          </label>
          <label>
            Quiet until
            <input
              type="time"
              value={form.quiet_hours_end ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, quiet_hours_end: e.target.value }))}
              disabled={!form.quiet_hours_enabled}
              required
            />
          </label>
          <label>
            Quiet hours timezone
            <input
              type="text"
              value={form.quiet_hours_timezone ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, quiet_hours_timezone: e.target.value }))}
              disabled={!form.quiet_hours_enabled}
              placeholder="Pi local time"
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
