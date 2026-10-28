/* 模型报警独立记录，不控制设备 */
(function () {
  'use strict';
  var root = document.getElementById('modelMonitor');
  if (!root) return;
  var scope = null, cursor = null, rows = [], pending = [], seen = new Set();
  var before = null, hasOlder = false, busy = false, olderBusy = false;
  var sound = false, generation = 0;
  var storageKey = '', eventError = false;
  var $ = function (id) { return document.getElementById(id); };
  function activeDevice() { return (window.lastSnapshot && window.lastSnapshot.device_id) || ''; }
  function savePending() {
    try { localStorage.setItem(storageKey, JSON.stringify(pending.slice(-200))); } catch (e) {}
  }
  function resetScope(device) {
    scope = device; generation++; cursor = null; rows = []; before = null; hasOlder = false;
    root.dataset.device = device;
    pending = []; seen = new Set(); storageKey = 'fd-ml-unread-v1:' + (device || 'all');
    try {
      var saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
      if (Array.isArray(saved)) pending = saved.filter(validEvent).slice(-200);
    } catch (e) {}
    pending.forEach(function (event) { seen.add(event.id); });
    $('mlEvents').innerHTML = '<div class="empty">正在加载模型报警…</div>';
    $('mlMore').hidden = true;
    $('mlBadge').className = 'badge off'; $('mlBadge').textContent = '正在连接';
    $('mlScore').textContent = '--'; $('mlThreshold').textContent = '--';
    renderPending();
  }
  function validEvent(event) {
    return event && typeof event.id === 'string' && typeof event.device_id === 'string' &&
      typeof event.boot_id === 'string' && Number.isFinite(event.received_at) &&
      Number.isFinite(event.score) && Number.isFinite(event.threshold);
  }
  function timeText(timestamp) {
    return new Date(timestamp * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
  }
  function number(value) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : '--'; }
  function renderPending() {
    var count = pending.length;
    $('mlPending').hidden = !count;
    root.dataset.alert = String(count > 0);
    $('mlPendingTitle').textContent = count + ' 条模型疑似跌倒待确认';
    if (count) {
      var last = pending[pending.length - 1];
      $('mlPendingDetail').textContent = '请确认使用者情况。最近：' + last.device_id + ' · ' + timeText(last.received_at);
    }
  }
  function renderRows() {
    var unread = new Set(pending.map(function (event) { return event.id; }));
    if (!rows.length) {
      $('mlEvents').innerHTML = '<div class="empty">暂无模型报警记录</div>';
    } else {
      $('mlEvents').innerHTML = rows.map(function (event) {
        return '<article class="ml-event"><div><div class="ml-event-label"><strong>模型疑似跌倒</strong>' +
          (unread.has(event.id) ? '<span class="ml-event-unread">待确认</span>' : '<small>模型辅助判断</small>') +
          '</div><p class="ml-event-meta">' + escapeHtml(event.device_id) + ' · ' + escapeHtml(event.model_id || '模型版本未知') +
          '</p></div><span class="ml-event-score">分数 ' + number(event.score) + ' / 阈值 ' + number(event.threshold) +
          '</span><time datetime="' + new Date(event.received_at * 1000).toISOString() + '">' + escapeHtml(timeText(event.received_at)) +
          ' 收到</time></article>';
      }).join('');
    }
    $('mlMore').hidden = !hasOlder;
    $('mlMore').disabled = olderBusy;
  }
  function merge(incoming) {
    var all = new Map(rows.map(function (event) { return [event.id, event]; }));
    incoming.forEach(function (event) { all.set(event.id, event); });
    rows = Array.from(all.values()).sort(function (a, b) { return b.batch_id - a.batch_id || b.up_ms - a.up_ms; });
  }
  function feedback(text) {
    $('mlFeedback').textContent = text; $('mlFeedback').hidden = !text;
  }
  function renderStatus(data) {
    var badge = $('mlBadge'), label, tone = 'warn', message;
    var live = data.online && data.window_in_memory;
    var latest = live && data.status === 'ready' ? data.latest : null;
    if (data.model_error || data.status === 'model_unavailable') {
      label = '模型不可用'; message = '电脑未能加载模型，请检查模型文件并重启后端。';
    } else if (data.status === 'no_data') {
      label = '等待六轴数据'; message = '模型已就绪；设备上传六轴数据后开始分析，请确认已烧录支持上传的固件。';
    } else if (!data.online) {
      label = '六轴数据已中断'; message = '暂停实时判断，请检查设备与网络。已保存的模型报警仍可查看。';
    } else if (!data.window_in_memory || data.status !== 'ready') {
      label = '正在收集采样'; message = '需要约 3 秒连续六轴数据，断流或重启后会重新收集。';
    } else if (latest && latest.above_threshold) {
      label = '分数达到阈值'; tone = 'bad'; message = '请关注使用者情况；连续两次达到阈值时记录模型报警。';
    } else {
      label = '模型监测中'; tone = 'ok'; message = '当前模型分数未达到报警阈值，正在持续分析运动变化。';
    }
    badge.className = 'badge ' + tone; badge.textContent = label;
    $('mlDescription').textContent = message;
    $('mlScore').textContent = number(latest && latest.score);
    $('mlThreshold').textContent = number(latest && latest.threshold);
    $('mlWindow').textContent = (data.window_in_memory ? data.window_samples || 0 : 0) + ' / 150';
    $('mlDevice').textContent = (data.device_id ? '模型设备 ' + data.device_id : '尚无设备上传六轴数据') +
      (data.model_id ? ' · ' + data.model_id : '');
  }
  function eventsUrl(extra) {
    var params = new URLSearchParams(extra || {});
    if (scope) params.set('device_id', scope);
    return '/api/ml/events?' + params.toString();
  }
  function receive(data, initial) {
    if (!data || !data.ok || !Array.isArray(data.events) || !Number.isSafeInteger(data.cursor)) throw new Error('Invalid model events');
    var incoming = data.events.filter(validEvent), added = [];
    if (!initial && !data.reset) {
      incoming.forEach(function (event) {
        if (!seen.has(event.id)) { added.push(event); pending.push(event); }
      });
    }
    incoming.forEach(function (event) { seen.add(event.id); });
    if (initial || data.reset) {
      rows = []; before = data.next_before; hasOlder = data.has_more;
    }
    cursor = data.cursor; merge(incoming); savePending(); renderPending(); renderRows();
    if (added.length) {
      $('mlAnnouncement').textContent = '新增 ' + added.length + ' 条模型疑似跌倒报警，请确认使用者情况。';
      // 历史补拉不发声
      var recent = added.some(function (event) { return Math.abs(data.server_time - event.received_at) <= 30; });
      if (sound && recent) beep(880, .5, .3);
    }
    return data.has_more && !initial && !data.reset;
  }
  function pollModel(force) {
    var device = activeDevice();
    if (device !== scope) resetScope(device);
    if (busy || (!force && document.hidden)) return;
    var token = generation, initial = cursor === null;
    busy = true; $('mlRefresh').disabled = true;
    var params = scope ? '?device_id=' + encodeURIComponent(scope) : '';
    var more = false;
    var statusJob = requestJson('/api/ml/status' + params).then(function (data) {
      if (token !== generation || device !== activeDevice()) return;
      renderStatus(data);
    }).catch(function () {
      if (token !== generation || device !== activeDevice()) return;
      $('mlBadge').className = 'badge warn'; $('mlBadge').textContent = '模型服务连接中断';
      $('mlDescription').textContent = '正在重试连接，已保存的模型报警仍保留。';
      $('mlScore').textContent = '--'; $('mlThreshold').textContent = '--';
    });
    var eventsJob = requestJson(eventsUrl(initial ? {limit:20} : {after:cursor,limit:100})).then(function (data) {
      if (token !== generation || device !== activeDevice()) return;
      more = receive(data, initial); eventError = false; feedback('');
    }).catch(function () {
      if (token !== generation || device !== activeDevice()) return;
      eventError = true; feedback('模型报警记录暂时加载失败，正在重试；当前保留上次记录。');
      if (initial) $('mlEvents').innerHTML = '<div class="empty">暂时无法加载模型报警</div>';
    });
    Promise.all([statusJob, eventsJob]).finally(function () {
      busy = false; $('mlRefresh').disabled = false;
      if (more || token !== generation) setTimeout(function () { pollModel(true); }, 0);
    });
  }
  $('mlRefresh').addEventListener('click', function () { pollModel(true); });
  $('mlAck').addEventListener('click', function () {
    pending = []; savePending(); renderPending(); renderRows();
    $('mlAnnouncement').textContent = '已确认收到当前模型报警。';
  });
  $('mlSound').addEventListener('click', async function () {
    if (sound) {
      sound = false; this.textContent = '启用报警声音'; this.setAttribute('aria-pressed','false');
      $('mlSoundNote').textContent = '模型报警声音已关闭，网页仍显示提醒。'; return;
    }
    try {
      ensureAudio();
      if (actx.state !== 'running') await actx.resume();
      if (actx.state !== 'running') throw new Error('Audio blocked');
      sound = true; this.textContent = '关闭报警声音'; this.setAttribute('aria-pressed','true');
      $('mlSoundNote').textContent = '声音已启用；请保持本页面打开。刷新页面后需重新启用。';
      beep(660, .15, .15);
    } catch (e) { $('mlSoundNote').textContent = '浏览器未允许播放声音，请再次点击启用；网页提醒仍可使用。'; }
  });
  $('mlMore').addEventListener('click', function () {
    if (olderBusy || before === null) return;
    var token = generation; olderBusy = true; this.disabled = true;
    requestJson(eventsUrl({before:before,limit:20})).then(function (data) {
      if (token !== generation || activeDevice() !== scope) return;
      merge(data.events.filter(validEvent)); before = data.next_before; hasOlder = data.has_more;
      if (!eventError) feedback('');
    }).catch(function () { if (token === generation) feedback('更早模型报警加载失败，请再次点击重试。'); })
      .finally(function () { olderBusy = false; if (token === generation) renderRows(); });
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) pollModel(true); });
  window.addEventListener('fd-device-change', function () { pollModel(true); });
  setInterval(function () { pollModel(false); }, 1000);
  pollModel(true);
})();
