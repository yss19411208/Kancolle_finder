'use strict';
// 管理画面。データはすべて GitHub 上の JSON で、/api/file/<key> 経由で読み書きする。
const TYPES = ['戦艦級', '航空母艦', '重巡級', '軽巡級', '駆逐艦', '海防艦', '潜水艦', '補助艦艇'];
const CSRF = document.querySelector('meta[name=csrf]').content;
const $ = id => document.getElementById(id);
const S = { config: null, resident: null, history: null, drop: null, master: null, routes: null, log: null, lastdrop: null, limpage: null, sha: {} };
const dirty = new Set();

// ---------------------------------------------------------------- 共通
function el(tag, props, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c);
  return e;
}
let toastTimer;
function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, isErr ? 7000 : 3000);
}
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = '/login'; throw new Error('ログインし直してください'); }
  const j = await r.json().catch(() => ({ error: `エラー ${r.status}` }));
  if (!r.ok) throw new Error(j.error || `エラー ${r.status}`);
  return j;
}
function setDirty(key, on) {
  if (on) dirty.add(key); else dirty.delete(key);
  document.body.classList.toggle('dirty-' + key, on);
}
function setState(msg, isErr) {
  const e = $('saveState');
  e.textContent = msg;
  e.className = isErr ? 'err' : '';
}
const clock = () => fmt(new Date()).slice(11);
// 設定 (アップデート日時・限定・除外) は操作のたびにその場で保存する。常駐リストと更新履歴は「保存」ボタン。
let saveChain = Promise.resolve();
function saveConfig(what) {
  setState('保存中…');
  saveChain = saveChain.then(async () => {
    try {
      const r = await api('/api/file/config', { content: S.config, sha: S.sha.config, message: `管理画面: ${what}` });
      S.sha.config = r.sha;
      setState(`保存しました ${clock()}`);
      watchRuns();
    } catch (e) {
      setState('保存できませんでした', true);
      toast(e.message + '（最新の状態を読み込み直しました。もう一度操作してください）', true);
      await loadAll().catch(() => {});
    }
  });
  return saveChain;
}
function markDirty(key, what) {
  if (key === 'config') return saveConfig(what || '設定を更新');
  setDirty(key, true);
}
const pad = n => String(n).padStart(2, '0');
function fmt(d) {   // Date -> 'YYYY/MM/DD HH:MM:SS' (日本時間)
  const j = new Date(d.getTime() + 9 * 3600e3);
  return `${j.getUTCFullYear()}/${pad(j.getUTCMonth() + 1)}/${pad(j.getUTCDate())} ${pad(j.getUTCHours())}:${pad(j.getUTCMinutes())}:${pad(j.getUTCSeconds())}`;
}
function parse(s) { // 'YYYY/MM/DD HH:MM:SS' (日本時間) -> epoch ms
  const m = /^(\d{4})\/(\d{2})\/(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(s || '');
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 9, +m[5], +m[6]) : NaN;
}
function fromInput(v) { // datetime-local の値 (日本時間として扱う) -> 'YYYY/MM/DD HH:MM:SS'
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(v || '');
  return m ? `${m[1]}/${m[2]}/${m[3]} ${m[4]}:${m[5]}:${m[6] || '00'}` : null;
}
const norm = s => String(s).replace(/[\s　]/g, '').toLowerCase();
const mapCmp = (a, b) => { const [a1, a2] = a.split('-').map(Number), [b1, b2] = b.split('-').map(Number); return a1 - b1 || a2 - b2; };
const nodeCmp = (a, b) => a.localeCompare(b, 'en', { numeric: true });
// 絞り込み: 空白区切りの語がすべて含まれていれば一致 (「1-3 大井」「大井 J」など)
const matchQ = (key, raw) => String(raw).split(/[\s\u3000]+/).filter(Boolean).every(t => key.includes(norm(t)));
const splitNames = s => s.split(/[、,\n]+/).map(x => x.trim()).filter(Boolean);
let nameByNorm = {}, tiByNorm = {};
function canon(name) { return nameByNorm[norm(name)] || name.trim(); }

// ---------------------------------------------------------------- 読み込み
async function loadAll() {
  const keys = ['config', 'resident', 'history', 'drop', 'master', 'routes', 'log', 'lastdrop', 'limpage'];
  const res = await Promise.all(keys.map(k => api('/api/file/' + k)));
  keys.forEach((k, i) => { S[k] = res[i].content; S.sha[k] = res[i].sha; });
  S.config = Object.assign({ updates: [], limited_manual: [], limited_exclude: [], limited_keep: [], auto_resident_exclude: [] }, S.config || {});
  delete S.config.end_dates;   // 終了日の機能は廃止
  S.resident = S.resident || {};
  S.history = Array.isArray(S.history) ? S.history : [];
  nameByNorm = {}; tiByNorm = {};
  const ids = Object.keys(S.master || {}).map(Number).sort((a, b) => b - a);
  for (const id of ids) { const v = S.master[id]; nameByNorm[norm(v.name)] = v.name; tiByNorm[norm(v.name)] = v.ti; }
  const dl = $('shipNames');
  dl.replaceChildren(...Object.values(nameByNorm).sort((a, b) => a.localeCompare(b, 'ja')).map(n => el('option', { value: n })));
  ['resident', 'history'].forEach(k => setDirty(k, false));
  lastLoad = Date.now();
  setState(`読み込み ${clock()}`);
  renderAll();
}
function renderAll() {
  renderDash(); renderLog(); renderUpdates(); fillMapSelects(); renderResident(); renderAutoResident(); renderLimited(); renderLastDrop(); renderHistory();
}

