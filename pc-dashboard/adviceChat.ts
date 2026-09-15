import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin, PreviewServer, ViteDevServer } from 'vite'

const MISTRAL_URL = 'https://api.mistral.ai/v1/chat/completions'

const SYSTEM_PROMPT = `You are a studio climate advisor for a LAN-only room monitor.
Goals: prevent mold and keep PLA filament in a stable temperature/humidity band.
The indoor sensor is a DHT11 (about 1°C and a few % RH); do not over-interpret small differences.
Use the attached room notes, strategies log, weekly JSON reports, anomalies, and live snapshot.
Prefer airing out only when outdoor dew point is clearly below indoor dew point.
Do not claim to have changed Pi settings or run commands. Suggest what a human can do.`

type ChatMessage = { role: string; content: string }

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    req.on('data', (chunk) => chunks.push(Buffer.from(chunk)))
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function json(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify(body))
}

async function loadPiContext(env: Record<string, string>) {
  const base = (env.VITE_PI_API_BASE || process.env.VITE_PI_API_BASE || 'http://127.0.0.1:8787').replace(
    /\/$/,
    '',
  )
  const response = await fetch(`${base}/advisor/context`)
  if (!response.ok) {
    throw new Error(`Pi /advisor/context failed: ${response.status} ${await response.text()}`)
  }
  return (await response.json()) as {
    pack?: string
    weeks?: string[]
    truncated?: boolean
  }
}

async function handleAdvice(
  req: IncomingMessage,
  res: ServerResponse,
  env: Record<string, string>,
) {
  if (req.method !== 'POST') {
    json(res, 405, { error: 'Method not allowed' })
    return
  }
  const key = env.MISTRAL_API_KEY || process.env.MISTRAL_API_KEY || ''
  if (!key) {
    json(res, 503, {
      error: 'MISTRAL_API_KEY is not set in pc-dashboard/.env (server-only, not VITE_)',
    })
    return
  }

  let payload: { messages?: ChatMessage[] }
  try {
    payload = JSON.parse(await readBody(req)) as { messages?: ChatMessage[] }
  } catch {
    json(res, 400, { error: 'Invalid JSON' })
    return
  }
  const history = (payload.messages ?? []).filter(
    (item) =>
      (item.role === 'user' || item.role === 'assistant') && typeof item.content === 'string',
  )
  if (!history.length || history[history.length - 1]?.role !== 'user') {
    json(res, 400, { error: 'Send at least one user message' })
    return
  }

  const context = await loadPiContext(env)
  const system = `${SYSTEM_PROMPT}\n\n---\n${context.pack ?? ''}`
  const mistralRes = await fetch(MISTRAL_URL, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${key}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: env.MISTRAL_MODEL || process.env.MISTRAL_MODEL || 'mistral-small-latest',
      messages: [{ role: 'system', content: system }, ...history],
      temperature: 0.3,
    }),
  })
  const raw = await mistralRes.text()
  if (!mistralRes.ok) {
    json(res, 502, { error: raw || `Mistral HTTP ${mistralRes.status}` })
    return
  }
  const data = JSON.parse(raw) as {
    choices?: { message?: { content?: string } }[]
  }
  const reply = data.choices?.[0]?.message?.content?.trim()
  if (!reply) {
    json(res, 502, { error: 'Mistral returned an empty reply' })
    return
  }
  json(res, 200, { reply, weeks: context.weeks ?? [], truncated: Boolean(context.truncated) })
}

function attach(
  server: ViteDevServer | PreviewServer,
  env: Record<string, string>,
) {
  server.middlewares.use((req, res, next) => {
    const url = req.url?.split('?')[0]
    if (url !== '/advice/chat') {
      next()
      return
    }
    void handleAdvice(req, res, env).catch((err: unknown) => {
      json(res, 500, { error: err instanceof Error ? err.message : String(err) })
    })
  })
}

export function adviceChatPlugin(env: Record<string, string>): Plugin {
  return {
    name: 'advice-chat',
    configureServer(server) {
      attach(server, env)
    },
    configurePreviewServer(server) {
      attach(server, env)
    },
  }
}
