/* 學生端與後台共用：API 呼叫、HTML 跳脫、本機儲存（失敗也不影響使用） */
(function () {
  'use strict';

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { return null; }
  }

  async function api(method, path, body, token) {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = 'Bearer ' + token;
    let res;
    try {
      res = await fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      const err = new Error('連不上伺服器，請檢查網路');
      err.network = true;
      throw err;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(typeof data.detail === 'string' ? data.detail : '發生錯誤（' + res.status + '）');
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  // 例：11月20日（四）08:10
  function fmtDateTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const w = '日一二三四五六'[d.getDay()];
    return (d.getMonth() + 1) + '月' + d.getDate() + '日（' + w + '）' +
      String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  // 秒數 → 「2 天 3 小時」「1 小時 5 分」「4 分 10 秒」
  function fmtSpan(sec) {
    sec = Math.max(0, Math.round(sec));
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
    const m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (d) return d + ' 天 ' + h + ' 小時';
    if (h) return h + ' 小時 ' + m + ' 分';
    return m + ' 分 ' + s + ' 秒';
  }

  function clock(sec) {
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    const mm = String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    return h ? h + ':' + mm : mm;
  }

  // 自訂確認框，回傳 Promise<boolean>；頁面上要有 #modal 結構
  function ask(title, html, okText, cancelText) {
    const modal = document.getElementById('modal');
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = html;
    const ok = document.getElementById('modal-ok');
    const cancel = document.getElementById('modal-cancel');
    ok.textContent = okText || '確定';
    cancel.textContent = cancelText || '取消';
    cancel.classList.toggle('hidden', cancelText === null);
    modal.classList.remove('hidden');
    ok.focus();
    return new Promise(resolve => {
      function done(v) {
        modal.classList.add('hidden');
        ok.onclick = cancel.onclick = null;
        resolve(v);
      }
      ok.onclick = () => done(true);
      cancel.onclick = () => done(false);
    });
  }

  const LETTERS = 'ABCDEF';

  window.exam = { store, api, esc, fmtTime, fmtDateTime, fmtSpan, clock, ask, LETTERS };
})();