// ---------------------------------------------------------------- 保存
async function save(key, btn) {
  if (key === 'resident') readResidentTable();
  const label = { config: '設定', resident: '常駐リスト', history: '更新履歴' }[key];
  btn.disabled = true;
  try {
    const r = await api('/api/file/' + key, { content: S[key], sha: S.sha[key], message: `管理画面: ${label}を更新` });
    S.sha[key] = r.sha;
    setDirty(key, false);
    setState(`保存しました ${clock()}`);
    toast(key === 'history' ? '保存しました' : '保存しました。再集計が自動で始まります（数分後に公開ページへ反映）');
    if (key !== 'history') watchRuns();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- 状況
function renderDash() {
  const d = S.drop || {};
  let nLim = 0, nMan = 0;
  for (const nodes of Object.values(d.maps || {})) for (const v of Object.values(nodes)) for (const r of v.ships) {
    if (r[2]) { nLim++; if (r[7] === 'manual') nMan++; }
  }
  const rows = [
    ['最終集計', d.generated || '未実行'],
    ['集計対象の報告', d.records ? `${d.records.toLocaleString()} 件（${d.data_from || '?'} ～ ${d.data_to || '?'}）` : 'なし'],
    ['有効なアップデート日時', d.cutoff || 'なし（全期間）'],
    ['限定ドロップ', `${nLim} 件（うち手動 ${nMan} 件）`],
    ['海域数', Object.keys(d.maps || {}).length],
  ];
  $('rateMode').value = S.config.rate_mode || 'node_s';
  $('dashInfo').replaceChildren(...rows.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, String(v))]));
}
// ---------------------------------------------------------------- データ更新の記録
const signed = n => (n > 0 ? '+' : '') + Number(n).toLocaleString();
function renderLog() {
  const list = (Array.isArray(S.log) ? S.log : []).slice().reverse();
  const trig = { schedule: '定時の自動更新', workflow_dispatch: '手動の再集計', push: '設定の保存', manual: '手元で実行' };
  const box = $('logList');
  if (!list.length) { box.replaceChildren(el('p', { class: 'note' }, 'まだ記録がありません。次の集計から記録されます。')); return; }
  box.replaceChildren(...list.map((e, i) => {
    const parts = [];
    if (e.drop_added !== null && e.drop_added !== undefined) parts.push(`ドロップ記録 ${signed(e.drop_added)}件`);
    if (e.build_added !== null && e.build_added !== undefined) parts.push(`建造記録 ${signed(e.build_added)}件`);
    const tags = [];
    if (e.limited_added_n) tags.push(el('span', { class: 'tag ok' }, `限定 +${e.limited_added_n}`));
    if (e.limited_removed_n) tags.push(el('span', { class: 'tag ng' }, `限定 −${e.limited_removed_n}`));
    if (e.resident_added_n) tags.push(el('span', { class: 'tag ok' }, `常駐 +${e.resident_added_n}`));
    if (e.resident_removed_n) tags.push(el('span', { class: 'tag ng' }, `常駐 −${e.resident_removed_n}`));
    if (!tags.length) tags.push(el('span', { class: 'tag' }, '掲載内容の変化なし'));
    const group = (label, arr, n) => (n ? el('div', {}, el('b', {}, `${label}（${n}件）`), ' ', arr.join('、') + (n > arr.length ? ` ほか${n - arr.length}件` : '')) : null);
    return el('details', { open: i === 0 },
      el('summary', {},
        el('span', { class: 'when' }, (e.at || '').slice(5, 16)),
        el('span', { class: 'trig' }, trig[e.trigger] || e.trigger || ''),
        el('span', { class: 'nums' }, parts.join(' / ') || '件数の変化なし'), ...tags),
      el('div', { class: 'log-body' },
        el('div', {}, `集計後の合計: ドロップ ${Number(e.drop_records || 0).toLocaleString()}件（${(e.months || []).join('、')}、${e.data_to || '?'} まで） / 建造 ${Number(e.build_records || 0).toLocaleString()}件 / 限定 ${e.limited}件・常駐 ${e.resident}件`),
        e.cutoff ? el('div', {}, `限定の基準: ${e.cutoff} 以降`) : null,
        group('限定に追加', e.limited_added || [], e.limited_added_n),
        group('限定から外れた', e.limited_removed || [], e.limited_removed_n),
        group('常駐に追加', e.resident_added || [], e.resident_added_n),
        group('常駐から削除', e.resident_removed || [], e.resident_removed_n),
        ...(e.notes || []).map(n => el('div', { class: 'warn' }, n))));
  }));
}

