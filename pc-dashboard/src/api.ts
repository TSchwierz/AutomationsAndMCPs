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
}

export type StatsResponse = {
  count: number
  temperature_c: { min: number | null; max: number | null; avg: number | null }
  humidity_pct: { min: number | null; max: number | null; avg: number | null }
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

export function fetchSettings() {
  return request<Settings>('/settings')
}

export function updateSettings(partial: Partial<Settings>) {
  return request<Settings>('/settings', {
    method: 'PUT',
    body: JSON.stringify(partial),
  })
}
