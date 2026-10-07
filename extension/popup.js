const API = 'http://127.0.0.1:8377';
const $ = (s) => document.querySelector(s);

// ── 状态探测 ──
fetch(API + '/api/health')
  .then((r) => r.json())
  .then((h) => {
    const el = $('#status');
    if (!el) return;
    el.className = 'status-badge status-ok';
    const mode = h.mode === 'core' ? 'core' : `${(h.words || 0).toLocaleString()} forms`;
    el.textContent = `✓ Running (${mode})`;
  })
  .catch(() => {
    const el = $('#status');
    if (el) {
      el.className = 'status-badge status-bad';
      el.textContent = '✗ Not running';
    }
  });

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

$('#opt-scan-page').onclick = () => sendToActiveTab({ type: 'scan-full-page' });
$('#opt-open-panel').onclick = () => sendToActiveTab({ type: 'open-panel' });
$('#opt-clear-hl').onclick = () => sendToActiveTab({ type: 'clear-highlights' });

// Dashboard = server frontend（rules / proposals / settings）
$('#opt-open-dashboard').onclick = () => {
  chrome.tabs.create({ url: API + '/' });
  window.close();
};
