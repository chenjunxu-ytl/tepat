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
// 有新版 → 最左侧脉冲下载 icon → 点击：
//   1. 先尝试 server 自更新（/api/self-update，源码/bat 部署）——成功则
//      server 自动重启到新版，本页只提示 extension 是否还需手动重载
//   2. exe 部署/自更新失败 → 回退打开 release 页手动下载
fetch(API + '/api/extension-update')
  .then((r) => r.json())
  .then((r) => {
    const what = [];
    if (r.update_available) what.push(`extension ${r.local_version || '?'} → ${r.latest_version}`);
    if (r.server_update_available) what.push(`server ${r.server_version || '?'} → ${r.latest_version}`);
    if (!what.length) return;
    const btn = $('#opt-update');
    if (!btn) return;
    const latest = r.latest_version;
    const extNeedsManual = !!r.update_available;  // 浏览器不许代点重载
    btn.classList.add('show');
    btn.title = `Update available: ${what.join(' · ')} — click to update now`;
    btn.onclick = async () => {
      btn.title = 'Updating…';
      try {
        const res = await fetch(API + '/api/self-update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version: latest })
        }).then(x => x.json());
        if (res.ok) {
          // server 正在替换文件并重启（start.bat 循环拉起新版）——
          // 几秒后 health 探测确认回来
          btn.title = 'Server updated — restarting…';
          for (let i = 0; i < 30; i++) {
            await new Promise(rs => setTimeout(rs, 1000));
            try {
              await fetch(API + '/api/health').then(x => x.json());
              btn.title = `Server updated to ${latest}`;
              alert(`Server updated to ${latest} and restarted.` +
                (extNeedsManual ? `\n\nThe extension also needs a manual reload: open chrome://extensions and click ⟳ on the Tepat card.` : ''));
              window.close();
              return;
            } catch { /* 还没起来，继续等 */ }
          }
          alert('Server is still restarting — give it a few more seconds, then reload this popup.');
          return;
        }
        // 自更新不可用（exe 部署等）→ 打开 release 页
        alert(`${res.error || 'Self-update unavailable.'}\n\nOpening the release page for manual update.`);
      } catch {
        alert('Could not reach the server for self-update.\n\nOpening the release page.');
      }
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

// 页面整页扫描已砍（用户裁决 2026-10-12）：检查一律走浮动面板——右上 💬
// 打开，右键选中文本或面板内输入均可；命中卡片点击可在页面上高亮对应片段。

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
