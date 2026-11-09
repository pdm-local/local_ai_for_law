import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { spawn } from 'node:child_process'

const root = resolve(import.meta.dirname, '..')
const chrome = process.env.CHROME_BIN || '/usr/bin/google-chrome'
const port = 9229 + Math.floor(Math.random() * 1000)
const httpPort = 19500 + Math.floor(Math.random() * 1000)
const profile = await mkdtemp(join(tmpdir(), 'rag-chat-smoke-'))
const bundlePath = resolve(root, 'static/chat/chat.js')

const pageHtml = `<!doctype html>
<html>
  <body>
    <div id="react-chat-root"></div>
    <script>
      localStorage.setItem('rag_token', 'smoke-token')
      window.__sentFrames = []
      window.__errors = []
      window.addEventListener('error', event => {
        window.__errors.push(String(event.error?.stack || event.message || event.filename || 'error'))
      })
      window.addEventListener('unhandledrejection', event => {
        window.__errors.push(String(event.reason?.stack || event.reason || 'unhandled rejection'))
      })
      window.__shell = {
        group: { id: 'group-smoke', name: 'Smoke Group', emoji: '*', chunk_count: 1 },
        cfg: { chat_model: 'smoke-model', top_k: 7, difficulty: 'intermediate' },
        models: ['smoke-model'],
        token: 'smoke-token'
      }
      window.__ragChatState = () => window.__shell
      window.renderMarkdown = (src) => String(src || '')
      window.highlightIn = () => {}
      window.toast = (msg, type) => { window.__lastToast = { msg, type } }
      class FakeWebSocket {
        static CONNECTING = 0
        static OPEN = 1
        static CLOSING = 2
        static CLOSED = 3
        constructor(url) {
          this.url = url
          this.readyState = FakeWebSocket.CONNECTING
          window.__lastSocket = this
          setTimeout(() => {
            this.readyState = FakeWebSocket.OPEN
            this.onopen && this.onopen()
          }, 20)
        }
        send(payload) {
          window.__sentFrames.push(JSON.parse(payload))
          const frame = JSON.parse(payload)
          if (frame.type === 'query') {
            setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify({ type: 'query_received', client_query_id: frame.client_query_id }) }), 5)
          }
        }
        close() {
          this.readyState = FakeWebSocket.CLOSED
          this.onclose && this.onclose()
        }
      }
      window.WebSocket = FakeWebSocket
    </script>
    <script type="module" src="/static/chat/chat.js"></script>
  </body>
</html>`

const server = createServer(async (req, res) => {
  try {
    if (req.url === '/' || req.url === '/chat-smoke.html') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
      res.end(pageHtml)
      return
    }
    if (req.url === '/static/chat/chat.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' })
      res.end(await readFile(bundlePath, 'utf8'))
      return
    }
    res.writeHead(404)
    res.end('not found')
  } catch (err) {
    res.writeHead(500)
    res.end(String(err?.stack || err))
  }
})
await new Promise(resolve => server.listen(httpPort, '127.0.0.1', resolve))

const browser = spawn(chrome, [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--disable-dev-shm-usage',
  '--disable-crash-reporter',
  '--disable-crashpad',
  '--disable-breakpad',
  `--user-data-dir=${profile}`,
  `--remote-debugging-port=${port}`,
  `http://127.0.0.1:${httpPort}/chat-smoke.html`,
], { stdio: ['ignore', 'ignore', 'pipe'] })

let stderr = ''
browser.stderr.on('data', chunk => { stderr += chunk.toString() })

async function sleep(ms) {
  await new Promise(resolve => setTimeout(resolve, ms))
}

async function getJson(path) {
  const res = await fetch(`http://127.0.0.1:${port}${path}`)
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

async function connectCdp() {
  for (let i = 0; i < 80; i++) {
    try {
      const pages = await getJson('/json')
      const page = pages.find(item => item.type === 'page' && item.url?.includes('/chat-smoke.html'))
      if (page?.webSocketDebuggerUrl) {
        return new WebSocket(page.webSocketDebuggerUrl)
      }
    } catch {}
    await sleep(100)
  }
  throw new Error(`Chrome DevTools did not start. stderr:\n${stderr}`)
}

const ws = await connectCdp()
let nextId = 1
const pending = new Map()
ws.addEventListener('message', event => {
  const msg = JSON.parse(event.data)
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id)
    pending.delete(msg.id)
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
  }
})
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true })
  ws.addEventListener('error', reject, { once: true })
})

function cdp(method, params = {}) {
  const id = nextId++
  ws.send(JSON.stringify({ id, method, params }))
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }))
}

async function evalExpr(expression) {
  const result = await cdp('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  })
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || 'Runtime exception')
  }
  return result.result.value
}

try {
  await sleep(500)
  await cdp('Runtime.enable')
  await evalExpr(`new Promise((resolve, reject) => {
    const deadline = Date.now() + 5000
    const tick = () => {
      const textarea = document.querySelector('.rc-input-row textarea')
      const send = document.querySelector('.rc-icon-btn.primary')
      const debug = window.RagChat?.debug?.()
      if (textarea && send && debug?.readyState === WebSocket.OPEN) resolve(true)
      else if (Date.now() > deadline) reject(new Error(JSON.stringify({
        hasTextarea: Boolean(textarea),
        hasSend: Boolean(send),
        debug,
        errors: window.__errors,
        scripts: [...document.scripts].map(script => ({ src: script.src, type: script.type })),
        html: document.body.innerHTML.slice(0, 500)
      })))
      else setTimeout(tick, 50)
    }
    tick()
  })`)

  await evalExpr(`(() => {
    const textarea = document.querySelector('.rc-input-row textarea')
    const value = 'enter smoke'
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
    textarea.focus()
    setter.call(textarea, value)
    textarea.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }))
    textarea.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'Enter' }))
  })()`)
  await sleep(100)
  const afterEnter = await evalExpr(`window.__sentFrames.filter(frame => frame.type === 'query').map(frame => frame.query)`)
  if (!afterEnter.includes('enter smoke')) {
    throw new Error(`Enter did not send query. Sent frames: ${JSON.stringify(afterEnter)}`)
  }

  await evalExpr(`window.__lastSocket.onmessage({ data: JSON.stringify({ type: 'done', stats: {} }) })`)
  await sleep(50)
  await evalExpr(`(() => {
    const textarea = document.querySelector('.rc-input-row textarea')
    const button = document.querySelector('.rc-icon-btn.primary')
    const value = 'button smoke'
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
    textarea.focus()
    setter.call(textarea, value)
    textarea.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }))
    button.click()
  })()`)
  await sleep(100)
  const afterButton = await evalExpr(`window.__sentFrames.filter(frame => frame.type === 'query').map(frame => frame.query)`)
  if (!afterButton.includes('button smoke')) {
    throw new Error(`Button did not send query. Sent frames: ${JSON.stringify(afterButton)}`)
  }

  const debug = await evalExpr(`window.RagChat.debug()`)
  console.log(JSON.stringify({ ok: true, sentQueries: afterButton, debug }, null, 2))
} finally {
  ws.close()
  browser.kill('SIGTERM')
  await new Promise(resolve => browser.once('exit', resolve))
  await new Promise(resolve => server.close(resolve))
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
}
