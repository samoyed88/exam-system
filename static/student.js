/* 學生端：輸入學號 → 考試清單 → 作答（每題自動存）→ 作答摘要 → 交卷檢視 */
(function () {
  'use strict';
  const { store, api, esc, fmtDateTime, fmtSpan, clock, ask, LETTERS } = window.exam;
  const $ = id => document.getElementById(id);
  const $$ = sel => document.querySelectorAll(sel);

  let token = store('exam_token');
  let student = JSON.parse(store('exam_student') || 'null');
  let pending = null;          // 輸入學號後、按「是我」前的登入結果
  let current = null;          // 作答中的考試
  let timerId = null;
  let listTimer = null;
  let clockOffset = 0;         // 伺服器時間 − 本機時間，倒數用伺服器時間為準
  let observer = null;
  const queue = new Map();     // 尚未成功送出的作答：question_id → response
  let flushing = false;
  let saveTimer = null;
  let viewing = null;          // 畫面中央的題目 id，用來累計每題停留時間
  let dwell = {};              // 尚未回報的停留秒數：question_id → 秒
  let dwellTicks = 0;
  let away = false;            // 目前是否離開考試畫面（切視窗、切分頁、離開全螢幕），避免重複計次
  let guardAction = null;      // 遮罩按鈕要做的事
  const TYPE_TEXT = { single: '單選題', multi: '多選題', fill: '填充題' };
  // 管理員模擬作答：/?preview=考試編號，用後台登入取題與評分，不寫入任何作答紀錄
  const previewExam = new URLSearchParams(location.search).get('preview');
  const adminToken = previewExam ? store('exam_admin_token') : null;
  let previewSeed = '';
  const PROMPT = { single: '請選擇一個答案：', multi: '可選擇多個答案：', fill: '請在空格中填入答案：' };

  function show(view) {
    ['login', 'list', 'take', 'summary', 'result'].forEach(v => $('view-' + v).classList.toggle('hidden', v !== view));
    const busy = view === 'take' || view === 'summary';
    $('who').innerHTML = previewExam ? '<span class="avatar">預覽</span> 管理員' :
      student ? '<span class="avatar">' + esc(student.name.slice(-2)) + '</span> ' +
      esc(student.name) + '（' + esc(student.student_id) + '）' : '';
    $('logout').classList.toggle('hidden', !student || busy);
    $('chpw').classList.toggle('hidden', !student || busy);
    if (view !== 'list') clearInterval(listTimer);
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
    resetLoginForm();
    show('login');
  }

  function resetLoginForm() {
    $('confirm').classList.add('hidden');
    $('login-form').classList.remove('hidden');
    $('pw-row').classList.add('hidden');
    $('spw').value = '';
    $('sid').readOnly = false;
    $('login-btn').textContent = '下一步';
  }

  function enter(data) {
    token = data.token; student = data.student;
    store('exam_token', token); store('exam_student', JSON.stringify(student));
    loadList();
  }

  document.addEventListener('click', e => {
    if (e.target.closest('[data-to-list]')) {
      e.preventDefault();
      if (previewExam) { location.href = '/admin#exam/' + previewExam; return; }
      if (!current) loadList();
    }
  });

  // ── 輸入學號 ──

  $('login-form').addEventListener('submit', async e => {
    e.preventDefault();
    $('login-msg').textContent = '';
    const sid = $('sid').value.trim();
    if (!sid) return;
    const withPw = !$('pw-row').classList.contains('hidden');
    try {
      pending = await api('POST', '/api/login', { student_id: sid, password: withPw ? $('spw').value : '' });
      if (pending.need_password) {
        $('pw-row').classList.remove('hidden');
        $('sid').readOnly = true;
        $('login-btn').textContent = '登入';
        $('spw').focus();
        return;
      }
      if (withPw) return enter(pending);   // 密碼已驗證，不用再確認姓名
      $('confirm-name').textContent = pending.student.name;
      $('confirm').classList.remove('hidden');
      $('login-form').classList.add('hidden');
      $('confirm-yes').focus();
    } catch (err) {
      $('login-msg').textContent = err.message;
      if (withPw) { $('spw').value = ''; $('spw').focus(); }
    }
  });
  $('sid').addEventListener('click', () => {
    // 輸入密碼階段點學號欄 = 想換學號
    if ($('sid').readOnly) { resetLoginForm(); $('login-msg').textContent = ''; }
  });
  $('confirm-yes').addEventListener('click', () => enter(pending));
  $('confirm-no').addEventListener('click', () => {
    pending = null;
    resetLoginForm();
    $('sid').value = '';
    $('sid').focus();
  });

  // ── 密碼 ──

  function renderPwBanner() {
    $('pw-banner').innerHTML = student && !student.has_password
      ? '<div class="alert warn row" style="justify-content:space-between">' +
        '<span>你還沒設定密碼，知道你學號的人都能用你的身分登入。</span>' +
        '<button class="btn sm" data-setpw>設定密碼</button></div>'
      : '';
  }

  async function changePassword() {
    const has = student.has_password;
    const ok = await ask(has ? '修改密碼' : '設定密碼',
      (has ? '<label for="pw-old">目前的密碼</label><input type="password" id="pw-old" autocomplete="current-password">' : '') +
      '<label for="pw-new">新密碼（4 個字元以上）</label><input type="password" id="pw-new" autocomplete="new-password">' +
      '<label for="pw-new2">再輸入一次新密碼</label><input type="password" id="pw-new2" autocomplete="new-password">' +
      '<p class="hint" style="margin-top:.75rem">設定後登入要輸入學號和密碼。忘記密碼請找老師重設。</p>',
      '儲存', '取消');
    if (!ok) return;
    const oldPw = has ? $('pw-old').value : '';
    const pw = $('pw-new').value, pw2 = $('pw-new2').value;
    let msg;
    if (pw !== pw2) msg = '兩次輸入的新密碼不一樣。';
    else {
      try {
        const r = await call('POST', '/api/me/password', { old_password: oldPw, new_password: pw });
        token = r.token;  // 舊 token 隨密碼失效
        store('exam_token', token);
        student.has_password = true;
        store('exam_student', JSON.stringify(student));
        renderPwBanner();
        msg = '密碼已設定，下次登入請輸入學號和密碼。';
      } catch (err) { msg = err.message; }
    }
    await ask(has ? '修改密碼' : '設定密碼', '<p>' + esc(msg) + '</p>', '確定', null);
  }
  $('chpw').addEventListener('click', changePassword);
  document.addEventListener('click', e => { if (e.target.closest('[data-setpw]')) changePassword(); });
  $('logout').addEventListener('click', logout);

  // ── 考試清單 ──

  const serverNow = () => Date.now() + clockOffset;
  let exams = [];

  async function loadList() {
    show('list');
    renderPwBanner();
    $('list-msg').textContent = '';
    const box = $('exam-list');
    box.innerHTML = '<p class="muted">載入中…</p>';
    try {
      const data = await call('GET', '/api/exams');
      clockOffset = Date.parse(data.server_now) - Date.now();
      exams = data.exams;
      renderList();
      clearInterval(listTimer);
      listTimer = setInterval(tickList, 1000);
    } catch (err) {
      box.innerHTML = '';
      $('list-msg').textContent = err.message;
    }
  }

  function examStatus(ex) {
    const now = serverNow();
    if (ex.attempt && ex.attempt.submitted) {
      return ex.release_at && now < Date.parse(ex.release_at) ? 'submitted_wait' : 'submitted';
    }
    if (ex.attempt) return 'in_progress';
    if (ex.start_at && now < Date.parse(ex.start_at)) return 'not_started';
    if (ex.end_at && now > Date.parse(ex.end_at)) return 'closed';
    return 'open';
  }

  function statusLine(ex) {
    const now = serverNow();
    const st = examStatus(ex);
    if (st === 'not_started') return '距離開放還有 <b class="countdown">' + fmtSpan((Date.parse(ex.start_at) - now) / 1000) + '</b>';
    if (st === 'open' && ex.end_at) return '關閉前還有 <b class="countdown">' + fmtSpan((Date.parse(ex.end_at) - now) / 1000) + '</b> 可以進場';
    if (st === 'in_progress') return '你已經開始作答，計時仍在進行，請盡快繼續。';
    if (st === 'closed') return '考試已關閉，你沒有作答紀錄。';
    if (st === 'submitted_wait') {
      return '已交卷，成績將於 ' + fmtDateTime(ex.release_at) + ' 公布（還有 <b class="countdown">' +
        fmtSpan((Date.parse(ex.release_at) - now) / 1000) + '</b>）。';
    }
    if (st === 'submitted') {
      if (!ex.allow_review) return '已交卷，可以查看總分。';
      if (ex.review_until && now > Date.parse(ex.review_until)) return '已交卷，複習期限已過，只能查看總分。';
      if (ex.review_until) return '已交卷，可以複習作答到 ' + fmtDateTime(ex.review_until) + '。';
      return '已交卷，可以查看成績與複習作答。';
    }
    return '';
  }

  function renderList() {
    const box = $('exam-list');
    if (!exams.length) {
      box.innerHTML = '<div class="card"><p class="muted" style="margin:0">目前沒有開放給你的考試。</p></div>';
      return;
    }
    const BADGE = {
      not_started: '<span class="badge gray">尚未開放</span>', open: '<span class="badge ok">開放中</span>',
      in_progress: '<span class="badge warn">作答中</span>', submitted: '<span class="badge">已交卷</span>',
      submitted_wait: '<span class="badge">已交卷・待公布</span>',
      closed: '<span class="badge ng">已關閉</span>',
    };
    box.innerHTML = exams.map(ex => {
      const st = examStatus(ex);
      let action = '';
      if (st === 'open') action = '<button class="btn" data-start="' + ex.id + '">開始作答</button>';
      if (st === 'in_progress') action = '<button class="btn" data-start="' + ex.id + '">繼續作答</button>';
      if (st === 'submitted') action = '<button class="btn ghost" data-result="' + ex.attempt.id + '">查看成績</button>';
      if (st === 'submitted_wait') action = '<button class="btn ghost" data-result="' + ex.attempt.id + '">查看交卷紀錄</button>';
      return '<div class="card exam-card"><div class="exam-icon">卷</div><div class="body">' +
        '<div class="row"><h2>' + esc(ex.title) + '</h2>' + BADGE[st] + '</div>' +
        '<div class="exam-meta">' +
        '<span>題數 <b>' + ex.question_count + '</b> 題</span>' +
        '<span>作答時間限制 <b>' + ex.duration_min + '</b> 分鐘</span>' +
        '<span>開放 <b>' + (ex.start_at ? fmtDateTime(ex.start_at) : '隨時') + '</b></span>' +
        '<span>關閉 <b>' + (ex.end_at ? fmtDateTime(ex.end_at) : '不限') + '</b></span>' +
        '</div><p class="muted" data-status="' + ex.id + '">' + statusLine(ex) + '</p>' + action + '</div></div>';
    }).join('');
  }

  function tickList() {
    // 狀態跨過開放或關閉時間就整個重畫，否則只更新倒數文字
    const changed = exams.some(ex => {
      const el = document.querySelector('[data-status="' + ex.id + '"]');
      const was = el && el.dataset.st;
      const st = examStatus(ex);
      if (el) { el.dataset.st = st; el.innerHTML = statusLine(ex); }
      return was && was !== st;
    });
    if (changed) renderList();
  }

  $('exam-list').addEventListener('click', async e => {
    const start = e.target.closest('[data-start]');
    const result = e.target.closest('[data-result]');
    if (result) return loadResult(+result.dataset.result);
    if (!start) return;
    const ex = exams.find(x => x.id === +start.dataset.start);
    if (examStatus(ex) === 'open') {
      const left = ex.end_at ? (Date.parse(ex.end_at) - serverNow()) / 1000 : Infinity;
      const short = left < ex.duration_min * 60;
      const ok = await ask('開始作答',
        '<table class="info-table"><tr><th>考試</th><td>' + esc(ex.title) + '</td></tr>' +
        '<tr><th>題數</th><td>' + ex.question_count + ' 題</td></tr>' +
        '<tr><th>作答時間限制</th><td>' + ex.duration_min + ' 分鐘</td></tr>' +
        (ex.end_at ? '<tr><th>關閉時間</th><td>' + fmtDateTime(ex.end_at) + '</td></tr>' : '') + '</table>' +
        (short ? '<div class="alert warn">距離關閉只剩 ' + fmtSpan(left) + '，作答時間會在關閉時結束。</div>' : '') +
        guardRules(ex) +
        '<p style="margin-top:.75rem">按下「開始作答」後立即開始計時，中途離開計時不會暫停。時間到系統會自動交卷。</p>',
        '開始作答', '取消');
      if (!ok) return;
    }
    startExam(ex.id);
  });

  // ── 作答 ──

  // 開始前告知這場考試的防切換規則
  function guardRules(ex) {
    const rules = [];
    if (ex.fullscreen) rules.push('需要<b>全螢幕</b>作答，按 Esc 離開全螢幕會被記錄。');
    if (ex.focus_mode === 'warn') rules.push('切換視窗、分頁或其他程式會被記錄並跳出警告。');
    if (ex.focus_mode === 'lock') rules.push('離開考試畫面超過 <b>' + ex.focus_limit + '</b> 次，作答會被<b>鎖定</b>，需要老師解鎖。');
    if (ex.focus_mode === 'submit') rules.push('離開考試畫面超過 <b>' + ex.focus_limit + '</b> 次，系統會<b>直接交卷</b>。');
    if (ex.block_copy) rules.push('不能複製、貼上或按右鍵。');
    return rules.length ? '<div class="alert warn" style="margin-top:.75rem"><b>本場考試規則</b><ul class="rules" style="color:inherit;margin-top:.25rem">' +
      rules.map(r => '<li>' + r + '</li>').join('') + '</ul></div>' : '';
  }

  async function startExam(examId) {
    try {
      const data = previewExam
        ? await api('GET', '/api/admin/exams/' + examId + '/preview', undefined, adminToken)
        : await call('POST', '/api/exams/' + examId + '/start', { device: store('exam_pc') || '' });
      if (data.preview) previewSeed = data.seed;
      current = data;
      current.exam_id = examId;
      current.deadline = Date.now() + data.remaining_seconds * 1000;
      current.answers = Object.assign({}, data.saved);
      current.flags = new Set(JSON.parse(store('exam_flags_' + data.attempt_id) || '[]'));
      viewing = data.questions.length ? data.questions[0].id : null;
      away = false;
      dwell = {};
      dwellTicks = 0;
      renderExam();
      show('take');
      applyGuard();
      tick();
      clearInterval(timerId);
      timerId = setInterval(tick, 1000);
    } catch (err) {
      $('list-msg').textContent = err.message;
      window.scrollTo(0, 0);
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
    $$('[data-grid]').forEach(g => {
      g.innerHTML = current.questions.map((q, n) =>
        '<a href="#q' + q.id + '" data-nav="' + q.id + '">' + (n + 1) + '</a>').join('');
    });
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
            '<span class="letter">' + LETTERS[k] + '.</span><span>' + esc(o.text) + '</span></label>';
        }).join('');
      }
      return '<div class="que" id="q' + q.id + '">' +
        '<div class="que-info"><div class="no">第<b>' + (n + 1) + '</b>題</div>' +
        '<div class="state" data-state="' + q.id + '"></div>' +
        '<div class="grade">配分 ' + q.points + ' 分</div>' +
        '<button class="flag-btn" data-flag="' + q.id + '"></button></div>' +
        '<div class="que-content"><div class="kind">' + TYPE_TEXT[q.type] + '</div>' +
        '<div class="q-stem">' + esc(q.stem) + '</div>' + (q.code ? '<pre>' + esc(q.code) + '</pre>' : '') +
        '<div class="answer-area"><div class="prompt">' + PROMPT[q.type] + '</div>' + body + '</div></div></div>';
    }).join('');
    updateProgress();
    watchCurrent();
  }

  function updateProgress() {
    let done = 0;
    current.questions.forEach(q => {
      const ok = answered(q);
      const flagged = current.flags.has(q.id);
      if (ok) done++;
      $$('[data-nav="' + q.id + '"]').forEach(a => {
        a.classList.toggle('done', ok);
        a.classList.toggle('flagged', flagged);
      });
      const st = document.querySelector('[data-state="' + q.id + '"]');
      if (st) st.innerHTML = ok ? '<span class="badge ok">已作答</span>' : '<span class="badge gray">尚未作答</span>';
      const fb = document.querySelector('[data-flag="' + q.id + '"]');
      if (fb) {
        fb.classList.toggle('on', flagged);
        fb.textContent = flagged ? '⚑ 已標記' : '⚐ 標記此題';
      }
    });
    const text = '已答 ' + done + ' / ' + current.questions.length;
    $('progress').textContent = text;
    $('m-progress').textContent = text;
    return current.questions.length - done;
  }

  // 捲動時標出目前看到的題目
  function watchCurrent() {
    if (observer) observer.disconnect();
    if (!('IntersectionObserver' in window)) return;
    observer = new IntersectionObserver(entries => {
      entries.forEach(en => {
        if (!en.isIntersecting) return;
        const id = en.target.id.slice(1);
        viewing = +id;
        $$('[data-nav]').forEach(a => a.classList.toggle('current', a.dataset.nav === id));
      });
    }, { rootMargin: '-40% 0px -55% 0px' });
    $$('.que').forEach(el => observer.observe(el));
  }

  $('questions').addEventListener('click', e => {
    const fb = e.target.closest('[data-flag]');
    if (!fb) return;
    const id = +fb.dataset.flag;
    if (current.flags.has(id)) current.flags.delete(id); else current.flags.add(id);
    store('exam_flags_' + current.attempt_id, JSON.stringify([...current.flags]));
    updateProgress();
  });

  $('m-nav-toggle').addEventListener('click', () => $('m-nav').classList.toggle('open'));
  document.addEventListener('click', e => {
    const a = e.target.closest('[data-nav]');
    if (!a) return;
    e.preventDefault();
    if (!$('view-summary').classList.contains('hidden')) show('take');
    $('m-nav').classList.remove('open');
    document.getElementById('q' + a.dataset.nav).scrollIntoView({ behavior: 'smooth' });
  });

  $('questions').addEventListener('change', e => onAnswer(e.target));
  $('questions').addEventListener('input', e => { if (e.target.type === 'text') onAnswer(e.target); });
  $('questions').addEventListener('focusout', e => {
    if (e.target.type === 'text' && queue.size) { clearTimeout(saveTimer); flush(); }
  });

  function onAnswer(el) {
    const qid = +el.dataset.q;
    if (!qid) return;
    viewing = qid;
    const q = current.questions.find(x => x.id === qid);
    let resp;
    if (q.type === 'single') {
      resp = +el.value;
    } else if (q.type === 'multi') {
      resp = [...$$('input[name="q' + qid + '"]:checked')].map(i => +i.value);
    } else {
      resp = [...$$('input[data-q="' + qid + '"]')].map(i => i.value);
    }
    if (q.type !== 'fill') {
      $$('input[name="q' + qid + '"]').forEach(i => i.closest('.opt').classList.toggle('picked', i.checked));
    }
    current.answers[qid] = resp;
    queue.set(qid, resp);
    updateProgress();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, q.type === 'fill' ? 800 : 200);
  }

  function setSave(text, cls) {
    $$('[data-save]').forEach(el => { el.textContent = text; el.className = 'save-state ' + (cls || ''); });
  }

  async function flush() {
    if (previewExam) { queue.clear(); setSave('模擬作答：答案只留在這個畫面', 'ok'); return; }
    if (flushing || !current) return;
    flushing = true;
    try {
      while (queue.size) {
        const [qid, resp] = queue.entries().next().value;
        setSave('儲存中…');
        await call('PUT', '/api/attempts/' + current.attempt_id + '/answers', { question_id: qid, response: resp });
        if (queue.get(qid) === resp) queue.delete(qid);
      }
      const t = new Date();
      setSave('已儲存 ' + String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0') +
        ':' + String(t.getSeconds()).padStart(2, '0'), 'ok');
    } catch (err) {
      if (err.status === 409) {
        setSave(err.message, 'ng');
        finishExam();
      } else if (err.status === 423) {
        // 被鎖定：答案留在佇列，老師解鎖後再送
        setSave('作答已鎖定，答案會在解鎖後送出', 'ng');
        current.guard.locked = true;
        showLock();
        setTimeout(flush, 5000);
      } else {
        setSave('尚未儲存，自動重試中…', 'ng');
        setTimeout(flush, 3000);
      }
    } finally {
      flushing = false;
    }
  }

  function tick() {
    if (!current) return;
    const left = Math.max(0, (current.deadline - Date.now()) / 1000);
    const text = clock(left);
    const low = left <= 300;
    $('timer').textContent = text;
    $('m-timer').textContent = text;
    $('sum-timer').textContent = text;
    $('timer-box').classList.toggle('low', low);
    $('m-timer').classList.toggle('low', low);
    countDwell();
    pollFocus();
    if (left <= 0) {
      clearInterval(timerId);
      finishExam(true);
    }
  }

  // 保險：每秒確認考試視窗仍在最前面。macOS 三指滑動切換桌面等情況，系統偶爾不會送出 blur 事件
  function pollFocus() {
    if (!current || current.finishing) return;
    const out = document.hidden || !document.hasFocus();
    if (out && !away) goAway('window');
    else if (!out && away) comeBack();
  }

  // 每秒記一次：作答畫面在前景時，算在畫面中央那一題；每 15 秒回報一次
  function countDwell() {
    if (document.hidden || !viewing || $('view-take').classList.contains('hidden')) return;
    dwell[viewing] = (dwell[viewing] || 0) + 1;
    if (++dwellTicks >= 15) sendDwell();
  }

  function sendDwell(keepalive) {
    dwellTicks = 0;
    if (previewExam) { dwell = {}; return; }
    if (!current || !Object.keys(dwell).length) return;
    const seconds = dwell;
    dwell = {};
    const path = '/api/attempts/' + current.attempt_id + '/event';
    if (keepalive) {
      fetch(path, {
        method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ type: 'time', seconds }),
      }).catch(() => {});
      return;
    }
    // 送失敗就併回去下次再送
    return call('POST', path, { type: 'time', seconds }).catch(() => {
      for (const k in seconds) dwell[k] = (dwell[k] || 0) + seconds[k];
    });
  }

  // ── 作答摘要 ──

  function showSummary() {
    const missing = updateProgress();
    $('sum-title').textContent = current.title;
    $('sum-alert').innerHTML = missing
      ? '<div class="alert warn">還有 ' + missing + ' 題尚未作答。送出後就不能再修改。</div>'
      : '<div class="alert ok">所有題目都已作答。送出後就不能再修改。</div>';
    $('sum-table').innerHTML = '<tr><th style="width:90px">題號</th><th>狀態</th><th style="width:110px">標記</th></tr>' +
      current.questions.map((q, n) => {
        const ok = answered(q);
        return '<tr><td><a href="#q' + q.id + '" data-nav="' + q.id + '">第 ' + (n + 1) + ' 題</a></td>' +
          '<td class="' + (ok ? 'state-done' : 'state-empty') + '">' + (ok ? '已作答' : '尚未作答') + '</td>' +
          '<td>' + (current.flags.has(q.id) ? '<span style="color:var(--flag)">⚑ 已標記</span>' : '') + '</td></tr>';
      }).join('');
    show('summary');
  }

  $$('[data-finish]').forEach(b => b.addEventListener('click', () => { $('m-nav').classList.remove('open'); showSummary(); }));
  $('sum-back').addEventListener('click', () => show('take'));
  $('sum-submit').addEventListener('click', async () => {
    const missing = updateProgress();
    const ok = await ask('確認送出',
      (missing ? '<p>還有 <b>' + missing + '</b> 題尚未作答。</p>' : '') +
      '<p>送出後就不能再修改答案，確定要結束這次作答嗎？</p>', '全部送出並結束', '取消');
    if (ok) finishExam();
  });

  async function finishExam(timeUp) {
    if (!current || current.finishing) return;
    current.finishing = true;
    clearInterval(timerId);
    clearTimeout(saveTimer);
    setSave(timeUp ? '時間到，交卷中…' : '交卷中…');
    if (timeUp) show('take');
    // 先把還沒送出的作答送完（最多重試幾次），再交卷
    for (let i = 0; i < 5 && queue.size; i++) {
      flushing = false;
      await flush();
      if (queue.size) await new Promise(r => setTimeout(r, 1500));
    }
    await sendDwell();  // 交卷後伺服器就不再收停留時間，要先送完
    const attemptId = current.attempt_id;
    if (previewExam) {
      try {
        const result = await api('POST', '/api/admin/exams/' + previewExam + '/preview/submit',
          { seed: previewSeed, responses: current.answers }, adminToken);
        current = null;
        leaveGuard();
        renderResult(result, timeUp);
        $('preview-note').textContent = result.student_view;
      } catch (err) { setSave(err.message, 'ng'); current.finishing = false; }
      return;
    }
    for (let i = 0; i < 10; i++) {
      try {
        const result = await call('POST', '/api/attempts/' + attemptId + '/submit');
        current = null;
        leaveGuard();
        renderResult(result, timeUp);
        return;
      } catch (err) {
        if (!err.network) { current = null; leaveGuard(); loadList(); return; }
        setSave('網路不穩，交卷重試中…時間到伺服器也會用已存的答案自動交卷', 'ng');
        await new Promise(r => setTimeout(r, 3000));
      }
    }
  }

  // ── 離開考試畫面（防切換視窗）──
  // 瀏覽器無法真的禁止切換程式，這裡做的是偵測與處置；次數與鎖定由伺服器判斷，改前端也繞不過。

  const guarding = () => current && !current.finishing &&
    (!$('view-take').classList.contains('hidden') || !$('view-summary').classList.contains('hidden'));
  const fsSupported = () => !!(document.fullscreenEnabled && document.documentElement.requestFullscreen);
  const needFullscreen = () => current.guard.fullscreen && fsSupported() && !document.fullscreenElement;

  function applyGuard() {
    const g = current.guard;
    document.body.classList.toggle('no-copy', g.block_copy);
    renderGuardCount();
    if (g.locked) showLock();
    else if (needFullscreen()) showFullscreenPrompt(false);
    else hideGuard();
  }

  function renderGuardCount() {
    const g = current.guard;
    const el = document.querySelector('[data-guard-count]');
    const limited = g.mode === 'lock' || g.mode === 'submit';
    el.classList.toggle('hidden', g.mode === 'record');
    el.classList.toggle('near', limited && g.switches >= g.limit);
    el.textContent = limited ? '離開考試畫面 ' + g.switches + ' / ' + g.limit + ' 次' : '離開考試畫面 ' + g.total + ' 次（已記錄）';
  }

  function showGuard(kind, title, body, button, action) {
    $('guard').querySelector('.guard-box').className = 'guard-box ' + kind;
    $('guard-title').textContent = title;
    $('guard-body').innerHTML = body;
    $('guard-ok').textContent = button;
    guardAction = action;
    $('guard').classList.remove('hidden');
  }
  function hideGuard() { $('guard').classList.add('hidden'); guardAction = null; }

  async function enterFullscreen() {
    try { await document.documentElement.requestFullscreen(); } catch (e) { /* 使用者拒絕時維持提示 */ }
    if (!document.fullscreenElement) return;
    if (away) {  // 離開全螢幕後，重新進入才算回來
      away = false;
      call('POST', '/api/attempts/' + current.attempt_id + '/event', { type: 'focus' }).catch(() => {});
    }
    if (current.guard.locked) showLock(); else hideGuard();
  }

  function showFullscreenPrompt(again) {
    showGuard('warn', again ? '請回到全螢幕' : '這場考試需要全螢幕作答',
      '<p>' + (again ? '離開全螢幕已記錄一次。' : '') + '按下按鈕進入全螢幕後才能看到題目。</p>' +
      '<p class="muted">作答中按 Esc 或切換視窗都會被記錄。</p>', '進入全螢幕', enterFullscreen);
  }

  function showLock() {
    showGuard('lock', '作答已鎖定', '<p>離開考試畫面超過 <b>' + current.guard.limit + '</b> 次，系統已鎖定作答。</p>' +
      '<p><b>請舉手找老師解鎖。</b>已作答的內容都有保存，但計時不會暫停。</p>', '老師已解鎖，繼續作答', checkUnlock);
  }

  async function checkUnlock() {
    if (previewExam) {  // 模擬作答沒有老師可以解鎖，直接解除並重新計次
      Object.assign(current.guard, { locked: false, switches: 0 });
      return applyGuard();
    }
    try {
      const data = await call('POST', '/api/exams/' + current.exam_id + '/start', { device: store('exam_pc') || '' });
      current.guard = data.guard;
      if (data.guard.locked) {
        $('guard-body').insertAdjacentHTML('beforeend', '<p class="msg ng">老師還沒有解鎖。</p>');
        return;
      }
      applyGuard();
    } catch (err) {
      if (err.status === 409) { hideGuard(); current.finishing = true; loadList(); }
    }
  }

  $('guard-ok').addEventListener('click', () => guardAction && guardAction());

  function goAway(reason) {
    if (away || !guarding()) return;
    away = true;
    (previewExam ? Promise.resolve(previewBlur()) : call('POST', '/api/attempts/' + current.attempt_id + '/event', { type: 'blur', reason })).then(r => {
      if (!current) return;
      if (r.submitted) { onViolationSubmit(); return; }
      Object.assign(current.guard, { switches: r.switches, total: r.total, locked: r.locked });
      renderGuardCount();
      if (!away) afterReturn(reason);   // 回應比「回到畫面」晚到
      else if (r.locked) showLock();
    }).catch(() => {});
  }

  function comeBack() {
    if (!away || !current) return;
    if (needFullscreen()) return;  // 要求全螢幕時，重新進入全螢幕才算回來
    away = false;
    if (!previewExam) call('POST', '/api/attempts/' + current.attempt_id + '/event', { type: 'focus' }).catch(() => {});
    afterReturn();
  }

  function afterReturn(reason) {
    const g = current.guard;
    if (g.locked) return showLock();
    if (g.fullscreen && fsSupported() && !document.fullscreenElement) return showFullscreenPrompt(true);
    if (g.mode === 'record') return;
    const left = g.limit - g.switches;
    let body = '<div class="big-count">第 ' + (g.mode === 'warn' ? g.total : g.switches) + ' 次</div><p>你離開了考試畫面（切換視窗、分頁或其他程式），系統已記錄，老師會看到。</p>';
    if (g.mode === 'lock' || g.mode === 'submit') {
      body += left > 0 ? '<p>再離開 <b>' + left + '</b> 次以上，' + (g.mode === 'lock' ? '作答會被鎖定，需要老師解鎖。' : '系統會<b>直接交卷</b>。') + '</p>'
        : '<p><b>再離開一次，' + (g.mode === 'lock' ? '作答就會被鎖定。' : '系統就會直接交卷。') + '</b></p>';
    }
    showGuard('warn', '請專心作答', body, '我知道了，回到考試', hideGuard);
  }

  // 交卷後解除限制
  function leaveGuard() {
    hideGuard();
    document.body.classList.remove('no-copy');
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }

  // 模擬作答的離開畫面：照考試設定在瀏覽器裡計次，不送到伺服器
  function previewBlur() {
    const g = current.guard;
    const switches = g.switches + 1, over = switches > g.limit;
    return { switches, total: g.total + 1, locked: g.locked || (g.mode === 'lock' && over), submitted: g.mode === 'submit' && over };
  }

  async function onViolationSubmit() {
    if (previewExam) {
      await ask('已自動交卷', '<p>離開考試畫面超過允許次數，正式考試會在這時候直接交卷。</p>', '查看結果', null);
      current.finishing = false;
      return finishExam(false);
    }
    const attemptId = current.attempt_id;
    current.finishing = true;
    clearInterval(timerId);
    current = null;
    leaveGuard();
    await ask('已自動交卷', '<p>離開考試畫面超過允許次數，系統已用目前儲存的答案交卷。</p>', '查看結果', null);
    loadResult(attemptId);
  }

  // 切分頁、縮小視窗 → visibilitychange；切到其他程式 → window blur。兩者常一起發生，只算一次
  document.addEventListener('visibilitychange', () => { if (document.hidden) goAway('window'); else if (document.hasFocus()) comeBack(); });
  window.addEventListener('blur', () => goAway('window'));
  window.addEventListener('focus', () => { if (!document.hidden) comeBack(); });
  document.addEventListener('fullscreenchange', () => {
    if (!current || current.finishing || !current.guard.fullscreen) return;
    if (!document.fullscreenElement && guarding()) {
      goAway('fullscreen');  // 之後跟著來的 window blur 因為 away 已成立，不會重複計次
      if (!current.guard.locked) showFullscreenPrompt(true);
    }
  });
  // 禁止複製、貼上、右鍵（考試有開啟時）
  ['copy', 'cut', 'paste', 'contextmenu'].forEach(type => document.addEventListener(type, e => {
    if (current && current.guard && current.guard.block_copy && guarding()) e.preventDefault();
  }));
  // 關分頁、切 App 時把還沒送出的作答用 keepalive 送出，瀏覽器關掉也會送完
  window.addEventListener('pagehide', () => {
    if (current) sendDwell(true);
    if (!current || !queue.size || previewExam) return;
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

  // ── 成績與檢視 ──

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
    return value.map((v, i) => '第 ' + (i + 1) + ' 格：' + (Array.isArray(v) ? v.join(' 或 ') : (v || '（空白）'))).join('；');
  }

  function verdict(q) {
    if (q.response == null) return ['wrong', '<span class="badge gray">未作答</span>'];
    if (q.score >= q.points) return ['right', '<span class="badge ok">正確</span>'];
    if (q.score > 0) return ['partial', '<span class="badge warn">部分正確</span>'];
    return ['wrong', '<span class="badge ng">錯誤</span>'];
  }

  function renderResult(r, timeUp) {
    show('result');
    $('result-title').textContent = r.title;
    const used = r.started_at && r.submitted_at ? fmtSpan((Date.parse(r.submitted_at) - Date.parse(r.started_at)) / 1000) : '';
    const info = '<table class="info-table">' +
      '<tr><th>狀態</th><td>已完成' + (timeUp ? '（時間到自動交卷）' : '') + '</td></tr>' +
      (r.started_at ? '<tr><th>開始時間</th><td>' + fmtDateTime(r.started_at) + '</td></tr>' : '') +
      (r.submitted_at ? '<tr><th>交卷時間</th><td>' + fmtDateTime(r.submitted_at) + '</td></tr>' : '') +
      (used ? '<tr><th>作答用時</th><td>' + used + '</td></tr>' : '') +
      (r.pending ? '' : '<tr><th>分數</th><td><b>' + r.score + '</b> / ' + r.max_score + '</td></tr>') + '</table>';
    if (r.pending) {
      $('result-panel').innerHTML = '<div class="card-title">已交卷</div>' +
        '<div class="alert info">成績將於 ' + (r.release_at ? fmtDateTime(r.release_at) : '考試結束後') + ' 公布，屆時可回到「我的考試」查看。</div>' + info;
    } else {
      const pct = r.max_score ? Math.round(r.score / r.max_score * 100) : 0;
      $('result-panel').innerHTML = '<div class="score-panel"><div class="score-ring" style="--pct:' + pct + '">' +
        '<span>' + pct + '%</span><small>' + r.score + ' / ' + r.max_score + '</small></div>' +
        '<div style="flex:1;min-width:240px">' + info + '</div></div>' +
        (r.show_mode === 'score' ? '<p class="muted" style="margin:.9rem 0 0">老師設定只公布總分。</p>' :
          r.review_closed ? '<div class="alert info" style="margin-bottom:0">這場考試不開放回頭複習，或複習期限已過，只能查看總分。</div>' : '');
    }
    $('result-detail').innerHTML = r.questions.map((q, n) => {
      const [cls, badge] = verdict(q);
      let body = '';
      if (q.type !== 'fill') {
        body = q.options.map((o, i) => {
          const right = q.answer.includes(i);
          const picked = q.type === 'single' ? q.response === i : (q.response || []).includes(i);
          const kind = q.type === 'single' ? 'radio' : 'checkbox';
          return '<div class="opt' + (picked ? (right ? ' right' : ' wrong') : '') + '">' +
            '<input type="' + kind + '" disabled' + (picked ? ' checked' : '') + '>' +
            '<span class="letter">' + LETTERS[i] + '.</span><span>' + esc(o) + '</span>' +
            (picked ? '<span class="mark">' + (right ? '✓' : '✗') + '</span>' : '') + '</div>';
        }).join('');
      } else {
        body = '<p>你的答案：' + esc(showAnswer(q, q.response)) + '</p>';
      }
      return '<div class="que static" id="r' + n + '">' +
        '<div class="que-info"><div class="no">第<b>' + (n + 1) + '</b>題</div><div class="state">' + badge + '</div>' +
        '<div class="grade">得分 ' + q.score + ' / ' + q.points + '</div></div>' +
        '<div class="que-content"><div class="kind">' + TYPE_TEXT[q.type] + '</div>' +
        '<div class="q-stem">' + esc(q.stem) + '</div>' + (q.code ? '<pre>' + esc(q.code) + '</pre>' : '') +
        '<div class="answer-area">' + body + '</div>' +
        '<div class="feedback"><div class="right-answer">正確答案：' + esc(showAnswer(q, q.answer)) + '</div>' +
        (q.explanation ? '<div style="margin-top:.35rem"><b>解析：</b>' + esc(q.explanation) + '</div>' : '') +
        '</div></div></div>';
    }).join('');
    $('result-nav').innerHTML = r.questions.length ? '<div class="card"><div class="nav-title"><span>作答檢視</span></div>' +
      '<div class="nav-grid">' + r.questions.map((q, n) => '<a href="#r' + n + '" class="' + verdict(q)[0] + '">' + (n + 1) + '</a>').join('') + '</div>' +
      '<button class="btn ghost block" data-to-list>回到我的考試</button></div>' :
      '<div class="card"><button class="btn ghost block" data-to-list>回到我的考試</button></div>';
  }

  // 模擬作答：顯示提示列，跳過學號登入，直接進到開始作答的確認畫面
  async function startPreview() {
    document.body.insertAdjacentHTML('afterbegin', '<div class="preview-bar"><b>管理員模擬作答</b>' +
      '<span id="preview-note">作答與成績都不會被記錄，學生端與監控頁看不到。</span>' +
      '<a href="?preview=' + encodeURIComponent(previewExam) + '">重新開始</a><a href="/admin#exam/' + encodeURIComponent(previewExam) + '">回到後台</a></div>');
    $('who').innerHTML = '<span class="avatar">預覽</span> 管理員';
    if (!adminToken) {
      show('list');
      $('list-msg').textContent = '請先在這個瀏覽器登入後台，再從後台開啟模擬作答。';
      return;
    }
    try {
      const p = await api('GET', '/api/admin/exams/' + previewExam + '/preview', undefined, adminToken);
      show('list');
      $('exam-list').innerHTML = '';
      const ex = p.exam;
      const ok = await ask('開始模擬作答',
        '<table class="info-table"><tr><th>考試</th><td>' + esc(ex.title) + '</td></tr>' +
        '<tr><th>題數</th><td>' + ex.question_count + ' 題</td></tr>' +
        '<tr><th>作答時間限制</th><td>' + ex.duration_min + ' 分鐘</td></tr></table>' + guardRules(ex) +
        '<p style="margin-top:.75rem">這是學生按下「開始作答」後看到的畫面。計時、打散、防切換規則都和正式考試相同，但不會留下任何紀錄。</p>',
        '開始作答', '取消');
      if (!ok) { location.href = '/admin#exam/' + previewExam; return; }
      startExam(+previewExam);
    } catch (err) {
      show('list');
      $('list-msg').textContent = err.status === 401 ? '後台登入已過期，請重新登入後台。' : err.message;
    }
  }

  if (previewExam) startPreview();
  else if (token && student) loadList(); else show('login');
})();
