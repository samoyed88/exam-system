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

  const LETTERS = 'ABCDEF';

  window.exam = { store, api, esc, fmtTime, LETTERS };
})();
