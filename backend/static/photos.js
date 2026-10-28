(function () {
  'use strict';
  var gallery = document.getElementById('photoGallery');
  var preview = document.getElementById('photoPreview');
  var box = gallery || preview;
  if (!box) return;
  var labels = {TIMER: '定时记录', FALL: '跌倒报警', AIR: '空气超标', MANUAL: '手动拍摄'};
  var status = document.getElementById('photoStatus');
  var refresh = document.getElementById('photoRefresh');
  var more = document.getElementById('photoMore');
  var params = new URLSearchParams(gallery ? window.location.search : '');
  var reason = params.get('reason') || '';
  var before = null, busy = false, version = 0;
  var seen = new Set();
  if (!(reason in labels)) reason = '';
  function text(tag, cls, value) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    el.textContent = value;
    return el;
  }
  function dateLabel(value) {
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? '时间未知' : new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    }).format(date);
  }
  function item(photo) {
    var figure = document.createElement('figure');
    figure.className = 'photo-item';
    var link = document.createElement('a');
    link.className = 'photo-open';
    link.href = '/api/photos/' + Number(photo.id) + '/image';
    link.target = '_blank'; link.rel = 'noopener';
    link.setAttribute('aria-label', (labels[photo.reason] || '照片') + '，查看原图');
    var img = document.createElement('img');
    img.src = link.href; img.loading = 'lazy'; img.decoding = 'async';
    img.alt = (labels[photo.reason] || '现场照片') + '，后端接收于 ' + dateLabel(photo.received_at);
    img.width = photo.width; img.height = photo.height;
    img.addEventListener('error', function () {
      link.replaceChildren(text('span', '', '图片加载失败，点击重试查看原图'));
      link.classList.add('photo-image-failed');
    });
    link.appendChild(img); figure.appendChild(link);
    var caption = document.createElement('figcaption');
    var top = text('div', 'photo-caption-top', '');
    var kind = text('span', 'photo-kind', labels[photo.reason] || '现场照片');
    kind.dataset.reason = photo.reason;
    top.appendChild(kind);
    var time = text('time', 'photo-time', dateLabel(photo.received_at) + ' 收到');
    time.dateTime = photo.received_at; top.appendChild(time);
    caption.appendChild(top);
    var detail = photo.device_id + ' · ' + photo.width + '×' + photo.height;
    if (photo.event_seq) detail += ' · 报警 #' + photo.event_seq;
    caption.appendChild(text('p', 'photo-details', detail));
    figure.appendChild(caption);
    return figure;
  }
  function updateFilters() {
    document.querySelectorAll('#photoFilters button').forEach(function (button) {
      var active = button.dataset.reason === reason;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }
  var scopes = ['device_id', 'boot_id', 'event_seq'].filter(function (key) { return params.has(key); });
  if (gallery && scopes.length) {
    document.getElementById('photoScope').hidden = false;
    document.getElementById('photoScopeText').textContent = params.has('event_seq')
      ? '正在查看这次报警的照片。拍摄后需要上传，请稍后刷新。' : '正在查看指定设备或运行会话的照片。';
  }
  async function load(append, automatic) {
    if (busy && automatic) return;
    if (busy && append) return;
    busy = true;
    var current = ++version;
    if (refresh) refresh.disabled = true;
    if (more) more.disabled = true;
    if (!automatic) status.textContent = append ? '正在加载更早照片…' : '正在加载照片…';
    status.classList.remove('photo-error');
    var query = new URLSearchParams();
    if (reason) query.set('reason', reason);
    scopes.forEach(function (key) { query.set(key, params.get(key)); });
    query.set('limit', gallery ? '12' : '3');
    if (append && before) query.set('before', before);
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 8000);
    try {
      var response = await fetch('/api/photos?' + query, {signal: controller.signal, cache: 'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      var data = await response.json();
      if (!Array.isArray(data.photos)) throw new Error('Invalid data');
      if (current !== version) return;
      if (!append) { box.replaceChildren(); seen.clear(); }
      data.photos.forEach(function (photo) {
        if (!seen.has(photo.id)) { box.appendChild(item(photo)); seen.add(photo.id); }
      });
      before = data.next_before;
      if (more) more.hidden = !data.has_more;
      status.textContent = seen.size ? (gallery ? '已显示 ' + seen.size + ' 张照片，点击画面查看原图。' : '最近上传的照片')
        : '尚无照片。设备开始监测五分钟后，或触发跌倒、空气超标报警后，会在这里显示。';
    } catch (error) {
      if (current !== version) return;
      status.textContent = '照片加载失败，请检查后端连接后点击刷新。';
      status.classList.add('photo-error');
    } finally {
      clearTimeout(timeout);
      if (current === version) {
        busy = false;
        if (refresh) refresh.disabled = false;
        if (more) more.disabled = false;
      }
    }
  }
  if (refresh) refresh.addEventListener('click', function () { load(false, false); });
  if (more) more.addEventListener('click', function () { load(true, false); });
  document.querySelectorAll('#photoFilters button').forEach(function (button) {
    button.addEventListener('click', function () {
      reason = button.dataset.reason;
      updateFilters();
      var url = new URL(window.location.href);
      if (reason) url.searchParams.set('reason', reason); else url.searchParams.delete('reason');
      history.replaceState(null, '', url);
      load(false, false);
    });
  });
  updateFilters();
  load(false, false);
  // 画廊手动刷新；看板预览每 15s 更新
  if (preview) setInterval(function () { if (!document.hidden) load(false, true); }, 15000);
}());