// ---------------------------------------------------------------- 最終ドロップ
let ldRows = null;
function buildLdRows() {
  const kind = {};
  for (const [m, nodes] of Object.entries((S.drop || {}).maps || {})) for (const [node, v] of Object.entries(nodes)) for (const r of v.ships) kind[`${m}|${node}|${r[0]}`] = r[2] ? 'limited' : r[7] === 'data' ? 'auto' : 'normal';
  for (const [m, node, ship] of (S.drop || {}).single_hidden || []) kind[`${m}|${node}|${ship}`] = 'single';
  const ongoing = (S.limpage || {}).ongoing || {};   // 限定ページで継続中 (海域 -> 艦)
  const rows = [];
  for (const [m, nodes] of Object.entries((S.lastdrop || {}).maps || {})) for (const [node, ships] of Object.entries(nodes)) for (const [ship, v] of Object.entries(ships)) {
    const [at, count] = Array.isArray(v) ? v : [v, null];
    let k = kind[`${m}|${node}|${ship}`];
    if (!k) k = ((ongoing[m] || []).includes(ship) || limitedEvents(m, node, ship).some(e => !e.ended)) ? 'page' : 'none';
    rows.push({ map: m, node, ship, at, count, t: parse(at + ':00'), kind: k, key: norm(`${m}-${node} ${ship}`) });
  }
  return rows;
}
function renderLastDrop() {
  ldRows = buildLdRows();
  const months = (S.lastdrop || {}).months || [];
  $('ldRange').textContent = months.length ? ` 対象: ${months[0]} ～ ${months[months.length - 1]} の記録。` : ' まだデータがありません。次の集計で作られます。';
  drawLastDrop();
}
function drawLastDrop() {
  const q = $('ldFilter').value, k = $('ldKind').value, sort = $('ldSort').value;
  let rows = ldRows.filter(r => (k === 'all' || r.kind === k) && matchQ(r.key, q));
  const cmp = {
    old: (a, b) => a.t - b.t, new: (a, b) => b.t - a.t, count: (a, b) => (b.count || 0) - (a.count || 0),
    place: (a, b) => mapCmp(a.map, b.map) || nodeCmp(a.node, b.node) || a.ship.localeCompare(b.ship, 'ja'),
    ship: (a, b) => a.ship.localeCompare(b.ship, 'ja') || mapCmp(a.map, b.map) || nodeCmp(a.node, b.node),
  }[sort];
  rows.sort(cmp);
  const MAX = 500, now = Date.now();
  $('ldCount').textContent = `${rows.length.toLocaleString()} 件` + (rows.length > MAX ? `（先頭 ${MAX} 件を表示。絞り込むと残りも見られます）` : '');
  const label = { limited: ['限定', 'wait'], normal: ['常設', ''], auto: ['常設（全月）', 'ok'], none: ['どちらでもない', 'ng'], page: ['継続中だが未確認', 'ng'], single: ['1隻報告', ''] };
  $('ldTable').tBodies[0].replaceChildren(...rows.slice(0, MAX).map(r => {
    const h = (now - r.t) / 3600e3;
    const ago = h < 48 ? `${Math.max(0, Math.round(h))}時間前` : `${Math.floor(h / 24)}日前`;
    return el('tr', {},
      el('td', {}, r.ship), el('td', {}, r.map), el('td', {}, r.node),
      el('td', {}, el('span', { class: 'tag ' + label[r.kind][1] }, label[r.kind][0])),
      el('td', { class: 'num' }, r.at), el('td', { class: 'num' }, ago),
      el('td', { class: 'num' }, r.count === null ? '' : Number(r.count).toLocaleString()));
  }));
}

async function loadRuns() {
  try {
    const { runs } = await api('/api/runs');
    const ev = { schedule: '毎日の自動実行', workflow_dispatch: '手動', push: '設定の保存' };
    $('runsTable').tBodies[0].replaceChildren(...runs.map(r => {
      const st = r.status !== 'completed' ? ['実行中', 'wait'] : r.conclusion === 'success' ? ['成功', 'ok'] : [r.conclusion || '失敗', 'ng'];
      return el('tr', {},
        el('td', { class: 'num' }, fmt(new Date(r.created_at))),
        el('td', {}, ev[r.event] || r.event),
        el('td', {}, el('span', { class: 'tag ' + st[1] }, st[0])),
        el('td', {}, el('a', { href: r.url, target: '_blank', rel: 'noopener' }, 'ログ')));
    }));
    return runs;
  } catch (e) { toast(e.message, true); return []; }
}

