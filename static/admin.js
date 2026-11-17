/* 老師後台：考試設定、考場監控、各題分析、題庫、名單 */
(function () {
  'use strict';
  const { store, api, esc, LETTERS } = window.exam;
  const $ = id => document.getElementById(id);

  let token = store('exam_admin_token');
  let questions = [];
  let editingExam = null;
  let picked = [];
  let editingQ = null;
  let monitorExam = null;
  let monitorTimer = null;
  const TYPE_TEXT = { single: '單選', multi: '多選', fill: '填充' };

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
    ['login', 'exams', 'monitor', 'stats', 'questions', 'students'].forEach(v =>
      $('view-' + v).classList.toggle('hidden', v !== view));
    $('tabs').classList.toggle('hidden', view === 'login');
    $('logout').classList.toggle('hidden', view === 'login');
    if (view !== 'monitor') clearInterval(monitorTimer);
    const tab = { monitor: 'exams', stats: 'exams' }[view] || view;
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
    $('exam-table').innerHTML = '<tr><th>名稱</th><th>狀態</th><th>題數</th><th>作答時間</th><th>開放 ～ 關閉</th><th>打散</th><th>已進場</th><th></th></tr>' +
      exams.map(e => '<tr><td><b>' + esc(e.title) + '</b></td>' +
        '<td>' + (e.published ? WINDOW[e.window] : '<span class="badge gray">學生看不到</span>') + '</td>' +
        '<td>' + e.question_count + '</td><td>' + e.duration_min + ' 分鐘</td>' +
        '<td class="nowrap">' + (when(e.start_at) || '立即') + ' ～ ' + (when(e.end_at) || '不限') + '</td>' +
        '<td class="nowrap">' + [e.shuffle_q ? '題目' : '', e.shuffle_opt ? '選項' : ''].filter(Boolean).join('、') + '</td>' +
        '<td>' + e.attempt_count + '</td>' +
        '<td class="row"><button class="btn sm" data-monitor="' + e.id + '">監控／成績</button>' +
        '<button class="btn ghost sm" data-edit="' + e.id + '">編輯</button>' +
        '<button class="btn danger sm" data-del="' + e.id + '">刪除</button></td></tr>').join('');
    if (!exams.length) $('exam-table').innerHTML += '<tr><td colspan="8" class="muted">還沒有考試，先到「題庫」匯入題目，再回來新增考試。</td></tr>';
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
    questions = await call('GET', '/api/admin/questions');
    editingExam = id;
    const e = id ? await call('GET', '/api/admin/exams/' + id) : {
      title: '', duration_min: 50, start_at: '', end_at: '', class_names: '', show_mode: 'score',
      review_when: 'immediate', review_at: '', allow_review: 1, review_until: '', shuffle_q: 1, shuffle_opt: 1, published: 0, allowed_ips: '', question_ids: [],
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
    $('e-ips').value = e.allowed_ips;
    $('e-filter').value = '';
    picked = e.question_ids.slice();
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
    const pts = picked.reduce((s, id) => s + ((questions.find(q => q.id === id) || {}).points || 0), 0);
    $('e-picked').textContent = '已選 ' + picked.length + ' 題，共 ' + pts + ' 分';
  }

  $('e-filter').addEventListener('input', renderPicker);
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
      end_at: $('e-end-on').checked ? $('e-end').value : '', class_names: '',
      show_mode: $('e-show').value, review_when: $('e-review').value, review_at: $('e-review-at').value,
      allow_review: $('e-allow-review').value !== 'no',
      review_until: $('e-allow-review').value === 'until' ? $('e-review-until').value : '',
      shuffle_q: $('e-shuffle-q').value === '1', shuffle_opt: $('e-shuffle-opt').value === '1',
      published: $('e-published').checked, allowed_ips: $('e-ips').value, question_ids: picked,
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
      '<div><b>' + avg + '</b><span>平均分</span></div>';
    const status = { absent: '<span class="badge">未進場</span>', in_progress: '<span class="badge warn">作答中</span>', submitted: '<span class="badge ok">已交卷</span>' };
    $('mon-table').innerHTML = '<tr><th>學號</th><th>姓名</th><th>狀態</th><th>進度</th><th>得分</th><th>IP</th><th>電腦</th><th>切換視窗</th><th>注意</th><th></th></tr>' +
      data.rows.map(r => '<tr class="' + (r.flags.length ? 'flagged' : '') + '">' +
        '<td>' + esc(r.student_id) + '</td><td>' + esc(r.name) + '</td>' +
        '<td>' + status[r.status] + '</td><td>' + (r.status === 'absent' ? '' : r.answered + ' / ' + r.total) + '</td>' +
        '<td>' + (r.score != null ? r.score + ' / ' + r.max_score : '') + '</td>' +
        '<td>' + esc(r.ip || '') + '</td><td>' + esc(r.device || '') + '</td><td>' + (r.tab_switches || '') + '</td>' +
        '<td>' + r.flags.map(esc).join('<br>') + '</td>' +
        '<td class="row">' + (r.status === 'in_progress' ? '<button class="btn ghost sm" data-force="' + r.attempt_id + '">強制交卷</button>' : '') +
        (r.attempt_id ? '<button class="btn danger sm" data-reset="' + r.attempt_id + '" data-name="' + esc(r.name) + '">重考</button>' : '') +
        '</td></tr>').join('');
  }

  $('mon-table').addEventListener('click', async e => {
    const t = e.target;
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

  // ── 各題分析 ──

  $('mon-stats').addEventListener('click', loadStats);
  $('stats-back').addEventListener('click', e => { e.preventDefault(); openMonitor(monitorExam); });

  async function loadStats() {
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
      return '<div class="card"><div class="q-head"><span class="q-num">' + (n + 1) + '.</span><span class="badge">' + TYPE_TEXT[q.type] + '</span>' +
        '<span class="muted">滿分率 ' + rate + '（' + q.full_marks + ' / ' + q.submitted + '）</span></div>' +
        '<div class="q-stem">' + esc(q.stem) + '</div>' + (q.code ? '<pre>' + esc(q.code) + '</pre>' : '') + detail + '</div>';
    }).join('') || '<p class="muted">這場考試沒有題目。</p>';
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
      list.map(q => '<tr><td>' + q.id + '</td><td>' + TYPE_TEXT[q.type] + (q.type === 'multi' && q.multi_mode === 'partial' ? '（部分給分）' : '') + '</td>' +
        '<td>' + esc(q.stem.slice(0, 100)) + '</td><td>' + esc(answerText(q)) + '</td><td>' + q.points + '</td>' +
        '<td>' + esc(q.tags) + '</td><td class="row"><button class="btn ghost sm" data-qedit="' + q.id + '">編輯</button>' +
        '<button class="btn danger sm" data-qdel="' + q.id + '">刪除</button></td></tr>').join('');
  }

  $('q-filter').addEventListener('input', renderQuestions);
  $('new-q').addEventListener('click', () => openQForm(null));
  $('cancel-q').addEventListener('click', () => $('q-form').classList.add('hidden'));
  $('q-type').addEventListener('change', syncQType);

  function syncQType() {
    const t = $('q-type').value;
    $('q-opt-wrap').classList.toggle('hidden', t === 'fill');
    $('q-multi-wrap').classList.toggle('hidden', t !== 'multi');
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

  // ── 名單 ──

  async function loadStudents() {
    const list = await call('GET', '/api/admin/students');
    $('s-table').innerHTML = '<tr><th>學號</th><th>姓名</th><th>密碼</th><th></th></tr>' +
      list.map(s => '<tr><td>' + esc(s.student_id) + '</td><td>' + esc(s.name) + '</td>' +
        '<td>' + (s.has_password ? '<span class="badge ok">已設定</span>' : '<span class="badge gray">未設定</span>') + '</td>' +
        '<td class="row">' + (s.has_password ? '<button class="btn ghost sm" data-spw="' + esc(s.student_id) + '" data-name="' + esc(s.name) + '">重設密碼</button>' : '') +
        '<button class="btn danger sm" data-sdel="' + esc(s.student_id) + '">刪除</button></td></tr>').join('');
    say('s-msg', '共 ' + list.length + ' 人', true);
  }

  $('add-s').addEventListener('click', async () => {
    try {
      await call('POST', '/api/admin/students', { student_id: $('s-id').value, name: $('s-name').value });
      $('s-id').value = $('s-name').value = '';
      loadStudents();
    } catch (err) { say('s-msg', err.message); }
  });

  $('s-table').addEventListener('click', async e => {
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
    const opts = { replace: $('s-replace').checked };
    try {
      const r = /\.xlsx$/i.test(f.name)
        ? await call('POST', '/api/admin/students/import-xlsx', Object.assign({ data: await readBase64(e.target) }, opts))
        : await call('POST', '/api/admin/students/import', Object.assign({ csv: await readFile(e.target) }, opts));
      await loadStudents();
      say('s-msg', '匯入 ' + r.count + ' 人', true);
    } catch (err) { say('s-msg', err.message); }
    e.target.value = '';
  });

  if (token) openTab('exams'); else show('login');
})();
