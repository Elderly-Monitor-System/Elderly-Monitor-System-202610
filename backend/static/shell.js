// 共享主题与布局
var THEME_KEY = 'fd-theme';
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  var button = document.getElementById('btnTheme');
  if (button) button.textContent = theme === 'dark' ? '浅色模式' : '深色模式';
}
function initTheme() {
  var saved;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
  applyTheme(saved === 'light' || saved === 'dark' ? saved :
    (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
}
function toggleTheme() {
  var theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) {}
  applyTheme(theme);
  if (typeof refreshChart === 'function') refreshChart();
  if (typeof lastSnapshot !== 'undefined' && lastSnapshot) { lastSig = ''; render(lastSnapshot); }
}
function toggleFullscreen() {
  var action = document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  if (action && action.catch) action.catch(function () {});
}
document.addEventListener('fullscreenchange', function () {
  var button = document.getElementById('btnFs');
  if (button) button.textContent = document.fullscreenElement ? '退出全屏' : '全屏显示';
});
document.addEventListener('click', function (event) {
  document.querySelectorAll('.settings[open]').forEach(function (details) {
    if (!details.contains(event.target)) details.open = false;
  });
});
document.addEventListener('keydown', function (event) {
  if (event.key === 'Escape') document.querySelectorAll('.settings[open]').forEach(function (d) { d.open = false; });
});
initTheme();
document.addEventListener('DOMContentLoaded', initTheme);
