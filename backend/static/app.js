// 看板状态、曲线与报警

var POLL_MS   = 500;     // 状态轮询（ms）
var CURVE_MS  = 5000;    // 曲线刷新间隔
var STAT_MS   = 10000;   // 今日统计刷新间隔
var OFFLINE_SECS = 8;    // 离线阈值（s）
var lastSig = "";        // 指标快照缓存
var lastEc  = -1;        // 事件总数缓存
var lastEvents = [];     // 未筛选的最近事件缓存
var polling = false;
var failStreak = 0;      // 连续失败次数，≥3 显示断连
var actx = null;
var histPageNum = 1, histTotal = 0, histPer = 50;
var etypeFilter = "";    // 事件类型筛选
var lastSamples = null;  // 曲线缓存，缩放时重绘
var wasOnline = null;    // 上次在线状态
var unseen = 0;          // 未读报警数
var lastSnapshot = null;
var ackEc = null;
try { var savedAck = localStorage.getItem('fd-ack-count'); if (savedAck !== null) ackEc = Number(savedAck); } catch(e) {}
if (!Number.isFinite(ackEc)) ackEc = null;
var nextPollAt = 0;
var sampleRequestId = 0;
var historyRequestId = 0;
var curveRange = '3600';

// 曲线参考阈值：2.5g / 45° / 空气 60
var TH_M = 2.5, TH_ANG = 45, TH_AIR = 60;
var AIR_GOOD = 40;   // 空气恢复阈值
// 离线区间标注
var GAP_MIN_S = 45;     // 断开阈值下限（s）
var GAP_MULT  = 5;      // 断开阈值倍率
var GAP_MAX_S = 1800;   // 断开阈值上限（s）
var GAP_LABEL_MIN_PX = 70;  // 标签显示最小宽度（px）
var curDeviceId = null;     // 当前设备，曲线按此筛选
// 空气等级：[文字色, 底色, 标签]
// 等级：正常、一般、偏高、超标
var AIR_UI = {
  dark:  [["#4ade80","rgba(74,222,128,.14)","正常"],
          ["#fbbf24","rgba(251,191,36,.14)","一般"],
          ["#fb923c","rgba(251,146,60,.15)","偏高"],
          ["#f87171","rgba(248,113,113,.18)","超标·请通风"]],
  light: [["#15803d","rgba(21,128,61,.10)","正常"],
          ["#92400e","rgba(146,64,14,.10)","一般"],
          ["#c2410c","rgba(194,65,12,.10)","偏高"],
          ["#b91c1c","rgba(185,28,28,.10)","超标·请通风"]]
};

var STATE_CN = {ARMED:"正常监测", TILT_WAIT:"倾斜待确认", TILT_ALARM:"倾倒报警",
                FALL_IMPACT:"跌落冲击", FALL_STILL:"落地确认", OFFLINE:"设备离线", UNKNOWN:"未知"};
var TYPE_CN = {TILT:"倾倒", FALL:"跌落", AIR:"空气污染"};
var EV_CLS = {TILT:"tilt", FALL:"fall", AIR:"air"};   // 事件类型 → tag 颜色 class
var EV_SEV = {TILT:1, FALL:1, AIR:0};                 // 1 强报警，0 轻提示


var DEV_KEY  = "fd-devmode";   // 布局：auto / phone / desktop
function cssVar(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim() || "#000"; }

