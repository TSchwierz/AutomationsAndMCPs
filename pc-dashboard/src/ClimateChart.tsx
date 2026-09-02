import {
  Chart as ChartJS,
  LinearScale,
  TimeScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
  type ChartType,
  type Plugin,
} from 'chart.js'
import 'chartjs-adapter-date-fns'
import { Line } from 'react-chartjs-2'
import { useMemo } from 'react'
import type { Measurement } from './api'

ChartJS.register(
  LinearScale,
  TimeScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
)

/** A stretch of missing data: no sample between `start` and `end`. */
type Gap = {
  /** Index of the last sample before the gap. */
  index: number
  start: number
  end: number
}

declare module 'chart.js' {
  interface PluginOptionsByType<TType extends ChartType> {
    gapHighlight?: { gaps: Gap[] }
  }
}

// A gap counts as downtime once it is this much longer than the usual spacing,
// and never below the floor (so ordinary sampling jitter stays quiet).
const GAP_FACTOR = 3
const MIN_GAP_MS = 60_000

function detectGaps(times: number[]): Gap[] {
  if (times.length < 3) return []
  const deltas: number[] = []
  for (let i = 1; i < times.length; i += 1) {
    deltas.push(times[i] - times[i - 1])
  }
  const sorted = [...deltas].sort((a, b) => a - b)
  const median = sorted[Math.floor(sorted.length / 2)]
  const threshold = Math.max(median * GAP_FACTOR, MIN_GAP_MS)

  const gaps: Gap[] = []
  deltas.forEach((delta, i) => {
    if (delta > threshold) {
      gaps.push({ index: i, start: times[i], end: times[i + 1] })
    }
  })
  return gaps
}

function formatDuration(ms: number): string {
  const minutes = Math.round(ms / 60_000)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  if (hours < 24) return restMinutes ? `${hours} h ${restMinutes} min` : `${hours} h`
  const days = Math.floor(hours / 24)
  const restHours = hours % 24
  return restHours ? `${days} d ${restHours} h` : `${days} d`
}

/** Sample points with a null inserted inside every gap so the line breaks there. */
function toPoints(
  measurements: Measurement[],
  times: number[],
  gapIndexes: Set<number>,
  pick: (m: Measurement) => number,
): { x: number; y: number | null }[] {
  const points: { x: number; y: number | null }[] = []
  measurements.forEach((m, i) => {
    points.push({ x: times[i], y: pick(m) })
    if (gapIndexes.has(i)) {
      points.push({ x: (times[i] + times[i + 1]) / 2, y: null })
    }
  })
  return points
}

function extent(values: number[]): { min: number; max: number } {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const value of values) {
    if (value < min) min = value
    if (value > max) max = value
  }
  return { min, max }
}

const gapHighlight: Plugin<'line'> = {
  id: 'gapHighlight',
  beforeDatasetsDraw(chart, _args, opts) {
    const gaps = opts?.gaps ?? []
    const scale = chart.scales.x
    if (!gaps.length || !scale) return

    const { ctx, chartArea } = chart
    const top = chartArea.top
    const height = chartArea.bottom - chartArea.top

    ctx.save()
    ctx.beginPath()
    ctx.rect(chartArea.left, top, chartArea.right - chartArea.left, height)
    ctx.clip()

    for (const gap of gaps) {
      const left = scale.getPixelForValue(gap.start)
      const right = scale.getPixelForValue(gap.end)
      const width = Math.max(right - left, 2)

      ctx.fillStyle = 'rgba(100, 116, 139, 0.14)'
      ctx.fillRect(left, top, width, height)

      ctx.strokeStyle = 'rgba(71, 85, 105, 0.5)'
      ctx.lineWidth = 1
      ctx.setLineDash([4, 4])
      ctx.beginPath()
      ctx.moveTo(left, top)
      ctx.lineTo(left, top + height)
      ctx.moveTo(right, top)
      ctx.lineTo(right, top + height)
      ctx.stroke()
      ctx.setLineDash([])

      if (width > 58) {
        ctx.fillStyle = '#475569'
        ctx.font = '600 11px system-ui, -apple-system, sans-serif'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'top'
        ctx.fillText(`no data · ${formatDuration(gap.end - gap.start)}`, left + width / 2, top + 6)
      }
    }
    ctx.restore()
  },
}

/** Outdoor samples arrive hourly, far sparser than the indoor series. */
type OutsidePoint = {
  ts: string
  temperature_c: number
  humidity_pct: number
}