// 再集計が終わるまで状況を見張り、終わったら公開中のデータを読み直す
let watchTimer = null;
function watchRuns() {
  clearInterval(watchTimer);
  let n = 0, seenRunning = false;
  watchTimer = setInterval(async () => {
    const runs = await loadRuns();
    const running = runs.some(r => r.status !== 'completed');
    seenRunning = seenRunning || running;
    if ((seenRunning && !running) || ++n > 60) {
      clearInterval(watchTimer);
      if (seenRunning) {
        try {
          const [r, l, d] = await Promise.all([api('/api/file/drop'), api('/api/file/log'), api('/api/file/lastdrop')]);
          S.drop = r.content; S.log = l.content; S.lastdrop = d.content;
          renderDash(); renderLog(); renderAutoResident(); renderLimited(); renderLastDrop();
          toast('再集計が終わりました。公開ページには 1〜2 分で反映されます');
        } catch (e) { /* 次の「最新に更新」で読み直す */ }
      }
    }
  }, 8000);
}
let lastLoad = 0;
async function reloadAll(silent) {
  if (dirty.size && !confirm('未保存の変更（常駐リスト・更新履歴）があります。破棄して最新を読み込みますか？')) return;
  setState('読み込み中…');
  try { await loadAll(); await loadRuns(); if (!silent) toast('最新の状態を読み込みました'); }
  catch (e) { setState('読み込めませんでした', true); toast(e.message, true); }
}

// ---------------------------------------------------------------- アップデート日時
function renderUpdates() {
  const now = Date.now();
  const list = S.config.updates.slice().sort((a, b) => parse(b.at) - parse(a.at));
  const eff = list.find(u => parse(u.at) <= now);
  $('updTable').tBodies[0].replaceChildren(...list.map(u => {
    const t = parse(u.at);
    const tag = t > now ? ['予約（未来）', 'wait'] : u === eff ? ['有効', 'ok'] : ['過去', ''];
    return el('tr', {},
      el('td', { class: 'num' }, u.at), el('td', {}, u.note || ''),
      el('td', {}, el('span', { class: 'tag ' + tag[1] }, tag[0])),
      el('td', {}, el('button', { class: 'btn small danger', on: { click: () => { S.config.updates.splice(S.config.updates.indexOf(u), 1); markDirty('config', `アップデート日時 ${u.at} を削除`); renderUpdates(); } } }, '削除')));
  }));
}

// ---------------------------------------------------------------- 常駐リスト
function fillMapSelects() {
  const maps = [...new Set([...Object.keys(S.resident), ...Object.keys(S.routes || {})])].sort(mapCmp);
  const opts = (first) => [...(first ? [el('option', { value: '' }, first)] : []), ...maps.map(m => el('option', { value: m }, m))];
  const cur = $('resMap').value;
  $('resMap').replaceChildren(...Object.keys(S.resident).sort(mapCmp).map(m => el('option', { value: m }, m)));
  if (cur && S.resident[cur]) $('resMap').value = cur;
  $('manMap').replaceChildren(...opts());
}
function renderResident() {
  const m = $('resMap').value;
  const nodes = S.resident[m] || {};
  const head = el('tr', {}, el('th', {}, 'マス'), ...TYPES.map(t => el('th', {}, t)));
  const rows = Object.keys(nodes).sort(nodeCmp).map(node => el('tr', { 'data-node': node },
    el('td', { class: 'node' }, node, el('br'),
      el('button', { class: 'btn small danger', on: { click: () => { if (confirm(`${m}-${node} を削除しますか？`)) { readResidentTable(); delete S.resident[m][node]; markDirty('resident'); renderResident(); } } } }, '削除')),
    ...nodes[node].map((names, ti) => el('td', { class: 'c' + ti },
      el('textarea', { rows: Math.max(2, Math.ceil(names.join('、').length / (ti === 4 ? 17 : 7))), value: names.join('、'), on: { input: () => markDirty('resident'), change: checkResidentNames } })))));
  $('resTable').replaceChildren(el('thead', {}, head), el('tbody', {}, ...rows));
  $('resTable').dataset.map = m;
  checkResidentNames();
}
// 出撃ドロップに無いが、全月で出ているので常設として公開しているもの
function renderAutoResident() {
  const off = S.config.auto_resident_exclude;
  const q = $('autoResFilter').value;
  const all = [];
  for (const [m, nodes] of Object.entries((S.drop || {}).maps || {})) for (const [node, v] of Object.entries(nodes)) for (const r of v.ships) {
    if (r[7] === 'data') {
      const ld = (((S.lastdrop || {}).maps || {})[m] || {})[node] || {};
      const v = ld[r[0]];
      all.push({ map: m, node, ship: r[0], rate: r[3], drops: r[4], n: r[5], last: Array.isArray(v) ? v[0] : (v || ''),
                 ev: limitedEvents(m, node, r[0]) });
    }
  }
  all.sort((a, b) => mapCmp(a.map, b.map) || nodeCmp(a.node, b.node) || a.ship.localeCompare(b.ship, 'ja'));
  const onlyHit = $('autoResOnlyHit').checked;
  const rows = all.filter(a => !off.some(e => same(e, a))).filter(a => matchQ(norm(`${a.map}-${a.node} ${a.ship}`), q))
    .filter(a => !onlyHit || a.ev.length);
  $('autoResHit').textContent = `限定ページに記載があるもの: ${all.filter(a => !off.some(e => same(e, a)) && a.ev.length).length} 件`;
  const jm = (S.drop || {}).judge_months || [];
  $('autoMonths').textContent = jm.length ? `判定に使っている月: ${jm.join('、')}。` : '';
  $('autoResCount').textContent = `${rows.length} 件`;
  $('autoResTable').tBodies[0].replaceChildren(...(rows.length ? rows.map(a => el('tr', {},
    el('td', {}, a.map), el('td', {}, a.node), el('td', {}, a.ship),
    el('td', { class: 'num' }, a.rate === null ? '—' : a.rate + '%'),
    el('td', { class: 'num' }, `${Number(a.drops).toLocaleString()} / ${Number(a.n).toLocaleString()}`),
    el('td', { class: 'num' + (a.last && Date.now() - parse(a.last + ':00') >= 72 * 3600e3 ? ' stale' : '') }, a.last || '—'),
    evCell(a.ev, '記載なし'),
    el('td', {}, el('button', { class: 'btn small', on: { click: () => { off.push({ map: a.map, node: a.node, ship: a.ship }); markDirty('config', `常設(全月)から外す ${a.map}-${a.node} ${a.ship}`); renderAutoResident(); } } }, '常設から外す')))) : [el('tr', {}, el('td', { colSpan: 8 }, '該当なし'))]));
  $('autoOffTable').tBodies[0].replaceChildren(...(off.length ? off.map(e => el('tr', {},
    el('td', {}, e.map), el('td', {}, e.node), el('td', {}, e.ship),
    el('td', {}, el('button', { class: 'btn small danger', on: { click: () => { off.splice(off.indexOf(e), 1); markDirty('config', `常設(全月)に戻す ${e.map}-${e.node} ${e.ship}`); renderAutoResident(); } } }, '常設に戻す')))) : [el('tr', {}, el('td', { colSpan: 4 }, 'なし'))]));
}
function readResidentTable() {
  const m = $('resTable').dataset.map;
  if (!m || !S.resident[m]) return;
  for (const tr of $('resTable').tBodies[0].rows) {
    S.resident[m][tr.dataset.node] = [...tr.querySelectorAll('textarea')].map(t => [...new Set(splitNames(t.value).map(canon))]);
  }
}
function checkResidentNames() {
  const bad = new Set();
  for (const t of $('resTable').querySelectorAll('textarea')) for (const n of splitNames(t.value)) if (!nameByNorm[norm(n)]) bad.add(n);
  $('resWarn').textContent = bad.size ? `艦名マスターに無い名前: ${[...bad].join('、')}` : '';
}

