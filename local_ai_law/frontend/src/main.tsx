import React, { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

type Role = 'user' | 'assistant'

interface Group {
  id: string
  name: string
  emoji?: string
  color?: string
  language?: string
  chunk_count?: number
}

// The model, top-k and level are the server's (the owner's .env); the page only
// knows the language.
interface Settings {
  app_language?: string
}

interface Source {
  type?: string
  source: string
  page?: number
  score?: number
  text?: string
  page_type?: string
  heading_path?: string
  sensitivity?: string
}

interface ChatMessage {
  id: string
  clientQueryId?: string
  role: Role
  content: string
  images?: PendingImage[]
  sources?: Source[]
  // Where this answer's passages are kept: GET /api/sessions/{id}/turns/{n}/passages/{k}
  sessionId?: string
  turn?: number
  agent?: string
  visionBadges?: string[]
  streaming?: boolean
}

interface PendingImage {
  b64: string
  mime: string
  dataUrl: string
}

declare global {
  interface Window {
    __ragChatState?: () => { group: Group | null; cfg: Settings; token: string }
    RagChat?: {
      resumeSession: (id: string) => Promise<void>
      clear: (notify?: boolean) => void
      ask: (query: string) => void
      sendCurrent?: () => void
      debug?: () => Record<string, unknown>
    }
    marked?: { parse: (src: string) => string }
    renderMarkdown?: (src: string) => string
    highlightIn?: (el: Element | null) => void
    toast?: (msg: string, type?: string) => void
    showSrc?: (i: number, ref?: { sources?: Source[]; session?: string; turn?: number }) => void
    lastSources?: Source[]
    // The Documents dialog in index.html: the rel paths chosen for this chat, or null for all.
    chatSources?: () => string[] | null
    resetChatSources?: () => void
  }
}

const MAX_IMAGES = 5

// Browser storage throws in a private window or when site data is blocked.
function readToken(): string {
  try { return localStorage.getItem('rag_token') || '' } catch { return '' }
}

function makeId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  const bytes = new Uint8Array(16)
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(b => b.toString(16).padStart(2, '0'))
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10).join('')}`
}

type ShellChatState = 'connecting' | 'connected' | 'disconnected' | 'error' | 'sending' | 'received' | 'working' | 'ready'

function publishShellChatState(state: ShellChatState, label: string) {
  window.dispatchEvent(new CustomEvent('rag:chat-connection', {
    detail: { state, label },
  }))
}

function authHeaders(): HeadersInit {
  const token = readToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = init.body instanceof FormData
    ? authHeaders()
    : { 'Content-Type': 'application/json', ...authHeaders() }
  const res = await fetch(path, { ...init, headers: { ...headers, ...(init.headers || {}) } })
  if (!res.ok) throw new Error(await res.text())
  return res.json() as Promise<T>
}

function getShellState() {
  return window.__ragChatState?.() || { group: null, cfg: {}, token: readToken() }
}

function renderMarkdown(src: string) {
  try {
    const cleaned = src.replace(/<think>[\s\S]*?(<\/think>|$)/g, '').trim()
    if (window.renderMarkdown) return String(window.renderMarkdown(cleaned) || '')
    if (window.marked) return String(window.marked.parse(cleaned) || '')
    return cleaned.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch] || ch))
  } catch {
    return src.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch] || ch))
  }
}

// Server messages are English; show them in the account's language.
function uiT(s: string): string {
  return (window as any).uiT?.(s) ?? s
}

function sourceScore(score?: number) {
  return `${Math.round((score || 0) * 100)}%`
}

function Icon({ name }: { name: 'paperclip' | 'send' | 'plus' | 'stop' }) {
  if (name === 'paperclip') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.8-9.8a4 4 0 0 1 5.7 5.7l-9.8 9.8a2 2 0 1 1-2.8-2.8l9.2-9.2" />
      </svg>
    )
  }
  if (name === 'send') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="m22 2-7 20-4-9-9-4 20-7Z" />
        <path d="M22 2 11 13" />
      </svg>
    )
  }
  if (name === 'stop') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3" y="3" width="18" height="18" rx="2" />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

function App() {
  const shell = getShellState()
  const [group, setGroup] = useState<Group | null>(shell.group)
  const [cfg, setCfg] = useState<Settings>(shell.cfg || {})
  const [token, setToken] = useState(shell.token || '')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([])
  const [streaming, setStreaming] = useState(false)
  const [status, setStatus] = useState('')
  const [connected, setConnected] = useState(false)
  const [disclaimer, setDisclaimer] = useState('AI responses may be inaccurate. Always verify important information from original sources.')
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const responseTimerRef = useRef<number | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const messagesRef = useRef(messages)
  const activeAssistantIdRef = useRef<string | null>(null)
  const clearNoticeRef = useRef(false)
  const sendRef = useRef<(override?: string) => void>(() => {})
  const lastSubmitAttemptRef = useRef('')
  const sendAttemptsRef = useRef(0)

  useEffect(() => { messagesRef.current = messages }, [messages])

  useEffect(() => {
    api<{ disclaimer?: string }>('/api/meta')
      .then(meta => { if (meta?.disclaimer) setDisclaimer(meta.disclaimer) })
      .catch(() => { /* keep the default disclaimer */ })
  }, [])

  const updateActiveAssistant = useCallback((updater: (message: ChatMessage) => ChatMessage) => {
    const id = activeAssistantIdRef.current
    if (!id) return
    setMessages(prev => {
      const idx = prev.findIndex(message => message.id === id)
      if (idx < 0) return prev
      return prev.map((message, index) => index === idx ? updater(message) : message)
    })
  }, [])

  const addSystemNotice = useCallback((content: string) => {
    setMessages(prev => [
      ...prev,
      {
        id: makeId(),
        role: 'assistant',
        content,
        streaming: false,
      },
    ])
  }, [])

  const clearResponseTimer = useCallback(() => {
    if (responseTimerRef.current) {
      window.clearTimeout(responseTimerRef.current)
      responseTimerRef.current = null
    }
  }, [])

  const refreshShell = useCallback(() => {
    const s = getShellState()
    setGroup(s.group)
    const shellCfg = s.cfg || {}
    setCfg(prev => ({ ...prev, ...shellCfg }))
    setToken(s.token || '')
  }, [])

  useEffect(() => {
    const onChange = () => refreshShell()
    window.addEventListener('rag:state-change', onChange)
    const timer = window.setInterval(refreshShell, 5000)
    return () => {
      window.removeEventListener('rag:state-change', onChange)
      window.clearInterval(timer)
    }
  }, [refreshShell])

  useEffect(() => {
    if (!token) return
    api<Settings>('/api/settings').then(s => {
      setCfg(prev => ({ ...prev, ...s }))
    }).catch(() => {})
  }, [token])

  useEffect(() => {
    if (!token) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    // Prefer the httpOnly rag_token cookie (sent automatically on same-origin WS)
    // so the token never rides in the URL, where it can leak into logs/proxies.
    // Fall back to ?token= once if the cookie handshake is rejected (4001).
    const cookieUrl = `${proto}://${location.host}/ws/chat`
    const tokenUrl = `${cookieUrl}?token=${encodeURIComponent(token)}`
    let useTokenUrl = false
    let closedByEffect = false
    const openSocket = () => {
      const ws = new WebSocket(useTokenUrl ? tokenUrl : cookieUrl)
      wsRef.current = ws

      setConnected(false)
      publishShellChatState('connecting', 'Connecting...')
      ws.onopen = () => {
        setConnected(true)
        setStatus('')
        publishShellChatState('connected', 'Connected')
      }
      ws.onclose = event => {
        setConnected(false)
        clearResponseTimer()
        if (closedByEffect) return
        if (!useTokenUrl && event.code === 4001) {
          // No/invalid cookie on this setup — retry immediately with the token URL.
          useTokenUrl = true
          openSocket()
          return
        }
        setStatus('Disconnected')
        publishShellChatState('disconnected', 'Disconnected')
        setStreaming(false)
        updateActiveAssistant(message => message.streaming
          ? { ...message, streaming: false, content: message.content || 'The chat connection closed before the response finished.' }
          : message)
        if (readToken()) reconnectTimerRef.current = window.setTimeout(openSocket, 1500)
      }
      ws.onerror = () => {
        setConnected(false)
        setStatus('Connection error')
        publishShellChatState('error', 'Chat error')
      }
      ws.onmessage = event => {
      let msg: any
      try {
        msg = JSON.parse(event.data)
      } catch {
        setStreaming(false)
        setStatus('')
        publishShellChatState('error', 'Invalid chat response')
        updateActiveAssistant(message => ({ ...message, streaming: false, content: message.content || 'The server sent an invalid chat response.' }))
        window.toast?.('Invalid chat response from server', 'error')
        return
      }
      if (msg.type === 'query_received') {
        clearResponseTimer()
        setStatus('Prompt received by server...')
        publishShellChatState('received', 'Prompt received')
      }
      if (msg.type === 'status') {
        setStatus(uiT(msg.content || ''))
        publishShellChatState('working', msg.content || 'Working')
      }
      if (msg.type === 'start') {
        clearResponseTimer()
        setStatus('')
        publishShellChatState('working', 'Generating')
      }
      if (msg.type === 'chunk') {
        updateActiveAssistant(message => ({ ...message, content: message.content + (msg.content || '') }))
      }
      if (msg.type === 'vision_result') {
        const label = `${msg.total > 1 ? `Image ${msg.index}/${msg.total}` : 'Image'}: ${msg.image_type}${msg.is_code ? ' · coder' : ''}`
        updateActiveAssistant(message => ({ ...message, visionBadges: [...(message.visionBadges || []), label] }))
      }
      if (msg.type === 'sources') {
        const sources = msg.sources || []
        window.lastSources = sources
        updateActiveAssistant(message => ({ ...message, sources, agent: msg.agent, sessionId: msg.session_id, turn: msg.turn }))
      }
      if (msg.type === 'done') {
        clearResponseTimer()
        updateActiveAssistant(message => ({ ...message, streaming: false }))
        activeAssistantIdRef.current = null
        setStreaming(false)
        setStatus('')
        publishShellChatState('connected', 'Connected')
      }
      if (msg.type === 'error') {
        clearResponseTimer()
        updateActiveAssistant(message => ({
          ...message,
          streaming: false,
          content: message.content || uiT(msg.content || 'The assistant is not available right now. Please try again in a moment.'),
        }))
        activeAssistantIdRef.current = null
        setStreaming(false)
        setStatus('')
        publishShellChatState('error', 'Chat error')
        window.toast?.(uiT(msg.content || 'The assistant is not available right now. Please try again in a moment.'), 'error')
      }
      if (msg.type === 'warning') window.toast?.(uiT(msg.content || 'Warning'), 'warn')
      if (msg.type === 'history_cleared') {
        // only toast for an explicit user clear — group switches clear silently
        if (clearNoticeRef.current) window.toast?.('Chat history cleared', 'info')
        clearNoticeRef.current = false
      }
      if (msg.type === 'session_resumed') window.toast?.(`Session resumed - ${msg.turns} turns restored`, 'info')
    }
    }
    openSocket()

    return () => {
      closedByEffect = true
      if (reconnectTimerRef.current) window.clearTimeout(reconnectTimerRef.current)
      clearResponseTimer()
      publishShellChatState('disconnected', 'Disconnected')
      wsRef.current?.close()
    }
  }, [clearResponseTimer, token, updateActiveAssistant])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`
  }, [input])

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const incoming = Array.from(files).filter(f => f.type.startsWith('image/')).slice(0, MAX_IMAGES - pendingImages.length)
    const loaded = await Promise.all(incoming.map(file => new Promise<PendingImage>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => {
        const dataUrl = String(reader.result || '')
        resolve({ dataUrl, mime: file.type, b64: dataUrl.split(',')[1] || '' })
      }
      reader.onerror = reject
      reader.readAsDataURL(file)
    })))
    setPendingImages(prev => [...prev, ...loaded].slice(0, MAX_IMAGES))
  }, [pendingImages.length])

  const send = useCallback((override?: string) => {
    sendAttemptsRef.current += 1
    lastSubmitAttemptRef.current = new Date().toISOString()
    const query = (override ?? inputRef.current?.value ?? input).trim()
    const shell = getShellState()
    const activeGroup = group || shell.group
    if (!activeGroup) {
      addSystemNotice('Open a group first, then send your message.')
      setStatus('No group selected')
      publishShellChatState('error', 'No group selected')
      return
    }
    if (streaming) {
      clearResponseTimer()
      updateActiveAssistant(message => message.streaming ? { ...message, streaming: false } : message)
      activeAssistantIdRef.current = null
      setStreaming(false)
      setStatus('')
      publishShellChatState('connected', 'Connected')
      return
    }
    if (!query && !pendingImages.length) {
      setStatus('Type a message first')
      return
    }
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      const state = wsRef.current?.readyState ?? -1
      const msg = state === WebSocket.CONNECTING
        ? 'Chat is still connecting. Try again in a moment.'
        : 'Chat is not connected. I am reconnecting now; try again when it says Connected.'
      addSystemNotice(msg)
      setStatus(state === WebSocket.CONNECTING ? 'Connecting' : 'Reconnecting')
      publishShellChatState('disconnected', state === WebSocket.CONNECTING ? 'Connecting' : 'Reconnecting')
      wsRef.current?.close()
      window.toast?.(msg, 'warn')
      return
    }
    const userImages = pendingImages
    const assistantId = makeId()
    const clientQueryId = makeId()
    activeAssistantIdRef.current = assistantId
    setMessages(prev => [
      ...prev,
      { id: makeId(), clientQueryId, role: 'user', content: query || '(image)', images: userImages },
      { id: assistantId, role: 'assistant', content: '', streaming: true },
    ])
    try {
      wsRef.current.send(JSON.stringify({
        type: 'query',
        client_query_id: clientQueryId,
        query: query || '(image)',
        group_id: activeGroup.id,
        sources: window.chatSources?.() || undefined,
        image_data: userImages.length ? userImages.map(img => img.b64) : undefined,
      }))
    } catch (err) {
      updateActiveAssistant(message => ({
        ...message,
        streaming: false,
        content: `Could not send the prompt: ${err instanceof Error ? err.message : String(err)}`,
      }))
      activeAssistantIdRef.current = null
      setStreaming(false)
      setStatus('Send failed')
      publishShellChatState('error', 'Send failed')
      return
    }
    setInput('')
    if (inputRef.current) inputRef.current.value = ''
    setPendingImages([])
    setStreaming(true)
    setStatus('Sending prompt...')
    publishShellChatState('sending', 'Sending prompt')
    responseTimerRef.current = window.setTimeout(() => {
      updateActiveAssistant(message => message.streaming
        ? { ...message, content: message.content || 'The server did not acknowledge this prompt. Reconnecting chat...' }
        : message)
      wsRef.current?.close()
      setStreaming(false)
      setStatus('Reconnecting')
      publishShellChatState('disconnected', 'Reconnecting')
    }, 15000)
  }, [addSystemNotice, clearResponseTimer, group, input, pendingImages, streaming, updateActiveAssistant])

  useEffect(() => {
    sendRef.current = send
  }, [send])


  useEffect(() => {
    window.RagChat = {
      clear: (notify = false) => {
        clearNoticeRef.current = !!notify
        activeAssistantIdRef.current = null
        setMessages([])
        window.resetChatSources?.()
        wsRef.current?.send(JSON.stringify({ type: 'clear_history' }))
      },
      ask: (query: string) => {
        setInput(query)
        window.setTimeout(() => send(query), 0)
      },
      sendCurrent: () => sendRef.current(),
      resumeSession: async (id: string) => {
        const session = await api<any>(`/api/sessions/${id}`)
        const restored: ChatMessage[] = []
        for (const [n, turn] of (session.turns || []).entries()) {
          restored.push({ id: makeId(), role: 'user', content: turn.user || '' })
          restored.push({
            id: makeId(),
            role: 'assistant',
            content: turn.assistant || '',
            sources: turn.sources || [],
            sessionId: id,
            turn: n,
            agent: turn.agent,
          })
        }
        activeAssistantIdRef.current = null
        setMessages(restored)
        window.resetChatSources?.()
        wsRef.current?.send(JSON.stringify({ type: 'resume_session', session_id: id }))
      },
      debug: () => ({
        group,
        shellGroup: getShellState().group,
        connected,
        streaming,
        status,
        sendAttempts: sendAttemptsRef.current,
        lastSubmitAttempt: lastSubmitAttemptRef.current,
        textareaValue: inputRef.current?.value || '',
        pendingImages: pendingImages.length,
        readyState: wsRef.current?.readyState ?? -1,
        tokenPresent: Boolean(token),
      }),
    }
  }, [connected, group, pendingImages.length, send, status, streaming, token])

  const submit = (e: FormEvent) => {
    e.preventDefault()
    send()
  }

  const keyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const tr = uiT
  const welcome = useMemo(() => {
    if (!group) return tr('Choose a client to start chatting.')
    if (!group.chunk_count) return tr('No documents are ready yet. Add them under Files; chat can use a file once it shows a green tick.')
    return `${tr('Ask anything about')} ${group.name}.`
  }, [group, cfg.app_language])

  return (
    <div className="rc-shell">
      <div className="rc-messages">
        {!messages.length && (
          <div className="rc-welcome">
            <div>{welcome}</div>
          </div>
        )}
        {messages.map(msg => <MessageView key={msg.id} message={msg} />)}
        {status && <div className="rc-status"><span />{status}</div>}
        <div ref={bottomRef} />
      </div>

      <form className="rc-composer" onSubmit={submit}>
        <div className="rc-input-wrap">
          {!!pendingImages.length && (
            <div className="rc-images">
              {pendingImages.map((img, idx) => (
                <button type="button" key={img.dataUrl} onClick={() => setPendingImages(prev => prev.filter((_, i) => i !== idx))} title={tr('Remove image')}>
                  <img src={img.dataUrl} alt="" />
                </button>
              ))}
            </div>
          )}
          <div className="rc-input-row" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); addFiles(e.dataTransfer.files) }}>
          <button className="rc-icon-btn" type="button" onClick={() => fileRef.current?.click()} disabled={pendingImages.length >= MAX_IMAGES} title={tr('Attach image')} aria-label={tr('Attach image')}>
            <Icon name="paperclip" />
          </button>
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={keyDown}
            onPaste={e => {
              const files = e.clipboardData?.files
              if (files?.length) addFiles(files)
            }}
            rows={1}
            placeholder={tr("Ask about this client's documents...")}
          />
          <input ref={fileRef} type="file" accept="image/*" multiple hidden onChange={e => { if (e.target.files) addFiles(e.target.files); e.target.value = '' }} />
          <button className="rc-icon-btn primary" type="button" onClick={() => sendRef.current()} title={streaming ? tr('Stop') : tr('Send')} aria-label={tr('Send')}>
            <Icon name={streaming ? 'stop' : 'send'} />
          </button>
          </div>
        </div>
      </form>
      <div style={{fontSize:'var(--fs-xs)',color:'var(--text3)',textAlign:'center',padding:'4px 12px 0',lineHeight:1.4}}>
        {tr(disclaimer)}
      </div>
    </div>
  )
}

function MessageView({ message }: { message: ChatMessage }) {
  const bubbleRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!bubbleRef.current || message.streaming) return
    bubbleRef.current.querySelectorAll<HTMLElement>('pre code:not([data-highlighted])').forEach(block => {
      (window as any).hljs?.highlightElement?.(block)
    })
  }, [message.content, message.streaming])
  return (
    <div className={`rc-msg ${message.role}`}>
      <div className="rc-avatar">{message.role === 'user' ? 'U' : 'AI'}</div>
      <div className="rc-bubble" ref={bubbleRef}>
        {!!message.images?.length && (
          <div className="rc-img-grid">
            {message.images.map(img => <img key={img.dataUrl} src={img.dataUrl} alt="" />)}
          </div>
        )}
        {!!message.visionBadges?.length && (
          <div className="rc-badges">{message.visionBadges.map(b => <span key={b}>{b}</span>)}</div>
        )}
        <div className={`rc-md ${message.streaming ? 'streaming' : ''}`} dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content || (message.streaming ? ((window as any).uiT?.('Thinking...') ?? 'Thinking...') : '')) }} />
        {!!message.sources?.length && (
          <div className="rc-sources">
            <div className="rc-sources-title">{(window as any).uiT?.('Sources') ?? 'Sources'}</div>
            <div className="rc-source-list">
              {message.sources.slice(0, 10).map((src, idx) => (
                <button type="button" key={`${src.source}-${idx}`} onClick={() => { window.lastSources = message.sources; window.showSrc?.(idx, { sources: message.sources, session: message.sessionId, turn: message.turn }) }}
                  title={src.heading_path || undefined}>
                  <span>{src.source}</span>
                  {(src.sensitivity === 'privileged' || src.sensitivity === 'sensitive') && (
                    <small className="badge-privileged">PRIVILEGED</small>
                  )}
                  <small>{src.type === 'wiki' ? src.page_type || 'wiki' : `p.${src.page || 0}`}</small>
                  <b>{sourceScore(src.score)}</b>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const root = document.getElementById('react-chat-root')
if (root) createRoot(root).render(<App />)