// 设备布局预览：自动 / 手机 / 桌面
function applyDevMode(mode){
  document.body.classList.toggle("dev-phone", mode === "phone");
  document.body.classList.toggle("dev-desktop", mode === "desktop");
  var seg = document.getElementById("devMode");
  if(seg) seg.querySelectorAll("button").forEach(function(b){
    var on = b.getAttribute("data-mode") === mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
}
function initDevMode(){
  var saved = null;
  try{ saved = localStorage.getItem(DEV_KEY); }catch(e){}
  var mode = (saved === "phone" || saved === "desktop") ? saved : "auto";
  applyDevMode(mode);
  var seg = document.getElementById("devMode");
  if(seg) seg.addEventListener("click", function(ev){
    var t = ev.target;
    while(t && t !== seg && t.nodeName !== "BUTTON") t = t.parentNode;
    if(!t || t === seg) return;
    var m = t.getAttribute("data-mode") || "auto";
    try{ localStorage.setItem(DEV_KEY, m); }catch(e){}
    applyDevMode(m);
    refreshChart();
  });
}


function ensureAudio(){
  if(!actx) actx = new (window.AudioContext||window.webkitAudioContext)();
  if(actx.state==="suspended") actx.resume();
}
function beep(freq, dur, vol){
  try { ensureAudio(); } catch(e) { return; }
  var o = actx.createOscillator(), g = actx.createGain();
  var f = freq || 880, d = dur || 0.4, v = vol || 0.4;
  o.type = "square"; o.frequency.value = f;
  o.connect(g); g.connect(actx.destination);
  g.gain.setValueAtTime(v, actx.currentTime);
  g.gain.exponentialRampToValueAtTime(0.001, actx.currentTime + d);
  o.start(); o.stop(actx.currentTime + d);
}
function testBeep(){ ensureAudio(); beep(); }


function pad(n){ return (n<10?"0":"")+n; }
function fmtClock(t){
  if(!t) return "--";
  var d = new Date(t*1000);
  return pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds());
}
function fmtUp(s){
  if(isNaN(s)) return "--";
  s = Math.floor(s);
  var h = Math.floor(s/3600), m = Math.floor(s%3600/60), sec = s%60;
  return h>0 ? h+"小时"+m+"分" : m+"分"+sec+"秒";
}
function fmtTs(e){
  // 接收时间；悬停显示运行时长
  if(e && e.created_at) return e.created_at;
  return "--";
}
function fmtAgo(ts){
  // 相对接收时间
  if(!ts) return "--";
  var t = parseServerTime(ts);
  if(isNaN(t)) return ts;
  var diff = Math.floor((Date.now() - t)/1000);
  if(diff < 0) diff = 0;
  if(diff < 60) return "刚刚";
  if(diff < 3600) return Math.floor(diff/60)+" 分钟前";
  if(diff < 86400) return Math.floor(diff/3600)+" 小时前";
  return Math.floor(diff/86400)+" 天前";
}

function parseServerTime(ts){
  if(!ts) return NaN;
  return new Date(ts.replace(' ', 'T') + (/[zZ]|[+-]\d\d:\d\d$/.test(ts) ? '' : '+08:00')).getTime();
}
function requestJson(url){
  var controller = new AbortController();
  var timer = setTimeout(function(){ controller.abort(); }, 8000);
  return fetch(url, {signal:controller.signal, cache:'no-store'}).then(function(response){
    if(!response.ok) throw new Error('请求失败');
    return response.json();
  }).finally(function(){clearTimeout(timer);});
}
function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});
}
function eventMarkup(e){
  var locationUrl = '/map?device_id=' + encodeURIComponent(e.device_id || '') + '&event_time=' + encodeURIComponent(e.created_at || '');
  var photoLink = '';
  if ((e.etype === 'FALL' || e.etype === 'AIR') && e.boot_id) {
    var photoUrl = '/photos?device_id=' + encodeURIComponent(e.device_id || '') + '&boot_id=' + encodeURIComponent(e.boot_id) + '&event_seq=' + encodeURIComponent(e.seq);
    photoLink = '<a class="event-location" href="' + escapeHtml(photoUrl) + '">现场照片</a>';
  }
  return '<div class="ev"><span class="tp '+ (EV_CLS[e.etype] || 'tilt') +'">' + escapeHtml(TYPE_CN[e.etype] || e.etype) + '</span>' +
    '<span class="tm" title="设备运行 ' + escapeHtml(fmtUp(e.up_ms/1000)) + '">' + escapeHtml(fmtTs(e)) + '</span>' +
    photoLink + '<a class="event-location" href="' + escapeHtml(locationUrl) + '" title="按记录接收时间查找附近定位；离线补发可能与发生时刻不同">附近定位</a></div>';
}