// Wiki HTML -> {map: {node: [8 分類の艦名配列]}} (青字は除く)
function parseWiki(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const out = {};
  for (const h of doc.querySelectorAll('h4')) {
    const mm = /^(\d-\d)\./.exec(h.textContent.trim());
    if (!mm) continue;
    let n = h.nextElementSibling, table = null;
    for (let i = 0; n && i < 6 && !table; i++, n = n.nextElementSibling) table = n.matches('table') ? n : n.querySelector('table');
    if (!table) continue;
    for (const tr of table.querySelectorAll('tbody tr')) {
      const tds = tr.children;
      if (tds.length < 9) continue;
      const head = tds[0].cloneNode(true);
      head.querySelectorAll('br').forEach(b => b.replaceWith(' '));   // 「C<br>ボス」「G<br>第一<br>ボス」
      const node = head.textContent.trim().split(/\s+/)[0];
      if (!node) continue;
      const cols = [];
      for (let ti = 0; ti < 8; ti++) {
        const tokens = [];
        const walk = (nd, blue) => {
          for (const c of nd.childNodes) {
            if (c.nodeType === 3) { if (!blue) tokens.push(...c.textContent.split(/[、,\s　]+/).filter(Boolean)); }
            else if (c.nodeType === 1) {
              if (c.tagName === 'SUP' || /note/.test(c.className || '')) continue;   // 脚注の番号
              const col = (c.style && c.style.color || '').toLowerCase();
              walk(c, blue || col === 'blue' || col === 'rgb(0, 0, 255)');
            }
          }
        };
        walk(tds[ti + 1], false);
        const names = [];
        for (let i = 0; i < tokens.length;) {   // 「De Ruyter」など空白入りの艦名をつなぐ
          let hit = 0;
          for (const k of [3, 2, 1]) {
            const cand = tokens.slice(i, i + k).join(' ').replace(/[※*]/g, '');
            if (i + k <= tokens.length && nameByNorm[norm(cand)]) { names.push(nameByNorm[norm(cand)]); hit = k; break; }
          }
          if (!hit) { names.push(tokens[i].replace(/[※*]/g, '')); hit = 1; }
          i += hit;
        }
        cols.push(names);
      }
      (out[mm[1]] = out[mm[1]] || {})[node] = cols;
    }
  }
  return out;
}
function showWikiDiff(wiki) {
  readResidentTable();
  const box = $('wikiDiff');
  const lines = [];
  const maps = [...new Set([...Object.keys(wiki), ...Object.keys(S.resident)])].sort(mapCmp);
  for (const m of maps) {
    const nodes = [...new Set([...Object.keys(wiki[m] || {}), ...Object.keys(S.resident[m] || {})])].sort(nodeCmp);
    for (const node of nodes) {
      const w = (wiki[m] || {})[node], r = (S.resident[m] || {})[node];
      if (!w) { lines.push([`${m}-${node}`, [], ['（Wiki にこのマスがありません）']]); continue; }
      if (!r) { lines.push([`${m}-${node}`, ['（常駐リストにこのマスがありません）'], []]); continue; }
      const add = [], del = [];
      for (let ti = 0; ti < 8; ti++) {
        const ws = new Set(w[ti].map(norm)), rs = new Set(r[ti].map(norm));
        for (const n of w[ti]) if (!rs.has(norm(n))) add.push(`${n}（${TYPES[ti]}）`);
        for (const n of r[ti]) if (!ws.has(norm(n))) del.push(`${n}（${TYPES[ti]}）`);
      }
      if (add.length || del.length) lines.push([`${m}-${node}`, add, del]);
    }
  }
  if (!Object.keys(wiki).length) { box.replaceChildren(el('p', { class: 'warn' }, '表を読み取れませんでした。「出撃ドロップ」ページの HTML か確認してください。')); return; }
  box.className = 'diff';
  box.replaceChildren(
    el('p', {}, lines.length ? `差分 ${lines.length} マス（Wiki ${Object.keys(wiki).length} 海域を読み取り）` : `差分はありません（Wiki ${Object.keys(wiki).length} 海域を読み取り）`),
    ...lines.map(([place, add, del]) => el('div', { class: 'd' },
      el('a', { href: '#', on: { click: e => { e.preventDefault(); const m = place.split('-').slice(0, 2).join('-'); if (S.resident[m]) { $('resMap').value = m; renderResident(); window.scrollTo({ top: 0, behavior: 'smooth' }); } } } }, place), ' ',
      add.length ? el('span', { class: 'add' }, 'Wiki にあり常駐リストに無い: ' + add.join('、') + ' ') : null,
      del.length ? el('span', { class: 'del' }, '常駐リストにあり Wiki に無い: ' + del.join('、')) : null)));
}

