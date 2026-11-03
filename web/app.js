const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
const ls = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d } catch (e) { return d } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)) } catch (e) { } },
};
const CHUNK = 5 * 1024 * 1024;
const ZONAS = [['ent', 'Entrada'], ['main', 'Piso 3 · Arena'], ['cow', 'Piso 6 · Coworking'], ['vip', 'Piso 11 · VIP'], ['red', 'Sótano · Networking'], ['labs', 'Sótano · Policy Labs'], ['prensa', 'Sótano · Prensa'], ['stand', 'Stands']];
const zName = k => (ZONAS.find(z => z[0] == k) || [, k])[1];
const box = (x, y, w, h, c) => `<rect x=${x} y=${y} width=${w} height=${h} rx=2 fill='${c}'/>`;
const V = '#3ba9f0', I = '#6d747c', T = '#fff';
const svg = inner => `<svg viewBox='0 0 36 64'><rect width=36 height=64 rx=4 fill='#26292e'/>${inner}</svg>`;
const FORMATOS = [
  { id: 'auto', n: 'Que Claude elija', ok: () => true, s: svg(`<text x=18 y=40 text-anchor=middle font-size=24 font-weight=800 fill='${V}'>?</text>`) },
  { id: 'video_texto', n: 'Video + texto', ok: f => f.v == 1 && f.i == 0, s: svg(box(0, 0, 36, 64, V) + box(6, 44, 24, 9, T)) },
  { id: 'imagen_texto', n: 'Imagen + texto', ok: f => f.i == 1 && f.v == 0, s: svg(box(0, 0, 36, 64, I) + box(6, 44, 24, 9, T)) },
  { id: 'tres_videos', n: 'Tres videos', ok: f => f.v == 3 && f.i == 0, s: svg(box(0, 0, 36, 20.5, V) + box(0, 21.7, 36, 20.5, V) + box(0, 43.5, 36, 20.5, V)) },
  { id: 'img_vid_img', n: 'Imagen, video, imagen', ok: f => f.v == 1 && f.i == 2, s: svg(box(0, 0, 36, 20.5, I) + box(0, 21.7, 36, 20.5, V) + box(0, 43.5, 36, 20.5, I)) },
  { id: 'cortes', n: 'Cortes rápidos', ok: f => f.v + f.i >= 2, s: svg(box(2, 4, 10, 56, V) + box(13, 4, 10, 56, I) + box(24, 4, 10, 56, V)) },
  { id: 'frase', n: 'Frase de speaker', ok: f => f.v == 1 && f.i == 0, s: svg(box(0, 0, 36, 64, V) + box(5, 36, 26, 3, T) + box(8, 42, 20, 3, T)) },
  { id: 'nota', n: 'Nota de bienvenida', ok: f => f.i == 1 && f.v == 0, s: svg(box(0, 0, 36, 64, I) + `<rect x=7 y=20 width=22 height=22 fill='#fff' transform='rotate(-5 18 31)'/><circle cx=18 cy=20 r=2.4 fill='${V}'/>`) },
  { id: 'idea', n: 'Como me lo imagino', ok: () => true, s: svg(box(6, 20, 24, 3, T) + box(6, 28, 24, 3, T) + box(6, 36, 15, 3, T)) },
];
const ESTADOS = { subiendo: 'Subiendo', en_cola: 'En cola', editando: 'Claude está editando', corrigiendo: 'Claude está corrigiendo', lista: 'Lista', publicada: 'Publicada', error: 'Problema' };

let base = '', token = ls.get('h_token', ''), tab = 'nueva', state = { jobs: [] }, agenda = [], online = false;
let picked = [], zona = ls.get('h_zona', ''), formato = 'auto', sending = false, draft = { desc: '', idea: '' };
const qT = new URLSearchParams(location.search).get('t');

function toast(t) { const e = $('toast'); e.textContent = t; e.classList.add('show'); clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove('show'), 2800) }
function setConn(on, label) { online = on; $('conn').innerHTML = `<i class='dot ${on ? 'on' : 'off'}'></i>${esc(label)}` }

