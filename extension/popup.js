const API = 'http://127.0.0.1:8377';
const $ = (s) => document.querySelector(s);

// ── 状态探测 ──
fetch(API + '/api/health')
  .then((r) => r.json())
  .then((h) => {
    const el = $('#status');
    if (el) {
      el.className = 'status-badge status-ok';
      el.textContent = `✓ Aktif (${h.words.toLocaleString()} bentuk DBP)`;
    }
  })
  .catch(() => {
    const el = $('#status');
    if (el) {
      el.className = 'status-badge status-bad';
      el.textContent = '✗ Tepat Tidak Aktif';
    }
  });

// ── 日志数量显示 ──
function refreshLogCount() {
  chrome.storage.local.get({ log: [] }, (s) => {
    const el = $('#log-count');
    if (el) el.textContent = s.log.length;
  });
}
refreshLogCount();

// 获取当前活动标签页并发送指令（若页面未注入 content script 则自动注入后发送）
async function sendToActiveTab(msg) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;

  // 内部页面（chrome://, edge://, about:）无法注入
  if (!tab.url || tab.url.startsWith('chrome://') || tab.url.startsWith('edge://') || tab.url.startsWith('chrome-extension://') || tab.url.startsWith('about:')) {
    alert('Fungsi ini hanya boleh dijalankan di laman web biasa (bukan laman tetapan pelayar).');
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
      // 稍微延迟让 content script 监听器就绪
      setTimeout(async () => {
        try {
          await chrome.tabs.sendMessage(tab.id, msg);
          window.close();
        } catch {
          alert('Sila muat semula (refresh) laman web ini sekali untuk membolehkan semakan.');
        }
      }, 100);
    } catch {
      alert('Sila muat semula (refresh) laman web ini sekali untuk membolehkan semakan.');
    }
  }
}

// ── 选项 1: 全页扫描并在页面高亮标记 ──
$('#opt-scan-page').onclick = () => {
  sendToActiveTab({ type: 'scan-full-page' });
};

// ── 选项 2: 打开弹窗 (在页面显示我们做好的高亮弹窗) ──
$('#opt-open-panel').onclick = () => {
  sendToActiveTab({ type: 'open-panel' });
};

// ── 选项 3: 导出日志 JSON ──
$('#opt-export-log').onclick = () => {
  chrome.storage.local.get({ log: [] }, (s) => {
    if (!s.log.length) {
      alert('Tiada log kesalahan tersimpan untuk dieksport.');
      return;
    }
    const blob = new Blob([JSON.stringify(s.log, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `tepat-log-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    window.close();
  });
};

// ── 清除全页高亮标记 ──
const btnClearHl = $('#opt-clear-hl');
if (btnClearHl) {
  btnClearHl.onclick = () => {
    sendToActiveTab({ type: 'clear-highlights' });
  };
}