// ---------------------------------------------------------------- 限定ドロップ
function autoLimited() {
  const out = [];
  for (const [m, nodes] of Object.entries((S.drop || {}).maps || {})) for (const [node, v] of Object.entries(nodes)) for (const r of v.ships) {
    if (r[2] && r[7] === 'auto') out.push({ map: m, node, ship: r[0], ti: r[1], rate: r[3], drops: r[4], n: r[5] });
  }
  return out.sort((a, b) => mapCmp(a.map, b.map) || nodeCmp(a.node, b.node) || a.ship.localeCompare(b.ship, 'ja'));
}
// 期間限定ドロップのページ (data/limited_page.json) と照合する
// 戻り値: [{title, start, status, ended}]  継続中のものを先に、新しい順
function limitedEvents(map, node, ship) {
  const page = S.limpage || {};
  const ev = page.events || [];
  const nn = norm(ship);
  const hits = (page.entries || [])
    .filter(([m, n, s]) => m === map && (n === null || n === node) && norm(s) === nn)
    .map(([, , , ei, ended]) => ({ ...(ev[ei] || {}), ended: !!ended }));
  const seen = new Set();
  const list = hits.filter(h => { const k = h.title + h.start + h.ended; if (seen.has(k)) return false; seen.add(k); return true; })
    .sort((a, b) => (a.ended - b.ended) || (b.start || '').localeCompare(a.start || ''));
  // 個別のイベント表に無い、昔から続いている限定: ページ先頭の「現在継続中」一覧と、出撃ドロップの青字
  if (!list.some(e => !e.ended)) {
    if (((page.blue || {})[map] || {})[node]?.some(n => norm(n) === nn)) list.unshift({ title: '出撃ドロップで青字（期間限定）', start: '', ended: false, note: true });
    else if (((page.ongoing || {})[map] || []).some(n => norm(n) === nn)) list.unshift({ title: '限定ページ先頭の「現在継続中」一覧に記載（海域単位）', start: '', ended: false, note: true });
  }
  return list;
}
const evLabel = e => e.note ? e.title : `${e.title} ${e.start}〜${e.end || ''}${e.status === '終了' ? '（終了済）' : e.ended ? '（この艦は終了）' : ''}`;
function evCell(list, emptyText) {
  if (!list.length) return el('td', { class: 'ev none' }, emptyText);
  const [first, ...rest] = list;
  return el('td', { class: 'ev' + (first.ended ? ' past' : '') },
    el('div', {}, evLabel(first)),
    rest.length ? el('div', { class: 'ev-more' }, `ほか ${rest.length} 件: ` + rest.map(evLabel).join(' / ')) : null);
}

