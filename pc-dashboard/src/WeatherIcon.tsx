/** Line icons for the shared condition vocabulary the Pi sends. */

type Props = {
  condition: string | null
  label?: string
  className?: string
}

const CLOUD_PATH = 'M7 17h9.2a3.4 3.4 0 0 0 .3-6.78 5 5 0 0 0-9.62-.9A3.55 3.55 0 0 0 7 17Z'

function Cloud() {
  return <path d={CLOUD_PATH} />
}

function Sun({ cx = 12, cy = 12, r = 4 }: { cx?: number; cy?: number; r?: number }) {
  const rays = [0, 45, 90, 135, 180, 225, 270, 315]
  return (
    <>
      <circle cx={cx} cy={cy} r={r} />
      {rays.map((deg) => {
        const rad = (deg * Math.PI) / 180
        const inner = r + 2
        const outer = r + 4.5
        return (
          <line
            key={deg}
            x1={cx + Math.cos(rad) * inner}
            y1={cy + Math.sin(rad) * inner}
            x2={cx + Math.cos(rad) * outer}
            y2={cy + Math.sin(rad) * outer}
          />
        )
      })}
    </>
  )
}

function Drops({ long = false }: { long?: boolean }) {
  const end = long ? 23 : 21.5
  return (
    <>
      <line x1="9" y1="19" x2="8.2" y2={end} />
      <line x1="12.5" y1="19" x2="11.7" y2={end} />
      <line x1="16" y1="19" x2="15.2" y2={end} />
    </>
  )
}

function Flakes() {
  return (
    <>
      <line x1="9.5" y1="19.2" x2="9.5" y2="22.2" />
      <line x1="8.2" y1="19.9" x2="10.8" y2="21.5" />
      <line x1="10.8" y1="19.9" x2="8.2" y2="21.5" />
      <line x1="15.5" y1="19.2" x2="15.5" y2="22.2" />
      <line x1="14.2" y1="19.9" x2="16.8" y2="21.5" />
      <line x1="16.8" y1="19.9" x2="14.2" y2="21.5" />
    </>
  )
}

function glyph(condition: string | null) {
  switch (condition) {
    case 'clear':
      return <Sun />
    case 'partly_cloudy':
      return (
        <>
          <Sun cx={8.5} cy={7.5} r={2.6} />
          <Cloud />
        </>
      )
    case 'fog':
      return (
        <>
          <Cloud />
          <line x1="6" y1="20.2" x2="18" y2="20.2" />
          <line x1="8" y1="23" x2="16" y2="23" />
        </>
      )
    case 'drizzle':
      return (
        <>
          <Cloud />
          <line x1="10" y1="19.5" x2="9.5" y2="21" />
          <line x1="14.5" y1="19.5" x2="14" y2="21" />
        </>
      )
    case 'rain':
      return (
        <>
          <Cloud />
          <Drops />
        </>
      )
    case 'heavy_rain':
      return (
        <>
          <Cloud />
          <Drops long />
        </>
      )
    case 'sleet':
      return (
        <>
          <Cloud />
          <line x1="9.5" y1="19.2" x2="8.7" y2="22" />
          <line x1="15.5" y1="19.2" x2="15.5" y2="22.2" />
          <line x1="14.2" y1="19.9" x2="16.8" y2="21.5" />
          <line x1="16.8" y1="19.9" x2="14.2" y2="21.5" />
        </>
      )
    case 'snow':
      return (
        <>
          <Cloud />
          <Flakes />
        </>
      )
    case 'hail':
      return (
        <>
          <Cloud />
          <circle cx="9.5" cy="21" r="1.1" />
          <circle cx="14.5" cy="21" r="1.1" />
        </>
      )
    case 'thunderstorm':
      return (
        <>
          <Cloud />
          <path d="M13.4 18.2 10.6 21.4h2.4l-1.1 3 3.4-4h-2.4Z" />
        </>
      )
    case 'cloudy':
      return <Cloud />
    default:
      return (
        <>
          <Cloud />
          <line x1="9" y1="20.5" x2="15" y2="20.5" strokeDasharray="2 2" />
        </>
      )
  }
}

export function WeatherIcon({ condition, label, className }: Props) {
  return (
    <svg
      className={className ? `weather-icon ${className}` : 'weather-icon'}
      viewBox="0 0 24 26"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label={label ?? condition ?? 'unknown conditions'}
    >
      {glyph(condition)}
    </svg>
  )
}
