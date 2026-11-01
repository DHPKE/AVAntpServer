// Wall clock for one VLAN: /clock/<vlan id>. Time comes from the box (round-trip compensated), formatted here in the VLAN's
// time zone. What is shown and how it looks is set per VLAN in the WebUI; URL parameters override it (?seconds=0&fg=ff9900).
(() => {
  'use strict'
  const POLL_MS = 2000, STALE_MS = 6000, IDLE_MS = 3000
  const FONTS = {'ds-digital': "'DS-Digital', monospace", 'chakra-petch': "'Chakra Petch', monospace", oxanium: "'Oxanium', monospace",
                 'share-tech-mono': "'Share Tech Mono', monospace", system: 'ui-monospace, Menlo, Consolas, monospace'}
  const LOCALE = {en: 'en-GB', de: 'de-AT'}
  const FLAGS = ['seconds', 'weekday', 'day', 'month', 'year', 'zone', 'name', 'sync', 'hours12', 'blink']
  const CHOICES = {month_style: ['long', 'short', 'numeric'], lang: Object.keys(LOCALE), font: Object.keys(FONTS)}
  const $ = id => document.getElementById(id)
  const vlan = (location.pathname.match(/^\/clock\/(\d+)$/) || [])[1]
  const params = new URLSearchParams(location.search)

  let info = null, skew = 0, lastOk = 0, lastKey = '', laidOut = ''

  function settings() {
    const s = Object.assign({}, info.settings)
    for (const k of FLAGS) if (params.has(k)) s[k] = ['1', 'true', 'on'].includes(params.get(k).toLowerCase())
    for (const [k, allowed] of Object.entries(CHOICES)) if (allowed.includes(params.get(k))) s[k] = params.get(k)
    for (const k of ['fg', 'bg']) {
      const c = '#' + (params.get(k) || '').replace(/^#/, '')
      if (/^#[0-9a-f]{6}$/i.test(c)) s[k] = c
    }
    return s
  }

  async function poll() {
    const t0 = Date.now()
    try {
      const r = await fetch('/api/clock/' + vlan, {cache: 'no-store'})
      if (r.status === 404) { info = null; $('warn').textContent = 'NO SUCH VLAN'; return }
      if (!r.ok) return
      const d = await r.json(), t1 = Date.now()
      skew = d.time_ms - (t0 + t1) / 2            // the reply was made halfway through the round trip
      info = d
      lastOk = t1
    } catch (_) { /* the next frame shows the dashes once the data is too old */ }
  }

  function look(s) {
    const root = document.documentElement.style
    root.setProperty('--fg', s.fg)
    root.setProperty('--bg', s.bg)
    root.setProperty('--font', FONTS[s.font])
    document.body.classList.toggle('blink', s.blink)
  }

  function timeText(s, now, live) {
    if (!live) return s.seconds ? '--:--:--' : '--:--'
    const p = new Intl.DateTimeFormat(LOCALE[s.lang], {timeZone: info.timezone, hourCycle: s.hours12 ? 'h12' : 'h23',
      hour: '2-digit', minute: '2-digit', second: s.seconds ? '2-digit' : undefined}).formatToParts(now)
    const v = t => (p.find(x => x.type === t) || {}).value || ''
    return `${v('hour')}:${v('minute')}` + (s.seconds ? ':' + v('second') : '') + (s.hours12 ? ' ' + v('dayPeriod').toUpperCase() : '')
  }

  function drawTime(text) {
    const el = $('time')
    el.textContent = ''
    const [clock, period] = text.split(' ')
    for (const ch of clock) {
      const span = document.createElement('span')
      span.className = ch === ':' ? 'col' : 'dig'
      span.textContent = ch
      el.appendChild(span)
    }
    if (period) {
      const span = document.createElement('span')
      span.className = 'ampm'
      span.textContent = period
      el.appendChild(span)
    }
  }

  const hasDate = s => s.weekday || s.day || s.month || s.year
  function dateOptions(s) {
    const numeric = s.month && s.month_style === 'numeric'
    return {weekday: s.weekday ? 'long' : undefined, day: s.day ? (numeric ? '2-digit' : 'numeric') : undefined,
            month: s.month ? (numeric ? '2-digit' : s.month_style) : undefined, year: s.year ? 'numeric' : undefined}
  }
  const dateText = (s, now) => hasDate(s) ? new Intl.DateTimeFormat(LOCALE[s.lang], Object.assign({timeZone: info.timezone}, dateOptions(s))).format(now) : ''

  const zoneText = tz => tz.replace(/_/g, ' ').split('/').join(' / ')

  // ---- layout: sizes depend on the settings, the screen and the font, never on the digits or the date being shown
  const ctx = document.createElement('canvas').getContext('2d')
  const LINE_SPACING = {date: .1, sub: .12, warn: .12}          // em, as in the style sheet
  const WARNINGS = ['NO CONNECTION TO THE CLOCK', 'STOPPED: NO UPSTREAM TIME SOURCE', 'NO UPSTREAM \u00b7 TIME MAY DRIFT']
  const textWidth = (text, px, s, spacing) => { ctx.font = `700 ${px}px ${FONTS[s.font]}`; return ctx.measureText(text).width + text.length * spacing * px }

  // the longest date this format can produce: the template of a sample date, each name replaced by the widest one
  function widestDate(s) {
    const loc = LOCALE[s.lang], o = dateOptions(s), at = (y, m, d) => new Date(Date.UTC(y, m, d, 12))
    const widest = (type, dates) => {
      const f = new Intl.DateTimeFormat(loc, {timeZone: 'UTC', [type]: o[type]})
      return dates.map(d => f.format(d)).sort((a, b) => textWidth(b, 100, s, 0) - textWidth(a, 100, s, 0))[0]
    }
    const days = [...Array(7).keys()].map(i => at(2026, 10, 1 + i)), months = [...Array(12).keys()].map(i => at(2026, i, 28))
    return new Intl.DateTimeFormat(loc, Object.assign({timeZone: 'UTC'}, o)).formatToParts(at(2026, 0, 28))
      .map(p => p.type === 'weekday' ? widest('weekday', days) : p.type === 'month' && isNaN(p.value) ? widest('month', months) : p.value).join('')
  }

  function layout(s) {
    const lines = {date: hasDate(s), sub: s.name || s.zone, warn: true}      // the warning line always keeps its place
    for (const k of Object.keys(lines)) $(k).hidden = !lines[k]
    const t = $('time')
    t.style.fontSize = '100px'
    const count = Object.values(lines).filter(Boolean).length
    const fs = Math.floor(100 * Math.min(innerWidth * .94 / t.scrollWidth, innerHeight * (.8 - .1 * count) / t.scrollHeight))
    t.style.fontSize = fs + 'px'
    const fit = (k, share, texts) => {
      const px = Math.max(8, Math.floor(fs * share))
      const wide = Math.max(...texts.map(x => textWidth(x, px, s, LINE_SPACING[k])))
      $(k).style.fontSize = Math.max(8, wide > innerWidth * .96 ? Math.floor(px * innerWidth * .96 / wide) : px) + 'px'
    }
    if (lines.date) fit('date', .18, [widestDate(s)])
    if (lines.sub) fit('sub', .1, [$('sub').textContent.toUpperCase()])
    fit('warn', .1, WARNINGS)
  }

  function draw(live, now) {
    const s = info ? settings() : null
    if (!s) return
    look(s)
    drawTime(timeText(s, now, live))
    $('date').textContent = live ? dateText(s, now) : ''
    $('sub').textContent = [s.name && info.name, s.zone && zoneText(info.timezone)].filter(Boolean).join('  ·  ')
    $('warn').textContent = !live ? WARNINGS[info.serving ? 0 : 1] : s.sync && info.connected === false ? WARNINGS[2] : ''
    const key = JSON.stringify(s) + innerWidth + 'x' + innerHeight + document.fonts.status
    if (key !== laidOut) { laidOut = key; layout(s) }
  }

  function frame() {
    if (info) {
      const live = Date.now() - lastOk < STALE_MS && info.serving
      const now = new Date(Date.now() + skew)
      const key = live ? Math.floor(now / 1000) + JSON.stringify(info.settings) + info.connected : 'dashes' + info.serving
      if (key !== lastKey) { lastKey = key; draw(live, now) }
    }
    requestAnimationFrame(frame)
  }

  // ---- full screen, cursor, screen wake lock
  const root = document.documentElement
  const canFullscreen = !!(root.requestFullscreen)
  function toggleFullscreen() {
    if (!canFullscreen) return
    if (document.fullscreenElement) document.exitFullscreen()
    else root.requestFullscreen().catch(() => {})
    keepAwake()
  }
  let lock = null
  async function keepAwake() {
    try {
      if (navigator.wakeLock && !lock) {
        lock = await navigator.wakeLock.request('screen')
        lock.addEventListener('release', () => { lock = null })
      }
    } catch (_) { /* not allowed (plain http, battery saver): the screen may dim */ }
  }
  let idle
  function awake() {
    document.body.classList.remove('idle')
    clearTimeout(idle)
    idle = setTimeout(() => document.body.classList.add('idle'), IDLE_MS)
  }

  async function chooser() {
    document.title = 'Clocks'
    const list = await (await fetch('/api/clock')).json()
    for (const v of list.vlans) {
      const a = document.createElement('a')
      a.href = '/clock/' + v.id
      a.textContent = `VLAN ${v.id}  ${v.name}`
      $('pick').appendChild(a)
    }
    if (!list.vlans.length) $('pick').textContent = 'No VLANs configured.'
  }

  if (!vlan) { chooser(); return }
  $('fs').hidden = !canFullscreen
  $('fs').addEventListener('click', toggleFullscreen)
  document.addEventListener('dblclick', toggleFullscreen)
  document.addEventListener('keydown', e => { if (e.key === 'f' || e.key === 'F') toggleFullscreen() })
  for (const ev of ['mousemove', 'touchstart', 'keydown']) document.addEventListener(ev, awake, {passive: true})
  document.addEventListener('visibilitychange', () => { if (!document.hidden) keepAwake() })
  window.addEventListener('resize', () => { lastKey = '' })              // draw again: the layout is measured for the new size
  if (document.fonts) document.fonts.ready.then(() => { lastKey = '' })  // and once the font is really there
  keepAwake()
  poll().then(() => { document.title = info ? `${info.name || 'VLAN ' + vlan} clock` : 'Clock' })
  setInterval(poll, POLL_MS)
  requestAnimationFrame(frame)
})()