type Props = {
  measurements: Measurement[]
  outside?: OutsidePoint[]
  humidityMin: number
  humidityMax: number
  tempMin: number
  tempMax: number
}

export function ClimateChart({
  measurements,
  outside = [],
  humidityMin,
  humidityMax,
  tempMin,
  tempMax,
}: Props) {
  const { data, gaps, tempRange, humidityRange } = useMemo(() => {
    const times = measurements.map((m) => new Date(m.ts).getTime())
    const found = detectGaps(times)
    const gapIndexes = new Set(found.map((g) => g.index))
    const shared = {
      tension: 0.25,
      spanGaps: false,
      pointRadius: measurements.length > 200 ? 0 : 2,
    }
    // Outdoor readings are hourly, so gaps there are expected, not downtime.
    const outsideShared = {
      tension: 0.25,
      spanGaps: true,
      borderDash: [6, 4],
      borderWidth: 1.75,
      pointRadius: outside.length > 200 ? 0 : 1.5,
    }
    const outsidePoints = outside.map((o) => new Date(o.ts).getTime())
    return {
      gaps: found,
      tempRange: extent([
        ...measurements.map((m) => m.temperature_c),
        ...outside.map((o) => o.temperature_c),
      ]),
      humidityRange: extent([
        ...measurements.map((m) => m.humidity_pct),
        ...outside.map((o) => o.humidity_pct),
      ]),
      data: {
        datasets: [
          {
            label: 'Inside °C',
            data: toPoints(measurements, times, gapIndexes, (m) => m.temperature_c),
            borderColor: '#c45c26',
            backgroundColor: 'rgba(196, 92, 38, 0.12)',
            yAxisID: 'yTemp',
            ...shared,
          },
          {
            label: 'Inside %',
            data: toPoints(measurements, times, gapIndexes, (m) => m.humidity_pct),
            borderColor: '#2a6f7a',
            backgroundColor: 'rgba(42, 111, 122, 0.12)',
            yAxisID: 'yHumidity',
            ...shared,
          },
          {
            label: 'Outside °C',
            data: outside.map((o, i) => ({ x: outsidePoints[i], y: o.temperature_c })),
            borderColor: '#d9a26a',
            backgroundColor: 'transparent',
            yAxisID: 'yTemp',
            ...outsideShared,
          },
          {
            label: 'Outside %',
            data: outside.map((o, i) => ({ x: outsidePoints[i], y: o.humidity_pct })),
            borderColor: '#7fa9b0',
            backgroundColor: 'transparent',
            yAxisID: 'yHumidity',
            ...outsideShared,
          },
        ],
      },
    }
  }, [measurements, outside])

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index' as const, intersect: false },
    plugins: {
      legend: { position: 'top' as const },
      title: { display: false },
      tooltip: { callbacks: {} },
      gapHighlight: { gaps },
    },
    scales: {
      yTemp: {
        type: 'linear' as const,
        position: 'left' as const,
        title: { display: true, text: '°C' },
        suggestedMin: Math.min(tempMin - 2, tempRange.min),
        suggestedMax: Math.max(tempMax + 2, tempRange.max),
        grid: { color: 'rgba(0,0,0,0.06)' },
      },
      yHumidity: {
        type: 'linear' as const,
        position: 'right' as const,
        title: { display: true, text: '% RH' },
        suggestedMin: Math.min(humidityMin - 5, humidityRange.min),
        suggestedMax: Math.max(humidityMax + 5, humidityRange.max),
        grid: { drawOnChartArea: false },
      },
      x: {
        type: 'time' as const,
        time: {
          tooltipFormat: 'MMM d, HH:mm',
          displayFormats: {
            minute: 'HH:mm',
            hour: 'HH:mm',
            day: 'MMM d',
          },
        },
        ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
        grid: { display: false },
      },
    },
  }

  if (measurements.length === 0) {
    return <div className="chart-empty">No measurements in this range yet.</div>
  }

  const longestGap = gaps.reduce((max, g) => Math.max(max, g.end - g.start), 0)

  return (
    <>
      <div className="chart-wrap">
        <Line data={data} options={options} plugins={[gapHighlight]} />
      </div>
      {gaps.length > 0 && (
        <p className="chart-note">
          {gaps.length === 1 ? '1 recording gap' : `${gaps.length} recording gaps`} in this range —
          longest {formatDuration(longestGap)} without data.
        </p>
      )}
    </>
  )
}
