const {createApp, ref, reactive, computed, watch, onMounted, onUnmounted} = Vue

createApp({setup() {
  const tabs = ['monitor', 'network', 'vlans', 'ntp', 'system']
  const tab = ref('monitor')
  const user = ref(null), csrf = ref(''), needsSetup = ref(false)
  const lu = ref(''), lp = ref(''), loginErr = ref(''), busy = ref(false)
  const connected = ref(false), status = ref(null), skew = ref(0), now = ref(Date.now())
  const cfg = ref({active: null, pending: null})
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
    user.value = s.user; csrf.value = s.csrf || ''; needsSetup.value = !!s.needs_setup
    if (user.value) await afterLogin()
  }
  async function afterLogin() {
    await poll()
    await loadConfig()
    timers = [setInterval(poll, 2000), setInterval(() => now.value = Date.now(), 1000)]
  }
  function stop() { timers.forEach(clearInterval); timers = [] }
  async function doLogin() {
    busy.value = true; loginErr.value = ''
    try {
      const d = await api('POST', '/api/login', {user: lu.value, password: lp.value})
      user.value = d.user; csrf.value = d.csrf; lp.value = ''
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
    } catch (_) { connected.value = false }
  }
  const tr = computed(() => status.value && status.value.chrony.tracking)
  const chronyRunning = computed(() => !!(status.value && status.value.chrony.running))
  const sources = computed(() => status.value ? status.value.chrony.sources : [])
  const ifaces = computed(() => status.value ? status.value.interfaces : [])
  const ifaceNames = computed(() => {
    const n = ifaces.value.map(i => i.name)
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
  function tcText(v) {
    const t = v.timecode || {}, o = []
    if (t.artnet) o.push('Art-Net'); if (t.mtc_rtp) o.push('MTC-RTP')
    return o.length ? o.join(' + ') + ' @' + (t.fps || 25) : 'off'
  }

  // ---- config
  async function loadConfig() {
    cfg.value = await api('GET', '/api/config')
    initDraft()
  }
  const cfgVlans = computed(() => ((cfg.value.pending || cfg.value.active || {}).vlans) || [])
  const hasPending = computed(() => !!cfg.value.pending && JSON.stringify(cfg.value.pending) !== JSON.stringify(cfg.value.active))
  function blank() {
    const n = ifaces.value.map(i => i.name)
    const wan = (ifaces.value.find(i => i.state === 'up' && i.addrs.length) || {}).name || n[0] || ''
    const lan = n.find(x => x !== wan) || ''
    return {wan: {iface: wan, mode: 'dhcp', commissioning: true}, lan: {iface: lan},
            upstream: {default: ['pool.ntp.org'], backup: ['time.cloudflare.com'], custom: [], local_fallback: false}, vlans: []}
  }
  function normVlan(v) {
    v.mode = v.mode || 'dhcp'; v.timezone = v.timezone || 'UTC'; delete v.utc_offset_min
    v.ntp = v.ntp !== false; v.mgmt = !!v.mgmt; v.address = v.address || ''
    v.timecode = Object.assign({artnet: false, mtc_rtp: false, fps: 25}, v.timecode || {})
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
  async function save() {
    busy.value = true; msg.value = ''
    try {
      const d = await api('PUT', '/api/config', payload())
      cfg.value.pending = d.pending
      initDraft()
      msg.value = 'Saved. Not applied to the system yet.'; msgCls.value = 'ok'
    } catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  async function changePw() {
    busy.value = true; msg.value = ''
    try {
      await api('POST', '/api/password', {old: pw.old, new: pw.new})
      pw.old = pw.new = ''; msg.value = 'Password changed. Other sessions were logged out.'; msgCls.value = 'ok'
    } catch (e) { msg.value = e.message; msgCls.value = 'err' } finally { busy.value = false }
  }
  watch(tab, () => { msg.value = '' })

  onMounted(start)
  onUnmounted(stop)
  return {tabs, tab, user, needsSetup, lu, lp, loginErr, busy, connected, doLogin, doLogout, draft, up, msg, msgCls, pw,
    tr, chronyRunning, sources, ifaces, ifaceNames, syncCls, syncTxt, syncIcon, syncName, fmtS, off, offCls, utcNow,
    tzLabel, tzGroups, localTime, tcText, cfgVlans, hasPending, draftDirty, wanDns, addVlan, save, changePw}
}}).mount('#app')
