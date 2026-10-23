const {createApp, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick} = Vue

createApp({setup() {
  const tabs = ['monitor', 'network', 'vlans', 'ntp', 'log', 'system']
  const tab = ref('monitor')
  const user = ref(null), csrf = ref(''), needsSetup = ref(false), version = ref('')
  const lu = ref(''), lp = ref(''), loginErr = ref(''), busy = ref(false)
  const connected = ref(false), status = ref(null), skew = ref(0), now = ref(Date.now())
  const cfg = ref({active: null, pending: null}), ap = ref({state: 'idle'}), um = ref({state: 'off'}), sw = ref({state: 'idle'}), tc = ref({running: false, vlans: []})
  const draft = ref(null), up = reactive({default: '', backup: '', custom: ''}), baseline = ref('')
  const msg = ref(''), msgCls = ref('ok'), pw = reactive({old: '', new: ''})
  let timers = []

  async function api(method, path, body) {
    const h = {'Content-Type': 'application/json'}
    if (method !== 'GET') h['X-CSRF'] = csrf.value
    const r = await fetch(path, {method, headers: h, body: body === undefined ? undefined : JSON.stringify(body)})
    const d = await r.json().catch(() => ({}))
    if (r.status === 401 && path !== '/api/login') { stop(); user.value = null }
    if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status)
    return d
  }

  // ---- session
  async function start() {
    const s = await fetch('/api/session').then(r => r.json())
    user.value = s.user; csrf.value = s.csrf || ''; needsSetup.value = !!s.needs_setup; version.value = s.version || ''
    if (user.value) await afterLogin()
  }
  async function afterLogin() {
    await poll()
    await loadConfig()
    timers = [setInterval(poll, 2000), setInterval(() => now.value = Date.now(), 1000)]
    if (tab.value === 'monitor') startLive()
    startBeat()
    loadBoots()
  }
  // ---- log window
  const logSel = reactive({source: 'all', level: 'all', boot: 0, lines: 200, auto: true})
  const logLines = ref([]), logErr = ref(''), logLoaded = ref(false), logbox = ref(null)
  const boots = ref([]), bootsErr = ref(''), dismissed = ref('')
  try { dismissed.value = localStorage.getItem('avantp-restart-dismissed') || '' } catch (_) {}
  let logTimer = null
  const dt = t => t ? new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : ''
  function logCls(l) {
    if (/traceback|exception|error|fail|panic|segfault|oom|crit|killed|unhandled/i.test(l) && !/error: 0|no error/i.test(l)) return 'bad'
    if (/warn|degraded|timed out|link is down|downgraded|denied|rolling back|not confirmed/i.test(l)) return 'warn'
    return ''
  }
  async function loadLog() {
    try {
      const q = new URLSearchParams({source: logSel.source, level: logSel.level, boot: logSel.boot, lines: logSel.lines})
      const d = await api('GET', '/api/logs?' + q)
      const box = logbox.value
      const atEnd = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 40
      logLines.value = d.lines; logErr.value = d.error || ''; logLoaded.value = true
      if (atEnd) nextTick(() => { if (logbox.value) logbox.value.scrollTop = logbox.value.scrollHeight })
    } catch (e) { logErr.value = e.message }
  }
  async function loadBoots() {
    try { const d = await api('GET', '/api/boots'); boots.value = d.boots; bootsErr.value = d.error || '' } catch (e) { bootsErr.value = e.message }
  }
  async function copyLog() {
    try { await navigator.clipboard.writeText(logLines.value.join('\n')); msg.value = 'Log copied to the clipboard.'; msgCls.value = 'ok' }
    catch (_) { msg.value = 'Copy is not allowed in this browser: select the text and copy it by hand.'; msgCls.value = 'err' }
  }
  const restartNotice = computed(() => {
    const prev = boots.value.filter(b => b.ended !== 'running').slice(-1)[0]
    if (!prev || prev.ended !== 'abrupt' || String(prev.last) === dismissed.value) return null
    return {at: dt(prev.last), key: String(prev.last)}
  })
  function dismissRestart() {
    dismissed.value = restartNotice.value.key
    try { localStorage.setItem('avantp-restart-dismissed', dismissed.value) } catch (_) {}
  }
  function startLog() {
    stopLog(); loadLog(); loadBoots()
    logTimer = setInterval(() => { if (logSel.auto && !document.hidden) loadLog() }, 3000)
  }
  function stopLog() { clearInterval(logTimer); logTimer = null }
  // ---- the SYNCED icon breathes once every 2 seconds. The cycle is restarted on every odd second of the (server-corrected)
  // clock, so the glow peaks one second later, on the even second. It is restarted rather than left running forever
  // because a long-running CSS animation starts at the next drawn frame and then keeps that lag.
  const BEAT_PERIOD = 2000, BEAT_PHASE = 1000
  let beatTimer = null
  const reduceMotion = () => { try { return matchMedia('(prefers-reduced-motion: reduce)').matches } catch (_) { return false } }
  function scheduleBeat() {
    const t = Date.now() + skew.value
    let wait = (((BEAT_PHASE - t) % BEAT_PERIOD) + BEAT_PERIOD) % BEAT_PERIOD
    if (wait < 20) wait += BEAT_PERIOD                                  // never fire twice for the same boundary
    beatTimer = setTimeout(beat, wait + 2)
  }
  function beat() {
    const r = document.querySelector('.ring.ok')
    if (r && !document.hidden && !reduceMotion()) { r.classList.remove('beat'); void r.offsetWidth; r.classList.add('beat') }
    scheduleBeat()
  }
  function startBeat() { if (!beatTimer) scheduleBeat() }
  function stopBeat() { clearTimeout(beatTimer); beatTimer = null }
  function stop() { timers.forEach(clearInterval); timers = []; stopLive(); stopBeat(); stopLog() }
  async function doLogin() {
    busy.value = true; loginErr.value = ''
    try {
      const d = await api('POST', '/api/login', {user: lu.value, password: lp.value})
      user.value = d.user; csrf.value = d.csrf; version.value = d.version || ''; lp.value = ''
      await afterLogin()
    } catch (e) { loginErr.value = e.message } finally { busy.value = false }
  }
  async function doLogout() {
    try { await api('POST', '/api/logout', {}) } catch (_) {}
    stop(); user.value = null; draft.value = null
  }

  // ---- status
  async function poll() {
    try {
      const s = await api('GET', '/api/status')
      status.value = s; skew.value = s.utc * 1000 - Date.now(); connected.value = true
      ap.value = await api('GET', '/api/apply')
      um.value = await api('GET', '/api/update-mode')
      tc.value = await api('GET', '/api/timecode')
      const was = sw.value.state
      sw.value = await api('GET', '/api/software')
      // an update restarts the WebUI: the page that saw it 'updating' must reload to get the new program (and log in again)
      if (was === 'updating' && sw.value.state === 'done') setTimeout(() => location.reload(), 3500)
    } catch (_) { connected.value = false }
  }
  const tr = computed(() => status.value && status.value.chrony.tracking)
  const chronyRunning = computed(() => !!(status.value && status.value.chrony.running))
  const sources = computed(() => status.value ? status.value.chrony.sources : [])
  const ifaces = computed(() => status.value ? status.value.interfaces : [])
  // VLAN sub-interfaces (enp3s0.10) are not NICs; never offer them as WAN/LAN
  const physical = computed(() => ifaces.value.filter(i => !i.name.includes('.')))
  const ifaceNames = computed(() => {
    const n = physical.value.map(i => i.name)
    for (const k of [draft.value && draft.value.wan.iface, draft.value && draft.value.lan.iface]) if (k && !n.includes(k)) n.push(k)
    return n
  })
  const synced = computed(() => tr.value && tr.value.stratum < 16 && tr.value.ref_id !== '00000000')
  const syncCls = computed(() => !chronyRunning.value ? 'bad' : synced.value ? 'ok' : 'warn')
  const syncTxt = computed(() => !chronyRunning.value ? 'badc' : synced.value ? 'good' : 'warnc')
  const syncIcon = computed(() => !chronyRunning.value ? '✕' : synced.value ? '✓' : '…')
  const syncName = computed(() => !chronyRunning.value ? 'STOPPED' : synced.value ? 'SYNCED' : 'NOT SYNCED')
  function fmtS(s) {
    const a = Math.abs(s)
    if (a < 1e-6) return (s * 1e9).toFixed(0) + ' ns'
    if (a < 1e-3) return (s * 1e6).toFixed(1) + ' µs'
    if (a < 1) return (s * 1e3).toFixed(2) + ' ms'
    return s.toFixed(2) + ' s'
  }
  const off = computed(() => {
    if (!tr.value) return {val: '—', unit: ''}
    const [v, u] = fmtS(tr.value.offset_s).split(' ')
    return {val: v, unit: u}
  })
  const offCls = computed(() => !tr.value ? '' : Math.abs(tr.value.offset_s) < 1e-3 ? 'good' : Math.abs(tr.value.offset_s) < 0.05 ? 'warnc' : 'badc')

  // ---- time helpers
  const utcMs = computed(() => now.value + skew.value)
  const utcNow = computed(() => new Date(utcMs.value).toISOString().slice(11, 19))
  // time zones are IANA names (Europe/Berlin), shown by city with the current UTC offset
  function zoneOffset(tz, ms) {
    try {
      const p = new Intl.DateTimeFormat('en-US', {timeZone: tz, timeZoneName: 'longOffset'}).formatToParts(ms).find(x => x.type === 'timeZoneName')
      const o = p.value.replace('GMT', '')
      return 'UTC' + (o || '+00:00').replace('-', '−')
    } catch (_) { return '' }
  }
  const cityOf = tz => tz === 'UTC' ? 'UTC' : tz.split('/').pop().replace(/_/g, ' ')
  function tzLabel(tz) { return tz ? cityOf(tz) + ' (' + zoneOffset(tz, utcMs.value) + ')' : '—' }
  const tzGroups = computed(() => {
    let ids = []
    try { ids = Intl.supportedValuesOf('timeZone') } catch (_) {}
    if (!ids.includes('UTC')) ids = ['UTC', ...ids]
    const g = {}
    for (const id of ids) {
      const region = id.includes('/') ? id.split('/')[0] : 'UTC'
      if (region === 'Etc') continue
      ;(g[region] = g[region] || []).push({id, label: cityOf(id) + ' (' + zoneOffset(id, Date.now()) + ')'})
    }
    for (const r in g) g[r].sort((a, b) => a.label.localeCompare(b.label))
    return Object.keys(g).sort((a, b) => a === 'UTC' ? -1 : b === 'UTC' ? 1 : a.localeCompare(b)).map(region => ({region, zones: g[region]}))
  })
  function localTime(tz) {
    try { return new Intl.DateTimeFormat('sv-SE', {timeZone: tz || 'UTC', dateStyle: 'short', timeStyle: 'medium'}).format(utcMs.value) }
    catch (_) { return '—' }
  }
  // the address the system really has on the VLAN interface; falls back to what is configured (not applied yet)
  function vlanAddr(v) {
    const c = shown.value
    const nic = ifaces.value.find(i => i.name === ((c.lan || {}).iface + '.' + v.id))
    if (nic && nic.addrs.length) return {live: true, text: nic.addrs.join(', ')}
    return {live: false, text: v.mode === 'static' ? v.address + ' (not active)' : 'DHCP: no lease'}
  }
  // folded unless this browser was told to keep it open; without browser storage it simply starts folded
  const collapsed = reactive({sources: true})
  try { collapsed.sources = localStorage.getItem('avantp-collapse-sources') !== '0' } catch (_) {}
  function toggleCard(name) {
    collapsed[name] = !collapsed[name]
    try { localStorage.setItem('avantp-collapse-' + name, collapsed[name] ? '1' : '0') } catch (_) {}
  }
  // ---- live timecode: the time of day in the VLAN's zone as HH:MM:SS:FF, the same arithmetic the sender uses
  const liveMs = ref(Date.now())
  let liveTimer = null
  const zoneFmt = {}
  const pad = n => String(n).padStart(2, '0')
  function dropFrame(n) {                       // 29.97 fps: frames 0 and 1 are skipped each minute except every tenth
    const d = Math.floor(n / 17982), m = n % 17982
    n += 18 * d + (m >= 2 ? 2 * Math.floor((m - 2) / 1798) : 0)
    return [Math.floor(n / 108000) % 24, Math.floor(n / 1800) % 60, Math.floor(n / 30) % 60, n % 30]
  }
  function liveTc(v) {
    const fps = (v.timecode || {}).fps || 25, tz = v.timezone || 'UTC', ms = liveMs.value
    try {
      const f = zoneFmt[tz] = zoneFmt[tz] || new Intl.DateTimeFormat('en-GB', {timeZone: tz, hourCycle: 'h23', hour: '2-digit', minute: '2-digit', second: '2-digit'})
      const part = t => +f.formatToParts(ms).find(x => x.type === t).value
      const h = part('hour'), m = part('minute'), s = part('second'), frac = (ms - Math.floor(ms / 1000) * 1000) / 1000
      if (fps === 29.97) { const t = dropFrame(Math.floor((h * 3600 + m * 60 + s + frac) * 30000 / 1001)); return `${pad(t[0])}:${pad(t[1])}:${pad(t[2])};${pad(t[3])}` }
      const rate = Math.floor(fps)
      return `${pad(h)}:${pad(m)}:${pad(s)}:${pad(Math.min(rate - 1, Math.floor(frac * rate)))}`
    } catch (_) { return '--:--:--:--' }
  }
  function startLive() { if (!liveTimer) liveTimer = setInterval(() => { liveMs.value = Date.now() + skew.value }, 100) }
  function stopLive() { clearInterval(liveTimer); liveTimer = null }
  const lastUpdate = computed(() => {
    if (!tr.value || !tr.value.ref_time) return '—'
    const t = Math.max(0, Math.round(utcMs.value / 1000 - tr.value.ref_time))
    return t < 90 ? t + ' s ago' : Math.round(t / 60) + ' min ago'
  })
  const utcDate = computed(() => new Date(utcMs.value).toISOString().slice(0, 10))
  const NOT = {state: 'off'}
  const tcStat = v => {
    const e = tc.value.vlans.find(x => x.id === v.id)
    return e || {artnet: NOT, mtc_rtp: NOT}
  }
  // what the sender really reports; with the toggle on but no report, the service is not running
  // DHCP VLANs: the address the system really got (or why there is none yet), for the VLANS tab
  function dhcpState(v) {
    const lan = (shown.value.lan || {}).iface
    const nic = ifaces.value.find(i => i.name === lan + '.' + v.id)
    if (!nic) return {text: 'not applied yet', cls: 'warnc', hint: 'This VLAN does not exist on the system yet: save and apply.'}
    if (nic.addrs.length) return {text: nic.addrs.join(', '), cls: 'good', hint: 'Address received from the DHCP server on this VLAN.'}
    return {text: nic.state === 'up' ? 'waiting for a DHCP lease…' : 'link down', cls: 'warnc',
            hint: 'The VLAN is up but no DHCP server has answered yet (is there one on this VLAN?).'}
  }
  function tcText(v) {
    const t = v.timecode || {}, r = tcStat(v)
    const a = r.artnet || NOT
    let artnet = 'off', mtc = 'off'
    if (t.artnet) {
      artnet = !tc.value.running ? 'service not running' :
        a.state === 'sending' ? 'sending ' + (t.fps || 25) + ' fps \u2192 ' + a.dest :
        a.state === 'no-address' ? 'waiting for an IP address' :
        a.state === 'error' ? 'error: ' + a.error : 'starting\u2026'
    }
    const m = tcStat(v).mtc_rtp || NOT
    if (t.mtc_rtp) {
      mtc = !tc.value.running ? 'service not running' :
        m.state === 'sending' ? 'sending ' + (m.fps || t.fps) + ' fps to ' + m.peers.length + ' receiver' + (m.peers.length === 1 ? '' : 's') + ': ' + m.peers.map(p => p.ip).join(', ') :
        m.state === 'listening' ? 'ready on ' + m.listen + ' (session "' + m.name + '"): connect a receiver to this address' :
        m.state === 'no-address' ? 'waiting for an IP address' :
        m.state === 'error' ? 'error: ' + m.error : 'starting…'
    }
    return {artnet, mtc}
  }
  function tcCls(v) {
    const t = v.timecode || {}, a = tcStat(v).artnet || NOT
    return {artnet: !t.artnet ? '' : (tc.value.running && a.state === 'sending') ? 'good' : a.state === 'error' ? 'badc' : 'warnc',
            mtc: !t.mtc_rtp ? '' : (tc.value.running && (tcStat(v).mtc_rtp || NOT).state === 'sending') ? 'good' :
                 (tcStat(v).mtc_rtp || NOT).state === 'error' ? 'badc' : (tcStat(v).mtc_rtp || NOT).state === 'listening' ? '' : 'warnc'}
  }

  // ---- config
  async function loadConfig() {
    cfg.value = await api('GET', '/api/config')
    if (!draft.value || !draftDirty.value) initDraft()   // never throw away edits the user is still typing
  }
  const shown = computed(() => cfg.value.active || cfg.value.pending || {})   // what the system runs; before the first apply, what is saved
  const cfgVlans = computed(() => shown.value.vlans || [])
  const canon = o => JSON.stringify(o, (k, v) => v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort()) : v)
  const hasPending = computed(() => !!cfg.value.pending && canon(cfg.value.pending) !== canon(cfg.value.active))
  const left = computed(() => Math.max(0, Math.ceil((ap.value.deadline || 0) - utcMs.value / 1000)))
  const recent = (a, secs = 600) => a.time && utcMs.value / 1000 - a.time < secs
  const umLeftText = computed(() => {
    const t = Math.max(0, Math.ceil((um.value.until || 0) - utcMs.value / 1000))
    return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0') + ' min'
  })
  async function setUpdateMode(on) {
    busy.value = true
    try { await api('POST', '/api/update-mode', {on}); um.value = on ? {state: 'on', until: utcMs.value / 1000 + 900} : {state: 'off'} }
    catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  // ---- software update
  const swBusy = computed(() => ['checking', 'updating'].includes(sw.value.state))
  const applyBusy = computed(() => ['applying', 'pending-confirm'].includes(ap.value.state))
  const cfgReady = computed(() => true)
  const swFresh = computed(() => sw.value.time && utcMs.value / 1000 - sw.value.time < 120)
  const swCls = computed(() => { const c = (sw.value.check || {}).state; return c === 'uptodate' ? 'good' : c === 'newer' ? 'warnc' : '' })
  const swText = computed(() => {
    const s = sw.value, c = s.check || {}
    if (s.state === 'checking') return 'Asking GitHub for the latest version…'
    if (s.state === 'updating') return 'Updating: ' + (s.step || 'working') + '…'
    if (s.state === 'done') return 'Updated from ' + (s.from_version || '?') + ' to ' + (s.installed || '?') + '. The page reloads in a moment; log in again.'
    if (s.state === 'error') return s.error || 'Something went wrong.'
    if (s.state === 'uptodate' || c.state === 'uptodate') return 'You are up to date.'
    if (c.state === 'newer') return c.ahead_by + ' newer commit' + (c.ahead_by === 1 ? '' : 's') + ' on GitHub: "' + c.message + '"'
    if (c.state === 'local-newer') return 'This box runs a version that is newer than GitHub (a local build).'
    if (c.state) return 'The installed version is not on GitHub (a local build). UPDATE NOW replaces it with the latest: "' + c.message + '"'
    return 'Not checked yet. UPDATE NOW checks first and installs only when there is something newer.'
  })
  async function swAsk(action) {
    busy.value = true
    try { await api('POST', '/api/software', {action}); sw.value = {...sw.value, state: action === 'check' ? 'checking' : 'updating', step: 'Starting', log: []} }
    catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  async function applyNow(force) {
    busy.value = true
    try { await api('POST', '/api/apply', force === true ? {force: true} : {}); ap.value = {state: 'applying'} } catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  async function confirm() {
    busy.value = true
    try { await api('POST', '/api/confirm', {}); await poll(); await loadConfig() } catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  function blank() {
    const n = physical.value.map(i => i.name)
    const wan = (physical.value.find(i => i.state === 'up' && i.addrs.length) || {}).name || n[0] || ''
    const lan = n.find(x => x !== wan) || ''
    return {wan: {iface: wan, mode: 'dhcp', commissioning: true}, lan: {iface: lan},
            upstream: {default: ['pool.ntp.org'], backup: ['time.cloudflare.com'], custom: [], local_fallback: false}, vlans: []}
  }
  function normVlan(v) {
    v.mode = v.mode || 'dhcp'; v.timezone = v.timezone || 'UTC'; delete v.utc_offset_min
    v.ntp = v.ntp !== false; v.mgmt = !!v.mgmt; v.address = v.address || ''
    v.timecode = Object.assign({artnet: false, mtc_rtp: false, fps: 25}, v.timecode || {})
    const to = v.timecode.artnet_dest || 'broadcast'                 // the server stores one value; the form edits it as mode + address
    v.timecode.dest_mode = to === 'broadcast' ? 'broadcast' : 'unicast'
    v.timecode.dest_ip = to === 'broadcast' ? '' : to
    return v
  }
  function initDraft() {
    const src = cfg.value.pending || cfg.value.active || blank()
    const d = JSON.parse(JSON.stringify(src))
    d.wan.commissioning = !!d.wan.commissioning
    d.upstream = Object.assign({default: [], backup: [], custom: [], local_fallback: false}, d.upstream)
    d.vlans = (d.vlans || []).map(normVlan)
    for (const k of ['default', 'backup', 'custom']) up[k] = d.upstream[k].join('\n')
    draft.value = d
    baseline.value = snapshot()
  }
  function payload() {
    const d = JSON.parse(JSON.stringify(draft.value))
    for (const k of ['default', 'backup', 'custom']) d.upstream[k] = up[k].split('\n').map(s => s.trim()).filter(Boolean)
    for (const v of d.vlans) { if (v.mode !== 'static') delete v.address }
    for (const v of d.vlans) {
      const t = v.timecode
      t.artnet_dest = t.dest_mode === 'unicast' ? (t.dest_ip || '').trim() : 'broadcast'
      delete t.dest_mode; delete t.dest_ip
    }
    if (d.wan.mode !== 'static') { delete d.wan.address; delete d.wan.gateway; delete d.wan.dns }
    return d
  }
  function snapshot() { return draft.value ? JSON.stringify(payload()) : '' }
  const draftDirty = computed(() => draft.value && snapshot() !== baseline.value)
  const wanDns = computed({
    get: () => (draft.value.wan.dns || []).join(', '),
    set: v => { draft.value.wan.dns = v.split(',').map(s => s.trim()).filter(Boolean) }
  })
  function addVlan() {
    const used = new Set(draft.value.vlans.map(v => v.id))
    let id = 10; while (used.has(id)) id++
    draft.value.vlans.push(normVlan({id, name: 'vlan' + id, mode: 'dhcp', mgmt: draft.value.vlans.length === 0}))
  }
  // IPv4 helpers for the one check the server cannot do for a DHCP WAN: is a unicast receiver on the WAN network?
  const ip4 = a => { const p = String(a).trim().split('.'); return p.length === 4 && p.every(x => /^\d{1,3}$/.test(x) && +x < 256) ? p.reduce((n, x) => n * 256 + +x, 0) : null }
  function inNet(ip, cidr) {
    const [base, bits] = cidr.split('/'), a = ip4(ip), b = ip4(base), m = +bits === 0 ? 0 : (~0 << (32 - +bits)) >>> 0
    return a !== null && b !== null && ((a & m) >>> 0) === ((b & m) >>> 0)
  }
  function wanConflict() {
    const wan = ifaces.value.find(i => i.name === draft.value.wan.iface)
    const nets = [...(wan ? wan.addrs : []), ...(draft.value.wan.mode === 'static' && draft.value.wan.address ? [draft.value.wan.address] : [])]
    for (const v of draft.value.vlans) {
      const t = v.timecode
      if (!t.artnet || t.dest_mode !== 'unicast') continue
      const hit = nets.find(n => inNet(t.dest_ip, n))
      if (hit) return `VLAN ${v.id}: the Art-Net receiver ${t.dest_ip.trim()} is on the WAN network ${hit.replace(/\.\d+\//, '.0/')}: the VLANs never send to the WAN side. Choose a receiver on this VLAN.`
    }
    return ''
  }
  async function save() {
    busy.value = true; msg.value = ''
    const conflict = wanConflict()
    if (conflict) { msg.value = conflict; msgCls.value = 'err'; busy.value = false; return }
    try {
      const d = await api('PUT', '/api/config', payload())
      cfg.value.pending = d.pending
      initDraft()
      msg.value = 'Saved. Press APPLY TO SYSTEM at the top to activate it.'; msgCls.value = 'ok'
    } catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  async function changePw() {
    busy.value = true; msg.value = ''
    try {
      await api('POST', '/api/password', {old: pw.old, new: pw.new})
      pw.old = pw.new = ''; msg.value = 'Password changed. Other sessions were logged out.'; msgCls.value = 'ok'
    } catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  watch(tab, t => { msg.value = ''; t === 'monitor' ? startLive() : stopLive(); t === 'log' ? startLog() : stopLog() })
  watch(() => ap.value.state, (n, o) => { if (o && n !== o && ['applied', 'rolled-back', 'error'].includes(n)) loadConfig().catch(() => {}) })

  onMounted(start)
  onUnmounted(stop)
  return {sw, swBusy, applyBusy, cfgReady, swFresh, swCls, swText, swAsk, logSel, logLines, logErr, logLoaded, logbox, boots, bootsErr, dt, logCls, loadLog, loadBoots, copyLog, restartNotice, dismissRestart, lastUpdate, dhcpState, collapsed, toggleCard, liveTc, synced, um, umLeftText, setUpdateMode, version, tabs, tab, user, needsSetup, lu, lp, loginErr, busy, connected, doLogin, doLogout, draft, up, msg, msgCls, pw,
    ap, left, recent, applyNow, confirm, cfg, shown, tr, chronyRunning, sources, ifaces, ifaceNames, syncCls, syncTxt, syncIcon, syncName, fmtS, off, offCls, utcNow,
    tzLabel, tzGroups, localTime, tcText, tcCls, tcStat, tc, utcDate, vlanAddr, cfgVlans, hasPending, draftDirty, wanDns, addVlan, save, changePw}
}}).mount('#app')