const same = (a, b) => a.map === b.map && a.node === b.node && norm(a.ship) === norm(b.ship);
function renderLimited() {
  const q = $('limFilter').value;
  const ex = S.config.limited_exclude;
  const rows = autoLimited().filter(a => !ex.some(e => same(e, a)))
    .filter(a => matchQ(norm(`${a.map}-${a.node} ${a.ship}`), q)).map(a => {
    const excluded = false;
    return el('tr', {},
      el('td', {}, a.map), el('td', {}, a.node), el('td', {}, a.ship),
      el('td', { class: 'num' }, a.rate === null ? '—' : a.rate + '%'),
      el('td', { class: 'num' }, `${a.drops} / ${a.n}`),
      evCell(limitedEvents(a.map, a.node, a.ship), '限定ページに記載なし'),
      el('td', {}, excluded ? el('span', { class: 'tag' }, '除外予定') : [
        el('button', { class: 'btn small', on: { click: () => { ex.push({ map: a.map, node: a.node, ship: a.ship }); markDirty('config', `限定から除外 ${a.map}-${a.node} ${a.ship}`); renderLimited(); } } }, '除外'), ' ',
        el('button', { class: 'btn small', on: { click: () => toResident(a) } }, '常駐へ')]));
  });
  $('autoTable').tBodies[0].replaceChildren(...(rows.length ? rows : [el('tr', {}, el('td', { colSpan: 7 }, '該当なし'))]));

  // 1 隻報告で載せていないもの / 復元したもの
  const keepList = S.config.limited_keep;
  const singles = ((S.drop || {}).single_hidden || []).map(([map, node, ship, n]) => ({ map, node, ship, n }))
    .filter(a => !keepList.some(e => same(e, a)));
  $('singleTable').tBodies[0].replaceChildren(...(singles.length ? singles.map(a => el('tr', {},
    el('td', {}, a.map), el('td', {}, a.node), el('td', {}, a.ship), el('td', { class: 'num' }, Number(a.n).toLocaleString()),
    el('td', {}, el('button', { class: 'btn small', on: { click: () => { keepList.push({ map: a.map, node: a.node, ship: a.ship }); markDirty('config', `1隻報告を復元 ${a.map}-${a.node} ${a.ship}`); renderLimited(); } } }, '復元')))) : [el('tr', {}, el('td', { colSpan: 5 }, 'なし'))]));
  $('keepTable').tBodies[0].replaceChildren(...(keepList.length ? keepList.map(e => el('tr', {},
    el('td', {}, e.map), el('td', {}, e.node), el('td', {}, e.ship),
    el('td', {}, el('button', { class: 'btn small danger', on: { click: () => { keepList.splice(keepList.indexOf(e), 1); markDirty('config', `復元を取り消し ${e.map}-${e.node} ${e.ship}`); renderLimited(); } } }, '取り消し')))) : [el('tr', {}, el('td', { colSpan: 4 }, 'なし'))]));

  const man = S.config.limited_manual;
  $('manTable').tBodies[0].replaceChildren(...man.map(e => el('tr', {},
    el('td', {}, e.map), el('td', {}, e.node), el('td', {}, e.ship), el('td', { class: 'num' }, e.added || ''),
    el('td', {}, el('button', { class: 'btn small danger', on: { click: () => { man.splice(man.indexOf(e), 1); markDirty('config', `手動の限定を削除 ${e.map}-${e.node} ${e.ship}`); renderLimited(); } } }, '削除')))));
  $('exTable').tBodies[0].replaceChildren(...(ex.length ? ex.map(e => el('tr', {},
    el('td', {}, e.map), el('td', {}, e.node), el('td', {}, e.ship),
    el('td', {}, el('button', { class: 'btn small danger', on: { click: () => { ex.splice(ex.indexOf(e), 1); markDirty('config', `除外を解除 ${e.map}-${e.node} ${e.ship}`); renderLimited(); } } }, '解除')))) : [el('tr', {}, el('td', { colSpan: 4 }, 'なし'))]));
}
function toResident(a) {
  readResidentTable();
  const m = S.resident[a.map] = S.resident[a.map] || {};
  const cols = m[a.node] = m[a.node] || TYPES.map(() => []);
  if (!cols[a.ti].some(n => norm(n) === norm(a.ship))) cols[a.ti].push(a.ship);
  markDirty('resident');
  fillMapSelects();
  $('resMap').value = a.map;
  renderResident();
  toast(`${a.map}-${a.node} の常駐リストに ${a.ship} を追加しました。「常駐リスト」タブで保存してください`);
}

// ---------------------------------------------------------------- 更新履歴
function renderHistory() {
  const box = $('histList');
  box.replaceChildren(...S.history.map((h, i) => el('div', { class: 'hist' },
    el('div', { class: 'row' },
      el('input', { type: 'text', value: h['更新日時'] || '', size: 12, on: { input: e => { h['更新日時'] = e.target.value; markDirty('history'); } } }),
      h.auto ? el('span', { class: 'tag' }, '自動') : null,
      el('span', { class: 'grow' }),
      el('button', { class: 'btn small danger', on: { click: () => { S.history.splice(i, 1); markDirty('history'); renderHistory(); } } }, '削除')),
    el('textarea', { rows: Math.max(2, (h['更新内容'] || []).length), value: (h['更新内容'] || []).join('\n'), on: { input: e => { h['更新内容'] = e.target.value.split('\n').map(x => x.trim()).filter(Boolean); markDirty('history'); } } }))).reverse());
}

