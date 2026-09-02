export type Measurement = {
  id: number
  ts: string
  temperature_c: number
  humidity_pct: number
}

export type BandStatus = {
  temperature_in_band?: boolean
  humidity_in_band?: boolean
  overall: string
}

export type CurrentResponse = {
  measurement: Measurement | null
  bands: {
    humidity_min: number
    humidity_max: number
    temp_min: number
    temp_max: number
  }
  status: BandStatus
}

export type Settings = {
  humidity_min: number
  humidity_max: number
  temp_min: number
  temp_max: number
  sustain_minutes: number
  alert_cooldown_minutes: number
  ntfy_server: string
  ntfy_topic: string
  ntfy_token?: string
  sample_interval_seconds: number
  quiet_hours_enabled: boolean
  quiet_hours_start: string
  quiet_hours_end: string
  quiet_hours_timezone: string
}

export type StatsResponse = {
  count: number
  temperature_c: { min: number | null; max: number | null; avg: number | null }
  humidity_pct: { min: number | null; max: number | null; avg: number | null }
}

/** Outdoor conditions, averaged across the weather services. */
export type OutsideReading = {
  ts: string
  temperature_c: number
  humidity_pct: number
  dew_point_c: number | null
  condition: string | null
  condition_label: string
  wind_kph: number | null
  wind_label: string | null
  precipitation_mm: number | null
  sources: string[]
  provider_count: number
}

export type ForecastPoint = {
  ts: string
  temperature_c: number
  humidity_pct: number | null
  condition: string | null
  condition_label: string
  precipitation_probability: number | null
  wind_kph: number | null
  provider_count: number
}

export type ForecastSummary = {
  from: string | null
  to: string | null
  condition_now: string | null
  condition_now_label: string
  condition_peak: string | null
  condition_peak_label: string
  changing: boolean
  change_at: string | null
  temperature_c: { min: number; max: number; delta: number }
  humidity_pct: { min: number | null; max: number | null }
  precipitation_probability_max: number | null
  wind_kph_max: number | null
  wind_label: string | null
  headline: string
}

export type VentilationAdvice = {
  action: 'ventilate' | 'keep_closed' | 'neutral' | 'unknown'
  delta_c: number | null
  reason: string
}

export type OutsideResponse = {
  enabled: boolean
  location: { latitude: number; longitude: number }
  providers: string[]
  poll_interval_minutes: number
  reading: OutsideReading | null
  age_seconds: number | null
  stale: boolean
  indoor: {
    ts: string
    temperature_c: number
    humidity_pct: number
    dew_point_c: number | null
  } | null
  comparison: {
    temperature_delta_c: number | null
    humidity_delta_pct: number | null
    indoor_dew_point_c: number | null
    outside_dew_point_c: number | null
    ventilation: VentilationAdvice
  }
  forecast: {
    fetched_at: string | null
    window_hours: number
    summary: ForecastSummary | null
    points: ForecastPoint[]
  }
}

const API_BASE = (import.meta.env.VITE_PI_API_BASE as string | undefined)?.replace(/\/$/, '') ||
  'http://127.0.0.1:8787'

const API_TOKEN = (import.meta.env.VITE_API_TOKEN as string | undefined) || ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (API_TOKEN && init?.method && init.method !== 'GET') {
    headers.set('X-API-Token', API_TOKEN)
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

export function getApiBase(): string {
  return API_BASE
}

export function fetchCurrent() {
  return request<CurrentResponse>('/current')
}

export function fetchMeasurements(fromIso: string, toIso: string) {
  const params = new URLSearchParams({ from: fromIso, to: toIso, limit: '20000' })
  return request<{ count: number; measurements: Measurement[] }>(`/measurements?${params}`)
}

export function fetchStats(fromIso: string, toIso: string) {
  const params = new URLSearchParams({ from: fromIso, to: toIso })
  return request<StatsResponse>(`/stats?${params}`)
}

export function fetchOutside() {
  return request<OutsideResponse>('/outside')
}

export function fetchOutsideMeasurements(fromIso: string, toIso: string) {
  const params = new URLSearchParams({ from: fromIso, to: toIso, limit: '5000' })
  return request<{ count: number; measurements: OutsideReading[] }>(
    `/outside/measurements?${params}`,
  )
}

export function refreshOutside() {
  return request<OutsideResponse>('/outside/refresh', { method: 'POST' })
}

export function fetchSettings() {
  return request<Settings>('/settings')
}

export function updateSettings(partial: Partial<Settings>) {
  return request<Settings>('/settings', {
    method: 'PUT',
    body: JSON.stringify(partial),
  })
}
