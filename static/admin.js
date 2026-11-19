/* 老師後台：考試設定、考場監控、各題分析、題庫、名單 */
(function () {
  'use strict';
  const { store, api, esc, fmtTime, clock, ask, LETTERS } = window.exam;
  const $ = id => document.getElementById(id);

  let token = store('exam_admin_token');
  let questions = [];
  let editingExam = null;
  let picked = [];
  let pickedPoints = {};       // 題目 id → 這場考試的配分（空 = 題庫預設）
  let editingQ = null;
  let monitorExam = null;
  let monitorTimer = null;
  const TYPE_TEXT = { single: '單選', multi: '多選', fill: '填充' };

  // 秒數 → 「12 分 05 秒」「48 秒」
  function fmtSec(sec) {
    if (sec == null) return '-';
    const m = Math.floor(sec / 60), s = Math.round(sec % 60);
    return m ? m + ' 分 ' + String(s).padStart(2, '0') + ' 秒' : s + ' 秒';
  }

  function call(method, path, body) {
    return api(method, path, body, token).catch(err => {
      if (err.status === 401) logout();
      throw err;
    });
  }

  function say(id, text, ok) {
    $(id).textContent = text;
    $(id).className = 'msg ' + (ok ? 'ok' : 'ng');
  }

  function show(view) {
    ['login', 'exams', 'monitor', 'scores', 'stats', 'attempt', 'seats', 'questions', 'students'].forEach(v =>
      $('view-' + v).classList.toggle('hidden', v !== view));
    $('tabs').classList.toggle('hidden', view === 'login');
    $('logout').classList.toggle('hidden', view === 'login');
    if (view !== 'monitor') clearInterval(monitorTimer);
    const tab = { monitor: 'exams', scores: 'exams', stats: 'exams', attempt: 'exams', seats: 'exams' }[view] || view;
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  }

  function logout() {
    token = null;
    store('exam_admin_token', null);
    show('login');
  }

  $('login-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const r = await api('POST', '/api/admin/login', { password: $('pw').value });
      token = r.token;
      store('exam_admin_token', token);
      $('pw').value = '';
      openTab('exams');
    } catch (err) {
      say('login-msg', err.message);
    }
  });
  $('logout').addEventListener('click', logout);
  document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => openTab(b.dataset.tab)));

  function openTab(tab) {
    show(tab);
    if (tab === 'exams') loadExams();
    if (tab === 'questions') loadQuestions();
    if (tab === 'students') loadStudents();
  }

  function readFile(input) {
    return new Promise((resolve, reject) => {
      const f = input.files[0];
      if (!f) return reject(new Error('沒有選檔案'));
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('讀檔失敗'));
      reader.readAsText(f, 'utf-8');
    });
  }

  // ── 考試列表與表單 ──

  async function loadExams() {
    const exams = await call('GET', '/api/admin/exams');
    const WINDOW = { not_started: '<span class="badge gray">尚未開放</span>', open: '<span class="badge ok">開放中</span>', closed: '<span class="badge ng">已關閉</span>' };
    const when = t => t ? esc(t.replace('T', ' ')) : '';
    $('exam-table').innerHTML = '<tr><th>名稱</th><th>班級</th><th>狀態</th><th>題數</th><th>作答時間</th><th>開放 ～ 關閉</th><th>打散</th><th>已進場</th><th></th></tr>' +
      exams.map(e => '<tr><td><b>' + esc(e.title) + '</b></td>' +
        '<td>' + (e.class_names ? esc(e.class_names.split(',').join('、')) : '<span class="muted">不限</span>') + '</td>' +
        '<td>' + (e.published ? WINDOW[e.window] : '<span class="badge gray">學生看不到</span>') + '</td>' +
        '<td>' + e.question_count + '</td><td>' + e.duration_min + ' 分鐘</td>' +
        '<td class="nowrap">' + (when(e.start_at) || '立即') + ' ～ ' + (when(e.end_at) || '不限') + '</td>' +
        '<td class="nowrap">' + [e.shuffle_q ? '題目' : '', e.shuffle_opt ? '選項' : ''].filter(Boolean).join('、') + '</td>' +
        '<td>' + e.attempt_count + '</td>' +
        '<td class="row"><button class="btn sm" data-monitor="' + e.id + '">監控／成績</button>' +
        '<button class="btn ghost sm" data-edit="' + e.id + '">編輯</button>' +
        '<button class="btn danger sm" data-del="' + e.id + '">刪除</button></td></tr>').join('');
    if (!exams.length) $('exam-table').innerHTML += '<tr><td colspan="9" class="muted">還沒有考試，先到「題庫」匯入題目，再回來新增考試。</td></tr>';
  }

  $('exam-table').addEventListener('click', async e => {
    const t = e.target;
    if (t.dataset.monitor) openMonitor(+t.dataset.monitor);
    if (t.dataset.edit) openExamForm(+t.dataset.edit);
    if (t.dataset.del && confirm('刪除考試會連同所有作答紀錄一起刪除，確定嗎？')) {
      await call('DELETE', '/api/admin/exams/' + t.dataset.del);
      loadExams();
    }
  });

  $('new-exam').addEventListener('click', () => openExamForm(null));
  $('cancel-exam').addEventListener('click', () => $('exam-form').classList.add('hidden'));

  async function openExamForm(id) {
    let classList;
    [questions, classList] = await Promise.all([call('GET', '/api/admin/questions'), call('GET', '/api/admin/classes')]);
    editingExam = id;
    const e = id ? await call('GET', '/api/admin/exams/' + id) : {
      title: '', duration_min: 50, start_at: '', end_at: '', class_names: '', show_mode: 'score',
      review_when: 'immediate', review_at: '', allow_review: 1, review_until: '', shuffle_q: 1, shuffle_opt: 1, published: 0, allowed_ips: '', question_ids: [],
      focus_mode: 'record', focus_limit: 3, fullscreen: 0, block_copy: 0,
    };
    $('exam-form-title').textContent = id ? '編輯考試' : '新增考試';
    $('e-title').value = e.title;
    $('e-duration').value = e.duration_min;
    $('e-start-on').checked = !!e.start_at;
    $('e-end-on').checked = !!e.end_at;
    $('e-start').value = e.start_at;
    $('e-end').value = e.end_at;
    syncTimeInputs();
    $('e-show').value = e.show_mode;
    $('e-review').value = e.review_when;
    $('e-review-at').value = e.review_at || '';
    $('e-allow-review').value = !e.allow_review ? 'no' : e.review_until ? 'until' : 'always';
    $('e-review-until').value = e.review_until || '';
    syncReview();
    $('e-shuffle-q').value = e.shuffle_q ? '1' : '0';
    $('e-shuffle-opt').value = e.shuffle_opt ? '1' : '0';
    $('e-published').checked = !!e.published;
    // 已設定但班級被刪掉的名稱也列出來，存檔前看得到
    const chosen = e.class_names ? e.class_names.split(',').map(x => x.trim()).filter(Boolean) : [];
    const names = classList.classes.map(c => c.name);
    chosen.forEach(n => { if (!names.includes(n)) names.push(n); });
    $('e-classes').innerHTML = names.map(n => {
      const c = classList.classes.find(x => x.name === n);
      return '<label class="check"><input type="checkbox" value="' + esc(n) + '"' + (chosen.includes(n) ? ' checked' : '') + '> ' + esc(n) +
        ' <span class="muted">' + (c ? c.students + ' 人' : '（班級已不存在）') + '</span></label>';
    }).join('') || '<span class="muted">還沒有班級，到「名單」新增。</span>';
    $('e-ips').value = e.allowed_ips;
    $('e-focus').value = e.focus_mode;
    $('e-focus-limit').value = e.focus_limit;
    $('e-fullscreen').checked = !!e.fullscreen;
    $('e-block-copy').checked = !!e.block_copy;
    syncFocus();
    $('e-filter').value = '';
    picked = e.question_ids.slice();
    pickedPoints = Object.assign({}, e.question_points || {});
    renderPicker();
    $('exam-msg').textContent = '';
    $('exam-form').classList.remove('hidden');
    $('exam-form').scrollIntoView({ behavior: 'smooth' });
  }

  function syncTimeInputs() {
    $('e-start').disabled = !$('e-start-on').checked;
    $('e-end').disabled = !$('e-end-on').checked;
  }
  function syncReview() {
    $('e-review-at-wrap').classList.toggle('hidden', $('e-review').value !== 'at_time');
    $('e-review-until-wrap').classList.toggle('hidden', $('e-allow-review').value !== 'until');
  }
  function syncFocus() {
    $('e-focus-limit-wrap').classList.toggle('hidden', !['lock', 'submit'].includes($('e-focus').value));
    $('e-focus-limit-next').textContent = (+$('e-focus-limit').value || 0) + 1;
  }
  $('e-focus').addEventListener('change', syncFocus);
  $('e-focus-limit').addEventListener('input', syncFocus);
  $('e-review').addEventListener('change', syncReview);
  $('e-allow-review').addEventListener('change', syncReview);
  $('e-start-on').addEventListener('change', syncTimeInputs);
  $('e-end-on').addEventListener('change', syncTimeInputs);

  function qMatches(q, text) {
    text = text.trim().toLowerCase();
    return !text || (q.stem + ' ' + q.tags).toLowerCase().includes(text);
  }

  function renderPicker() {
    const f = $('e-filter').value;
    $('e-questions').innerHTML = questions.filter(q => qMatches(q, f) || picked.includes(q.id)).map(q => {
      const n = picked.indexOf(q.id);
      return '<label><input type="checkbox" value="' + q.id + '"' + (n >= 0 ? ' checked' : '') + '>' +
        '<span>' + (n >= 0 ? '<b>' + (n + 1) + '.</b> ' : '') + '<span class="badge">' + TYPE_TEXT[q.type] + '</span> ' +
        esc(q.stem.slice(0, 80)) + ' <span class="muted">' + esc(q.tags) + '・' + q.points + ' 分</span></span></label>';
    }).join('') || '<p class="muted">題庫是空的。</p>';
    const pointOf = id => pickedPoints[id] != null && pickedPoints[id] !== '' ? +pickedPoints[id] : ((questions.find(q => q.id === id) || {}).points || 0);
    const pts = picked.reduce((s, id) => s + pointOf(id), 0);
    $('e-picked').textContent = '已選 ' + picked.length + ' 題，共 ' + Math.round(pts * 100) / 100 + ' 分';
    $('e-picked-wrap').classList.toggle('hidden', !picked.length);
    $('e-picked-list').innerHTML = '<tr><th style="width:56px">順序</th><th>題目</th><th style="width:150px">配分</th><th style="width:96px"></th></tr>' +
      picked.map((id, n) => {
        const q = questions.find(x => x.id === id) || { stem: '（題目已刪除）', tags: '', points: 0, type: 'single' };
        const v = pickedPoints[id] != null ? pickedPoints[id] : '';
        return '<tr><td>' + (n + 1) + '</td><td><span class="badge">' + TYPE_TEXT[q.type] + '</span> ' + esc(q.stem.slice(0, 60)) +
          ' <span class="muted">' + esc(q.tags) + '</span></td>' +
          '<td><input type="number" class="cut-input" min="0" step="any" data-pts="' + id + '" value="' + esc(v) + '" placeholder="' + q.points + '"> 分</td>' +
          '<td class="row"><button class="btn ghost sm" data-up="' + n + '"' + (n ? '' : ' disabled') + '>↑</button>' +
          '<button class="btn ghost sm" data-down="' + n + '"' + (n < picked.length - 1 ? '' : ' disabled') + '>↓</button></td></tr>';
      }).join('');
  }

  $('e-filter').addEventListener('input', renderPicker);
  $('e-picked-list').addEventListener('change', e => {
    const id = e.target.dataset.pts;
    if (!id) return;
    if (e.target.value === '') delete pickedPoints[id]; else pickedPoints[id] = +e.target.value;
    renderPicker();
  });
  $('e-picked-list').addEventListener('click', e => {
    const up = e.target.dataset.up, down = e.target.dataset.down;
    const i = up != null ? +up : down != null ? +down : -1;
    if (i < 0) return;
    const j = up != null ? i - 1 : i + 1;
    [picked[i], picked[j]] = [picked[j], picked[i]];
    renderPicker();
  });
  $('e-questions').addEventListener('change', e => {
    const id = +e.target.value;
    if (!id) return;
    if (e.target.checked) { if (!picked.includes(id)) picked.push(id); }
    else picked = picked.filter(x => x !== id);
    renderPicker();
  });

  $('add-my-ip').addEventListener('click', async () => {
    const { ip } = await call('GET', '/api/admin/myip');
    const lines = $('e-ips').value.split('\n').map(s => s.trim()).filter(Boolean);
    if (!lines.includes(ip)) lines.push(ip);
    $('e-ips').value = lines.join('\n');
    $('my-ip').textContent = '你目前的 IP：' + ip;
  });

  $('save-exam').addEventListener('click', async () => {
    const body = {
      title: $('e-title').value, duration_min: +$('e-duration').value,
      start_at: $('e-start-on').checked ? $('e-start').value : '',
      end_at: $('e-end-on').checked ? $('e-end').value : '',
      class_names: [...document.querySelectorAll('#e-classes input:checked')].map(i => i.value).join(','),
      show_mode: $('e-show').value, review_when: $('e-review').value, review_at: $('e-review-at').value,
      allow_review: $('e-allow-review').value !== 'no',
      review_until: $('e-allow-review').value === 'until' ? $('e-review-until').value : '',
      shuffle_q: $('e-shuffle-q').value === '1', shuffle_opt: $('e-shuffle-opt').value === '1',
      published: $('e-published').checked, allowed_ips: $('e-ips').value, question_ids: picked,
      question_points: Object.fromEntries(picked.filter(id => pickedPoints[id] != null && pickedPoints[id] !== '').map(id => [id, +pickedPoints[id]])),
      focus_mode: $('e-focus').value, focus_limit: +$('e-focus-limit').value || 3,
      fullscreen: $('e-fullscreen').checked, block_copy: $('e-block-copy').checked,
    };
    try {
      if (editingExam) await call('PUT', '/api/admin/exams/' + editingExam, body);
      else await call('POST', '/api/admin/exams', body);
      $('exam-form').classList.add('hidden');
      loadExams();
    } catch (err) {
      say('exam-msg', err.message);
    }
  });

  // ── 考場監控 ──

  function openMonitor(id) {
    monitorExam = id;
    show('monitor');
    $('mon-csv').onclick = downloadCsv;
    refreshMonitor();
    clearInterval(monitorTimer);
    monitorTimer = setInterval(refreshMonitor, 5000);
  }

  async function downloadCsv(e) {
    e.preventDefault();
    const res = await fetch('/api/admin/exams/' + monitorExam + '/results.csv', { headers: { Authorization: 'Bearer ' + token } });
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'exam-' + monitorExam + '-results.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function refreshMonitor() {
    let data;
    try { data = await call('GET', '/api/admin/exams/' + monitorExam + '/monitor'); }
    catch (err) { return; }
    $('mon-title').textContent = data.exam.title;
    const count = s => data.rows.filter(r => r.status === s).length;
    const done = data.rows.filter(r => r.status === 'submitted');
    const avg = done.length ? (done.reduce((s, r) => s + r.score, 0) / done.length).toFixed(1) : '-';
    $('mon-summary').innerHTML =
      '<div><b>' + data.rows.length + '</b><span>應考人數</span></div>' +
      '<div><b>' + count('absent') + '</b><span>未進場</span></div>' +
      '<div><b style="color:var(--warn)">' + count('in_progress') + '</b><span>作答中</span></div>' +
      '<div><b style="color:var(--ok)">' + count('submitted') + '</b><span>已交卷</span></div>' +
      (data.rows.some(r => r.locked) ? '<div><b style="color:var(--ng)">' + data.rows.filter(r => r.locked).length + '</b><span>已鎖定，等你解鎖</span></div>' : '') +
      '<div><b>' + avg + '</b><span>平均分</span></div>';
    const status = { absent: '<span class="badge">未進場</span>', in_progress: '<span class="badge warn">作答中</span>', submitted: '<span class="badge ok">已交卷</span>' };
    const locked = '<span class="badge ng">已鎖定</span>';
    $('mon-table').innerHTML = '<tr><th>學號</th><th>姓名</th><th>狀態</th><th>進度</th><th>用時</th><th>得分</th><th>IP</th><th>電腦</th><th>切換視窗</th><th>注意</th><th></th></tr>' +
      data.rows.map(r => '<tr class="' + (r.flags.length ? 'flagged' : '') + '">' +
        '<td>' + esc(r.student_id) + '</td><td>' + nameCell(r) + '</td>' +
        '<td>' + (r.locked ? locked : status[r.status]) + '</td><td>' + (r.status === 'absent' ? '' : r.answered + ' / ' + r.total) + '</td>' +
        '<td>' + (r.attempt_id ? fmtSec(r.seconds) : '') + '</td>' +
        '<td>' + (r.score != null ? r.score + ' / ' + r.max_score : '') + '</td>' +
        '<td>' + esc(r.ip || '') + '</td><td>' + esc(r.device || '') + '</td><td>' + (r.tab_switches || '') + '</td>' +
        '<td>' + r.flags.map(esc).join('<br>') + '</td>' +
        '<td class="row">' + (r.locked ? '<button class="btn sm" data-unlock="' + r.attempt_id + '">解鎖</button>' : '') +
        (r.status === 'in_progress' ? '<button class="btn ghost sm" data-force="' + r.attempt_id + '">強制交卷</button>' : '') +
        (r.attempt_id ? '<button class="btn danger sm" data-reset="' + r.attempt_id + '" data-name="' + esc(r.name) + '">重考</button>' : '') +
        '</td></tr>').join('');
  }

  // 有作答的學生，姓名可點開個人作答紀錄
  function nameCell(r) {
    return r.attempt_id ? '<a href="#" class="link-cell" data-attempt="' + r.attempt_id + '" title="查看作答紀錄">' + esc(r.name) + '</a>' : esc(r.name);
  }

  $('mon-table').addEventListener('click', async e => {
    const t = e.target;
    if (t.dataset.attempt) { e.preventDefault(); openAttempt(+t.dataset.attempt, 'monitor'); return; }
    if (t.dataset.unlock) {
      await call('POST', '/api/admin/attempts/' + t.dataset.unlock + '/unlock');
      refreshMonitor();
    }
    if (t.dataset.force && confirm('確定要替這位學生交卷？')) {
      await call('POST', '/api/admin/attempts/' + t.dataset.force + '/force-submit');
      refreshMonitor();
    }
    if (t.dataset.reset && confirm('會刪除「' + t.dataset.name + '」這次的作答與成績，讓他重新考，確定嗎？')) {
      await call('DELETE', '/api/admin/attempts/' + t.dataset.reset);
      refreshMonitor();
    }
  });
  document.querySelectorAll('[data-back]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); openTab('exams'); }));

  // ── 成績統計 ──

  const SVG = 'http://www.w3.org/2000/svg';
  let scores = null;
  const sc = { unit: 'raw', bin: 10, pass: null, mode: 'all', a: null, b: null };

  $('mon-scores').addEventListener('click', loadScores);
  $('scores-refresh').addEventListener('click', loadScores);
  $('scores-back').addEventListener('click', e => { e.preventDefault(); openMonitor(monitorExam); });

  async function loadScores() {
    show('scores');
    scores = await call('GET', '/api/admin/exams/' + monitorExam + '/scores');
    $('scores-title').textContent = scores.exam.title + '：成績統計';
    if (sc.pass == null || sc.exam !== monitorExam) {
      sc.exam = monitorExam;
      sc.unit = 'raw';
      sc.pass = round1(scaleMax() * 0.6);
      sc.mode = 'all';
      sc.cut = null;
    }
    $('sc-unit').value = sc.unit;
    $('sc-pass').value = sc.pass;
    renderScores();
  }

  const round1 = x => Math.round(x * 10) / 10;
  const scaleMax = () => sc.unit === 'pct' ? 100 : scores.max_score;
  const value = r => sc.unit === 'pct' ? (scores.max_score ? r.score / scores.max_score * 100 : 0) : r.score;
  const fmtNum = x => x == null ? '-' : String(round1(x));

  function renderScores() {
    const s = scores.summary;
    const conv = x => x == null ? null : (sc.unit === 'pct' && scores.max_score ? x / scores.max_score * 100 : x);
    const passed = scores.rows.filter(r => value(r) >= sc.pass).length;
    $('sc-summary').innerHTML = [
      [s.count, '已交卷' + (s.in_progress || s.absent ? '（作答中 ' + s.in_progress + '、未進場 ' + s.absent + '）' : '')],
      [fmtNum(conv(s.mean)), '平均'], [fmtNum(conv(s.median)), '中位數'], [fmtNum(conv(s.sd)), '標準差'],
      [fmtNum(conv(s.max)) + ' / ' + fmtNum(conv(s.min)), '最高 / 最低'],
      [fmtNum(conv(s.q1)) + ' / ' + fmtNum(conv(s.q3)), 'Q1 / Q3'],
      [s.count ? Math.round(passed / s.count * 100) + '%' : '-', '及格率（' + passed + ' 人）'],
    ].map(([v, t]) => '<div><b>' + esc(String(v)) + '</b><span>' + esc(t) + '</span></div>').join('');
    renderHistogram();
    renderScoreTable();
    renderQuestionChart();
  }

  function bins() {
    const max = scaleMax(), w = sc.bin;
    const n = Math.max(1, Math.ceil(max / w));
    const out = [];
    for (let i = 0; i < n; i++) out.push({ a: i * w, b: Math.min((i + 1) * w, max), last: i === n - 1, rows: [] });
    scores.rows.forEach(r => {
      const i = Math.min(n - 1, Math.max(0, Math.floor(value(r) / w)));
      out[i].rows.push(r);
    });
    return out;
  }

  function el(tag, attrs, parent) {
    const node = document.createElementNS(SVG, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }

  // 上方圓角 4px、底部貼齊基線的長條
  function barPath(x, y, w, h) {
    const r = Math.min(4, w / 2, h);
    return 'M' + x + ',' + (y + h) + 'V' + (y + r) + 'Q' + x + ',' + y + ' ' + (x + r) + ',' + y +
      'H' + (x + w - r) + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) + 'V' + (y + h) + 'Z';
  }

  // 長條寬度約佔每格 60%，中間留空隙；題數少時不超過 40px
  const barWidth = slot => Math.max(2, Math.min(40, slot * .6));

  function niceStep(max, ticks) {
    const raw = max / ticks, p = Math.pow(10, Math.floor(Math.log10(raw)));
    return [1, 2, 5, 10].map(m => m * p).find(s => s >= raw);
  }

  function frame(box, W, H, m, yMax, yStep, yFmt) {
    box.innerHTML = '';
    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img' }, box);
    for (let v = 0; v <= yMax + 1e-9; v += yStep) {
      const y = m.t + (H - m.t - m.b) * (1 - v / yMax);
      el('line', { x1: m.l, x2: W - m.r, y1: y, y2: y, class: v === 0 ? 'axis' : 'grid' }, svg);
      el('text', { x: m.l - 6, y: y + 4, class: 'tick', 'text-anchor': 'end' }, svg).textContent = yFmt(v);
    }
    return svg;
  }

  function tip(e, html) {
    const t = $('sc-tip');
    if (!html) { t.classList.add('hidden'); return; }
    t.innerHTML = html;
    t.classList.remove('hidden');
    const x = Math.min(e.clientX + 14, window.innerWidth - t.offsetWidth - 8);
    t.style.left = x + 'px';
    t.style.top = (e.clientY + 14) + 'px';
  }

  function renderHistogram() {
    tip(null, null);
    const box = $('sc-hist');
    const bs = bins();
    const W = 760, H = 260, m = { t: 12, r: 12, b: 34, l: 40 };
    const top = Math.max(1, ...bs.map(b => b.rows.length));
    const step = Math.max(1, niceStep(top, 4));
    const yMax = Math.ceil(top / step) * step;
    const svg = frame(box, W, H, m, yMax, step, v => v);
    const pw = W - m.l - m.r, ph = H - m.t - m.b, max = scaleMax();
    const x = v => m.l + pw * v / max;
    bs.forEach(b => {
      const pad = (x(b.b) - x(b.a)) * .18;  // 長條之間留白，不要滿版
      const x0 = x(b.a) + pad, x1 = x(b.b) - pad, h = ph * b.rows.length / yMax;
      const below = b.last ? b.b < sc.pass : b.b <= sc.pass;
      const selected = sc.mode === 'range' && sc.a === b.a && sc.b === b.b;
      if (b.rows.length) el('path', { d: barPath(x0, m.t + ph - h, Math.max(1, x1 - x0), h), class: 'mark' + (below ? ' ng' : '') + (selected ? ' sel' : '') }, svg);
      if (b.rows.length) el('text', { x: (x0 + x1) / 2, y: m.t + ph - h - 4, class: 'val', 'text-anchor': 'middle' }, svg).textContent = b.rows.length;
      el('text', { x: (x0 + x1) / 2, y: H - m.b + 16, class: 'tick', 'text-anchor': 'middle' }, svg).textContent = fmtNum(b.a) + '–' + fmtNum(b.b);
      const hit = el('rect', { x: x(b.a), y: m.t, width: x(b.b) - x(b.a), height: ph, class: 'hit' }, svg);
      const range = fmtNum(b.a) + ' ≤ 分數 ' + (b.last ? '≤ ' : '< ') + fmtNum(b.b);
      hit.addEventListener('mousemove', e => tip(e, '<b>' + b.rows.length + ' 人</b><br>' + range + (b.rows.length ? '<br><span class="muted">點一下列出名單</span>' : '')));
      hit.addEventListener('mouseleave', e => tip(e, null));
      hit.addEventListener('click', () => {
        Object.assign(sc, { mode: 'range', a: b.a, b: b.b, last: b.last });
        renderHistogram();
        renderScoreTable();
        $('sc-table').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    });
    if (sc.pass > 0 && sc.pass <= max) {
      const px = x(sc.pass);
      el('line', { x1: px, x2: px, y1: m.t, y2: m.t + ph, class: 'pass' }, svg);
      el('text', { x: px + 4, y: m.t + 10, class: 'tick pass-label' }, svg).textContent = '及格 ' + fmtNum(sc.pass);
    }
    el('text', { x: m.l + pw / 2, y: H - 2, class: 'tick', 'text-anchor': 'middle' }, svg).textContent =
      sc.unit === 'pct' ? '百分制分數' : '分數（滿分 ' + fmtNum(max) + '）';
  }

  function filtered() {
    return scores.rows.filter(r => {
      const v = value(r);
      if (sc.mode === 'below') return v < sc.cut;
      if (sc.mode === 'atleast') return v >= sc.cut;
      if (sc.mode === 'range') return v >= sc.a && (sc.last ? v <= sc.b : v < sc.b);
      return true;
    });
  }

  function renderScoreTable() {
    $('sc-mode').value = sc.mode;
    $('sc-mode').querySelector('[value=range]').textContent = sc.mode === 'range' ? '區間 ' + fmtNum(sc.a) + '–' + fmtNum(sc.b) : '區間';
    $('sc-cut').classList.toggle('hidden', sc.mode === 'all' || sc.mode === 'range');
    if (sc.cut == null) sc.cut = sc.pass;
    $('sc-cut').value = sc.cut;
    const rows = filtered();
    $('sc-count').textContent = rows.length + ' / ' + scores.rows.length + ' 人';
    const pct = r => scores.max_score ? round1(r.score / scores.max_score * 100) : 0;
    $('sc-table').innerHTML = '<tr><th style="width:70px">名次</th><th>學號</th><th>姓名</th><th>分數</th><th>百分制</th><th>用時</th><th>交卷時間</th></tr>' +
      (rows.map(r => '<tr><td>' + r.rank + '</td><td>' + esc(r.student_id) + '</td><td>' + nameCell(r) + '</td>' +
        '<td class="' + (value(r) < sc.pass ? 'score-ng' : '') + '">' + fmtNum(r.score) + ' / ' + fmtNum(scores.max_score) + '</td>' +
        '<td>' + pct(r) + '</td><td>' + fmtSec(r.seconds) + '</td><td>' + esc(fmtTime(r.submitted_at)) + '</td></tr>').join('') ||
        '<tr><td colspan="7" class="muted">沒有符合的學生</td></tr>');
  }

  function renderQuestionChart() {
    tip(null, null);
    const box = $('sc-qchart');
    const qs = scores.questions;
    if (!qs.length) { box.innerHTML = '<p class="muted">這場考試沒有題目。</p>'; return; }
    const W = 760, H = 220, m = { t: 12, r: 12, b: 30, l: 40 };
    const svg = frame(box, W, H, m, 100, 25, v => v + '%');
    const pw = W - m.l - m.r, ph = H - m.t - m.b, slot = pw / qs.length;
    const every = Math.ceil(qs.length / 25);
    qs.forEach((q, i) => {
      const w = barWidth(slot), x0 = m.l + slot * i + (slot - w) / 2;
      const rate = q.rate || 0, h = ph * rate;
      if (q.rate != null && h > 0) el('path', { d: barPath(x0, m.t + ph - h, w, h), class: 'mark' }, svg);
      const last = i === qs.length - 1;
      if (last || (i % every === 0 && qs.length - 1 - i >= every / 2))
        el('text', { x: x0 + w / 2, y: H - m.b + 16, class: 'tick', 'text-anchor': 'middle' }, svg).textContent = i + 1;
      const hit = el('rect', { x: m.l + slot * i, y: m.t, width: slot, height: ph, class: 'hit' }, svg);
      const stem = q.stem.length > 60 ? q.stem.slice(0, 60) + '…' : q.stem;
      hit.addEventListener('mousemove', e => tip(e, '<b>第 ' + (i + 1) + ' 題　' + (q.rate == null ? '-' : Math.round(q.rate * 100) + '%') + '</b>' +
        '<span class="muted">（' + TYPE_TEXT[q.type] + '・' + q.points + ' 分）</span><br>' + esc(stem) +
        (q.avg_seconds != null ? '<br><span class="muted">平均停留 ' + fmtSec(q.avg_seconds) + '</span>' : '')));
      hit.addEventListener('mouseleave', e => tip(e, null));
      hit.addEventListener('click', e => { tip(e, null); loadStats(q.id); });
    });
    el('text', { x: m.l + pw / 2, y: H - 2, class: 'tick', 'text-anchor': 'middle' }, svg).textContent = '題號（依考卷順序）';
  }

  $('sc-unit').addEventListener('change', () => {
    const toPct = $('sc-unit').value === 'pct';
    const f = scores.max_score ? (toPct ? 100 / scores.max_score : scores.max_score / 100) : 1;
    sc.unit = $('sc-unit').value;
    sc.pass = round1(sc.pass * f);
    if (sc.cut != null) sc.cut = round1(sc.cut * f);
    if (sc.mode === 'range') sc.mode = 'all';
    $('sc-pass').value = sc.pass;
    renderScores();
  });
  $('sc-bin').addEventListener('change', () => {
    sc.bin = +$('sc-bin').value;
    if (sc.mode === 'range') sc.mode = 'all';
    renderHistogram();
    renderScoreTable();
  });
  $('sc-pass').addEventListener('input', () => {
    const v = parseFloat($('sc-pass').value);
    if (isNaN(v)) return;
    sc.pass = v;
    renderScores();
  });
  $('sc-mode').addEventListener('change', () => {
    sc.mode = $('sc-mode').value;
    renderHistogram();
    renderScoreTable();
  });
  $('sc-cut').addEventListener('input', () => {
    const v = parseFloat($('sc-cut').value);
    if (isNaN(v)) return;
    sc.cut = v;
    renderScoreTable();
  });
  $('sc-copy').addEventListener('click', async () => {
    const rows = filtered();
    const text = '學號\t姓名\t分數\n' + rows.map(r => r.student_id + '\t' + r.name + '\t' + r.score).join('\n');
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    say('sc-msg', '已複製 ' + rows.length + ' 人（可直接貼到 Excel）', true);
  });

  $('sc-table').addEventListener('click', e => {
    const t = e.target;
    if (t.dataset.attempt) { e.preventDefault(); openAttempt(+t.dataset.attempt, 'scores'); }
  });

  // ── 個人作答紀錄 ──

  let attemptFrom = 'monitor';
  const EVENT_TEXT = {
    start: ['開始作答', 'sys'], resume: ['重新進入考試頁', 'sys'], blur: ['離開考試畫面（切換視窗或分頁）', 'away'],
    focus: ['回到考試畫面', 'answer'], submit: ['交卷', 'sys'], auto_submit: ['時間到，系統自動交卷', 'sys'],
    force_submit: ['老師代為交卷', 'sys'], violation_submit: ['離開畫面超過次數，系統自動交卷', 'away'],
    lock: ['離開畫面超過次數，作答鎖定', 'away'], unlock: ['老師解鎖', 'sys'],
  };
  const clockText = iso => {
    const d = new Date(iso);
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
  };

  $('attempt-back').addEventListener('click', () => attemptFrom === 'scores' ? loadScores() : openMonitor(monitorExam));
  $('attempt-mon').addEventListener('click', e => { e.preventDefault(); openMonitor(monitorExam); });

  async function openAttempt(id, from) {
    attemptFrom = from;
    show('attempt');
    tip(null, null);
    $('attempt-from').innerHTML = from === 'scores' ? '成績統計 / ' : '';
    const d = await call('GET', '/api/admin/attempts/' + id + '/log');
    const a = d.attempt, qs = d.questions;
    $('at-title').textContent = d.student.name + '（' + d.student.student_id + '）：' + d.exam.title;
    const answered = qs.filter(q => q.response).length;
    const changes = qs.reduce((n, q) => n + Math.max(0, q.saves - 1), 0);
    const ended = d.timeline.filter(e => e.kind.endsWith('submit')).pop();
    $('at-summary').innerHTML = [
      [a.score != null ? fmtNum(a.score) + ' / ' + fmtNum(a.max_score) : '作答中', '得分'],
      [fmtSec(a.seconds), '用時（限時 ' + d.exam.duration_min + ' 分）'],
      [answered + ' / ' + qs.length, '已作答題數'],
      [changes, '改答次數'],
      [a.tab_switches, '離開畫面次數'],
      [ended ? EVENT_TEXT[ended.kind][0] : '尚未交卷', '交卷方式'],
      [(a.device || '-') + '　' + (a.ip || ''), '電腦　IP'],
    ].map(([v, t]) => '<div><b>' + esc(String(v)) + '</b><span>' + esc(t) + '</span></div>').join('');
    renderDwellChart(qs);
    const verdict = q => !q.response ? '<span class="badge gray">未作答</span>' : q.score >= q.points ? '<span class="badge ok">對</span>' :
      q.score > 0 ? '<span class="badge warn">部分</span>' : '<span class="badge ng">錯</span>';
    $('at-table').innerHTML = '<tr><th>題號</th><th>題型</th><th>題目</th><th>停留</th><th>全班平均</th><th>作答次數</th><th>最後答案</th><th>正解</th><th>得分</th></tr>' +
      qs.map(q => '<tr><td>' + q.no + '</td><td>' + TYPE_TEXT[q.type] + '</td>' +
        '<td title="' + esc(q.stem) + '">' + esc(q.stem.length > 28 ? q.stem.slice(0, 28) + '…' : q.stem) + '</td>' +
        '<td>' + fmtSec(q.seconds) + '</td><td class="muted">' + fmtSec(q.avg_seconds) + '</td>' +
        '<td>' + (q.saves > 1 ? '<b style="color:var(--warn)">' + q.saves + '</b>' : q.saves || '') + '</td>' +
        '<td>' + esc(q.response) + '</td><td class="muted">' + esc(q.answer) + '</td>' +
        '<td>' + verdict(q) + ' ' + (q.score != null ? fmtNum(q.score) : '') + '</td></tr>').join('');
    const t0 = new Date(a.started_at);
    $('at-timeline').innerHTML = '<tr><th style="width:90px">時間</th><th style="width:90px">經過</th><th>事件</th></tr>' +
      d.timeline.map(e => {
        const sec = Math.max(0, (new Date(e.at) - t0) / 1000);
        let text, cls;
        if (e.kind === 'answer') {
          text = '第 ' + e.no + ' 題' + (e.change ? '改答為 ' : '作答 ') + (e.detail || '（清空）');
          cls = e.change ? 'change' : 'answer';
        } else {
          [text, cls] = EVENT_TEXT[e.kind] || [e.kind, 'answer'];
          if (e.detail) text += '（' + e.detail + '）';
        }
        return '<tr><td>' + clockText(e.at) + '</td><td class="muted">' + clock(sec) + '</td>' +
          '<td class="timeline-ev ' + cls + '">' + esc(text) + '</td></tr>';
      }).join('');
  }

  function renderDwellChart(qs) {
    const box = $('at-chart');
    if (!qs.length) { box.innerHTML = '<p class="muted">這場考試沒有題目。</p>'; return; }
    const W = 760, H = 240, m = { t: 12, r: 12, b: 30, l: 48 };
    const top = Math.max(10, ...qs.map(q => Math.max(q.seconds, q.avg_seconds || 0)));
    // 刻度取整秒、整分，避免出現「1.7 分」
    const step = [10, 15, 20, 30, 60, 120, 180, 300, 600, 900].find(s => s * 4 >= top) || 1800;
    const yMax = Math.ceil(top / step) * step;
    const svg = frame(box, W, H, m, yMax, step, v => v < 60 ? v + ' 秒' : v % 60 ? Math.floor(v / 60) + ' 分 ' + v % 60 + ' 秒' : v / 60 + ' 分');
    const pw = W - m.l - m.r, ph = H - m.t - m.b, slot = pw / qs.length;
    const every = Math.ceil(qs.length / 25);
    qs.forEach((q, i) => {
      const w = barWidth(slot), x0 = m.l + slot * i + (slot - w) / 2, h = ph * q.seconds / yMax;
      const cls = !q.response ? ' empty' : q.score >= q.points ? '' : q.score > 0 ? ' part' : ' ng';
      if (h > 0) el('path', { d: barPath(x0, m.t + ph - h, w, h), class: 'mark' + cls }, svg);
      if (q.avg_seconds != null) {
        const y = m.t + ph * (1 - q.avg_seconds / yMax);
        el('line', { x1: x0, x2: x0 + w, y1: y, y2: y, class: 'avg' }, svg);
      }
      const last = i === qs.length - 1;
      if (last || (i % every === 0 && qs.length - 1 - i >= every / 2))
        el('text', { x: x0 + w / 2, y: H - m.b + 16, class: 'tick', 'text-anchor': 'middle' }, svg).textContent = q.no;
      const hit = el('rect', { x: m.l + slot * i, y: m.t, width: slot, height: ph, class: 'hit' }, svg);
      const stem = q.stem.length > 60 ? q.stem.slice(0, 60) + '…' : q.stem;
      hit.addEventListener('mousemove', e => tip(e, '<b>第 ' + q.no + ' 題　停留 ' + fmtSec(q.seconds) + '</b><br>' +
        '<span class="muted">全班平均 ' + fmtSec(q.avg_seconds) + '・作答 ' + q.saves + ' 次・' +
        (q.response ? '答 ' + esc(q.response) + '（' + fmtNum(q.score) + ' / ' + q.points + ' 分）' : '未作答') + '</span><br>' + esc(stem)));
      hit.addEventListener('mouseleave', e => tip(e, null));
    });
    el('text', { x: m.l + pw / 2, y: H - 2, class: 'tick', 'text-anchor': 'middle' }, svg).textContent = '題號（這位學生看到的順序）';
  }

  // ── 座位分析 ──

  let seats = null;
  const LEVEL_BADGE = ['<span class="badge gray">正常</span>', '<span class="badge warn">需要留意</span>', '<span class="badge ng">高度可疑</span>'];

  $('mon-seats').addEventListener('click', () => loadSeats());
  $('seats-back').addEventListener('click', e => { e.preventDefault(); openMonitor(monitorExam); });

  async function loadSeats() {
    show('seats');
    $('seats-title').textContent = $('mon-title').textContent + '：座位分析';
    $('seat-msg').textContent = '';
    $('seat-compare-card').classList.add('hidden');
    renderSeats(await call('GET', '/api/admin/exams/' + monitorExam + '/seats'));
  }

  $('seat-save').addEventListener('click', async () => {
    try {
      renderSeats(await call('PUT', '/api/admin/exams/' + monitorExam + '/seats', { seat_map: $('seat-map').value }));
      $('seat-compare-card').classList.add('hidden');
      say('seat-msg', '已儲存並重新分析', true);
    } catch (err) { say('seat-msg', err.message); }
  });
  $('seat-prev').addEventListener('click', () => { $('seat-map').value = seats.previous.seat_map; $('seat-save').click(); });
  $('seat-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(',')[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    try {
      renderSeats(await call('POST', '/api/admin/exams/' + monitorExam + '/seats/import', { data, filename: file.name }));
      $('seat-compare-card').classList.add('hidden');
      const n = seats.grid.flat().filter(c => c).length;
      say('seat-msg', '已匯入「' + file.name + '」：' + seats.grid.length + ' 排、' + n + ' 個座位', true);
    } catch (err) { say('seat-msg', err.message); }
  });

  function renderSeats(d) {
    seats = d;
    $('seat-map').value = d.seat_map;
    $('seat-devices').innerHTML = d.devices.length
      ? '這場考試記錄到的電腦代碼：<span style="font-family:var(--mono)">' + d.devices.map(esc).join('、') + '</span>' : '這場考試沒有學生設定電腦代碼，座位表請填學號。';
    $('seat-prev').classList.toggle('hidden', !d.previous || !!d.seat_map);
    $('seat-owner').innerHTML = d.owner ? '這是班級「<b>' + esc(d.owner) + '</b>」的座位表，在這裡修改會套用到這個班的每一場考試（也可以到「名單」頁編輯）。'
      : '這場考試開放給多個班級或不限班級，座位表只存在這場考試。';
    if (d.previous) $('seat-prev').textContent = '套用「' + d.previous.title + '」的座位表';
    $('seat-result').classList.toggle('hidden', !d.grid.length);
    if (!d.grid.length) return;
    renderSeatGrid(d);
    const flagged = d.pairs.filter(p => p.level).length;
    $('seat-count').textContent = d.pairs.length + ' 組相鄰' + (flagged ? '，其中 ' + flagged + ' 組需要注意' : '，沒有發現異常');
    $('seat-pairs').innerHTML = pairHead(true) + (d.pairs.map(p => pairRow(p, true)).join('') ||
      '<tr><td colspan="7" class="muted">座位表上沒有兩位相鄰、都已交卷的學生。</td></tr>');
    $('seat-others-card').classList.toggle('hidden', !d.others.length);
    $('seat-others').innerHTML = pairHead(false) + d.others.map(p => pairRow(p, false)).join('');
    const notes = [];
    if (d.unmatched.length) notes.push('座位表上沒有交卷紀錄的座位（' + d.unmatched.length + '）：' + d.unmatched.map(esc).join('、') + '（缺考、學號或代碼打錯，或同一台電腦被另一位學生使用）');
    if (d.unseated.length) notes.push('已交卷但不在座位表上：' + d.unseated.map(s => esc(s.name) + '（' + esc(s.student_id) + (s.device ? '，' + esc(s.device) : '') + '）').join('、'));
    $('seat-notes').innerHTML = notes.join('<br>');
  }

  function renderSeatGrid(d) {
    const R = d.grid.length, C = d.grid[0].length;
    const conn = {};
    d.pairs.forEach((p, i) => { if (p.level) conn[p.cells[0].join(',') + '|' + p.cells[1].join(',')] = { level: p.level, i }; });
    const cols = [];
    for (let c = 0; c < C; c++) { cols.push('auto'); if (c < C - 1) cols.push('14px'); }
    const html = [];
    for (let r = 0; r < R; r++) {
      for (let c = 0; c < C; c++) {
        html.push(seatCell(d.grid[r][c], r, c));
        if (c < C - 1) html.push(connector('h', conn[r + ',' + c + '|' + r + ',' + (c + 1)]));
      }
      if (r < R - 1) for (let c = 0; c < C; c++) {
        html.push(connector('v', conn[r + ',' + c + '|' + (r + 1) + ',' + c]));
        if (c < C - 1) html.push('<div></div>');
      }
    }
    $('seat-grid').style.gridTemplateColumns = cols.join(' ');
    $('seat-grid').innerHTML = html.join('');
  }

  function seatCell(cell, r, c) {
    if (!cell) return '<div class="seat aisle"></div>';
    const pos = 'data-cell="' + r + ',' + c + '"';
    if (!cell.student_id) return '<div class="seat unmatched" ' + pos + ' title="' + esc(cell.label) + '"><div class="pc">' + esc(cell.code) + '</div>' +
      '<div class="nm">' + esc(cell.label.slice(cell.code.length).trim() || '—') + '</div><div class="sc">未交卷</div></div>';
    return '<div class="seat' + (cell.level ? ' lv' + cell.level : '') + '" ' + pos + ' title="' + esc(cell.name + '（' + cell.student_id + '）') + '">' +
      '<div class="pc">' + esc(cell.code) + '</div><div class="nm">' + esc(cell.name) + '</div>' +
      '<div class="sc">' + fmtNum(cell.score) + ' / ' + fmtNum(cell.max_score) + '</div></div>';
  }

  function connector(dir, c) {
    return '<div class="conn ' + dir + (c ? ' lv' + c.level + '" data-pair="' + c.i + '" title="點一下看逐題比對' : '') + '"></div>';
  }

  function pairHead(adjacent) {
    return '<tr><th>判讀</th>' + (adjacent ? '<th>座位</th>' : '') + '<th>學生</th><th>錯成同一個答案</th><th>30 秒內作答相同</th><th>說明</th><th></th></tr>';
  }

  function pairRow(p, adjacent) {
    const who = s => esc(s.name) + ' <span class="muted">' + esc(s.student_id) + (s.device ? '・' + esc(s.device) : '') + '</span>';
    const idx = adjacent ? seats.pairs.indexOf(p) : -1;
    return '<tr class="pair-row"' + (adjacent ? ' data-row="' + idx + '"' : '') + '>' +
      '<td>' + LEVEL_BADGE[p.level] + '</td>' + (adjacent ? '<td>' + p.relation_text + '</td>' : '') +
      '<td>' + who(p.a) + '<br>' + who(p.b) + '</td>' +
      '<td><b>' + p.eeic + '</b> <span class="muted">/ 兩人都錯 ' + p.both_wrong + ' 題（碰巧約 ' + p.expected + '）</span></td>' +
      '<td><b>' + p.sync + '</b> <span class="muted">題</span></td>' +
      '<td style="white-space:normal;min-width:16rem">' + (p.reasons.map(esc).join('<br>') || '<span class="muted">在正常範圍內</span>') + '</td>' +
      '<td><button class="btn ghost sm" data-compare="' + esc(p.a.student_id) + '|' + esc(p.b.student_id) + '">逐題比對</button></td></tr>';
  }

  function highlight(cells) {
    document.querySelectorAll('.seat.hl').forEach(el => el.classList.remove('hl'));
    (cells || []).forEach(c => { const el = document.querySelector('[data-cell="' + c.join(',') + '"]'); if (el) el.classList.add('hl'); });
  }

  $('seat-pairs').addEventListener('mouseover', e => {
    const row = e.target.closest('[data-row]');
    highlight(row ? seats.pairs[+row.dataset.row].cells : null);
  });
  $('seat-pairs').addEventListener('mouseleave', () => highlight(null));
  $('seat-grid').addEventListener('click', e => {
    const c = e.target.closest('[data-pair]');
    if (c) { const p = seats.pairs[+c.dataset.pair]; compare(p.a.student_id, p.b.student_id, p.cells); }
  });
  ['seat-pairs', 'seat-others'].forEach(id => $(id).addEventListener('click', e => {
    const b = e.target.closest('[data-compare]');
    if (!b) return;
    const [a, bb] = b.dataset.compare.split('|');
    const row = b.closest('[data-row]');
    compare(a, bb, row ? seats.pairs[+row.dataset.row].cells : null);
  }));

  async function compare(a, b, cells) {
    highlight(cells);
    const d = await call('GET', '/api/admin/exams/' + monitorExam + '/compare?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b));
    $('seat-compare-title').textContent = '逐題比對：' + d.a.name + '（' + d.a.student_id + '）vs ' + d.b.name + '（' + d.b.student_id + '）';
    const ans = s => s.text ? esc(s.text) + (s.score != null ? ' <span class="muted">' + fmtNum(s.score) + '</span>' : '') : '<span class="muted">未作答</span>';
    $('seat-compare').innerHTML = '<tr><th>題號</th><th>題目</th><th>' + esc(d.a.name) + '</th><th>' + esc(d.b.name) + '</th><th>正解</th><th>作答時間差</th></tr>' +
      d.questions.map(q => '<tr class="' + (q.same_wrong ? 'same-wrong' : q.same && q.gap_seconds != null && q.gap_seconds <= 30 ? 'same-sync' : '') + '">' +
        '<td>' + q.no + '</td><td title="' + esc(q.stem) + '">' + esc(q.stem.length > 26 ? q.stem.slice(0, 26) + '…' : q.stem) + '</td>' +
        '<td>' + ans(q.a) + '</td><td>' + ans(q.b) + '</td><td class="muted">' + esc(q.answer) + '</td>' +
        '<td>' + (q.gap_seconds != null ? q.gap_seconds + ' 秒' : '') + '</td></tr>').join('');
    $('seat-compare-card').classList.remove('hidden');
    $('seat-compare-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ── 各題分析 ──

  $('mon-stats').addEventListener('click', () => loadStats());
  $('stats-back').addEventListener('click', e => { e.preventDefault(); openMonitor(monitorExam); });

  async function loadStats(focusId) {
    show('stats');
    $('stats-title').textContent = $('mon-title').textContent + '：各題分析';
    const stats = await call('GET', '/api/admin/exams/' + monitorExam + '/stats');
    $('stats-body').innerHTML = stats.map((q, n) => {
      const rate = q.correct_rate == null ? '-' : Math.round(q.correct_rate * 100) + '%';
      let detail = '';
      if (q.type === 'fill') {
        detail = q.fill_variants.map((vs, b) => '<h3>第 ' + (b + 1) + ' 格（正解：' + esc(q.answer[b].join(' 或 ')) + '）</h3><table>' +
          vs.map(v => '<tr><td>' + (v.text ? esc(v.text) : '<span class="muted">（空白）</span>') + '</td><td>' + v.count + ' 人</td><td>' +
            (v.accepted ? '<span class="badge ok">算對</span>' : v.text ?
              '<button class="btn ghost sm" data-accept="' + q.id + '" data-blank="' + b + '" data-text="' + esc(v.text) + '">算對</button>' : '') +
            '</td></tr>').join('') + '</table>').join('');
      } else {
        const max = Math.max(1, ...q.option_counts);
        detail = '<table class="stat-table">' + q.options.map((o, i) => '<tr><td style="width:40%">' + (q.answer.includes(i) ? '✓ ' : '') +
          LETTERS[i] + '. ' + esc(o) + '</td><td><div class="bar-track"><div class="bar' + (q.answer.includes(i) ? ' right' : '') +
          '" style="width:' + (q.option_counts[i] / max * 100) + '%"></div></div></td><td style="width:5rem">' + q.option_counts[i] + ' 人</td></tr>').join('') + '</table>';
      }
      return '<div class="card" id="stat-q' + q.id + '"><div class="q-head"><span class="q-num">' + (n + 1) + '.</span><span class="badge">' + TYPE_TEXT[q.type] + '</span>' +
        '<span class="muted">滿分率 ' + rate + '（' + q.full_marks + ' / ' + q.submitted + '）</span></div>' +
        '<div class="q-stem">' + esc(q.stem) + '</div>' + (q.code ? '<pre>' + esc(q.code) + '</pre>' : '') + detail + '</div>';
    }).join('') || '<p class="muted">這場考試沒有題目。</p>';
    const target = typeof focusId === 'number' && $('stat-q' + focusId);
    if (target) target.scrollIntoView({ block: 'start' });
  }

  $('stats-body').addEventListener('click', async e => {
    const t = e.target;
    if (!t.dataset.accept) return;
    if (!confirm('把「' + t.dataset.text + '」也算對，並重新計算所有考過這題的分數？')) return;
    await call('POST', '/api/admin/questions/' + t.dataset.accept + '/accept-fill',
      { question_id: +t.dataset.accept, blank: +t.dataset.blank, text: t.dataset.text });
    loadStats();
  });

  // ── 題庫 ──

  async function loadQuestions() {
    questions = await call('GET', '/api/admin/questions');
    renderQuestions();
  }

  function answerText(q) {
    if (q.type === 'fill') return q.answer.map(b => b.join('|')).join(';');
    return q.answer.map(i => LETTERS[i]).join(';');
  }

  function renderQuestions() {
    const f = $('q-filter').value;
    const list = questions.filter(q => qMatches(q, f));
    $('q-table').innerHTML = '<tr><th>#</th><th>題型</th><th>題幹</th><th>答案</th><th>分數</th><th>標籤</th><th></th></tr>' +
      list.map(q => '<tr><td>' + q.id + '</td><td>' + TYPE_TEXT[q.type] + (q.type !== 'multi' ? '' : q.multi_mode === 'partial' ? '（部分給分）' : q.multi_mode === 'weighted' ? '（倒扣）' : '') + '</td>' +
        '<td>' + esc(q.stem.slice(0, 100)) + '</td><td>' + esc(answerText(q)) + '</td><td>' + q.points + '</td>' +
        '<td>' + esc(q.tags) + '</td><td class="row"><button class="btn ghost sm" data-qedit="' + q.id + '">編輯</button>' +
        '<button class="btn danger sm" data-qdel="' + q.id + '">刪除</button></td></tr>').join('');
  }

  $('q-filter').addEventListener('input', renderQuestions);
  $('new-q').addEventListener('click', () => openQForm(null));
  $('cancel-q').addEventListener('click', () => $('q-form').classList.add('hidden'));
  $('q-type').addEventListener('change', syncQType);
  $('q-multi').addEventListener('change', syncQType);

  function syncQType() {
    const t = $('q-type').value;
    $('q-opt-wrap').classList.toggle('hidden', t === 'fill');
    $('q-multi-wrap').classList.toggle('hidden', t !== 'multi');
    $('q-weights-wrap').classList.toggle('hidden', t !== 'multi' || $('q-multi').value !== 'weighted');
    $('q-answer-label').textContent = {
      single: '正確答案（一個字母，例如 B）',
      multi: '正確答案（用分號分隔，例如 A;C）',
      fill: '每格答案（格與格用分號分隔，同一格多種寫法用 | 分隔，例如 臺北|台北;高雄）',
    }[t];
  }

  function openQForm(id) {
    editingQ = id;
    const q = id ? questions.find(x => x.id === id) : { type: 'single', stem: '', code: '', options: [], answer: [], explanation: '', points: 1, multi_mode: 'all', tags: '' };
    $('q-form-title').textContent = id ? '編輯題目 #' + id : '新增題目';
    $('q-type').value = q.type;
    $('q-points').value = q.points;
    $('q-multi').value = q.multi_mode;
    $('q-weights').value = (q.weights || []).map(w => +w.toFixed(4)).join(';');
    $('q-tags').value = q.tags;
    $('q-stem').value = q.stem;
    $('q-code').value = q.code;
    $('q-options').value = q.options.join('\n');
    $('q-answer').value = id ? answerText(q) : '';
    $('q-explain').value = q.explanation;
    $('q-form-msg').textContent = '';
    syncQType();
    $('q-form').classList.remove('hidden');
    $('q-form').scrollIntoView({ behavior: 'smooth' });
  }

  $('save-q').addEventListener('click', async () => {
    const type = $('q-type').value;
    const raw = $('q-answer').value;
    let answer;
    if (type === 'fill') answer = raw.split(';').map(b => b.split('|').map(s => s.trim()).filter(Boolean));
    else answer = raw.split(/[;,、\s]+/).filter(Boolean).map(s => LETTERS.indexOf(s.toUpperCase()));
    if (type !== 'fill' && answer.some(i => i < 0)) return say('q-form-msg', '答案請用 A～F 的字母');
    const body = {
      type, stem: $('q-stem').value, code: $('q-code').value,
      options: $('q-options').value.split('\n').map(s => s.trim()).filter(Boolean),
      answer, explanation: $('q-explain').value, points: +$('q-points').value,
      multi_mode: $('q-multi').value, tags: $('q-tags').value,
      weights: $('q-multi').value === 'weighted' ? $('q-weights').value.split(/[;,\s]+/).filter(Boolean).map(Number) : [],
    };
    try {
      if (editingQ) await call('PUT', '/api/admin/questions/' + editingQ, body);
      else await call('POST', '/api/admin/questions', body);
      $('q-form').classList.add('hidden');
      say('q-msg', editingQ ? '已更新，考過這題的分數已重新計算' : '已新增', true);
      loadQuestions();
    } catch (err) {
      say('q-form-msg', err.message);
    }
  });

  $('q-table').addEventListener('click', async e => {
    const t = e.target;
    if (t.dataset.qedit) openQForm(+t.dataset.qedit);
    if (t.dataset.qdel && confirm('確定刪除第 ' + t.dataset.qdel + ' 題？')) {
      try {
        await call('DELETE', '/api/admin/questions/' + t.dataset.qdel);
        loadQuestions();
      } catch (err) { say('q-msg', err.message); }
    }
  });

  $('q-file').addEventListener('change', async e => {
    try {
      const r = await call('POST', '/api/admin/questions/import', { csv: await readFile(e.target) });
      say('q-msg', '匯入 ' + r.count + ' 題', true);
      loadQuestions();
    } catch (err) { say('q-msg', err.message); }
    e.target.value = '';
  });

  // ── 名單與班級 ──

  let classes = { classes: [], unassigned: 0, total: 0 };
  let students = [];
  let currentClass = null;     // null = 全部、'' = 未分班、其他 = 班級名稱
  let editingStudent = null;

  async function loadStudents() {
    [classes, students] = await Promise.all([call('GET', '/api/admin/classes'), call('GET', '/api/admin/students')]);
    if (currentClass && !classes.classes.some(c => c.name === currentClass)) currentClass = null;
    renderClasses();
    renderStudents();
  }

  const classInfo = name => classes.classes.find(c => c.name === name);

  function renderClasses() {
    const tab = (value, label, n) => '<button data-class="' + esc(value) + '" class="' + (currentClass === value ? 'active' : '') + '">' +
      esc(label) + '<span class="n">' + n + '</span></button>';
    $('class-tabs').innerHTML = '<button data-class="__all" class="' + (currentClass === null ? 'active' : '') + '">全部<span class="n">' + classes.total + '</span></button>' +
      classes.classes.map(c => tab(c.name, c.name, c.students)).join('') +
      (classes.unassigned ? tab('', '未分班', classes.unassigned) : '');
    const c = currentClass ? classInfo(currentClass) : null;
    $('class-card').classList.toggle('hidden', !c);
    if (c) {
      $('class-title').textContent = c.name;
      $('class-info').textContent = c.students + ' 位學生・座位表' + (c.seats ? '已設定 ' + c.seats + ' 個座位' : '尚未設定') +
        '・開放給這班的考試 ' + c.exams + ' 場';
    } else {
      $('class-seats').classList.add('hidden');
    }
    $('s-class').innerHTML = '<option value="">（未分班）</option>' +
      classes.classes.map(c => '<option value="' + esc(c.name) + '">' + esc(c.name) + '</option>').join('');
    if (!editingStudent) $('s-class').value = currentClass || '';
    $('import-target').textContent = currentClass ? '→ 匯入到「' + currentClass + '」' : '→ 依檔案裡的班級欄（沒有就是未分班）';
    $('s-replace-text').textContent = currentClass ? '先清除「' + currentClass + '」的舊名單' : '先清除所有舊名單';
  }

  function renderStudents() {
    const q = $('s-search').value.trim().toLowerCase();
    const list = students.filter(s => (currentClass === null || s.class_name === currentClass) &&
      (!q || (s.student_id + ' ' + s.name).toLowerCase().includes(q)));
    const options = cls => '<option value="">（未分班）</option>' + classes.classes.map(c =>
      '<option value="' + esc(c.name) + '"' + (c.name === cls ? ' selected' : '') + '>' + esc(c.name) + '</option>').join('');
    $('s-table').innerHTML = '<tr><th>學號</th><th>姓名</th><th>班級</th><th>密碼</th><th></th></tr>' +
      (list.map(s => '<tr><td>' + esc(s.student_id) + '</td><td>' + esc(s.name) + '</td>' +
        '<td><select class="inline-select" data-move="' + esc(s.student_id) + '">' + options(s.class_name) + '</select></td>' +
        '<td>' + (s.has_password ? '<span class="badge ok">已設定</span>' : '<span class="badge gray">未設定</span>') + '</td>' +
        '<td class="row"><button class="btn ghost sm" data-sedit="' + esc(s.student_id) + '">編輯</button>' +
        (s.has_password ? '<button class="btn ghost sm" data-spw="' + esc(s.student_id) + '" data-name="' + esc(s.name) + '">重設密碼</button>' : '') +
        '<button class="btn danger sm" data-sdel="' + esc(s.student_id) + '">刪除</button></td></tr>').join('') ||
        '<tr><td colspan="5" class="muted">沒有學生</td></tr>');
    $('s-count').textContent = list.length + ' 人';
  }

  $('s-search').addEventListener('input', renderStudents);
  $('class-tabs').addEventListener('click', e => {
    const b = e.target.closest('[data-class]');
    if (!b) return;
    currentClass = b.dataset.class === '__all' ? null : b.dataset.class;
    $('class-seats').classList.add('hidden');
    cancelEdit();
    renderClasses();
    renderStudents();
  });

  $('new-class').addEventListener('click', async () => {
    const name = prompt('新班級名稱（例如 資管一甲）');
    if (!name) return;
    try {
      const r = await call('POST', '/api/admin/classes', { name });
      currentClass = r.name;
      await loadStudents();
      say('s-msg', '已新增「' + r.name + '」，可以匯入名單或逐一新增學生', true);
    } catch (err) { say('s-msg', err.message); }
  });

  $('class-rename').addEventListener('click', async () => {
    const name = prompt('把「' + currentClass + '」改名為', currentClass);
    if (!name || name === currentClass) return;
    try {
      const r = await call('PUT', '/api/admin/classes/' + encodeURIComponent(currentClass), { name });
      currentClass = r.name;
      await loadStudents();
      say('s-msg', '已改名；學生與考試的開放班級都一起更新', true);
    } catch (err) { say('s-msg', err.message); }
  });

  $('class-delete').addEventListener('click', async () => {
    const c = classInfo(currentClass);
    const ok = await ask('刪除班級「' + c.name + '」',
      '<p>這個班有 ' + c.students + ' 位學生' + (c.exams ? '，' + c.exams + ' 場考試開放給這個班（會改成不限班級，請記得重新設定）' : '') + '。</p>' +
      '<label class="check"><input type="radio" name="del-mode" value="keep" checked> 學生保留，移到「未分班」</label><br>' +
      '<label class="check"><input type="radio" name="del-mode" value="delete"> 連學生一起刪除（有作答紀錄的會保留在未分班）</label>',
      '刪除班級', '取消');
    if (!ok) return;
    const mode = document.querySelector('[name=del-mode]:checked').value;
    try {
      const r = await call('DELETE', '/api/admin/classes/' + encodeURIComponent(c.name) + '?students=' + mode);
      currentClass = null;
      await loadStudents();
      say('s-msg', '已刪除班級' + (r.deleted ? '，刪除 ' + r.deleted + ' 位學生' : '') + (r.unassigned ? '，' + r.unassigned + ' 位移到未分班' : ''), true);
    } catch (err) { say('s-msg', err.message); }
  });

  // 學生：新增／編輯（同一個表單）、改班、重設密碼、刪除
  function cancelEdit() {
    editingStudent = null;
    $('s-form-title').textContent = '新增學生';
    $('add-s').textContent = '新增';
    $('s-id').readOnly = false;
    $('s-id').value = $('s-name').value = '';
    $('cancel-s').classList.add('hidden');
    $('s-class').value = currentClass || '';
  }
  $('cancel-s').addEventListener('click', cancelEdit);

  $('add-s').addEventListener('click', async () => {
    try {
      await call('POST', '/api/admin/students', { student_id: $('s-id').value, name: $('s-name').value, class_name: $('s-class').value });
      say('s-msg', (editingStudent ? '已更新 ' : '已新增 ') + $('s-id').value.trim(), true);
      cancelEdit();
      loadStudents();
    } catch (err) { say('s-msg', err.message); }
  });

  $('s-table').addEventListener('change', async e => {
    const sid = e.target.dataset.move;
    if (!sid) return;
    const s = students.find(x => x.student_id === sid);
    try {
      await call('POST', '/api/admin/students', { student_id: sid, name: s.name, class_name: e.target.value });
      say('s-msg', s.name + ' 改到「' + (e.target.value || '未分班') + '」', true);
      loadStudents();
    } catch (err) { say('s-msg', err.message); }
  });

  $('s-table').addEventListener('click', async e => {
    const edit = e.target.dataset.sedit;
    if (edit) {
      const s = students.find(x => x.student_id === edit);
      editingStudent = edit;
      $('s-form-title').textContent = '編輯學生';
      $('add-s').textContent = '儲存';
      $('s-id').value = s.student_id;
      $('s-id').readOnly = true;
      $('s-name').value = s.name;
      $('s-class').value = s.class_name;
      $('cancel-s').classList.remove('hidden');
      $('s-name').focus();
      $('s-name').scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    const reset = e.target.dataset.spw;
    if (reset && confirm('清除「' + e.target.dataset.name + '」的密碼？清除後只輸入學號就能登入，學生可以再自己設定新密碼。')) {
      try { await call('POST', '/api/admin/students/' + encodeURIComponent(reset) + '/reset-password'); loadStudents(); }
      catch (err) { say('s-msg', err.message); }
      return;
    }
    const sid = e.target.dataset.sdel;
    if (sid && confirm('確定刪除學號 ' + sid + '？')) {
      try { await call('DELETE', '/api/admin/students/' + encodeURIComponent(sid)); loadStudents(); }
      catch (err) { say('s-msg', err.message); }
    }
  });

  function readBase64(input) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(new Error('讀檔失敗'));
      reader.readAsDataURL(input.files[0]);
    });
  }

  $('s-file').addEventListener('change', async e => {
    const f = e.target.files[0];
    if (!f) return;
    const opts = { replace: $('s-replace').checked, class_name: currentClass || '' };
    try {
      const r = /\.xlsx$/i.test(f.name)
        ? await call('POST', '/api/admin/students/import-xlsx', Object.assign({ data: await readBase64(e.target) }, opts))
        : await call('POST', '/api/admin/students/import', Object.assign({ csv: await readFile(e.target) }, opts));
      await loadStudents();
      say('s-msg', '匯入 ' + r.count + ' 人' + (currentClass ? '到「' + currentClass + '」' : ''), true);
    } catch (err) { say('s-msg', err.message); }
    e.target.value = '';
  });

  // 班級座位表
  $('class-seats-btn').addEventListener('click', async () => {
    renderClassSeats(await call('GET', '/api/admin/classes/' + encodeURIComponent(currentClass) + '/seats'));
    $('class-seat-msg').textContent = '';
    $('class-seats').classList.remove('hidden');
    $('class-seats').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  $('class-seat-close').addEventListener('click', () => $('class-seats').classList.add('hidden'));
  $('class-seat-save').addEventListener('click', async () => {
    try {
      renderClassSeats(await call('PUT', '/api/admin/classes/' + encodeURIComponent(currentClass) + '/seats', { seat_map: $('class-seat-map').value }));
      say('class-seat-msg', '已儲存', true);
      loadStudents();
    } catch (err) { say('class-seat-msg', err.message); }
  });
  $('class-seat-file').addEventListener('change', async e => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const d = await call('POST', '/api/admin/classes/' + encodeURIComponent(currentClass) + '/seats/import', { data: await readBase64(e.target), filename: f.name });
      renderClassSeats(d);
      const n = d.grid.flat().filter(x => x).length;
      say('class-seat-msg', '已匯入「' + f.name + '」：' + d.grid.length + ' 排、' + n + ' 個座位', true);
      loadStudents();
    } catch (err) { say('class-seat-msg', err.message); }
    e.target.value = '';
  });

  function renderClassSeats(d) {
    $('class-seats-name').textContent = d.name;
    $('class-seat-map').value = d.seat_map;
    const C = d.grid.length ? d.grid[0].length : 0;
    $('class-seat-grid').style.gridTemplateColumns = 'repeat(' + C + ', auto)';
    $('class-seat-grid').style.gap = '8px';
    $('class-seat-grid').innerHTML = d.grid.map(row => row.map(c => !c ? '<div class="seat aisle" style="width:92px"></div>' :
      '<div class="seat' + (c.known ? '' : ' unknown') + '" title="' + esc(c.label) + '"><div class="pc">' + esc(c.code) + '</div>' +
      '<div class="nm">' + esc(c.name || '—') + '</div>' + (c.known ? '' : '<div class="sc">名單上沒有</div>') + '</div>').join('')).join('');
    const notes = [];
    if (d.unknown.length) notes.push('<span style="color:var(--ng)">名單上找不到的學號（' + d.unknown.length + '）：' + d.unknown.map(esc).join('、') + '</span>');
    if (d.unseated.length) notes.push('名單上有、但沒排座位（' + d.unseated.length + '）：' + d.unseated.map(s => esc(s.name) + '（' + esc(s.student_id) + '）').join('、'));
    $('class-seat-notes').innerHTML = notes.join('<br>');
  }

  if (token) openTab('exams'); else show('login');
})();
