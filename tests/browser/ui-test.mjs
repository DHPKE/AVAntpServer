// Real-browser test: drives headless Chrome over CDP (Node 22, no dependencies).
// Needs: a server started with fake chronyc/ip on PATH and AVANTP_ALLOW_HTTP=1 on 127.0.0.1:8765, user admin / "correct horse battery".
//   AVANTP_DIR=<dir with users.json> PATH=tests/browser/fakebin:$PATH AVANTP_ALLOW_HTTP=1 python3 -m avantp web 127.0.0.1 8765
//   node tests/browser/ui-test.mjs <screenshot-dir>
import { spawn } from 'node:child_process'
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
await setVal('#p', 'correct horse battery'); await click('form .btn'); await sleep(2500)
t = await text(); check('login works, monitor shown', t.includes('MONITOR') && t.includes('SYNCED'), t.slice(0, 160))
check('live pill', t.includes('LIVE'))
check('tracking data shown (offset 12 ns, stratum 3)', t.includes('12') && t.includes('Stratum 3'), t.slice(0, 300))
check('sources listed', t.includes('pool.ntp.org') && t.includes('time.cloudflare.com'))
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
 const i0=r[0].querySelectorAll('input,select'); set(i0[0],10); set(i0[1],'mgmt'); set(i0[2],'static'); return 1})()`)
await sleep(200)
await ev(`(()=>{const r=document.querySelectorAll('tbody tr');
 const set=(el,v)=>{el.value=v;el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input'))};
 const i0=r[0].querySelectorAll('input,select'); set(i0[3],'10.10.10.2/24'); set(i0[4],'Europe/Berlin'); if(!i0[6].checked) i0[6].click();
 const i1=r[1].querySelectorAll('input,select'); set(i1[0],20); set(i1[1],'show'); set(i1[4],'America/New_York'); i1[7].click(); return 1})()`)
await sleep(300)
await shot('4-vlans')
await click('.actions .btn'); await sleep(900)
t = await text(); check('save OK message', t.includes('Saved. Not applied'), t.slice(-200))
check('saved-not-applied banner', t.includes('not applied'))

// bad config is refused by the server and shown
await ev(`(()=>{const r=document.querySelectorAll('tbody tr');const e=r[1].querySelectorAll('input')[0];e.value=10;e.dispatchEvent(new Event('input'))})()`)
await click('.actions .btn'); await sleep(800)
t = await text(); check('duplicate VLAN id refused with server message', t.includes('duplicate vlan id 10'), t.slice(-200))
await ev(`(()=>{const r=document.querySelectorAll('tbody tr');const e=r[1].querySelectorAll('input')[0];e.value=20;e.dispatchEvent(new Event('input'))})()`)

await tab('NTP'); await sleep(300)
check('NTP defaults loaded', await ev(`document.querySelectorAll('textarea')[0].value`) === 'pool.ntp.org')
await tab('MONITOR'); await sleep(2500)
t = await text()
check('VLAN clocks table shows VLANs with city time zones and current offsets', t.includes('mgmt') && t.includes('show') && /Berlin \(UTC\+0[12]:00\)/.test(t) && /New York \(UTC−0[45]:00\)/.test(t), t.slice(300, 700))
check('VLAN IPs shown from the live interfaces (static + DHCP lease)', t.includes('10.10.10.2/24') && t.includes('10.20.0.77/24'), t.slice(300, 700))
check('timecode column: Art-Net on show', t.includes('Art-Net @25'))
await shot('5-monitor-vlans')

await tab('SYSTEM'); await sleep(300)
await ev(`(()=>{const i=document.querySelectorAll('input[type=password]');const set=(e,v)=>{e.value=v;e.dispatchEvent(new Event('input'))};set(i[0],'correct horse battery');set(i[1],'short')})()`)
await click('form .btn'); await sleep(700)
check('short new password rejected', (await text()).includes('at least 10 characters'))

// reload keeps session and shows saved config
await send('Page.reload'); await sleep(2500)
t = await text(); check('reload: still logged in', t.includes('MONITOR'))
check('reload: saved VLANs persisted', t.includes('show') && t.includes('mgmt'))

await click('.sub .btn'); await sleep(800)
t = await text(); check('logout returns to login', t.includes('LOGIN') && !t.includes('MONITOR'))
const real = errors.filter(e => !/status of 40[0-9]/.test(e)); check('no JS errors (expected 400/401 from deliberate bad requests ignored)', real.length === 0, real.join(' | '))
console.log(results.every(Boolean) ? 'ALL PASS' : 'SOME FAILED', `(${results.filter(Boolean).length}/${results.length})`)
ws.close(); chrome.kill()
process.exit(results.every(Boolean) ? 0 : 1)