// 实时状态渲染
function render(d){
  lastSnapshot = d;
  // 切换设备时刷新曲线
  var devId = d.device_id || null;
  if (devId && devId !== curDeviceId){
    curDeviceId = devId;
    window.dispatchEvent(new Event('fd-device-change'));
    fetchSamples(true);
  }
  var isOnline = d.online && (d.server_time - (d.last_seen||0)) < OFFLINE_SECS;
  // server_time 参与快照更新
  var sig = (isOnline?"1":"0")+"|"+d.m+"|"+d.ang+"|"+d.up+"|"+d.state+"|"+d.alarm
           +"|"+d.air+"|"+d.abad+"|"+d.server_time+"|"+devId;
  if (sig !== lastSig) {
    lastSig = sig;
    var badge = document.getElementById("badge");
    var devOff = document.getElementById("devOff");
    if (!isOnline) {
      // 设备离线提示
      var lastTxt = (d.last_seen ? fmtClock(d.last_seen) : "时间未知");
      badge.textContent = "设备离线";
      badge.className = "badge dead";
      badge.title = "上次在线 " + lastTxt;
      if (wasOnline === true) beep(430, .35, .26);       // 仅在线转离线时发声
      if (devOff) {
        devOff.innerHTML = "设备离线 · 上次在线 <b>" + lastTxt +
                           "</b> —— 请检查老人与设备电源 / 网络";
        devOff.classList.add("show");
      }
    } else {
      badge.textContent = STATE_CN[d.state] || d.state;
      badge.className = "badge " + (d.state==="ARMED" ? "ok"
                  : (d.state==="TILT_WAIT"||d.state==="FALL_STILL" ? "warn" : "bad"));
      badge.title = "";
      if (devOff) { devOff.classList.remove("show"); devOff.innerHTML = ""; }
    }
    wasOnline = isOnline;
    document.getElementById('statusTitle').textContent = !isOnline ? '设备暂未在线' :
      (d.alarm ? '设备正在报警' : d.state === 'ARMED' ? '设备正在监测' : '请关注设备状态');
    document.getElementById('statusDescription').textContent = !isOnline ? '保留最后一次读数，请检查设备电源与网络连接。' :
      (d.alarm ? '检测到报警，请及时确认设备与使用者的情况。' : d.state === 'ARMED' ? '当前未检测到姿态报警，监测数据持续更新。' : '当前状态：' + (STATE_CN[d.state] || '未知') + '。请留意后续变化。');
    document.getElementById('lastUpdate').textContent = d.last_seen ? '最近更新 ' + Math.max(0,Math.floor(d.server_time-d.last_seen)) + ' 秒前' : '尚未收到数据';
    document.getElementById("m").textContent = (d.m!=null) ? d.m.toFixed(2) : "--";
    document.getElementById("ang").textContent = (d.ang!=null) ? d.ang.toFixed(1) : "--";
    document.getElementById("up").textContent = fmtUp(d.up/1000);
    document.getElementById("dev").textContent = d.device_id || "--";
    document.getElementById("clock").textContent =
      (d.server_time ? "当前时间 " + fmtClock(d.server_time) : "未同步网络时间");

    // 空气指数与等级
    var aqEl = document.getElementById("air");
    var tagEl = document.getElementById("airTag");
    var lvl, pal;
    if (d.air == null) lvl = -1;
    else if (d.abad)   lvl = 3;
    else if (d.air >= TH_AIR)  lvl = 2;
    else if (d.air >= AIR_GOOD) lvl = 1;
    else                lvl = 0;
    pal = (document.documentElement.dataset.theme === "light") ? AIR_UI.light : AIR_UI.dark;
    var ac = (lvl < 0) ? "" : pal[lvl][0];
    var ab = (lvl < 0) ? "" : pal[lvl][1];
    var al = (lvl < 0) ? "" : pal[lvl][2];
    tagEl.hidden = (lvl < 0 || !al);
    aqEl.textContent = (lvl < 0) ? "--" : d.air.toFixed(0);
    aqEl.style.color = ac;
    tagEl.textContent = al;
    tagEl.style.color = ac;
    tagEl.style.background = ab;
  }

  // 事件计数变化时更新报警
  if (d.ec === lastEc) return;
  var delta = d.ec - lastEc;
  var isNew = (lastEc !== -1 && delta > 0);     // 首帧不触发报警
  lastEc = d.ec;
  if (ackEc === null || ackEc > d.ec) {
    ackEc = d.ec;
    try { localStorage.setItem('fd-ack-count', String(ackEc)); } catch(e) {}
  }
  unseen = Math.max(0, d.ec - ackEc);
  lastEvents = d.events || [];
  renderEventList();
  updateUnread();
  if (lastSamples && lastSamples.length) drawCurve(lastSamples);

  if (isNew) {
    var added = lastEvents.slice(-delta);
    var newest = added.find(function(e){ return EV_SEV[e.etype] === 1; }) || added[added.length-1] || {};
    var label = TYPE_CN[newest.etype] || "报警";
    if (EV_SEV[newest.etype] === 0) softToast(label, delta);
    else showAlert(label, delta);
  }
}

// 按筛选重绘最近事件
function renderEventList(){
  var box = document.getElementById("events");
  var list = lastEvents;
  if(etypeFilter) list = list.filter(function(e){ return e.etype === etypeFilter; });
  if(!list.length){
    box.innerHTML = '<div class="empty">' + (etypeFilter ? "当前筛选下暂无记录" : "暂无报警记录") + '</div>';
    return;
  }
  box.innerHTML = list.slice().reverse().map(eventMarkup).join('');
}