// ---------------------------------------------------------------- イベント
document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => {
  if (b.dataset.tab !== 'resident') readResidentTable();
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('main section').forEach(s => s.classList.toggle('active', s.id === 'tab-' + b.dataset.tab));
}));
document.querySelectorAll('.btn.save').forEach(b => b.addEventListener('click', () => save(b.dataset.save, b)));
$('rerunBtn').addEventListener('click', async e => {
  e.target.disabled = true;
  try { await api('/api/rerun', {}); toast('再集計を開始しました'); watchRuns(); }
  catch (err) { toast(err.message, true); }
  finally { setTimeout(() => { e.target.disabled = false; }, 5000); }
});
$('rateMode').addEventListener('change', e => {
  S.config.rate_mode = e.target.value;
  markDirty('config', `ドロップ率の計算方法を変更 (${e.target.value})`);
});
$('runsReload').addEventListener('click', () => reloadAll());
$('reloadBtn').addEventListener('click', () => reloadAll());
// 別の端末で変更したあとにこの画面へ戻ってきたら、自動で最新を読み込む
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && !dirty.size && Date.now() - lastLoad > 30000) reloadAll(true);
});
$('updAdd').addEventListener('click', () => {
  const at = fromInput($('updAt').value);
  if (!at) return toast('日時を入力してください', true);
  if (S.config.updates.some(u => u.at === at)) return toast('同じ日時が登録済みです', true);
  S.config.updates.push({ at, note: $('updNote').value.trim() });
  $('updAt').value = ''; $('updNote').value = '';
  markDirty('config', `アップデート日時 ${at} を追加`); renderUpdates();
});
$('resMap').addEventListener('change', () => { readResidentTable(); renderResident(); });
$('resAddNode').addEventListener('click', () => {
  const m = $('resMap').value;
  const node = (prompt(`${m} に追加するマス名（例: K）`) || '').trim();
  if (!node) return;
  readResidentTable();
  if (S.resident[m][node]) return toast('そのマスは既にあります', true);
  S.resident[m][node] = TYPES.map(() => []);
  markDirty('resident'); renderResident();
});
$('resAddMap').addEventListener('click', () => {
  const m = (prompt('追加する海域（例: 7-6）') || '').trim();
  if (!/^\d+-\d+$/.test(m)) return m ? toast('「7-6」の形式で入力してください', true) : null;
  readResidentTable();
  if (!S.resident[m]) { S.resident[m] = {}; markDirty('resident'); }
  fillMapSelects(); $('resMap').value = m; renderResident();
});
$('wikiFile').addEventListener('change', async e => {
  const f = e.target.files[0];
  if (!f) return;
  try { showWikiDiff(parseWiki(await f.text())); } catch (err) { toast('解析に失敗しました: ' + err.message, true); }
});
$('limFilter').addEventListener('input', renderLimited);
$('autoResFilter').addEventListener('input', renderAutoResident);
$('autoResOnlyHit').addEventListener('change', renderAutoResident);
['ldFilter', 'ldKind', 'ldSort'].forEach(id => $(id).addEventListener(id === 'ldFilter' ? 'input' : 'change', drawLastDrop));
$('manAdd').addEventListener('click', () => {
  const e = { map: $('manMap').value, node: $('manNode').value.trim(), ship: canon($('manShip').value), added: fmt(new Date()) };
  if (!e.map || !e.node || !e.ship) return toast('海域・マス・艦名を入力してください', true);
  if (!nameByNorm[norm(e.ship)] && !confirm(`「${e.ship}」は艦名マスターにありません。このまま追加しますか？`)) return;
  if (S.config.limited_manual.some(x => same(x, e))) return toast('登録済みです', true);
  S.config.limited_manual.push(e);
  $('manShip').value = '';
  markDirty('config', `手動の限定を追加 ${e.map}-${e.node} ${e.ship}`); renderLimited();
});
$('manClear').addEventListener('click', () => {
  if (!S.config.limited_manual.length || !confirm(`手動で追加した ${S.config.limited_manual.length} 件をすべて削除しますか？`)) return;
  S.config.limited_manual = [];
  markDirty('config', '手動の限定をすべて削除'); renderLimited();
});
$('histAdd').addEventListener('click', () => {
  S.history.push({ '更新日時': fmt(new Date()).slice(0, 10), '更新内容': [] });
  markDirty('history'); renderHistory();
});
window.addEventListener('beforeunload', e => { if (dirty.size) { e.preventDefault(); e.returnValue = ''; } });
$('logoutForm').addEventListener('submit', () => dirty.clear());
window.addEventListener('pagehide', () => clearInterval(watchTimer));

loadAll().then(loadRuns).catch(e => toast(e.message, true));
