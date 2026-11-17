/* 學生端：輸入學號 → 考試清單 → 作答（每題自動存）→ 交卷看成績 */
(function () {
  'use strict';
  const { store, api, esc, LETTERS } = window.exam;
  const $ = id => document.getElementById(id);

  let token = store('exam_token');
  let student = JSON.parse(store('exam_student') || 'null');
  let pending = null;          // 輸入學號後、按「是我」前的登入結果
  let current = null;          // 作答中的考試
  let timerId = null;
  const queue = new Map();     // 尚未成功送出的作答：question_id → response
  let flushing = false;
  let saveTimer = null;

  function show(view) {
    ['login', 'list', 'take', 'result'].forEach(v => $('view-' + v).classList.toggle('hidden', v !== view));
    $('nav').classList.toggle('hidden', view === 'take');
    $('who').textContent = student ? student.name + '（' + student.student_id + '）' : '';
    $('logout').classList.toggle('hidden', !student || view === 'take');
    window.scrollTo(0, 0);
  }

  function call(method, path, body) {
    return api(method, path, body, token).catch(err => {
      if (err.status === 401) logout();
      throw err;
    });
  }

  function logout() {
    token = null; student = null;
    store('exam_token', null); store('exam_student', null);
    clearInterval(timerId);
    $('sid').value = '';
    $('confirm').classList.add('hidden');
    show('login');
  }

  // ── 輸入學號 ──

  $('login-form').addEventListener('submit', async e => {
    e.preventDefault();
    $('login-msg').textContent = '';
    const sid = $('sid').value.trim();
    if (!sid) return;
    try {
      pending = await api('POST', '/api/login', { student_id: sid });
      $('confirm-name').textContent = pending.student.name +
        (pending.student.class_name ? '（' + pending.student.class_name + '）' : '');
      $('confirm').classList.remove('hidden');
    } catch (err) {
      $('login-msg').textContent = err.message;
    }
  });
  $('confirm-yes').addEventListener('click', () => {
    token = pending.token; student = pending.student;
    store('exam_token', token); store('exam_student', JSON.stringify(student));
    loadList();
  });
  $('confirm-no').addEventListener('click', () => {
    pending = null;
    $('confirm').classList.add('hidden');
    $('sid').value = '';
    $('sid').focus();
  });
  $('logout').addEventListener('click', logout);

  // ── 考試清單 ──

  async function loadList() {
    show('list');
    $('list-msg').textContent = '';
    const box = $('exam-list');
    box.innerHTML = '<p class="muted">載入中…</p>';
    try {
      const exams = await call('GET', '/api/exams');
      if (!exams.length) {
        box.innerHTML = '<div class="card"><p class="muted">目前沒有開放給你的考試。</p></div>';
        return;
      }
      box.innerHTML = exams.map(ex => {
        let badge, action = '';
        if (ex.attempt && ex.attempt.submitted) {
          badge = '<span class="badge ok">已交卷</span>';
          action = '<button class="btn ghost" data-result="' + ex.attempt.id + '">查看成績</button>';
        } else if (ex.window === 'not_started') {
          badge = '<span class="badge">尚未開始</span>';
        } else if (ex.window === 'closed' && !ex.attempt) {
          badge = '<span class="badge ng">已結束</span>';
        } else {
          badge = ex.attempt ? '<span class="badge warn">作答中</span>' : '<span class="badge ok">開放中</span>';
          action = '<button class="btn" data-start="' + ex.id + '">' + (ex.attempt ? '繼續作答' : '開始考試') + '</button>';
        }
        return '<div class="card"><div class="row"><h2 style="margin:0">' + esc(ex.title) + '</h2>' + badge + '</div>' +
          '<p class="muted">' + ex.question_count + ' 題・限時 ' + ex.duration_min + ' 分鐘' +
          (ex.start_at ? '・開放 ' + esc(ex.start_at.replace('T', ' ')) : '') +
          (ex.end_at ? ' 至 ' + esc(ex.end_at.replace('T', ' ')) : '') + '</p>' + action + '</div>';
      }).join('');
    } catch (err) {
      box.innerHTML = '';
      $('list-msg').textContent = err.message;
    }
  }

  $('exam-list').addEventListener('click', e => {
    const start = e.target.closest('[data-start]');
    const result = e.target.closest('[data-result]');
    if (start) {
      if (!confirm('開始後就會計時，確定要開始嗎？')) return;
      startExam(+start.dataset.start);
    }
    if (result) loadResult(+result.dataset.result);
  });

  // ── 作答 ──

  async function startExam(examId) {
    try {
      const data = await call('POST', '/api/exams/' + examId + '/start', { device: store('exam_pc') || '' });
      current = data;
      current.deadline = Date.now() + data.remaining_seconds * 1000;
      current.answers = Object.assign({}, data.saved);
      renderExam();
      show('take');
      tick();
      clearInterval(timerId);
      timerId = setInterval(tick, 1000);
    } catch (err) {
      $('list-msg').textContent = err.message;
    }
  }

  function answered(q) {
    const r = current.answers[q.id];
    if (r == null) return false;
    if (q.type === 'single') return true;
    if (q.type === 'multi') return r.length > 0;
    return r.some(s => s && s.trim());
  }

  function renderExam() {
    $('take-title').textContent = current.title;
    $('nav-grid').innerHTML = current.questions.map((q, n) =>
      '<a href="#q' + q.id + '" id="nav' + q.id + '">' + (n + 1) + '</a>').join('');
    $('questions').innerHTML = current.questions.map((q, n) => {
      const saved = current.answers[q.id];
      let body = '';
      if (q.type === 'fill') {
        for (let b = 0; b < q.blanks; b++) {
          const v = saved && saved[b] != null ? saved[b] : '';
          body += '<div class="blank-row"><span>第 ' + (b + 1) + ' 格</span>' +
            '<input type="text" data-q="' + q.id + '" data-b="' + b + '" value="' + esc(v) + '" autocomplete="off"></div>';
        }
      } else {
        const kind = q.type === 'single' ? 'radio' : 'checkbox';
        body = q.options.map((o, k) => {
          const on = q.type === 'single' ? saved === o.i : Array.isArray(saved) && saved.includes(o.i);
          return '<label class="opt' + (on ? ' picked' : '') + '">' +
            '<input type="' + kind + '" name="q' + q.id + '" data-q="' + q.id + '" value="' + o.i + '"' + (on ? ' checked' : '') + '>' +
            '<span><b>' + LETTERS[k] + '.</b> ' + esc(o.text) + '</span></label>';
        }).join('');
      }
      const tag = { single: '單選', multi: '多選', fill: '填充' }[q.type];
      return '<div class="card q" id="q' + q.id + '"><div class="q-head"><span class="q-num">' + (n + 1) + '.</span>' +
        '<span class="badge' + (q.type === 'multi' ? ' warn' : '') + '">' + tag + '</span>' +
        '<span class="muted">' + q.points + ' 分</span></div>' +
        '<div class="q-stem">' + esc(q.stem) + '</div>' +
        (q.code ? '<pre>' + esc(q.code) + '</pre>' : '') + body + '</div>';
    }).join('');
    updateProgress();
  }

  function updateProgress() {
    let done = 0;
    current.questions.forEach(q => {
      const ok = answered(q);
      if (ok) done++;
      const nav = $('nav' + q.id);
      if (nav) nav.classList.toggle('done', ok);
    });
    $('progress').textContent = '已答 ' + done + ' / ' + current.questions.length;
    return current.questions.length - done;
  }

  $('questions').addEventListener('change', e => onAnswer(e.target));
  $('questions').addEventListener('input', e => { if (e.target.type === 'text') onAnswer(e.target); });
  $('questions').addEventListener('focusout', e => {
    if (e.target.type === 'text' && queue.size) { clearTimeout(saveTimer); flush(); }
  });

  function onAnswer(el) {
    const qid = +el.dataset.q;
    if (!qid) return;
    const q = current.questions.find(x => x.id === qid);
    let resp;
    if (q.type === 'single') {
      resp = +el.value;
    } else if (q.type === 'multi') {
      resp = [...document.querySelectorAll('input[name="q' + qid + '"]:checked')].map(i => +i.value);
    } else {
      resp = [...document.querySelectorAll('input[data-q="' + qid + '"]')].map(i => i.value);
    }
    if (q.type !== 'fill') {
      document.querySelectorAll('input[name="q' + qid + '"]').forEach(i =>
        i.closest('.opt').classList.toggle('picked', i.checked));
    }
    current.answers[qid] = resp;
    queue.set(qid, resp);
    updateProgress();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, q.type === 'fill' ? 800 : 200);
  }

  async function flush() {
    if (flushing || !current) return;
    flushing = true;
    const state = $('save-state');
    try {
      while (queue.size) {
        const [qid, resp] = queue.entries().next().value;
        state.textContent = '儲存中…';
        state.classList.remove('ng');
        await call('PUT', '/api/attempts/' + current.attempt_id + '/answers', { question_id: qid, response: resp });
        if (queue.get(qid) === resp) queue.delete(qid);
      }
      state.textContent = '已儲存';
    } catch (err) {
      if (err.status === 409) {
        state.textContent = err.message;
        finishExam();
      } else {
        state.textContent = '尚未儲存，自動重試中…';
        state.classList.add('ng');
        setTimeout(flush, 3000);
      }
    } finally {
      flushing = false;
    }
  }

  function tick() {
    if (!current) return;
    const left = Math.max(0, Math.round((current.deadline - Date.now()) / 1000));
    const m = Math.floor(left / 60), s = left % 60;
    $('timer').textContent = String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    $('timer').classList.toggle('low', left <= 300);
    if (left <= 0) {
      clearInterval(timerId);
      finishExam(true);
    }
  }

  async function submit() {
    const missing = updateProgress();
    const text = missing ? '還有 ' + missing + ' 題沒作答，確定要交卷嗎？' : '確定要交卷嗎？交卷後就不能修改。';
    if (!confirm(text)) return;
    finishExam();
  }
  $('submit-btn').addEventListener('click', submit);
  $('submit-btn2').addEventListener('click', submit);

  async function finishExam(timeUp) {
    if (!current || current.finishing) return;
    current.finishing = true;
    clearInterval(timerId);
    clearTimeout(saveTimer);
    $('save-state').textContent = timeUp ? '時間到，交卷中…' : '交卷中…';
    // 先把還沒送出的作答送完（最多重試幾次），再交卷
    for (let i = 0; i < 5 && queue.size; i++) {
      flushing = false;
      await flush();
      if (queue.size) await new Promise(r => setTimeout(r, 1500));
    }
    const attemptId = current.attempt_id;
    for (let i = 0; i < 10; i++) {
      try {
        const result = await call('POST', '/api/attempts/' + attemptId + '/submit');
        current = null;
        renderResult(result);
        return;
      } catch (err) {
        if (!err.network) { current = null; loadList(); return; }
        $('save-state').textContent = '網路不穩，交卷重試中…（時間到伺服器也會用已存的答案自動交卷）';
        await new Promise(r => setTimeout(r, 3000));
      }
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden && current && !current.finishing) {
      call('POST', '/api/attempts/' + current.attempt_id + '/event', { type: 'blur' }).catch(() => {});
    }
  });
  // 關分頁、切 App 時把還沒送出的作答用 keepalive 送出，瀏覽器關掉也會送完
  window.addEventListener('pagehide', () => {
    if (!current || !queue.size) return;
    queue.forEach((resp, qid) => {
      fetch('/api/attempts/' + current.attempt_id + '/answers', {
        method: 'PUT', keepalive: true,
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ question_id: qid, response: resp }),
      }).catch(() => {});
    });
  });
  window.addEventListener('beforeunload', e => {
    if (current && queue.size) { e.preventDefault(); e.returnValue = ''; }
  });

  // ── 成績 ──

  async function loadResult(attemptId) {
    try {
      renderResult(await call('GET', '/api/attempts/' + attemptId + '/result'));
    } catch (err) {
      $('list-msg').textContent = err.message;
    }
  }

  function showAnswer(q, value) {
    if (value == null) return '（未作答）';
    if (q.type === 'single') return LETTERS[value];
    if (q.type === 'multi') return value.length ? value.map(i => LETTERS[i]).join('、') : '（未作答）';
    return value.map(v => Array.isArray(v) ? v.join(' 或 ') : (v || '（空白）')).join('；');
  }

  function renderResult(r) {
    show('result');
    $('result-title').textContent = r.title;
    $('result-score').textContent = r.score + ' / ' + r.max_score;
    $('result-note').textContent = r.show_mode === 'score' ? '老師設定只顯示總分。' : '';
    $('result-detail').innerHTML = r.questions.map((q, n) => {
      const full = q.score >= q.points;
      const badge = full ? '<span class="badge ok">答對</span>' :
        q.score > 0 ? '<span class="badge warn">部分給分</span>' : '<span class="badge ng">答錯</span>';
      let opts = '';
      if (q.type !== 'fill') {
        opts = q.options.map((o, i) => {
          const right = q.answer.includes(i);
          const picked = q.type === 'single' ? q.response === i : (q.response || []).includes(i);
          return '<div class="opt' + (right ? ' right' : picked ? ' wrong' : '') + '"><span><b>' + LETTERS[i] + '.</b> ' + esc(o) +
            (right ? ' ✓' : '') + (picked ? '（你選的）' : '') + '</span></div>';
        }).join('');
      }
      return '<div class="card"><div class="q-head"><span class="q-num">' + (n + 1) + '.</span>' + badge +
        '<span class="muted">' + q.score + ' / ' + q.points + ' 分</span></div>' +
        '<div class="q-stem">' + esc(q.stem) + '</div>' + (q.code ? '<pre>' + esc(q.code) + '</pre>' : '') + opts +
        '<p class="muted">你的答案：' + esc(showAnswer(q, q.response)) + '　正確答案：' + esc(showAnswer(q, q.answer)) + '</p>' +
        (q.explanation ? '<p><b>解析：</b>' + esc(q.explanation) + '</p>' : '') + '</div>';
    }).join('');
  }
  $('back-list').addEventListener('click', loadList);

  if (token && student) loadList(); else show('login');
})();