function poll(){
  if(polling) return;                            // 禁止请求重叠
  if(document.hidden) return;                    // 后台页暂停轮询
  if(Date.now() < nextPollAt) return;
  polling = true;
  requestJson("/api/state").then(function(d){
    if(failStreak) lastSig = '';
    failStreak = 0;
    document.getElementById("offBanner").classList.remove("show");
    render(d);
  }).catch(function(){                          // 下轮重试
    failStreak++;
    nextPollAt = Date.now() + Math.min(10000, 500 * Math.pow(2, failStreak));
    if(failStreak >= 3) document.getElementById("offBanner").classList.add("show");
    document.getElementById('statusTitle').textContent = '服务连接暂时中断';
    document.getElementById('statusDescription').textContent = '正在重试连接；显示的是最后一次收到的数据。';
  }).finally(function(){ polling = false; });
}

// 事件类型筛选
function setFilter(btn){
  var old = etypeFilter;
  etypeFilter = btn.getAttribute("data-etype") || "";
  if(etypeFilter === old) return;
  document.querySelectorAll("#filters button").forEach(function(b){
    var on = b === btn;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  histPageNum = 1;
  renderEventList();
  loadHist();
}

// 历史事件分页
function loadHist(){
  var requestId = ++historyRequestId;
  var q = "/api/events?page="+histPageNum+"&per_page="+histPer;
  if(etypeFilter) q += "&etype="+etypeFilter;
  requestJson(q)
   .then(function(d){
     if(requestId !== historyRequestId) return;
     histTotal = d.total;
     var pages = Math.max(1, Math.ceil(d.total / histPer));
     document.getElementById("histInfo").textContent =
       "第 "+histPageNum+"/"+pages+" 页 · 共 "+d.total+" 条";
     document.getElementById('histPrev').disabled = histPageNum <= 1;
     document.getElementById('histNext').disabled = histPageNum >= pages;
     var box = document.getElementById("hist");
     if(!d.items || !d.items.length){
       box.innerHTML = '<div class="empty">' + (etypeFilter ? "当前筛选下无历史记录" : "暂无历史记录") + '</div>';
       return;
     }
     box.innerHTML = d.items.slice().reverse().map(eventMarkup).join('');
   })
   .catch(function(){
     if(requestId !== historyRequestId) return;
     document.getElementById('hist').innerHTML = '<div class="empty">历史记录读取失败。<button class="btn" type="button" onclick="loadHist()">重新加载</button></div>';
     document.getElementById('histInfo').textContent = '暂时无法读取';
   });
}
function histPage(delta){
  var pages = Math.max(1, Math.ceil(histTotal / histPer));
  histPageNum = Math.min(pages, Math.max(1, histPageNum + delta));
  loadHist();
}

// 今日统计
function loadStats(){
  requestJson("/api/stats")
   .then(function(d){
     document.getElementById("stToday").textContent = d.today;
     document.getElementById("stTotal").textContent = d.total;
     document.getElementById("stLast").textContent = fmtAgo(d.last);
   })
   .catch(function(){ document.getElementById('stLast').title = '统计更新失败，保留上次结果'; });
}

// 趋势曲线：模值、倾角、空气
var CH = { bg:"#0f172a", grid:"#334155", m:"#22c55e", ang:"#f97316", air:"#a78bfa", th:"#ef4444", gap:"#fbbf24" };
function loadChartColors(){
  CH.bg  = cssVar("--ch-bg");   CH.grid = cssVar("--ch-grid");
  CH.m   = cssVar("--ch-m");    CH.ang  = cssVar("--ch-ang");
  CH.air = cssVar("--ch-air");  CH.th  = cssVar("--ch-th");
  CH.gap = cssVar("--ch-gap");  // 离线区间主题色
}

function fetchSamples(showLoading){
  var requestId = ++sampleRequestId;
  var message = document.getElementById('chartMessage');
  document.getElementById('chartTooltip').hidden = true;
  if(showLoading){lastSamples = null; message.textContent = '正在加载趋势数据…'; message.hidden = false;}
  var now = Math.floor(Date.now()/1000);
  var since;
  if(curveRange === 'today'){
    var bj = new Date(Date.now()+8*3600000);
    since = Date.UTC(bj.getUTCFullYear(),bj.getUTCMonth(),bj.getUTCDate())/1000 - 8*3600;
  } else since = now - Number(curveRange);
  // 曲线按设备筛选
  var url = "/api/samples?max_points=300&since="+since+'&until='+now;
  if (curDeviceId) url += "&device_id=" + encodeURIComponent(curDeviceId);
  requestJson(url)
   .then(function(d){
     if(requestId !== sampleRequestId) return;
     lastSamples = d && Array.isArray(d.items) ? d.items : [];
     message.hidden = lastSamples.length > 0;
     if(lastSamples.length) {
       drawCurve(lastSamples);
       document.getElementById('curveStatus').textContent = '点击曲线查看读数 · 报警标记为最近事件的记录接收时间';
     }
     else {message.textContent = '所选时间范围暂无采样数据'; document.getElementById('curveStatus').textContent = '设备上报后将在这里显示趋势';}
   })
   .catch(function(){
     if(requestId !== sampleRequestId) return;
     message.hidden = false;
     message.innerHTML = '趋势数据读取失败<button class="btn" type="button" onclick="fetchSamples(true)">重新加载</button>';
   });
}
function refreshChart(){ if(lastSamples && lastSamples.length){ drawCurve(lastSamples); } else { fetchSamples(); } }

function yv(v, ymax, y0, y1){ return y0 + (1 - v/ymax) * (y1 - y0); }

function drawCurve(items){
  if(!items || !items.length) return;
  loadChartColors();
  var c = document.getElementById("curve");
  var dpr = window.devicePixelRatio || 1;
  var W = c.clientWidth || 300;
  var H = c.clientHeight || 230;
  var bw = Math.round(W*dpr), bh = Math.round(H*dpr);
  if(c.width !== bw) c.width = bw;              // DPR 位图尺寸
  if(c.height !== bh) c.height = bh;
  var ctx = c.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);            // 使用 CSS 像素坐标
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = CH.bg; ctx.fillRect(0, 0, W, H);

  var t0 = items[0].ts, t1 = items[items.length-1].ts;
  if (t1 - t0 < 1) t1 = t0 + 1;
  var GUT = 20;                                 // 底部为时间轴预留的专用带
  var P = H - GUT;
  var s = P/3;
  var xs = function(ts){ return (ts - t0) / (t1 - t0) * W; };
  var span = t1 - t0, ticks = 4;

  // 各段量程（网格线与曲线共用同一套）
  var mmax = 4;
  items.forEach(function(p){ if(p.m > mmax) mmax = p.m; });
  mmax = Math.ceil(mmax * 1.2);
  var amax = 90;
  items.forEach(function(p){ if(p.ang > amax) amax = p.ang; });
  amax = Math.ceil(amax * 1.1);

  // 时间与量程网格
  ctx.lineWidth = 1;
  ctx.strokeStyle = hexRgba(CH.grid, .38);
  for(var i = 0; i < ticks; i++){
    var gx = xs(t0 + span * i / (ticks - 1));
    ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, P); ctx.stroke();
  }
  ctx.strokeStyle = hexRgba(CH.grid, .52);
  quadGrid(ctx, W, 0,   s,   mmax);
  quadGrid(ctx, W, s,   2*s, amax);
  quadGrid(ctx, W, 2*s, P,   100);

  // 离线区间底色
  var gapS  = computeGapS(items);
  var gaps  = findGapIntervals(items, gapS);
  drawGapBands(ctx, gaps, xs, P);
  drawGapLabels(ctx, gaps, xs);

  // 分段绘制，离线不连线
  drawLine(ctx, items, xs, "m",   CH.m,   mmax, 0,   s,   true, gapS);
  drawLine(ctx, items, xs, "ang", CH.ang, amax, s,   2*s, true, gapS);
  drawLine(ctx, items, xs, "air", CH.air, 100,  2*s, P,   true, gapS);

  // 图表分隔线
  ctx.strokeStyle = CH.grid; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, s);  ctx.lineTo(W, s);
  ctx.moveTo(0, 2*s); ctx.lineTo(W, 2*s);
  ctx.stroke();

  // 阈值线：2.5g / 45° / 空气 60
  ctx.setLineDash([6,5]); ctx.lineWidth = 1.5;
  ctx.strokeStyle = CH.th;
  ctx.fillStyle = CH.th; ctx.font = "11px sans-serif"; ctx.textAlign = "right";
  function dash(y, label){ ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke();
                           ctx.fillText(label, W - 6, y - 5); }
  dash(yv(TH_M,   mmax, 0,   s),  TH_M   + "g 冲击");
  dash(yv(TH_ANG, amax, s,   2*s), TH_ANG + "° 倾倒");
  dash(yv(TH_AIR, 100,  2*s, P),   TH_AIR + " 空气");
  ctx.setLineDash([]);

  // 图表仅标注最近事件
  var previousLabelX = -100;
  lastEvents.forEach(function(event){
    var ts = parseServerTime(event.created_at)/1000;
    if(!Number.isFinite(ts) || ts < t0 || ts > t1) return;
    var ex = xs(ts);
    ctx.strokeStyle = event.etype === 'AIR' ? CH.air : CH.th;
    ctx.lineWidth = 1; ctx.setLineDash([2,4]);
    ctx.beginPath();ctx.moveTo(ex,18);ctx.lineTo(ex,P);ctx.stroke();ctx.setLineDash([]);
    if(ex-previousLabelX > 48){
      ctx.font = '11px sans-serif';ctx.fillStyle=ctx.strokeStyle;
      ctx.textAlign = ex > W-35 ? 'right' : 'left';
      ctx.fillText(TYPE_CN[event.etype] || '报警',ex,12);previousLabelX=ex;
    }
  });

  // 底部时间轴
  var axCol = cssVar("--text-dim") || CH.grid;
  var mono  = cssVar("--mono") || "monospace";
  ctx.lineWidth = 1;
  ctx.strokeStyle = axCol;
  ctx.beginPath(); ctx.moveTo(0, P + .5); ctx.lineTo(W, P + .5); ctx.stroke();
  ctx.font = "600 11px " + mono;
  ctx.fillStyle = axCol;
  for(i = 0; i < ticks; i++){
    var tt = t0 + span * i / (ticks - 1);
    var tx = xs(tt);
    ctx.beginPath(); ctx.moveTo(tx, P + 1); ctx.lineTo(tx, P + 5); ctx.stroke();
    if(i === 0)            { ctx.textAlign = "left";  tx += 2; }   // 首标签左对齐
    else if(i === ticks-1) { ctx.textAlign = "right"; tx -= 2; }   // 末标签右对齐
    else                   { ctx.textAlign = "center"; }
    ctx.fillText(fmtAxis(tt, span), tx, P + 15);
  }
}
function quadGrid(ctx, W, y0, y1, ymax){        // 量程网格：四等分
  var i;
  for(i = 1; i < 4; i++){
    var y = yv(ymax * i / 4, ymax, y0, y1);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
}
function drawLine(ctx, items, xs, key, color, ymax, y0, y1, fill, gapS){
  var pts = [], i, j;
  items.forEach(function(p){
    var v = p[key];
    if (v == null) v = 0;                       // 旧数据缺失 air 时取 0
    pts.push({ x: xs(p.ts), y: y0 + (1 - v/ymax) * (y1 - y0) });
  });
  if(!pts.length) return;

  // 超过 gapS 时断开曲线
  var runs = [], cur = [pts[0]];
  for(i = 1; i < pts.length; i++){
    if(items[i].ts - items[i-1].ts > gapS){ runs.push(cur); cur = []; }
    cur.push(pts[i]);
  }
  runs.push(cur);

  // 渐变仅覆盖连续数据段
  if(fill){
    var grad = ctx.createLinearGradient(0, y0, 0, y1);
    grad.addColorStop(0, hexRgba(color, .13));
    grad.addColorStop(1, hexRgba(color, 0));
    ctx.fillStyle = grad;
    runs.forEach(function(run){
      if(run.length < 2) return;
      ctx.beginPath();
      ctx.moveTo(run[0].x, y1);
      for(j = 0; j < run.length; j++) ctx.lineTo(run[j].x, run[j].y);
      ctx.lineTo(run[run.length-1].x, y1);
      ctx.closePath(); ctx.fill();
    });
  }

  // 连续数据分段描线
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round";
  runs.forEach(function(run){
    if(run.length < 2) return;
    ctx.beginPath();
    for(j = 0; j < run.length; j++){
      if(j) ctx.lineTo(run[j].x, run[j].y); else ctx.moveTo(run[j].x, run[j].y);
    }
    ctx.stroke();
  });

  // 末端数值标记
  var lp = pts[pts.length-1];
  ctx.beginPath(); ctx.arc(lp.x, lp.y, 3, 0, 6.2832); ctx.fillStyle = color; ctx.fill();

  // 量程标签
  var t = (key === "m") ? (ymax.toFixed(1) + "g")
        : (key === "ang") ? (ymax + "°") : ("空气 " + ymax);
  ctx.fillStyle = color;
  ctx.font = "11px sans-serif"; ctx.textAlign = "left";
  ctx.fillText(t, 7, y0 + 13);
}

// 离线区间计算与绘制
function computeGapS(items){
  // 断开阈值：间隔中位数 × GAP_MULT
  var deltas = [], i;
  for(i = 1; i < items.length; i++){
    var dt = items[i].ts - items[i-1].ts;
    if(dt > 0) deltas.push(dt);               // 忽略非正时间间隔
  }
  var med = 0;
  if(deltas.length){ deltas.sort(function(a,b){ return a-b; }); med = deltas[deltas.length >> 1]; }
  var s = Math.max(GAP_MIN_S, med * GAP_MULT);
  var span = items[items.length-1].ts - items[0].ts;
  if(span > 0) s = Math.min(s, span * 0.5);   // 阈值上限：窗口时长的一半
  return Math.min(GAP_MAX_S, Math.max(GAP_MIN_S, s));
}
function findGapIntervals(items, gapS){
  var out = [], i;
  for(i = 1; i < items.length; i++){
    var dt = items[i].ts - items[i-1].ts;
    if(dt > gapS) out.push({ d0: items[i-1].ts, d1: items[i].ts, sec: dt });
  }
  return out;
}
function fmtGapLabel(g){
  var a = new Date(g.d0*1000), b = new Date(g.d1*1000);
  var dur = (g.sec < 60) ? ("约" + Math.max(1, Math.round(g.sec)) + "秒")
                         : ("约" + Math.round(g.sec/60) + "分钟");
  return "断开 " + pad(a.getHours()) + ":" + pad(a.getMinutes()) +
         "–" + pad(b.getHours()) + ":" + pad(b.getMinutes()) + " · " + dur;
}
function drawGapBands(ctx, gaps, xs, P){
  if(!gaps || !gaps.length) return;
  var i;
  for(i = 0; i < gaps.length; i++){
    var x0 = xs(gaps[i].d0), x1 = xs(gaps[i].d1);
    var w = Math.max(1, x1 - x0);
    ctx.fillStyle = hexRgba(CH.gap, .10);
    ctx.fillRect(x0, 0, w, P);
    ctx.strokeStyle = hexRgba(CH.gap, .6); ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x0)+.5, 0); ctx.lineTo(Math.round(x0)+.5, P);
    ctx.moveTo(Math.round(x1)+.5, 0); ctx.lineTo(Math.round(x1)+.5, P);
    ctx.stroke();
  }
}
function drawGapLabels(ctx, gaps, xs){
  if(!gaps || !gaps.length) return;
  ctx.font = "600 10px sans-serif"; ctx.textBaseline = "top"; ctx.textAlign = "left";
  var i;
  for(i = 0; i < gaps.length; i++){
    var g = gaps[i];
    var x0 = xs(g.d0), x1 = xs(g.d1);
    if(x1 - x0 < GAP_LABEL_MIN_PX) continue;            // 窄区间隐藏文字
    var full  = fmtGapLabel(g);
    var short = "断开 " + (g.sec < 60 ? ("约"+Math.max(1,Math.round(g.sec))+"秒")
                                      : ("约"+Math.round(g.sec/60)+"分钟"));
    var text  = (ctx.measureText(full).width  <= (x1-x0) - 6) ? full
              : (ctx.measureText(short).width <= (x1-x0) - 6) ? short : "";
    if(!text) continue;
    var tw = ctx.measureText(text).width;
    // 避开左侧量程标签
    var tx = Math.max(x0 + 4, 45);
    if(tx + tw > x1 - 2) tx = Math.max(x0 + 4, x1 - tw - 4);
    if(tx < x0 + 2 || tx + tw > x1 + 2) continue;
    ctx.fillStyle = hexRgba(CH.bg, .85);
    ctx.fillRect(tx - 2, 2, tw + 4, 13);
    ctx.fillStyle = CH.gap;
    ctx.fillText(text, tx, 3);
  }
  ctx.textBaseline = "alphabetic";
}
function hexRgba(color, a){                      // 十六进制颜色转 rgba
  var h = color.replace("#", "");
  if(h.length === 3) h = h.replace(/./g, function(x){ return x + x; });
  var n = parseInt(h, 16);
  return "rgba(" + (n>>16 & 255) + "," + (n>>8 & 255) + "," + (n & 255) + "," + a + ")";
}
function fmtAxis(ts, span){
  var d = new Date(ts * 1000);
  var hm = pad(d.getHours()) + ":" + pad(d.getMinutes());
  return (span >= 60) ? hm : hm + ":" + pad(d.getSeconds());
}

