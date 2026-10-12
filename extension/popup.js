const API = 'http://127.0.0.1:8377';
const $ = (s) => document.querySelector(s);

// ── 状态：Tepat 左侧的点（绿=在跑，红=没跑；不再显示文字徽章）──
fetch(API + '/api/health')
  .then((r) => r.json())
  .then(() => {
    const dot = $('#dot');
    if (dot) dot.classList.add('ok');
  })
  .catch(() => { /* dot stays red */ });

// ── update 按钮（用户裁决 2026-10-12）：popup 打开时问 server 有没有新
// release（/api/extension-update，server 端 60s 缓存）。extension 或 server
// 有新版 → 最左侧脉冲下载 icon → 点击开 release 页（浏览器不允许网页代点
// chrome://extensions 的刷新，装包/重载仍需手动）。title 带版本对比。
fetch(API + '/api/extension-update')
  .then((r) => r.json())
  .then((r) => {
    const what = [];
    if (r.update_available) what.push(`extension ${r.local_version || '?'} → ${r.latest_version}`);
    if (r.server_update_available) what.push(`server ${r.server_version || '?'} → ${r.latest_version}`);
    if (!what.length) return;
    const btn = $('#opt-update');
    if (!btn) return;
    btn.classList.add('show');
    btn.title = `Update available: ${what.join(' · ')} — click to open the release`;
    btn.onclick = () => {
      chrome.tabs.create({ url: r.release_url || `https://github.com/chenjunxu-ytl/tepat/releases` });
      window.close();
    };
  })
  .catch(() => { /* server 没跑/端点失败 → 不显示按钮 */ });

// 获取当前活动标签页并发送指令（若页面未注入 content script 则自动注入后发送）
async function sendToActiveTab(msg) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;

  // 内部页面（chrome://, edge://, about:）无法注入
  if (!tab.url || tab.url.startsWith('chrome://') || tab.url.startsWith('edge://') ||
      tab.url.startsWith('chrome-extension://') || tab.url.startsWith('about:')) {
    alert('This action only works on regular web pages (not browser settings pages).');
    return;
  }

  try {
    await chrome.tabs.sendMessage(tab.id, msg);
    window.close();
  } catch {
    // 页面未刷新导致旧页面没有加载新 content.js，动态注入兜底
    try {
      await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ['content.css'] });
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['results.js', 'content.js'] });
      setTimeout(async () => {
        try {
          await chrome.tabs.sendMessage(tab.id, msg);
          window.close();
        } catch {
          alert('Please refresh this page once to enable checking.');
        }
      }, 100);
    } catch {
      alert('Please refresh this page once to enable checking.');
    }
  }
}

// 检查此页正文（用户裁决 2026-10-12）：不再整页铺高亮（收集→scan→offset
// 映射在 SPA 上不可靠）——收集正文文本送检查面板，命中卡片点击时再在页面
// 上高亮对应片段。
$('#opt-scan-page').onclick = () => sendToActiveTab({ type: 'check-page-text' });
$('#opt-clear-hl').onclick = () => sendToActiveTab({ type: 'clear-highlights' });

// 右上角 ⟳：从 GitHub 拉最新规则/词表（复用 server 的 /api/sync——
// 拉 rules.json/indo_words.json/word-overrides.json 并热重载）。本按钮
// 只触发拉取，结果反馈靠 title + 图标短暂旋转。
$('#opt-sync').onclick = async (e) => {
  const btn = e.currentTarget;
  btn.style.transition = 'none';
  btn.style.transform = 'rotate(0deg)';
  requestAnimationFrame(() => {
    btn.style.transition = 'transform .7s cubic-bezier(.3,.6,.2,1)';
    btn.style.transform = 'rotate(360deg)';
  });
  try {
    const r = await fetch(API + '/api/sync', { method: 'POST' }).then(x => x.json());
    const ok = Object.values(r.files || {}).every(f => f.ok);
    btn.title = ok
      ? `Synced from GitHub — ${Object.entries(r.files).map(([f, v]) => `${f}: ${v.bytes || 0}B`).join(' · ')}`
      : `Sync failed — ${Object.entries(r.files || {}).filter(([, v]) => !v.ok).map(([f, v]) => `${f}: ${v.error}`).join('; ')}`;
  } catch (err) {
    btn.title = `Sync failed: ${err.message || 'server unreachable'}`;
  }
  setTimeout(() => { btn.style.transform = ''; btn.style.transition = ''; }, 800);
};

// 右上角 💬：打开浮动检查面板（选中文字直接送查，没选中开空面板）
$('#opt-open-panel').onclick = () => sendToActiveTab({ type: 'open-panel' });

// 右上角 ⚑：报告本页主观问题（general flag，区别于词/规则级 flag）
$('#opt-flag').onclick = () => sendToActiveTab({ type: 'open-general-flag' });

// Dashboard = server frontend（settings icon）
$('#opt-open-dashboard').onclick = () => {
  chrome.tabs.create({ url: API + '/' });
  window.close();
};