async function findPC() {
  const r = await fetch('tunnel.json?' + Date.now(), { cache: 'no-store' });
  base = (await r.json()).url;
}
async function api(path, opts = {}) {
  const r = await fetch(base + path, { ...opts, headers: { ...(opts.headers || {}), 'x-token': token } });
  if (r.status == 401) { token = ''; ls.set('h_token', ''); show(); throw new Error('pin') }
  return r;
}
const media = (id, name, dl) => `${base}/media/${id}/${name}?k=${token}${dl ? '&dl=1' : ''}`;

async function refresh() {
  try {
    if (!base) await findPC();
    const r = await api('/api/state');
    if (!r.ok) throw new Error(r.status);
    const prev = JSON.stringify(state.jobs.map(j => [j.id, j.status, j.versions.length]));
    state = await r.json();
    setConn(true, 'PC conectado');
    const cur = JSON.stringify(state.jobs.map(j => [j.id, j.status, j.versions.length]));
    if (cur != prev) { const was = JSON.parse(prev); const ready = state.jobs.filter(j => j.status == 'lista' && !was.some(w => w[0] == j.id && w[1] == 'lista' && w[2] == j.versions.length)); if (ready.length && was.length) { toast('Tu historia está lista'); if (navigator.vibrate) navigator.vibrate([60, 40, 60]) } rLista() }
    rNav(); rFalta();
  } catch (e) {
    if (e.message == 'pin') return;
    setConn(false, 'PC sin conexión. Reintentando…');
    base = '';
  }
}

function show() {
  const logged = !!token;
  $('login').classList.toggle('hide', logged);
  $('nav').classList.toggle('hide', !logged);
  ['nueva', 'lista', 'falta'].forEach(s => $(s).classList.toggle('hide', !logged || s != tab));
  if (logged) { rNav(); if (tab == 'falta') rFalta() }
}
function rNav() {
  const n = state.jobs.filter(j => j.status == 'lista').length;
  $('nav').innerHTML = [['nueva', 'Nueva'], ['lista', 'Historias'], ['falta', 'Pendiente']].map(t => `<button data-tab=${t[0]} class='${tab == t[0] ? 'on' : ''}'>${t[1]}${t[0] == 'lista' && n ? `<span class=badge>${n}</span>` : ''}</button>`).join('');
}

const counts = () => ({ v: picked.filter(p => p.file.type.startsWith('video') || /\.(mp4|mov|m4v|3gp)$/i.test(p.file.name)).length, i: picked.filter(p => !(p.file.type.startsWith('video') || /\.(mp4|mov|m4v|3gp)$/i.test(p.file.name))).length });
function rNueva() {
  if (sending) return;
  const c = counts(), opts = FORMATOS.filter(f => f.ok(c));
  if (!opts.some(f => f.id == formato)) formato = 'auto';
  $('nueva').innerHTML = `<h1>Nueva historia</h1>
  <label class=drop>${picked.length ? '+ Agregar más' : 'Elegir fotos o videos'}<input type=file id=files accept='image/*,video/*' multiple></label>
  <div class=thumbs>${picked.map((p, i) => `<div class=thumb>${p.video ? `<video src='${p.url}#t=0.1' muted playsinline preload=metadata></video>` : `<img src='${p.url}' alt=''>`}<b>${i + 1}</b><button data-rm=${i} aria-label='Quitar'>×</button></div>`).join('')}</div>
  <h2>¿Dónde estás?</h2><div class=chips>${ZONAS.map(z => `<button class='chip ${zona == z[0] ? 'on' : ''}' data-z=${z[0]}>${z[1]}</button>`).join('')}</div>
  <h2>¿Qué está pasando?</h2><textarea id=desc rows=3 placeholder='Cuéntame como si me lo dijeras. Puedes dictarlo con el micrófono del teclado.'>${esc(draft.desc)}</textarea>
  <h2>Formato</h2><div class=fmts>${opts.map(f => `<button class='fmt ${formato == f.id ? 'on' : ''}' data-f=${f.id}>${f.s}${f.n}</button>`).join('')}</div>
  <div class='${formato == 'idea' ? '' : 'hide'}'><h2>¿Cómo te la imaginas?</h2><textarea id=idea rows=3 placeholder='Ej.: empieza con la Arena llena y termina en primer plano del ministro.'>${esc(draft.idea)}</textarea></div>
  <p></p><button class=btn id=send ${picked.length ? '' : 'disabled'}>Enviar a Claude</button><p class='xs mut'>Deja esta pantalla abierta mientras sube.</p>`;
}