// 缩放后防抖重绘
var rzTimer = null;
window.addEventListener("resize", function(){
  if(!lastSamples) return;
  if(rzTimer) clearTimeout(rzTimer);
  rzTimer = setTimeout(function(){ rzTimer = null; drawCurve(lastSamples); }, 150);
});

// 未读报警，确认后清除
function updateUnread(){
  var c = document.getElementById("unreadChip");
  if(!c) return;
  if(unseen > 0){ c.hidden = false; c.textContent = unseen + " 条新报警 · 标记已读"; }
  else { c.hidden = true; c.textContent = ""; }
  document.getElementById('pendingAlert').hidden = unseen <= 0;
  document.getElementById('pendingTitle').textContent = unseen + ' 条新报警待确认';
  if(lastEvents.length){
    var latestEvent = lastEvents[lastEvents.length-1];
    document.getElementById('pendingDescription').textContent = '最新记录：' + (TYPE_CN[latestEvent.etype] || '报警') + ' · ' + fmtTs(latestEvent);
  }
}
function markRead(){
  ackEc = Math.max(0,lastEc);
  try { localStorage.setItem('fd-ack-count',String(ackEc)); } catch(e) {}
  unseen = 0;
  updateUnread();
  renderEventList();
  document.getElementById('overlay').classList.remove('show');
  hideToast();
}

