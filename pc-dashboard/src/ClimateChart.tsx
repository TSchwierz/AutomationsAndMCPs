import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js'
import { Line } from 'react-chartjs-2'
import type { Measurement } from './api'

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
)

type Props = {
  measurements: Measurement[]
  humidityMin: number
  humidityMax: number
  tempMin: number
  tempMax: number
}

export function ClimateChart({
  measurements,
  humidityMin,
  humidityMax,
  tempMin,
  tempMax,
}: Props) {
  const labels = measurements.map((m) => {
    const d = new Date(m.ts)
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  })

  const data = {
    labels,
    datasets: [
      {
        label: 'Temperature °C',
        data: measurements.map((m) => m.temperature_c),
        borderColor: '#c45c26',
        backgroundColor: 'rgba(196, 92, 38, 0.12)',
        yAxisID: 'yTemp',
        tension: 0.25,
        pointRadius: measurements.length > 200 ? 0 : 2,
      },
      {
        label: 'Humidity %',
        data: measurements.map((m) => m.humidity_pct),
        borderColor: '#2a6f7a',
        backgroundColor: 'rgba(42, 111, 122, 0.12)',
        yAxisID: 'yHumidity',
        tension: 0.25,
        pointRadius: measurements.length > 200 ? 0 : 2,
      },
    ],
  }

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index' as const, intersect: false },
    plugins: {
      legend: { position: 'top' as const },
      title: { display: false },
      tooltip: { callbacks: {} },
    },
    scales: {
      yTemp: {
        type: 'linear' as const,
        position: 'left' as const,
        title: { display: true, text: '°C' },
        suggestedMin: Math.min(tempMin - 2, ...measurements.map((m) => m.temperature_c)),
        suggestedMax: Math.max(tempMax + 2, ...measurements.map((m) => m.temperature_c)),
        grid: { color: 'rgba(0,0,0,0.06)' },
      },
      yHumidity: {
        type: 'linear' as const,
        position: 'right' as const,
        title: { display: true, text: '% RH' },
        suggestedMin: Math.min(humidityMin - 5, ...measurements.map((m) => m.humidity_pct)),
        suggestedMax: Math.max(humidityMax + 5, ...measurements.map((m) => m.humidity_pct)),
        grid: { drawOnChartArea: false },
      },
      x: {
        ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
        grid: { display: false },
      },
    },
  }

  if (measurements.length === 0) {
    return <div className="chart-empty">No measurements in this range yet.</div>
  }

  return (
    <div className="chart-wrap">
      <Line data={data} options={options} />
    </div>
  )
}