async function sendJob() {
  if (!online) return toast('El PC no está conectado todavía');
  sending = true;
  const items = picked.slice(), total = items.reduce((a, p) => a + p.file.size, 0);
  $('nueva').innerHTML = `<h1>Subiendo…</h1><div class=card><div class=bar><i id=up></i></div><p class=sm id=upT>Preparando</p><p class='xs mut'>Si se cae la conexión sigue solo desde donde iba. No cierres esta pantalla.</p></div>`;
  let lock; try { lock = await navigator.wakeLock?.request('screen') } catch (e) { }
  try {
    const r = await retry(() => api('/api/jobs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ zona: zName(zona), desc: draft.desc, idea: draft.idea, formato, files: items.map(p => ({ name: p.file.name, size: p.file.size })) }) }));
    const { id } = await r.json();
    let sent = 0;
    for (let i = 0; i < items.length; i++) {
      const f = items[i].file;
      const off = await (await retry(() => api(`/api/jobs/${id}/files/${i}`))).json();
      let pos = off.offset; sent += pos;
      while (pos < f.size) {
        const part = f.slice(pos, pos + CHUNK);
        const res = await retry(() => api(`/api/jobs/${id}/files/${i}?offset=${pos}`, { method: 'PUT', body: part }), [409]);
        const j = await res.json();
        sent += j.offset - pos; pos = j.offset;
        $('up').style.width = (sent / total * 100) + '%';
        $('upT').textContent = `Archivo ${i + 1} de ${items.length} · ${(sent / 1e6).toFixed(0)} de ${(total / 1e6).toFixed(0)} MB`;
      }
    }
    await retry(() => api(`/api/jobs/${id}/submit`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }));
    items.forEach(p => URL.revokeObjectURL(p.url));
    picked = []; formato = 'auto'; draft = { desc: '', idea: '' };
    sending = false; toast('Enviada. Ya la tengo'); tab = 'lista'; await refresh(); rLista(); rNueva(); show();
  } catch (e) {
    sending = false; rNueva(); toast('No se pudo enviar: ' + e.message);
  }
  try { lock?.release() } catch (e) { }
}
async function retry(fn, okCodes = []) {
  for (let wait = 1000; ; wait = Math.min(wait * 1.6, 15000)) {
    try {
      const r = await fn();
      if (r.ok || okCodes.includes(r.status)) return r;
      if (r.status >= 400 && r.status < 500) { const b = await r.json().catch(() => ({})); throw new Error(b.error || r.status) }
    } catch (e) {
      if (e instanceof TypeError == false) throw e;
    }
    setConn(false, 'Sin conexión. Reintentando…');
    await new Promise(res => setTimeout(res, wait));
    try { await findPC() } catch (e) { }
    setConn(true, 'PC conectado');
  }
}

function jobCard(j) {
  const v = j.versions[j.versions.length - 1], busy = ['en_cola', 'editando', 'corrigiendo', 'subiendo'].includes(j.status);
  const t = new Date(j.created * 1000).toLocaleTimeString('es-CO', { hour: 'numeric', minute: '2-digit', timeZone: 'America/Bogota' });
  return `<div class=card data-job=${j.id}><span class='st ${esc(j.status)} ${busy ? 'work' : ''}'>${ESTADOS[j.status] || esc(j.status)}</span> <span class='xs mut'>${t} · ${esc(j.zona)}</span>
  <p class=sm>${esc(j.desc) || '<span class=mut>Sin descripción</span>'}</p>
  ${j.error ? `<p class=sm style='color:var(--red)'>${esc(j.error)}</p>` : ''}
  ${v ? `<video class=pv controls playsinline preload=none poster='${media(j.id, `v${v.n}.jpg`)}' src='${media(j.id, `v${v.n}_preview.mp4`)}'></video>${v.note ? `<p class=sm>${esc(v.note)}</p>` : ''}
  <div class=row><button class=btn data-share=${j.id}_${v.n}>Compartir a Instagram</button><a class='btn alt' style='display:grid;place-items:center;text-decoration:none;box-sizing:border-box' href='${media(j.id, `v${v.n}.mp4`, 1)}'>Descargar</a></div>
  <details style='margin-top:9px'><summary class=sm style='min-height:40px;line-height:40px;cursor:pointer'>Algo no me cuadra</summary><textarea id=fb_${j.id} rows=2 placeholder='Dime qué cambio: texto, recorte, formato…'></textarea><p></p><button class='btn alt' data-fb=${j.id}>Enviar corrección</button></details>
  ${j.status == 'lista' ? `<p></p><button class='btn alt' data-pub=${j.id}>Ya la publiqué</button>` : ''}` : ''}</div>`;
}
function rLista() {
  const open = document.activeElement?.id?.startsWith('fb_');
  if (open || document.querySelector('#lista video:not([paused])')?.currentTime > 0) return;
  $('lista').innerHTML = `<h1>Historias</h1>` + (state.jobs.length ? state.jobs.map(jobCard).join('') : `<p class=mut>Todavía no hay historias.</p>`);
}

async function shareVideo(id, n, b) {
  const url = media(id, `v${n}.mp4`);
  b.disabled = true; b.textContent = 'Preparando…';
  try {
    const blob = await (await fetch(url)).blob();
    const file = new File([blob], `govtech-${id}-v${n}.mp4`, { type: 'video/mp4' });
    if (navigator.canShare?.({ files: [file] })) await navigator.share({ files: [file] });
    else location.href = media(id, `v${n}.mp4`, 1);
  } catch (e) { if (e.name != 'AbortError') toast('Usa Descargar: no se pudo compartir') }
  b.disabled = false; b.textContent = 'Compartir a Instagram';
}

function nowBog() {
  if (qT) return { y: qT.slice(0, 10), m: +qT.slice(11, 13) * 60 + +qT.slice(14, 16) };
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Bogota', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date()).map(x => [x.type, x.value]));
  return { y: `${p.year}-${p.month}-${p.day}`, m: +p.hour * 60 + +p.minute };
}
const mins = s => +s.slice(0, 2) * 60 + +s.slice(3);
const h12 = s => { const h = +s.slice(0, 2); return `${h > 12 ? h - 12 : h}:${s.slice(3)}` };
function rFalta() {
  const n = nowBog(), d = { '2026-11-03': 1, '2026-11-04': 2 }[n.y];
  if (!d) { $('falta').innerHTML = `<h1>Pendiente</h1><p class=mut>${n.y < '2026-11-03' ? 'Aquí verás lo que te falta grabar desde el martes 3 a las 7:15 a. m.' : 'El Summit terminó.'}</p>`; return }
  const ts = n.y + 'T00:00:00-05:00', from = Date.parse(ts) / 1000;
  const today = state.jobs.filter(j => j.created >= from), seen = new Set(today.map(j => j.zona));
  const cur = agenda.filter(a => a.d == d && mins(a.s) <= n.m && n.m < mins(a.e));
  const next = agenda.filter(a => a.d == d && mins(a.s) > n.m && a.p == 1).slice(0, 3);
  const missing = ZONAS.filter(z => !seen.has(z[1]) && z[0] != 'prensa' && z[0] != 'labs');
  const last = {}; today.forEach(j => { last[j.zona] = Math.max(last[j.zona] || 0, j.created) });
  const stale = Object.entries(last).filter(([, t]) => Date.now() / 1000 - t > 7200).map(([z]) => z);
  const missed = agenda.filter(a => a.d == d && a.p == 1 && mins(a.e) <= n.m && a.z != 'env' && !today.some(j => j.created * 1000 >= Date.parse(`${n.y}T${a.s}:00-05:00`) - 6e5 && j.created * 1000 <= Date.parse(`${n.y}T${a.e}:00-05:00`) + 18e5));
  $('falta').innerHTML = `<h1>Pendiente</h1><p class='sm mut'>${today.length} ${today.length == 1 ? 'historia' : 'historias'} hoy.</p>
  ${cur.length ? `<h2>Ahora</h2>` + cur.map(a => `<div class=card><b>${esc(a.t)}</b><p class='sm mut'>${h12(a.s)}–${h12(a.e)} · ${esc(a.zn)}</p></div>`).join('') : ''}
  ${next.length ? `<h2>No te lo pierdas</h2>` + next.map(a => `<div class=card><b>${esc(a.t)}</b><p class='sm mut'>${h12(a.s)} · ${esc(a.zn)} · en ${mins(a.s) - n.m} min</p></div>`).join('') : ''}
  <h2>Zonas sin material hoy</h2><div class=card>${missing.length ? missing.map(z => `<p class=sm>${z[1]}</p>`).join('') : `<p class=sm>Ya pasaste por todas.</p>`}</div>
  ${stale.length ? `<h2>Sin material hace más de 2 horas</h2><div class=card>${stale.map(z => `<p class=sm>${esc(z)}</p>`).join('')}</div>` : ''}
  ${missed.length ? `<h2>Imperdibles sin historia</h2><div class=card>${missed.map(a => `<p class=sm>${h12(a.s)} · ${esc(a.t)}</p>`).join('')}</div>` : ''}`;
}

document.addEventListener('input', e => { if (e.target.id == 'desc') draft.desc = e.target.value; if (e.target.id == 'idea') draft.idea = e.target.value });
document.addEventListener('change', e => {
  if (e.target.id != 'files') return;
  for (const file of e.target.files) picked.push({ file, url: URL.createObjectURL(file), video: file.type.startsWith('video') });
  rNueva();
});
document.addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b) return;
  const D = b.dataset;
  if (D.tab) { tab = D.tab; if (tab == 'lista') rLista(); if (tab == 'nueva') rNueva(); show(); scrollTo(0, 0) }
  if (D.rm) { const [p] = picked.splice(+D.rm, 1); URL.revokeObjectURL(p.url); rNueva() }
  if (D.z) { zona = D.z; ls.set('h_zona', zona); rNueva() }
  if (D.f) { formato = D.f; rNueva() }
  if (b.id == 'send') sendJob();
  if (D.share) { const [id, n] = D.share.split('_'); shareVideo(id, n, b) }
  if (D.pub) { await api(`/api/jobs/${D.pub}/status`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ status: 'publicada' }) }); await refresh(); rLista() }
  if (D.fb) { const t = $('fb_' + D.fb).value.trim(); if (!t) return; document.activeElement.blur(); b.disabled = true; await api(`/api/jobs/${D.fb}/feedback`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: t }) }); toast('Recibido. Ya lo corrijo'); await refresh(); rLista() }
  if (b.id == 'enter') login();
});
async function login() {
  $('loginErr').textContent = '';
  try {
    if (!base) await findPC();
    const r = await fetch(base + '/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ pin: $('pin').value.trim() }) });
    const j = await r.json();
    if (!r.ok) { $('loginErr').textContent = j.error || 'No se pudo entrar'; return }
    token = j.token; ls.set('h_token', token); show(); refresh();
  } catch (e) { $('loginErr').textContent = 'El PC no responde. Revisa que esté encendido y con internet.' }
}

fetch('agenda.json').then(r => r.json()).then(a => { agenda = a; rFalta() }).catch(() => { });
rNueva(); rLista(); show(); refresh();
setInterval(() => { if (!document.hidden && token) refresh() }, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && token) refresh() });