// 设备报警提示
var toastTimer = null;
function hideToast(){
  var t = document.getElementById("toast");
  t.classList.remove("show");
  t.classList.remove("soft");
}
function showAlert(label, n){            // 跌倒/倾倒强报警
  if(toastTimer){ clearTimeout(toastTimer); toastTimer = null; }
  document.getElementById("overlay").classList.add("show");
  var t = document.getElementById("toast");
  t.classList.remove("soft");
  t.textContent = label + (n>1 ? "（本次"+n+"次）" : "");
  t.classList.add("show");
  beep();
  toastTimer = setTimeout(function(){
    document.getElementById("overlay").classList.remove("show");
    hideToast();
  }, 4000);
}
function softToast(label, n){            // 空气超标轻提示
  if(toastTimer){ clearTimeout(toastTimer); toastTimer = null; }
  document.getElementById("overlay").classList.remove("show");
  var t = document.getElementById("toast");
  t.classList.add("soft");
  t.textContent = label + (n>1 ? "（本次"+n+"次）" : "");
  t.classList.add("show");
  beep(660, 0.3, 0.22);                  // 一声低短蜂鸣
  toastTimer = setTimeout(hideToast, 6000);
}

// 启动
initTheme();
initDevMode();
document.getElementById('curveRange').addEventListener('click',function(event){
  var button = event.target.closest('button[data-seconds]');
  if(!button || button.dataset.seconds === curveRange) return;
  curveRange = button.dataset.seconds;
  this.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});
  fetchSamples(true);
});
function inspectChart(event){
  if(!lastSamples || !lastSamples.length) return;
  var canvas = document.getElementById('curve');
  var bounds = canvas.getBoundingClientRect();
  var ratio = Math.max(0,Math.min(1,(event.clientX-bounds.left)/bounds.width));
  var target = lastSamples[0].ts + ratio * (lastSamples[lastSamples.length-1].ts-lastSamples[0].ts);
  var point = lastSamples.reduce(function(best,p){return Math.abs(p.ts-target)<Math.abs(best.ts-target)?p:best;});
  var text = fmtClock(point.ts)+' · '+Number(point.m).toFixed(2)+' g · '+Number(point.ang).toFixed(1)+'° · 空气 '+Number(point.air).toFixed(0);
  var tip = document.getElementById('chartTooltip');
  tip.textContent = text; tip.hidden = false;
  tip.style.left = Math.max(0,Math.min(bounds.width-tip.offsetWidth,event.clientX-bounds.left-80))+'px';
  tip.style.top = Math.max(0,Math.min(bounds.height-40,event.clientY-bounds.top-45))+'px';
  if(event.type === 'click') document.getElementById('curveStatus').textContent = text;
}
document.getElementById('curve').addEventListener('pointermove',inspectChart);
document.getElementById('curve').addEventListener('click',inspectChart);
document.getElementById('curve').addEventListener('pointerleave',function(){document.getElementById('chartTooltip').hidden=true;});
document.addEventListener('visibilitychange',function(){if(!document.hidden){nextPollAt=0;poll();fetchSamples();loadStats();loadHist();}});
setInterval(poll, POLL_MS);
setInterval(fetchSamples, CURVE_MS);
setInterval(loadHist, 30000);     // 历史刷新：30s
setInterval(loadStats, STAT_MS);
poll();
loadHist();
fetchSamples(true);
loadStats();
