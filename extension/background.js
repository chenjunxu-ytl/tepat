/* background.js — service worker：右键菜单 + 快捷键 + exe 状态 */

// ── 右键菜单（用户裁决 2026-10-02：选中后右键直达功能，不要浮条）──
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get({ prpmEnabled: true }, (s) => {
    chrome.storage.sync.set({ prpmEnabled: s.prpmEnabled });
  });
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: 'bmc-root',
      title: 'Tepat',
      contexts: ['selection']
    });
    chrome.contextMenus.create({
      id: 'bmc-prpm',
      parentId: 'bmc-root',
      title: '📖 PRPM lookup (dictionary)',
      contexts: ['selection']
    });
    chrome.contextMenus.create({
      id: 'bmc-check',
      parentId: 'bmc-root',
      title: '✓ Check text (evidence + rules)',
      contexts: ['selection']
    });
    chrome.contextMenus.create({
      id: 'bmc-rulebook',
      parentId: 'bmc-root',
      title: '📘 Rule Book (settled rules)',
      contexts: ['selection']
    });
    chrome.contextMenus.create({
      id: 'bmc-raise',
      parentId: 'bmc-root',
      title: '✗ Raise Error (log a mistake)',
      contexts: ['selection']
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab?.id) return;
  const type = { 'bmc-prpm': 'context-prpm', 'bmc-check': 'context-check',
                 'bmc-rulebook': 'context-rulebook', 'bmc-raise': 'context-raise' }[info.menuItemId];
  if (!type) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type, text: info.selectionText || '' });
  } catch {
    // content script 未注入（chrome:// 页面等）——忽略
  }
});

// ── 快捷键触发（chrome.commands）──
chrome.commands.onCommand.addListener(async (cmd) => {
  if (cmd !== 'check-selection') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url) return;
  if (tab.url.startsWith('chrome://') || tab.url.startsWith('edge://') || tab.url.startsWith('about:')) return;

  try {
    await chrome.tabs.sendMessage(tab.id, { type: 'check-selection' });
  } catch {
    // 若页面打开时间较早未加载新 content.js，动态注入兜底
    try {
      await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ['content.css'] });
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['results.js', 'content.js'] });
      setTimeout(() => {
        chrome.tabs.sendMessage(tab.id, { type: 'check-selection' }).catch(() => {});
      }, 100);
    } catch {}
  }
});
