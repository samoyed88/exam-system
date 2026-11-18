/* 設定考場電腦代碼：存在這台電腦的瀏覽器，開始作答時送給伺服器 */
(function () {
  var pc = document.getElementById('pc');
  var msg = document.getElementById('msg');
  var fromUrl = new URLSearchParams(location.search).get('pc');
  pc.value = fromUrl || exam.store('exam_pc') || '';
  function save() {
    var v = pc.value.trim().slice(0, 40);
    if (!v) return;
    exam.store('exam_pc', v);
    msg.textContent = exam.store('exam_pc') === v
      ? '已設定為「' + v + '」' : '這個瀏覽器無法儲存（可能是無痕模式）';
  }
  if (fromUrl) save();
  document.getElementById('f').addEventListener('submit', function (e) { e.preventDefault(); save(); });
})();
