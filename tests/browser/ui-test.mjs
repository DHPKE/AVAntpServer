// Real-browser test: drives headless Chrome over CDP (Node 22, no dependencies).
// Needs: a server started with fake chronyc/ip on PATH and AVANTP_ALLOW_HTTP=1 on 127.0.0.1:8765, user admin / "correct horse battery".
//   export AVANTP_DIR=<dir with users.json> AVANTP_CONFIRM_SECONDS=4 PATH=tests/browser/fakebin:$PATH
//   python3 -m avantp apply --fake --watch &        # development helper: records instead of touching the system
//   AVANTP_TC_DEST=127.0.0.1,127.0.0.1,16454,16504 python3 -m avantp timecode &   # the sender (Art-Net to :16454, MTC session on :16504)
//   AVANTP_ALLOW_HTTP=1 python3 -m avantp web 127.0.0.1 8765
//   node tests/browser/ui-test.mjs <screenshot-dir>
import { spawn } from 'node:child_process'
import dgram from 'node:dgram'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const S = process.argv[2], URL = 'http://127.0.0.1:8765/'
const prof = mkdtempSync(join(tmpdir(), 'chrome-'))
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new', '--remote-debugging-port=9333', `--user-data-dir=${prof}`, '--window-size=1400,1000', '--no-first-run', 'about:blank'], {stdio: 'ignore'})
const sleep = ms => new Promise(r => setTimeout(r, ms))
let targets
for (let i = 0; i < 50; i++) { try { targets = await (await fetch('http://127.0.0.1:9333/json')).json(); if (targets.find(t => t.type === 'page')) break } catch (_) {} await sleep(200) }
const ws = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl)
await new Promise(r => ws.onopen = r)
let id = 0; const pend = new Map(); const errors = []
ws.onmessage = e => {
  const m = JSON.parse(e.data)
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id) }
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text)
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push('console.error: ' + m.params.args.map(a => a.value || a.description).join(' '))
  if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') errors.push('log: ' + m.params.entry.text + ' ' + (m.params.entry.url || ''))
}
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({id: i, method, params})) })
const ev = async js => { const r = await send('Runtime.evaluate', {expression: js, awaitPromise: true, returnByValue: true}); if (r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails)); return r.result.result.value }
const shot = async name => { const r = await send('Page.captureScreenshot', {format: 'png'}); writeFileSync(`${S}/shot-${name}.png`, Buffer.from(r.result.data, 'base64')) }
const text = () => ev('document.body.innerText')
const results = []
// a minimal AppleMIDI initiator (what a Mac or rtpMIDI does): joins the box's MTC session and collects the RTP packets
const u32 = n => { const b = Buffer.alloc(4); b.writeUInt32BE(n >>> 0); return b }
const appleInv = (cmd, token, ssrc, name) => Buffer.concat([Buffer.from([0xff, 0xff]), Buffer.from(cmd), u32(2), u32(token), u32(ssrc), Buffer.from(name + '\0')])
const mtcPeer = {ok: [], rtp: [], ctrl: dgram.createSocket('udp4'), data: dgram.createSocket('udp4')}
for (const [k, tag] of [['ctrl', 'c'], ['data', 'd']]) mtcPeer[k].on('message', m => { if (m[0] === 0xff && m[2] === 0x4f && m[3] === 0x4b) mtcPeer.ok.push(tag); else if (m[0] === 0x80) mtcPeer.rtp.push(m) })
mtcPeer.ctrl.bind(0, '127.0.0.1'); mtcPeer.data.bind(0, '127.0.0.1')
const joinMtc = async () => {
  mtcPeer.ctrl.send(appleInv('IN', 31337, 0xC0FFEE, 'Node Mac'), 16504, '127.0.0.1'); await sleep(300)
  mtcPeer.data.send(appleInv('IN', 31337, 0xC0FFEE, 'Node Mac'), 16505, '127.0.0.1'); await sleep(300)
}
const artnet = []                                   // what the Art-Net sender really transmits
const udp = dgram.createSocket('udp4'); udp.on('message', m => artnet.push(m)); udp.bind(16454, '127.0.0.1')
const check = (name, ok, extra = '') => { results.push(ok); console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  -> ' + extra)) }
const setVal = (sel, v) => ev(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});e.value=${JSON.stringify(v)};e.dispatchEvent(new Event(e.tagName==='SELECT'?'change':'input'));return true})()`)
const click = sel => ev(`document.querySelector(${JSON.stringify(sel)}).click()`)
const tab = name => ev(`[...document.querySelectorAll('.tab-btn')].find(b=>b.textContent.trim()==='${name}').click()`)

await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable')
await send('Page.navigate', {url: URL}); await sleep(1200)
let t = await text()
check('login screen shown', t.includes('LOGIN') && !t.includes('MONITOR'), t.slice(0, 80))
await shot('1-login')

await setVal('#u', 'admin'); await setVal('#p', 'wrong password!!')
await click('form .btn'); await sleep(900)
t = await text(); check('wrong password rejected with message', t.includes('wrong user or password'), t.slice(0, 120))
await setVal('#p', 'correct horse battery'); await click('form .btn')
for (let i = 0; i < 20 && !(await text()).includes('ntp1.example.org'); i++) await sleep(400)   // names arrive on the second poll
t = await text(); check('login works, monitor shown', t.includes('MONITOR') && t.includes('SYNCED'), t.slice(0, 160))
check('live pill', t.includes('LIVE'))
check('tracking data shown (offset 12 us, stratum 3)', t.includes('12') && t.includes('Stratum 3'), t.slice(0, 300))
// the Upstream sources card starts folded, with a summary; unfold it to look at the servers
const srcOpen = () => ev(`[...document.querySelectorAll('.card')].some(c => c.textContent.includes('Upstream sources') && c.querySelector('table'))`)
check('upstream sources: folded by default', !(await srcOpen()) && !t.includes('time.cloudflare.com'), t.slice(t.indexOf('UPSTREAM'), t.indexOf('UPSTREAM') + 120))
check('upstream sources: the folded header already says how many servers and which one is used', /Upstream sources\s*·\s*3 servers, synced to ntp1\.example\.org/i.test(t))
await click('.lbl.fold'); await sleep(300); t = await text()
check('sources shown by host name (reverse DNS), address beside it', t.includes('ntp1.example.org') && t.includes('time.cloudflare.com') && t.includes('91.206.8.70') && t.includes('162.159.200.1'), t.slice(0, 600))
check('a source without a PTR record falls back to its address', t.includes('203.0.113.9'))
check('the reference line uses the host name', t.includes('Stratum 3 · ntp1.example.org'), t.slice(0, 300))
check('interfaces listed', t.includes('enp4s0') && t.includes('10.1.1.24/24'))
await shot('2-monitor')

await tab('NETWORK'); await sleep(400)
t = await text(); check('network tab: WAN + LAN + commissioning', t.toUpperCase().includes('WAN (INTERNET SIDE)') && t.toUpperCase().includes('LAN (CARRIES THE VLANS)') && t.toUpperCase().includes('COMMISSIONING'))
check('first-boot draft picks enp4s0 as WAN, enp3s0 as LAN', await ev(`(()=>{const s=document.querySelectorAll('select');return s[0].value==='enp4s0'&&s[1].value==='enp3s0'})()`))
check('commissioning banner visible (first boot default)', t.includes('COMMISSIONING is on'))
await ev(`document.querySelector('input[type=radio][value=static]').click()`); await sleep(200)
check('static fields appear', (await text()).toUpperCase().includes('GATEWAY'))
await ev(`document.querySelector('input[type=radio][value=dhcp]').click()`); await sleep(200)
await shot('3-network')

await tab('VLANS'); await sleep(300)
await ev(`[...document.querySelectorAll('.btn.ghost')].find(b=>b.textContent.includes('ADD VLAN')).click()`); await sleep(200)
await ev(`[...document.querySelectorAll('.btn.ghost')].find(b=>b.textContent.includes('ADD VLAN')).click()`); await sleep(200)
check('two VLAN rows', await ev(`document.querySelectorAll('tbody tr').length`) === 2)
check('zone dropdown is grouped by region and labelled by city', await ev(`(()=>{const g=[...document.querySelectorAll('optgroup')].map(o=>o.label);return g.includes('Europe')&&g.includes('America')&&g.includes('Asia')&&[...document.querySelectorAll('option')].some(o=>/^Tokyo \\(UTC\\+09:00\\)$/.test(o.textContent))})()`))
check('unsaved banner appears', (await text()).includes('Unsaved changes'))
// row 1: mgmt static, +01:00 ; row 2 DHCP with Art-Net, -05:00
await ev(`(()=>{const r=document.querySelectorAll('tbody tr');
 const set=(el,v)=>{el.value=v;el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input'))};
 const i0=r[0].querySelectorAll('input,select'); set(i0[0],10); set(i0[1],'Dante Prim (A–B)'); set(i0[2],'static'); return 1})()`)
await sleep(200)
await ev(`(()=>{const r=document.querySelectorAll('tbody tr');
 const set=(el,v)=>{el.value=v;el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input'))};
 const c=(row,n,sel='input,select')=>row.querySelector(sel.split(',').map(x=>'td:nth-child('+n+') '+x).join(','));
 set(c(r[0],4),'10.10.10.2/24'); set(c(r[0],5),'Europe/Berlin'); if(!c(r[0],7,'input').checked) c(r[0],7,'input').click();
 set(c(r[1],1),20); set(c(r[1],2),'show'); set(c(r[1],5),'America/New_York'); c(r[1],8,'input').click(); c(r[0],10,'input').click(); return 1})()`)
await sleep(300)
// Art-Net destination: broadcast, or unicast to one receiver on the VLAN
const dest = () => ev(`(() => { const r = [...document.querySelectorAll('tbody tr')]; const c = i => r[i].querySelector('td:nth-child(9)'); return {sel: !!c(1).querySelector('select'), ip: !!c(1).querySelector('input'), off: c(0).textContent.trim()} })()`)
let d9 = await dest()
check('Art-Net to: the broadcast/unicast choice appears once Art-Net TC is ticked, and not where it is off', d9.sel && !d9.ip && d9.off === '–', JSON.stringify(d9))
await ev(`(() => { const sel = document.querySelectorAll('tbody tr')[1].querySelector('td:nth-child(9) select'); sel.value = 'unicast'; sel.dispatchEvent(new Event('change')) })()`); await sleep(250)
d9 = await dest()
check('... choosing unicast shows the receiver address field', d9.ip === true, JSON.stringify(d9))
const setIp = v => ev(`(() => { const i = document.querySelectorAll('tbody tr')[1].querySelector('td:nth-child(9) input'); i.value = ${JSON.stringify(v)}; i.dispatchEvent(new Event('input')) })()`)
await setIp('not-an-ip'); await click('.actions .btn'); await sleep(800)
check('... a wrong address is refused with a clear message', (await text()).includes('not an IPv4 address'), (await text()).slice(-200))
// the receiver is on the WAN network (the fake box has WAN 10.1.1.24/24): refused in the page, before anything is saved
await setIp('10.1.1.50'); await click('.actions .btn'); await sleep(500)
t = await text()
check('unicast to an address on the WAN network is refused, with the reason', t.includes('on the WAN network 10.1.1.0/24') && t.includes('never send to the WAN'), t.slice(-260))
check('... and it was not saved', (await ev(`fetch('/api/config').then(r => r.json()).then(d => JSON.stringify(d.pending))`)).includes('10.1.1.50') === false)
await setIp('10.20.0.50')
const fit = await ev(`(() => { const r = document.querySelectorAll('tbody tr')[1]; const box = e => e.getBoundingClientRect();
  const cell = box(r.querySelector('td:nth-child(9)')), sel = box(r.querySelector('td:nth-child(9) select')), inp = box(r.querySelector('td:nth-child(9) input'));
  const next = box(r.querySelector('td:nth-child(10) input')), after = box(r.querySelector('td:nth-child(11) select'));
  const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  return {cell: [cell.left, cell.right].map(Math.round), sel: [sel.left, sel.right].map(Math.round), inp: [inp.left, inp.right].map(Math.round),
          inside: sel.right <= cell.right + 1 && inp.right <= cell.right + 1 && sel.left >= cell.left - 1, overlapsMtc: hit(inp, next) || hit(sel, next), overlapsFps: hit(inp, after) || hit(sel, after)} })()`)
check('Art-Net to: dropdown and address field stay inside their column and cover no neighbouring control', fit.inside && !fit.overlapsMtc && !fit.overlapsFps, JSON.stringify(fit))
await shot('4-vlans')
await click('.actions .btn'); await sleep(900)
t = await text(); check('names with spaces are accepted and saved', !(await text()).includes('invalid name'), (await text()).slice(-200))
check('the unicast destination was saved as that one address', await ev(`fetch('/api/config').then(r => r.json()).then(d => d.pending.vlans.find(v => v.id === 20).timecode.artnet_dest)`) === '10.20.0.50')
check('save OK message', t.includes('Saved. Press APPLY TO SYSTEM'), t.slice(-200))
check('saved-but-not-active banner with APPLY button', t.includes('not active on the system yet') && t.includes('APPLY TO SYSTEM'))

// apply -> confirm window -> confirm
await ev(`[...document.querySelectorAll('.banner .btn')].find(b=>b.textContent.includes('APPLY TO SYSTEM')).click()`)
let seen = false
for (let i = 0; i < 20 && !seen; i++) { await sleep(400); seen = (await text()).includes('Confirm within') }
check('after APPLY the confirm countdown banner appears', seen, (await text()).slice(0, 300))
await ev(`[...document.querySelectorAll('.banner .btn')].find(b=>b.textContent.includes('CONFIRM')).click()`)
seen = false
for (let i = 0; i < 20 && !seen; i++) { await sleep(400); seen = (await text()).includes('Applied and confirmed') }
check('after CONFIRM the change is shown as applied', seen, (await text()).slice(0, 300))
let gone = false
for (let i = 0; i < 20 && !gone; i++) { await sleep(300); gone = await ev(`![...document.querySelectorAll('.banner .btn')].some(b=>b.textContent.includes('APPLY'))`) }
check('apply banner gone once active == saved', gone, (await text()).slice(0, 200))

// a second change that nobody confirms must roll itself back
await tab('VLANS'); await sleep(300)
await ev(`(()=>{const e=document.querySelectorAll('tbody tr')[1].querySelectorAll('input')[1];e.value='renamed';e.dispatchEvent(new Event('input'))})()`)
await click('.actions .btn'); await sleep(900)
await ev(`[...document.querySelectorAll('.banner .btn')].find(b=>b.textContent.includes('APPLY TO SYSTEM')).click()`)
seen = false
for (let i = 0; i < 40 && !seen; i++) { await sleep(400); seen = (await text()).includes('Rolled back') }
check('unconfirmed change is rolled back and says why', seen && (await text()).includes('not confirmed within'), (await text()).slice(0, 300))

// bad config is refused by the server and shown
await ev(`(()=>{const r=document.querySelectorAll('tbody tr');const e=r[1].querySelectorAll('input')[0];e.value=10;e.dispatchEvent(new Event('input'))})()`)
await click('.actions .btn'); await sleep(800)
t = await text(); check('duplicate VLAN id refused with server message', t.includes('duplicate vlan id 10'), t.slice(-200))
await ev(`(()=>{const r=document.querySelectorAll('tbody tr');const e=r[1].querySelectorAll('input')[0];e.value=20;e.dispatchEvent(new Event('input'))})()`)

await tab('NTP'); await sleep(300)
check('NTP defaults loaded', await ev(`document.querySelectorAll('textarea')[0].value`) === 'pool.ntp.org')
await tab('MONITOR'); await sleep(2500)
t = await text()
check('VLAN clocks table shows VLANs with city time zones and current offsets', t.includes('Dante Prim (A–B)') && t.includes('show') && /Berlin \(UTC\+0[12]:00\)/.test(t) && /New York \(UTC−0[45]:00\)/.test(t), t.slice(300, 700))
check('VLAN IPs shown from the live interfaces (static + DHCP lease)', t.includes('10.10.10.2/24') && t.includes('10.20.0.77/24'), t.slice(300, 700))
// layout of the top row: Sync state | Current time | Accuracy, each a third of the width
const top = await ev(`(() => { const cs = [...document.querySelectorAll('.grid > .card')].slice(0, 3); const w = document.querySelector('.grid').clientWidth;
  return {titles: cs.map(c => c.querySelector('.lbl').textContent.trim()), x: cs.map(c => Math.round(c.getBoundingClientRect().left)), w: cs.map(c => Math.round(c.getBoundingClientRect().width)), total: w, y: cs.map(c => Math.round(c.getBoundingClientRect().top))} })()`)
check('top row order: Sync state (left), Current time (centre), Accuracy (right)', JSON.stringify(top.titles) === JSON.stringify(['Sync state', 'Current time', 'Accuracy']) && top.x[0] < top.x[1] && top.x[1] < top.x[2] && new Set(top.y).size === 1, JSON.stringify(top))
check('... each box is a third of the width', top.w.every(x => Math.abs(x - (top.total - 32 - 24) / 3) < 6) && Math.max(...top.w) - Math.min(...top.w) <= 2, JSON.stringify(top))
check('System offset and Root distance are one box: offset, RMS, skew, root delay and dispersion together', await ev(`(() => { const c = [...document.querySelectorAll('.grid > .card')][2].textContent; return ['system offset', 'RMS offset', 'Skew', 'Root delay', 'Root dispersion'].every(k => c.includes(k)) })()`) && !(await text()).includes('ROOT DISTANCE'))
// the SYNCED icon breathes once every 2 seconds, on the clock, with a smooth curve, and stays still for "reduce motion"
await ev(`window.__beats = []; new MutationObserver(ms => ms.forEach(m => { if (m.target.classList.contains('beat') && !(m.oldValue || '').split(' ').includes('beat')) window.__beats.push(Date.now() % 2000) }))
  .observe(document.querySelector('.ring'), {attributes: true, attributeFilter: ['class'], attributeOldValue: true})`)
await sleep(6300)
const beats = await ev(`window.__beats`)
const circ = (a, b, m) => { const d = Math.abs(a - b) % m; return Math.min(d, m - d) }
check('SYNCED icon: one pulse every 2 seconds (3 in 6.3 s)', beats.length >= 2 && beats.length <= 4, JSON.stringify(beats))
check('... each cycle starts on an odd second, so the glow peaks on the even second (within 60 ms)', beats.length >= 2 && beats.every(b => circ(b, 1000, 2000) < 60), JSON.stringify(beats))
check('... the animation is 2 s long, runs once per restart, with ease-in-out', await ev(`(() => { const cs = getComputedStyle(document.querySelector('.ring')); return cs.animationName === 'ringBeat' && cs.animationDuration === '2s' && cs.animationIterationCount === '1' && cs.animationTimingFunction === 'ease-in-out' })()`))
// measure the glow along the cycle on a throw-away copy: smooth bell, not a triangle
const curve = await ev(`(() => { const el = document.createElement('div'); el.className = 'ring ok beat'; el.style.cssText = 'position:fixed;left:-300px;top:0'; document.body.appendChild(el);
  const a = el.getAnimations()[0]; a.pause();
  const blur = t => { a.currentTime = t; const sh = getComputedStyle(el).boxShadow; return parseFloat(sh.slice(sh.indexOf(')') + 1).trim().split(' ')[2]) };
  const ts = [0, 250, 500, 750, 900, 1000, 1100, 1250, 1500, 1750, 2000], r = ts.map(t => [t, +blur(t).toFixed(2)]); el.remove(); return r })()`)
const at = t => curve.find(c => c[0] === t)[1]
const lo = at(0), hi = at(1000), lin = f => lo + (hi - lo) * f
check('pulse shape: dark at the start and at the end, brightest in the middle', Math.abs(at(2000) - lo) < 0.5 && hi > lo + 20 && curve.every(c => c[1] <= hi + 0.01), JSON.stringify(curve))
check('pulse shape: symmetric (rise and fall mirror each other within 1 px)', [250, 500, 750].every(t => Math.abs(at(t) - at(2000 - t)) < 1.0), JSON.stringify(curve))
check('pulse shape: eased, not linear (a quarter of the way up it has risen far less than a quarter)', at(250) < lin(0.25 / 1) - 0 && at(250) - lo < 0.18 * (hi - lo) && at(500) > lin(0.4) && at(500) < lin(0.6), JSON.stringify(curve))
check('pulse shape: round top, no corner (the last 100 ms before the peak change by under 3% of the swing)', hi - at(900) < 0.03 * (hi - lo), JSON.stringify(curve))
await send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]}); await sleep(300)
await ev(`window.__beats = []`); await sleep(4300)
check('... and does not pulse when the user asked for reduced motion', (await ev(`window.__beats`)).length === 0 && (await ev(`getComputedStyle(document.querySelector('.ring')).animationName`)) === 'none')
await send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'no-preference'}]}); await sleep(2600)
check('... and pulses again when that is switched off', (await ev(`window.__beats`)).length >= 1)
check('current time card agrees with the sync state card (synchronised, not "not synchronised")', /Clock\s+synchronised/.test(t) && !/not synchronised/.test(t), t.slice(0, 300))
check('current time card shows a UTC clock', /Current time[\s\S]*\d\d:\d\d:\d\d[\s\S]*UTC/i.test(t) && /\d{4}-\d\d-\d\d/.test(t), t.slice(0, 200))
let tcSeen = false
for (let i = 0; i < 25 && !tcSeen; i++) { await sleep(400); tcSeen = (await text()).includes('sending 25 fps → 127.0.0.1:16454') }
t = await text()
check('timecode card: VLAN 20 is reported as really sending (from the sender service)', tcSeen, t.slice(t.indexOf('TIMECODE'), t.indexOf('TIMECODE') + 400))
check('timecode card: VLANs with the toggle off say off, MTC says off', /Dante Prim[^\n]*off/.test(t) || t.includes('\toff'), '')
let mtcSeen = false
for (let i = 0; i < 25 && !mtcSeen; i++) { await sleep(400); mtcSeen = (await text()).includes('ready on 127.0.0.1:16504') }
t = await text()
check('MTC over RTP-MIDI: the sender listens and the card says where to connect a receiver', mtcSeen && t.includes('session "AVAntp Dante Prim (A\u2013B)"') && t.includes('connect a receiver to this address'), t.slice(t.indexOf('TIMECODE'), t.indexOf('TIMECODE') + 700))
check('... no longer says "not implemented"', !t.includes('not implemented'))
await joinMtc()
check('... a receiver completes the session handshake (control OK, data OK)', JSON.stringify(mtcPeer.ok) === '["c","d"]', JSON.stringify(mtcPeer.ok))
let joined = false
for (let i = 0; i < 25 && !joined; i++) { await sleep(300); joined = (await text()).includes('sending 25 fps to 1 receiver: 127.0.0.1') }
check('... and the card shows MTC sending to that receiver', joined, (await text()).slice(0, 100))
await sleep(500)
const qf = mtcPeer.rtp.filter(m => m[12] === 2 && m[13] === 0xf1)
check('... the receiver gets MIDI Timecode quarter frames (about 100 per second) after a full-frame message', mtcPeer.rtp.length > 40 && qf.length > 30 && mtcPeer.rtp[0][12] === 10 && mtcPeer.rtp[0][13] === 0xf0, `${mtcPeer.rtp.length} packets, ${qf.length} quarter frames`)
check('the sender really transmits ArtTimeCode packets', artnet.length > 5 && artnet[0].subarray(0, 8).toString('latin1') === 'Art-Net\0' && artnet[0][9] === 0x97 && artnet[0].length === 19, `packets: ${artnet.length}`)
const tcs = async () => ev(`[...document.querySelectorAll('.tcnow')].map(e => e.textContent.trim())`)
const tc1 = await tcs(); await sleep(450); const tc2 = await tcs()
check('live timecode is shown for every VLAN as HH:MM:SS:FF, also where the sender is off', tc1.length === 2 && tc1.every(x => /^\d\d:\d\d:\d\d:\d\d$/.test(x)), JSON.stringify(tc1))
check('... and it is running (changes within half a second)', tc1[0] !== tc2[0] && tc1[1] !== tc2[1], JSON.stringify([tc1, tc2]))
const hourIn = tz => +new Intl.DateTimeFormat('en-GB', {timeZone: tz, hourCycle: 'h23', hour: '2-digit'}).format(new Date())
const h10 = +tc2[0].slice(0, 2), h20 = +tc2[1].slice(0, 2)
check("... each VLAN in its own zone (Berlin, New York)", [hourIn('Europe/Berlin'), (hourIn('Europe/Berlin') + 23) % 24].includes(h10) && [hourIn('America/New_York'), (hourIn('America/New_York') + 23) % 24].includes(h20), JSON.stringify([tc2, hourIn('Europe/Berlin'), hourIn('America/New_York')]))
check('VLAN 10 (sender off) is labelled not sending, VLAN 20 (sending) is not', await ev(`(() => { const r = [...document.querySelectorAll('.card')].find(c => c.textContent.includes('Timecode now')).querySelectorAll('tbody tr'); return r[0].textContent.includes('not sending') && !r[1].textContent.includes('not sending') })()`))
check('timecode card shows the last sent timecode', /\d\d:\d\d:\d\d:\d\d/.test(t))
await shot('5-monitor-vlans')

await tab('SYSTEM'); await sleep(300)
await ev(`(()=>{const i=document.querySelectorAll('input[type=password]');const set=(e,v)=>{e.value=v;e.dispatchEvent(new Event('input'))};set(i[0],'correct horse battery');set(i[1],'short')})()`)
await click('form .btn'); await sleep(700)
check('short new password rejected', (await text()).includes('at least 10 characters'))

// reload keeps session and shows saved config
await send('Page.reload'); await sleep(2500)
t = await text(); check('reload: still logged in', t.includes('MONITOR'))
check('reload: saved VLANs persisted', t.includes('show') && t.includes('Dante Prim'))

// VLANS tab: DHCP rows show the address the system really received
await tab('VLANS'); await sleep(500)
t = await text()
check('VLANS tab: the DHCP VLAN shows its received address (10.20.0.77/24)', t.includes('10.20.0.77/24'), t.slice(0, 300))
check('VLANS tab: the static VLAN still has its input field', await ev(`[...document.querySelectorAll('tbody tr')][0].querySelector('input[placeholder="10.20.0.2/24"]') !== null`))
check('VLANS tab: the received address is not an editable field', await ev(`![...document.querySelectorAll('tbody tr')][1].querySelector('input[placeholder="10.20.0.2/24"]')`))
await ev(`[...document.querySelectorAll('.btn.ghost')].find(b=>b.textContent.includes('ADD VLAN')).click()`); await sleep(300)
check('VLANS tab: a new, unsaved DHCP row says it is not applied yet', (await text()).includes('not applied yet'))
await tab('MONITOR'); await sleep(400)

// LOG tab: the journal in the browser, and a warning after an unclean restart
t = await text()
check('unclean restart is announced after login (the previous boot ended without a shutdown)', t.includes('The box restarted unexpectedly') && /previous boot ended at/.test(t), t.slice(0, 200))
await tab('LOG'); await sleep(900)
t = await text()
check('LOG tab shows the AVAntp services log', t.includes('timecode sender started') && t.includes('Art-Net sending 25 fps'), t.slice(0, 300))
check('errors are red and warnings yellow', await ev(`(() => { const l = [...document.querySelectorAll('.logbox .ln')]; const c = k => l.filter(x => x.textContent.includes(k)); return c('unexpected error')[0].classList.contains('bad') && c('Traceback')[0].classList.contains('bad') && c('rolling back')[0].classList.contains('warn') && !c('sender started')[0].classList.contains('bad') })()`))
await setVal('.logbar select', 'kernel'); await sleep(700)
t = await text()
check('source "kernel" shows NIC link messages (SmartSpeed downgrade is flagged)', t.includes('Link Speed was downgraded by SmartSpeed') && await ev(`[...document.querySelectorAll('.logbox .ln')].some(x => x.textContent.includes('SmartSpeed') && x.classList.contains('warn'))`))
await setVal('.logbar select', 'all'); await sleep(300)
await ev(`(() => { const s = document.querySelectorAll('.logbar select')[1]; s.value = 'error'; s.dispatchEvent(new Event('change')) })()`); await sleep(700)
check('level "errors only" asks for errors and shows them', (await text()).includes('unexpected error') && !(await text()).includes('sender started'))
t = await text()
check('boot history says the previous boot ended abruptly, this one is running', t.includes('ended abruptly') && t.includes('no shutdown was logged') && /this boot[\s\S]*running/i.test(t), t.slice(t.indexOf('BOOT HISTORY'), t.indexOf('BOOT HISTORY') + 400))
await click('.logbar .btn.ghost'); await sleep(500)
check('LOG tab keeps running without errors (auto-refresh)', (await ev(`document.querySelectorAll('.logbox .ln').length`)) > 0)
await ev(`[...document.querySelectorAll('.banner .btn')].find(b => b.textContent.includes('DISMISS')).click()`); await sleep(300)
check('the restart notice can be dismissed (and stays dismissed)', !(await text()).includes('The box restarted unexpectedly'))
await tab('MONITOR'); await sleep(400)

// the upstream sources card folds, says what it hides, and remembers that across a reload
const srcShown = () => ev(`[...document.querySelectorAll('.card')].some(c => c.textContent.includes('Upstream sources') && c.querySelector('table'))`)
check('upstream sources: still unfolded, as left earlier in this browser', await srcShown())
await click('.lbl.fold'); await sleep(300)
t = await text()
check('upstream sources: click folds the table away', !(await srcShown()) && !t.includes('time.cloudflare.com'))
check('upstream sources: folded header still summarises it', /Upstream sources\s*·\s*3 servers, synced to ntp1\.example\.org/i.test(t), t.slice(t.indexOf('UPSTREAM'), t.indexOf('UPSTREAM') + 150))
await send('Page.reload'); await sleep(2500)
check('upstream sources: stays folded after a reload (remembered in this browser)', !(await srcShown()) && (await text()).includes('MONITOR'))
await click('.lbl.fold'); await sleep(300)
check('upstream sources: click unfolds it again', await srcShown() && (await text()).includes('time.cloudflare.com'))

await click('.sub .btn'); await sleep(800)
t = await text(); check('logout returns to login', t.includes('LOGIN') && !t.includes('MONITOR'))
const real = errors.filter(e => !/status of 40[0-9]/.test(e)); check('no JS errors (expected 400/401 from deliberate bad requests ignored)', real.length === 0, real.join(' | '))
console.log(results.every(Boolean) ? 'ALL PASS' : 'SOME FAILED', `(${results.filter(Boolean).length}/${results.length})`)
udp.close(); mtcPeer.ctrl.close(); mtcPeer.data.close(); ws.close(); chrome.kill()
process.exit(results.every(Boolean) ? 0 : 1)
