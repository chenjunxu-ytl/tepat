/* content.js — Tepat content script
 *
 * 交互模型：
 *  1. 右键菜单直达功能（PRPM 直查 / Semak 语料规则 / Raise Error）
 *  2. 弹窗支持：
 *     - 点击外部区域自动关闭
 *     - 顶部拖拽移动
 *     - 弹窗内自带搜索框，可直接输入词语/句子回车重新查询
 *     - 状态使用直接的 ✓ / ✗ 徽章，无冗余文字，错误标红高亮
 *     - 去除过度 emoji 堆叠，采用可爱轻量的动效与圆润设计
 */
(() => {
  const PORT = 8377;
  const API = `http://127.0.0.1:${PORT}`;
  let prpmEnabled = true;

  chrome.storage.sync.get({ prpmEnabled: true }, (s) => { prpmEnabled = s.prpmEnabled; });
  chrome.storage.onChanged.addListener((ch) => {
    if (ch.prpmEnabled) prpmEnabled = ch.prpmEnabled.newValue;
  });

  let panel = null;
  let currentMode = 'prpm'; // 'prpm' | 'check' | 'raise'
  let isPinned = false;
  let isAutoGrab = false;
  let lastGrabbedText = '';
  let outsideClickListener = null;

  // 划词自动送入：当 isAutoGrab 激活且弹窗处于打开状态时，用户在页面上选中文本即自动送入并查询
  document.addEventListener('mouseup', () => {
    if (!isAutoGrab || !panel) return;
    setTimeout(() => {
      const sel = getPageSelection();
      if (!sel || sel.length < 2 || sel === lastGrabbedText) return;
      lastGrabbedText = sel;
      const input = panel.querySelector('.bmc-search-input');
      if (input) input.value = sel;
      const btnGrab = panel.querySelector('.bmc-grab-btn');
      if (btnGrab) {
        btnGrab.classList.add('bmc-btn-pop');
        setTimeout(() => btnGrab.classList.remove('bmc-btn-pop'), 300);
      }
      if (currentMode === 'check' || sel.includes(' ') || sel.length > 25) {
        runCheck(sel, false);
      } else {
        runPrpm(sel, false);
      }
    }, 10);
  });

  function removeUI() {
    if (outsideClickListener) {
      document.removeEventListener('pointerdown', outsideClickListener, true);
      outsideClickListener = null;
    }
    panel?.remove();
    panel = null;
    isPinned = false;
    lastGrabbedText = '';
  }

  // 页面内快捷键 Alt+S（使用捕获阶段 capture: true，防止被网页框架拦截）
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      removeUI();
      return;
    }
    const isAltS = (e.altKey && !e.ctrlKey && !e.metaKey) &&
      (e.key === 's' || e.key === 'S' || e.code === 'KeyS');

    if (isAltS) {
      e.preventDefault();
      e.stopPropagation();
      const sel = getPageSelection();
      if (sel && sel.length >= 2) {
        if (sel.includes(' ') || sel.length > 25) {
          runCheck(sel);
        } else {
          runPrpm(sel);
        }
      } else {
        if (panel) {
          removeUI();
        } else {
          openBlankPanel();
        }
      }
    }
  }, true);

  // ── 点击弹窗外部关闭 ──
  function attachOutsideClick() {
    if (outsideClickListener) {
      document.removeEventListener('pointerdown', outsideClickListener, true);
    }
    outsideClickListener = (e) => {
      if (!panel || isPinned) return;
      if (panel.contains(e.target)) return;
      removeUI();
    };
    // 延迟挂载，避免触发本次点击
    setTimeout(() => {
      if (panel) {
        document.addEventListener('pointerdown', outsideClickListener, true);
      }
    }, 80);
  }

  // 净化文本：去除不可见软连字符、零宽空格、BOM 等干扰字符
  function sanitizeText(s) {
    return String(s ?? '')
      .replace(/[\u00AD\u200B-\u200D\uFEFF]/g, '')
      .trim();
  }

  // 获取当前页面选区（支持网页普通文本与 input/textarea 选区）
  function getPageSelection() {
    let sel = window.getSelection()?.toString() || '';
    if (!sel.trim()) {
      const active = document.activeElement;
      if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA') && !active.classList.contains('bmc-search-input')) {
        const start = active.selectionStart;
        const end = active.selectionEnd;
        if (start != null && end != null && end > start) {
          sel = active.value.substring(start, end);
        }
      }
    }
    return sanitizeText(sel);
  }

  // ── 拖拽移动 ──
  function setupDraggable(headEl, panelEl) {
    let isDragging = false;
    let startX = 0, startY = 0;
    let initLeft = 0, initTop = 0;

    headEl.addEventListener('mousedown', (e) => {
      if (e.target.closest('button, input, textarea, a, .bmc-head-btn, .bmc-x')) return;
      isDragging = true;
      headEl.classList.add('bmc-dragging');

      const rect = panelEl.getBoundingClientRect();
      panelEl.style.left = `${rect.left}px`;
      panelEl.style.top = `${rect.top}px`;
      panelEl.style.right = 'auto';
      panelEl.style.bottom = 'auto';

      startX = e.clientX;
      startY = e.clientY;
      initLeft = rect.left;
      initTop = rect.top;
      e.preventDefault();
    });

    const onMove = (e) => {
      if (!isDragging || !panelEl) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;

      const maxLeft = Math.max(10, window.innerWidth - panelEl.offsetWidth - 12);
      const maxTop = Math.max(10, window.innerHeight - panelEl.offsetHeight - 12);

      const nextLeft = Math.min(Math.max(12, initLeft + dx), maxLeft);
      const nextTop = Math.min(Math.max(12, initTop + dy), maxTop);

      panelEl.style.left = `${nextLeft}px`;
      panelEl.style.top = `${nextTop}px`;
    };

    const onUp = () => {
      if (isDragging) {
        isDragging = false;
        headEl.classList.remove('bmc-dragging');
      }
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  // ── 面板构造（含顶部搜索栏与直接输入查询）──
  function mkPanel(title, initialQuery = '', mode = 'prpm') {
    removeUI();
    currentMode = mode;
    panel = document.createElement('div');
    panel.className = 'bmc-panel';

    panel.innerHTML = `
      <div class="bmc-drag-pill"></div>
      <div class="bmc-head">
        <div class="bmc-head-title">
          <span class="bmc-brand-dot"></span>
          <span class="bmc-title-text">${esc(title)}</span>
        </div>
        <div class="bmc-head-controls">
          <button class="bmc-head-btn bmc-pin-btn ${isPinned ? 'bmc-pinned' : ''}" title="${isPinned ? 'Nyahsemat tetingkap (klik luar untuk tutup)' : 'Sematkan tetingkap (jangan tutup bila klik luar)'}">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z"/></svg>
          </button>
          <button class="bmc-head-btn bmc-grab-btn" title="Isi & cari teks yang dipilih pada laman">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m14 12-8.5 8.5a2.12 2.12 0 1 1-3-3L11 9"/>
              <path d="M18 10l-4-4 2-2a2.83 2.83 0 0 1 4 4l-2 2z"/>
              <path d="m2 22 3-1"/>
            </svg>
          </button>
          <button class="bmc-head-btn bmc-x" title="Tutup (Esc)">✕</button>
        </div>
      </div>
      <div class="bmc-search-bar">
        <input type="text" class="bmc-search-input" placeholder="Type a word to check..." value="${esc(initialQuery)}">
        <div class="bmc-search-btns">
          <button class="bmc-pill-btn bmc-pill-prpm" title="Look up in the PRPM dictionary">PRPM</button>
          <button class="bmc-pill-btn bmc-pill-scan" title="Run the grammar rule check">Check</button>
        </div>
      </div>
      <div class="bmc-body"></div>
    `;

    panel.querySelector('.bmc-x').onclick = removeUI;

    const input = panel.querySelector('.bmc-search-input');
    const btnPin = panel.querySelector('.bmc-pin-btn');
    const btnGrab = panel.querySelector('.bmc-grab-btn');
    const btnPrpm = panel.querySelector('.bmc-pill-prpm');
    const btnScan = panel.querySelector('.bmc-pill-scan');

    btnPin.onclick = () => {
      isPinned = !isPinned;
      btnPin.classList.toggle('bmc-pinned', isPinned);
      btnPin.title = isPinned ? 'Nyahsemat tetingkap (klik luar untuk tutup)' : 'Sematkan tetingkap (jangan tutup bila klik luar)';
    };

    if (isAutoGrab) {
      btnGrab.classList.add('bmc-grab-active');
      btnGrab.title = 'Mod auto-ambil AKTIF (pilih teks di laman untuk semak terus). Klik untuk matikan.';
    }

    btnGrab.onclick = () => {
      isAutoGrab = !isAutoGrab;
      btnGrab.classList.toggle('bmc-grab-active', isAutoGrab);
      btnGrab.classList.add('bmc-btn-pop');
      setTimeout(() => btnGrab.classList.remove('bmc-btn-pop'), 300);

      if (isAutoGrab) {
        btnGrab.title = 'Mod auto-ambil AKTIF (pilih teks di laman untuk semak terus). Klik untuk matikan.';
        const sel = getPageSelection();
        if (sel && sel.length >= 2) {
          lastGrabbedText = sel;
          input.value = sel;
          if (currentMode === 'check' || sel.includes(' ') || sel.length > 25) {
            runCheck(sel, false);
          } else {
            runPrpm(sel, false);
          }
        }
      } else {
        btnGrab.title = 'Auto-grab mode: select text anywhere and it is sent here automatically';
      }
    };

    const triggerPrpm = () => {
      const q = input.value.trim();
      if (q) runPrpm(q, false);
    };

    const triggerScan = () => {
      const q = input.value.trim();
      if (q) runCheck(q, false);
    };

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const q = input.value.trim();
        if (!q) return;
        // 如果当前是 check 模式或输入含空格多词，走 scan；否则走 prpm
        if (currentMode === 'check' || q.includes(' ')) {
          triggerScan();
        } else {
          triggerPrpm();
        }
      }
    });

    btnPrpm.onclick = triggerPrpm;
    btnScan.onclick = triggerScan;

    // 默认放在右上并强制最高 z-index 防覆盖
    panel.style.setProperty('z-index', '2147483647', 'important');
    panel.style.right = '20px';
    panel.style.top = '80px';
    panel.style.left = 'auto';

    document.documentElement.appendChild(panel);
    setupDraggable(panel.querySelector('.bmc-head'), panel);
    attachOutsideClick();

    return panel;
  }

  function openBlankPanel() {
    panel = mkPanel('Tepat — Check', '', 'prpm');
    const b = panelBody();
    if (b) {
      b.innerHTML = `
        <div class="bmc-item" style="text-align:center;padding:16px 12px;color:#64748b;">
          <div style="font-size:22px;margin-bottom:6px;">📖</div>
          <div style="font-weight:600;color:#1e293b;margin-bottom:4px;">Type a word or sentence above</div>
          <div style="font-size:12px;">Press <b>PRPM</b> to look words up, or <b>Check</b> for the grammar scan.</div>
        </div>
      `;
    }
    const input = panel.querySelector('.bmc-search-input');
    if (input) setTimeout(() => input.focus(), 60);
    return panel;
  }

  function panelBody() { return panel?.querySelector('.bmc-body'); }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function cuteLoader(text = 'Checking…') {
    return `
      <div class="bmc-loading-box">
        <div class="bmc-cute-dots">
          <span></span><span></span><span></span>
        </div>
        <div class="bmc-loading-text">${esc(text)}</div>
      </div>
    `;
  }

  // ── PRPM 检索浮层 ──
  async function runPrpm(text, shouldCreatePanel = true) {
    const cleanText = sanitizeText(text);
    let rawWords = cleanText.toLowerCase().split(/[^\p{L}'-]+/u)
      .map(w => w.trim().replace(/^[-']+|[-']+$/g, ''))
      .filter(w => w.length >= 2 && w.length <= 40);

    // 如果选区切分出了多个短片段，但它们拼合起来是一个整词（音节连字符断词的情况），
    // 且整词长度在合理范围内，把拼合后的词排在最前查验
    const joinedForm = rawWords.join('');
    if (rawWords.length > 1 && joinedForm.length <= 35 && !rawWords.every((w, i, arr) => i === 0 || w === arr[0])) {
      // 非简单重叠词（如 buku-buku），尝试合并完整词
      rawWords.unshift(joinedForm);
    }

    // 无上限（用户裁决 2026-10-07）：选区切出多少词就查多少；server 端缓存
    // 命中的词瞬间回，新词走 3 并发限速，逐词渲染不用等全部完成。
    const words = [...new Set(rawWords)];
    if (!words.length) return;

    let done = 0;
    const title = words.length === 1 ? `PRPM — ${words[0]}` : `PRPM (${words.length} words)`;
    const updateTitle = () => {
      if (panel && words.length > 1)
        panel.querySelector('.bmc-title-text').textContent = `PRPM (${done}/${words.length} words)`;
    };
    if (shouldCreatePanel || !panel) {
      panel = mkPanel(title, cleanText, 'prpm');
    } else {
      currentMode = 'prpm';
      panel.querySelector('.bmc-title-text').textContent = title;
      panel.querySelector('.bmc-search-input').value = cleanText;
    }

    // PRPM 批量面板（用户裁决 2026-10-07）：
    //   ≤5 词 与 >8 词 → chips + attention 双视图，**默认 ☰ chips**（全部词），
    //     ⚠ 图标才切到 attention（miss/warn 条目；少量词时 hit 组也在）
    //   6-8 词 → 传统全条目列表
    // tab 按钮只有图标（无文字）。
    const compact = words.length > 8;
    const dualView = words.length <= 5 || compact;  // 双视图；6-8 词走传统列表
    const groups = { hit: [], miss: [], warn: [], pending: new Set(words) };

    const b = panelBody();
    if (dualView) {
      b.innerHTML = `
        <div class="bmc-prpm-summary" id="bmc-prpm-summary">
          <span class="bmc-sum-chip bmc-sum-miss" data-g="miss">✗ <b>0</b></span>
          <span class="bmc-sum-chip bmc-sum-warn" data-g="warn">? <b>0</b></span>
          <span class="bmc-sum-chip bmc-sum-hit" data-g="hit">✓ <b>0</b></span>
          <span style="flex:1"></span>
          <span class="bmc-tab-btn bmc-tab-btn-active" data-view="chips" title="Show all words">☰</span>
          <span class="bmc-tab-btn" data-view="attention" title="Show problem words">⚠</span>
        </div>
        <div id="bmc-prpm-attention" style="display:none">
          <div class="bmc-att-head" data-att="miss">Not found <span class="bmc-att-toggle">▾</span></div>
          <div data-group="miss"></div>
          <div class="bmc-att-head" data-att="warn">Needs review <span class="bmc-att-toggle">▾</span></div>
          <div data-group="warn"></div>
          <div class="bmc-att-head bmc-att-hit-head" data-att="hit">Found <span class="bmc-att-toggle">▾</span></div>
          <div data-group="hit"></div>
        </div>
        <div class="bmc-prpm-chips" id="bmc-prpm-chips">${words.map(w =>
          `<span class="bmc-chip" data-w="${esc(w)}"><span class="bmc-chip-w">${esc(w)}</span></span>`).join('')}</div>`;
      // tab 切换
      b.querySelector('#bmc-prpm-summary').addEventListener('click', e => {
        const tabBtn = e.target.closest('.bmc-tab-btn');
        if (tabBtn) {
          b.querySelectorAll('.bmc-tab-btn').forEach(t =>
            t.classList.toggle('bmc-tab-btn-active', t === tabBtn));
          b.querySelector('#bmc-prpm-attention').style.display =
            tabBtn.dataset.view === 'attention' ? '' : 'none';
          b.querySelector('#bmc-prpm-chips').style.display =
            tabBtn.dataset.view === 'chips' ? '' : 'none';
          return;
        }
        const chip = e.target.closest('.bmc-sum-chip');
        if (!chip) return;
        // 计数徽章：跳到 attention tab 的对应组（miss/warn）或 chips
        if (chip.dataset.g === 'hit') {
          b.querySelector('.bmc-tab-btn[data-view="chips"]').click();
          b.querySelector('#bmc-prpm-chips').scrollIntoView({ behavior: 'smooth' });
        } else {
          b.querySelector('.bmc-tab-btn[data-view="attention"]').click();
          const sec = b.querySelector(`#bmc-prpm-attention [data-group="${chip.dataset.g}"]`);
          if (sec && sec.children.length) sec.scrollIntoView({ behavior: 'smooth' });
        }
      });
      // Attention 组头点击折叠/展开
      b.querySelector('#bmc-prpm-attention').addEventListener('click', e => {
        const head = e.target.closest('.bmc-att-head');
        if (!head) return;
        const grp = head.nextElementSibling;
        const collapsed = grp.style.display === 'none';
        grp.style.display = collapsed ? '' : 'none';
        head.querySelector('.bmc-att-toggle').textContent = collapsed ? '▾' : '▸';
      });
      const chipsBox = b.querySelector('#bmc-prpm-chips');
      // flag 弹窗（用户裁决 2026-10-08）：三选一 + optional note + Submit。
      // 简单实现：面板内浮层，同时只存在一个。
      const openFlagDialog = (word, anchor) => {
        b.querySelectorAll('.bmc-flag-dialog').forEach(d => d.remove());
        chrome.storage.local.get({ flaggedWords: {} }, (s) => {
          const existing = s.flaggedWords[word];
          const dlg = document.createElement('div');
          dlg.className = 'bmc-flag-dialog';
          if (existing) {
            // 已 flag → unflag（关闭 issue）
            dlg.innerHTML = `
              <div class="bmc-flag-title">⚑ ${esc(word)}</div>
              <div class="bmc-flag-sub">Flagged as <b>${esc(existing.kind || '')}</b></div>
              <div class="bmc-flag-row">
                <button class="bmc-flag-un" data-act="unflag">Unflag (close issue)</button>
                <button class="bmc-flag-cancel" data-act="cancel">Cancel</button>
              </div>`;
            dlg.querySelector('[data-act="unflag"]').onclick = async () => {
              dlg.querySelector('.bmc-flag-un').textContent = 'Closing…';
              try {
                const r = await fetch(`${API}/api/unflag`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ word, issue: existing.issue })
                }).then(x => x.json());
                if (!r.ok) throw new Error(r.error || 'unflag failed');
                chrome.storage.local.get({ flaggedWords: {} }, (s2) => {
                  delete s2.flaggedWords[word];
                  chrome.storage.local.set({ flaggedWords: s2.flaggedWords });
                });
                b.querySelectorAll(`[data-w="${CSS.escape(word)}"], [data-att-w="${CSS.escape(word)}"]`)
                  .forEach(el => el.classList.remove('bmc-chip-flagged'));
                syncFloatState(word, false);
                dlg.remove();
              } catch (err) {
                dlg.querySelector('.bmc-flag-un').textContent = `✗ ${err.message}`;
              }
            };
          } else {
            dlg.innerHTML = `
              <div class="bmc-flag-title">⚑ Flag "${esc(word)}"</div>
              <label class="bmc-flag-opt"><input type="radio" name="bmc-fk" value="underflag"> Under-flagged — problem missed</label>
              <label class="bmc-flag-opt"><input type="radio" name="bmc-fk" value="mismeaning"> Wrong meaning — flag is off-target</label>
              <label class="bmc-flag-opt"><input type="radio" name="bmc-fk" value="overflag"> Over-flagged — word is fine</label>
              <input type="text" class="bmc-flag-note" placeholder="Explanation (optional)">
              <div class="bmc-flag-row">
                <button class="bmc-flag-go" disabled>Submit</button>
                <button class="bmc-flag-cancel" data-act="cancel">Cancel</button>
              </div>`;
            const go = dlg.querySelector('.bmc-flag-go');
            dlg.querySelectorAll('input[name="bmc-fk"]').forEach(r =>
              r.addEventListener('change', () => go.disabled = false));
            go.onclick = async () => {
              const kind = dlg.querySelector('input[name="bmc-fk"]:checked')?.value;
              const note = dlg.querySelector('.bmc-flag-note').value.trim();
              if (!kind) return;
              go.disabled = true; go.textContent = 'Submitting…';
              try {
                const r = await fetch(`${API}/api/flag`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ word, kind, note, page: location.href.slice(0, 300) })
                }).then(x => x.json());
                if (!r.ok) throw new Error(r.error || 'flag failed');
                chrome.storage.local.get({ flaggedWords: {} }, (s2) => {
                  s2.flaggedWords[word] = { ts: Date.now(), issue: r.github_issue, kind: r.kind };
                  chrome.storage.local.set({ flaggedWords: s2.flaggedWords });
                });
                b.querySelectorAll(`[data-w="${CSS.escape(word)}"], [data-att-w="${CSS.escape(word)}"]`)
                  .forEach(el => el.classList.add('bmc-chip-flagged'));
                syncFloatState(word, true);
                dlg.remove();
              } catch (err) {
                go.disabled = false; go.textContent = `✗ ${err.message}`;
              }
            };
          }
          dlg.querySelector('[data-act="cancel"]').onclick = () => dlg.remove();
          (anchor || b).after(dlg);
        });
      };

      // chips flag（用户裁决 2026-10-08）：icon 不进 chip DOM、不留 padding——
      // 单个面板级悬浮层（position:fixed）跟随 hover 的 chip，浮在 chip 右上角
      // 外侧的空白处。chip 宽度从渲染起恒定，不存在挤行；icon 有自己的热区，
      // 鼠标移到 icon 上不算离开 chip 区域（由 float 层的 pointerenter 保持）。
      const floatIc = document.createElement('div');
      floatIc.className = 'bmc-flag-float';
      floatIc.textContent = '⚑';
      floatIc.style.display = 'none';
      panel.appendChild(floatIc);
      let floatWord = null;
      const placeFloat = (chipEl) => {
        floatWord = chipEl.dataset.w;
        const r = chipEl.getBoundingClientRect();
        floatIc.style.display = 'block';
        floatIc.classList.toggle('done', chipEl.classList.contains('bmc-chip-flagged'));
        // chip 右上角外飘（叠在 chips 区 gap/空白上，不占 chip 文档流）
        floatIc.style.left = `${r.right + 3}px`;
        floatIc.style.top = `${r.top + r.height / 2}px`;
      };
      const hideFloat = () => { floatIc.style.display = 'none'; floatWord = null; };
      chipsBox.addEventListener('mouseover', e => {
        if (e.target.closest('.bmc-flag-float')) return;  // icon 自身热区：保持
        const chipEl = e.target.closest('.bmc-chip');
        if (chipEl && chipEl.dataset.st) placeFloat(chipEl);
        else if (!e.target.closest('.bmc-chip')) hideFloat();
      });
      chipsBox.addEventListener('mouseleave', hideFloat);
      // 滚动面板时重新贴位（fixed 坐标会脱节）
      panel.addEventListener('scroll', () => {
        const chipEl = floatWord && b.querySelector(`.bmc-chip[data-w="${CSS.escape(floatWord)}"]`);
        if (chipEl && floatIc.style.display !== 'none') placeFloat(chipEl);
      }, { passive: true });
      floatIc.addEventListener('click', e => {
        e.stopPropagation();
        if (!floatWord) return;
        const chipEl = b.querySelector(`.bmc-chip[data-w="${CSS.escape(floatWord)}"]`);
        openFlagDialog(floatWord, chipEl);
      });
      // flag/unflag 后同步悬浮层状态
      const syncFloatState = (word, flagged) => {
        if (floatWord === word) floatIc.classList.toggle('done', flagged);
      };
      chipsBox.addEventListener('click', async e => {
        // ── chip 主体：展开 details（单展开互斥）──
        const chip = e.target.closest('.bmc-chip');
        if (!chip || !chip.dataset.st) return;  // 未查询完/无状态不展开
        const open = chip.nextElementSibling;
        if (open && open.classList.contains('bmc-chip-def')) { open.remove(); return; }
        // 互斥：收起其它已展开的
        chipsBox.querySelectorAll('.bmc-chip-def').forEach(d => d.remove());
        const st = chip.dataset.st === 'unreachable' ? 'warn' : chip.dataset.st;
        const status = { miss: 'not found', warn: 'needs review', hit: 'found' }[st] || st;
        const box = document.createElement('div');
        box.className = 'bmc-chip-def';
        box.innerHTML = `
          <div><b>${esc(chip.dataset.w)}</b> — ${status}${chip.dataset.root ? ` · root: ${esc(chip.dataset.root)}` : ''}</div>
          <div style="margin:4px 0">${esc(chip.dataset.def || '—')}</div>`;
        chip.after(box);
      });
      // attention：每个词 box 右上角常驻 flag（独立格子，常驻渲染）
      b.querySelector('#bmc-prpm-attention').addEventListener('click', async e => {
        const ic = e.target.closest('.bmc-chip-flag-ic');
        if (!ic) return;
        e.stopPropagation();
        const box = ic.closest('[data-att-w]');
        openFlagDialog(box?.dataset.attW, box);
      });
      // 已 flag 过的词显示标记（chrome.storage.local 防重）
      chrome.storage.local.get({ flaggedWords: {} }, (s) => {
        for (const w of Object.keys(s.flaggedWords || {})) {
          b.querySelectorAll(`[data-w="${CSS.escape(w)}"], [data-att-w="${CSS.escape(w)}"]`)
            .forEach(el => el.classList.add('bmc-chip-flagged'));
        }
      });
    } else {
      b.innerHTML = words.map(w =>
        `<div class="bmc-item bmc-item-pending" id="bmc-p-${CSS.escape(w)}">
          <div class="bmc-item-main">
            <span class="bmc-word">${esc(w)}</span>
            <span class="bmc-badge bmc-badge-pending">…</span>
          </div>
        </div>`
      ).join('');
    }

    const attention = (group, html) => {
      // dualView 模式：miss/warn 全条目插入 attention 视图对应组。
      // 不自动切换视图（用户裁决：默认 ☰ chips，⚠ 图标才进 attention）。
      const sec = b.querySelector(`#bmc-prpm-attention [data-group="${group}"]`);
      if (sec) sec.insertAdjacentHTML('beforeend', html);
    };
    const bumpSummary = (group) => {
      const chip = b.querySelector(`#bmc-prpm-summary .bmc-sum-${group} b`);
      if (chip) chip.textContent = groups[group].length;
    };

    const lookupOne = async (w) => {
      let el = b.querySelector(`#bmc-p-${CSS.escape(w)}`);
      const chip = dualView ? b.querySelector(`.bmc-chip[data-w="${CSS.escape(w)}"]`) : null;
      if (!el && !chip) return;
      let status = 'warn', def = '';
      try {
        const r = await fetch(`${API}/api/prpm`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ word: w })
        }).then(x => x.json());
        const isSuggestion = r.status === 'warn' ||
          (r.definition && /adakah anda bermaksud/i.test(r.definition));
        status = isSuggestion ? 'warn' : r.status;
        def = r.definition || '';
        if (chip) {
          chip.dataset.st = status;
          if (r.root) chip.dataset.root = r.root;
        }
      } catch {
        status = 'unreachable';
        def = 'Tepat.exe is not running in the background';
      }
      groups.pending.delete(w);
      groups[status in groups ? status : 'warn'].push(w);

      if (dualView) {
        // chips 永远存在并着色；全部状态进 attention（Found/Not found/Needs review）
        if (chip) {
          chip.classList.remove('bmc-chip-pending');
          chip.classList.add(`bmc-chip-${status === 'unreachable' ? 'warn' : status}`);
          chip.dataset.def = def || '';  // 全状态都存：details 展开用
        }
        // attention box：右上角常驻 flag icon；无 ✓/✗/? 徽章——box 颜色已表达状态
        const flagIc = `<span class="bmc-chip-flag-ic bmc-att-flag" title="Flag this word">⚑</span>`;
        if (status === 'hit') {
          attention('hit', `<div class="bmc-item bmc-item-hit" data-att-w="${esc(w)}" data-st="hit"><div class="bmc-item-main"><span class="bmc-word">${esc(w)}</span>${flagIc}</div>${def ? `<div class="bmc-def">${esc(def)}</div>` : ''}</div>`);
        } else if (status === 'miss') {
          attention('miss', `<div class="bmc-item bmc-item-miss" data-att-w="${esc(w)}" data-st="miss"><div class="bmc-item-main"><span class="bmc-word bmc-word-err">${esc(w)}</span>${flagIc}</div><div class="bmc-miss-note">No dictionary entry found</div></div>`);
        } else {
          const note = status === 'unreachable' ? 'Could not reach PRPM' : (def || 'No exact entry — related suggestions available');
          attention('warn', `<div class="bmc-item bmc-item-warn" data-att-w="${esc(w)}" data-st="warn"><div class="bmc-item-main"><span class="bmc-word">${esc(w)}</span>${flagIc}</div><div class="bmc-warn-note">${esc(note)}</div></div>`);
        }
        bumpSummary(status === 'unreachable' ? 'warn' : status);
      } else {
        el.classList.remove('bmc-item-pending');
        if (status === 'hit') {
          el.classList.add('bmc-item-hit');
          el.querySelector('.bmc-item-main').innerHTML = `<span class="bmc-word">${esc(w)}</span>`;
          if (def) el.insertAdjacentHTML('beforeend', `<div class="bmc-def">${esc(def)}</div>`);
        } else if (status === 'miss') {
          el.classList.add('bmc-item-miss');
          el.querySelector('.bmc-item-main').innerHTML = `<span class="bmc-word bmc-word-err">${esc(w)}</span>`;
          el.insertAdjacentHTML('beforeend', `<div class="bmc-miss-note">No dictionary entry found</div>`);
        } else {
          const note = status === 'unreachable' ? 'Could not reach PRPM' : (def || 'No exact entry — related suggestions available');
          el.classList.add('bmc-item-warn');
          el.querySelector('.bmc-item-main').innerHTML = `<span class="bmc-word">${esc(w)}</span>`;
          el.insertAdjacentHTML('beforeend', `<div class="bmc-warn-note">${esc(note)}</div>`);
        }
      }
      done++; updateTitle();
    };

    // 并发 6 打 server（server 侧 3 槽信号量 + 缓存秒回，这里多点没关系），
    // 每词完成即渲染。
    const CONC = 6;
    for (let i = 0; i < words.length; i += CONC) {
      await Promise.all(words.slice(i, i + CONC).map(lookupOne));
    }
  }

  // ── 检查浮层 (Scan) ──
  async function runCheck(text, shouldCreatePanel = true) {
    const cleanText = sanitizeText(text);
    if (shouldCreatePanel || !panel) {
      panel = mkPanel('Check results', cleanText, 'check');
    } else {
      currentMode = 'check';
      panel.querySelector('.bmc-title-text').textContent = 'Check results';
      panel.querySelector('.bmc-search-input').value = cleanText;
    }

    panelBody().innerHTML = cuteLoader('Menganalisis teks…');
    let r;
    try {
      r = await fetch(`${API}/api/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: cleanText })
      }).then(x => x.json());
    } catch {
      panelBody().innerHTML = `
        <div class="bmc-err-box">
          <div class="bmc-err-title">Tepat.exe is not running</div>
          <div class="bmc-err-desc">Make sure the Tepat app is running on this computer (localhost:${PORT}).</div>
        </div>
      `;
      return;
    }
    renderCheck(r, cleanText);
  }

  function renderCheck(r, text) {
    panelBody().innerHTML = TepatResults.html(r);
  }

  document.addEventListener('click', async (e) => {
    const btn = e.target.closest?.('.bmc-evidence-btn');
    if (!btn) return;
    btn.disabled = true;
    try {
      const r = await fetch(`${API}/api/evidence`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:btn.dataset.q})}).then(x=>x.json());
      const rows = [];
      for (const d of r.lexical?.definitions || []) rows.push(`<div><b>${esc(d.dictionary_source || 'DBP')}</b>: ${esc(d.cleaned_text)}</div>`);
      for (const ex of r.lexical?.examples || []) rows.push(`<div><b>${esc(ex.source)}</b>: ${esc(ex.text)}</div>`);
      for (const ref of r.references || []) rows.push(`<div><b>${esc(ref.source)}</b>: ${esc(ref.text.slice(0,500))}</div>`);
      let target = btn.closest('.bmc-item').querySelector('.bmc-local-evidence');
      if (!target) { target=document.createElement('div');target.className='bmc-local-evidence';btn.closest('.bmc-item').appendChild(target); }
      target.innerHTML = rows.length ? rows.join('') : 'No local evidence found; check external sources.';
    } catch { showToast('Could not load local evidence.',3000); }
    finally { btn.disabled=false; }
  });

  function prpmBtn(word) {
    if (!prpmEnabled) return '';
    return `<button class="bmc-kamus-btn" data-w="${esc(word)}" title="Look up directly in PRPM">Dictionary</button>`;
  }

  // ── 行内 Kamus 按钮点击 ──
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest?.('.bmc-kamus-btn');
    if (!btn) return;
    const w = btn.dataset.w;
    btn.disabled = true;
    btn.innerHTML = `<span class="bmc-btn-loading">…</span>`;

    let r;
    try {
      r = await fetch(`${API}/api/prpm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word: w })
      }).then(x => x.json());
    } catch {
      r = { status: 'unreachable' };
    }

    const item = btn.closest('.bmc-item');
    const isSuggestion = r.status === 'warn' ||
      (r.definition && /adakah anda bermaksud/i.test(r.definition));

    if (isSuggestion) {
      btn.replaceWith(Object.assign(document.createElement('span'), {
        className: 'bmc-badge bmc-badge-warn',
        title: 'Cadangan perkataan terdekat',
        textContent: '?'
      }));
      if (r.definition && item) {
        item.insertAdjacentHTML('beforeend', `<div class="bmc-warn-note">${esc(r.definition)}</div>`);
      }
    } else if (r.status === 'hit') {
      btn.replaceWith(Object.assign(document.createElement('span'), {
        className: 'bmc-badge bmc-badge-hit',
        title: 'Wujud dalam PRPM',
        textContent: '✓'
      }));
      if (r.definition && item) {
        item.insertAdjacentHTML('beforeend', `<div class="bmc-def">${esc(r.definition)}</div>`);
      }
    } else if (r.status === 'miss') {
      btn.replaceWith(Object.assign(document.createElement('span'), {
        className: 'bmc-badge bmc-badge-miss',
        title: 'Not in PRPM',
        textContent: '✗'
      }));
      if (item && !item.querySelector('.bmc-miss-note')) {
        item.insertAdjacentHTML('beforeend', `<div class="bmc-miss-note">No entry found in the dictionary</div>`);
      }
    } else {
      btn.replaceWith(Object.assign(document.createElement('span'), {
        className: 'bmc-badge bmc-badge-warn',
        title: 'PRPM tidak dapat dihubungi',
        textContent: '?'
      }));
    }
  });

  // ── Raise Error 表单 ──
  function showRaiseForm(text) {
    panel = mkPanel('Raise Error', '', 'raise');
    // 隐藏搜索栏，保留反馈表单
    panel.querySelector('.bmc-search-bar').style.display = 'none';

    panelBody().innerHTML = `
      <div class="bmc-raise-form">
        <label class="bmc-form-label">Marked text
          <div class="bmc-sel-preview">${esc(text.slice(0, 300))}${text.length > 300 ? '…' : ''}</div>
        </label>
        <label class="bmc-form-label">Error type
          <select id="bmc-type" class="bmc-input-select">
            <option value="spelling">Spelling</option>
            <option value="grammar">Grammar</option>
            <option value="terminology">Terminology</option>
            <option value="collocation">Collocation</option>
            <option value="other">Other</option>
          </select>
        </label>
        <label class="bmc-form-label">Explanation (optional)
          <textarea id="bmc-why" class="bmc-input-textarea" rows="3" placeholder="Why is this wrong? / What is the correct form?"></textarea>
        </label>
        <div class="bmc-row">
          <button id="bmc-save" class="bmc-btn-primary">Save</button>
          <button id="bmc-cancel" class="bmc-btn-secondary">Cancel</button>
        </div>
        <div id="bmc-msg" class="bmc-msg"></div>
      </div>
    `;

    panel.querySelector('#bmc-cancel').onclick = removeUI;
    panel.querySelector('#bmc-save').onclick = () => {
      const entry = {
        ts: new Date().toISOString(),
        url: location.href.slice(0, 300),
        text,
        context: contextAround(text),
        type: panel.querySelector('#bmc-type').value,
        why: panel.querySelector('#bmc-why').value.trim(),
      };
      chrome.storage.local.get({ log: [] }, (s) => {
        const log = s.log.concat(entry);
        chrome.storage.local.set({ log }, () => {
          panel.querySelector('#bmc-msg').textContent = `✓ Saved (${log.length} entries in log)`;
          setTimeout(removeUI, 1200);
        });
      });
    };
  }

  function contextAround(needle, radius = 120) {
    const body = document.body.innerText || '';
    const i = body.indexOf(needle);
    if (i < 0) return '';
    return body.slice(Math.max(0, i - radius), i + needle.length + radius);
  }

  // ── 全页扫描高亮 (Imbas Seluruh Halaman) ──
  let activeHighlights = [];

  function clearPageHighlights() {
    const allHls = document.querySelectorAll('.bmc-hl');
    allHls.forEach(span => {
      const parent = span.parentNode;
      if (parent) {
        const textNode = document.createTextNode(span.textContent);
        parent.replaceChild(textNode, span);
        parent.normalize(); // 合并相邻文本节点，彻底恢复原始 DOM
      }
    });
    activeHighlights = [];
    document.querySelector('.bmc-toast')?.remove();
  }

  function showToast(html, duration = 6000) {
    document.querySelector('.bmc-toast')?.remove();
    const toast = document.createElement('div');
    toast.className = 'bmc-toast';
    toast.style.setProperty('z-index', '2147483647', 'important');
    toast.innerHTML = `
      <div style="display:flex;align-items:center;">
        <span>${html}</span>
      </div>
      <span class="bmc-toast-close" title="Tutup">✕</span>
    `;
    toast.querySelector('.bmc-toast-close').onclick = () => toast.remove();
    document.documentElement.appendChild(toast);
    if (duration > 0) {
      setTimeout(() => {
        if (toast.parentNode) toast.remove();
      }, duration);
    }
  }

  async function scanFullPage() {
    clearPageHighlights();
    showToast('Sedang mengimbas teks seluruh halaman…', 0);

    // 收集页面文本节点（放宽限制，只要含字母或符号且非脚本/系统控件即可）
    const walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          const val = node.nodeValue;
          if (!val || !val.trim() || val.trim().length < 2) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (!parent) return NodeFilter.FILTER_REJECT;
          const tag = parent.tagName;
          if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEXTAREA', 'INPUT', 'SELECT', 'OPTION'].includes(tag)) return NodeFilter.FILTER_REJECT;
          if (parent.closest('.bmc-panel, .bmc-toast')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      }
    );

    const textNodes = [];
    let n;
    while ((n = walker.nextNode())) {
      textNodes.push(n);
    }

    // Send raw text and retain exact UTF-16 ranges; do not mark identical words
    // elsewhere on the page when only one occurrence needs review.
    let fullText = '';
    const nodes = [];
    for (const node of textNodes) {
      if (fullText.length >= 30000) break;
      const start = fullText.length;
      const raw = node.nodeValue.slice(0,30000-start);
      nodes.push({node,start,raw});
      fullText += raw + '\n';
    }
    fullText = fullText.slice(0,30000);
    if (!fullText.trim()) { showToast('No suitable text found.',3000);return; }
    let r;
    try {
      r=await fetch(`${API}/api/scan`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:fullText})}).then(x=>x.json());
      if (r.error || r.engine!=='evidence-v2') throw new Error(r.error || 'Versi Tepat perlu dikemas kini.');
    } catch (e) { showToast(esc(e.message || 'The check did not complete.'),4000);return; }
    let hitCount=0;
    for (const {node,start,raw} of nodes) {
      if (!node.parentNode) continue;
      const parts=TepatResults.segments(raw,r.issues || [],start);
      if (!parts.some(p=>p.level)) continue;
      const frag=document.createDocumentFragment();
      for (const part of parts) {
        if (!part.level) {frag.appendChild(document.createTextNode(part.text));continue;}
        const span=document.createElement('span');
        span.className='bmc-hl '+({error:'bmc-hl-err',warning:'bmc-hl-warn',info:'bmc-hl-info'}[part.level]);
        span.textContent=part.text;
        span.title=part.notes.join(' · ');
        span.onclick=e=>{e.stopPropagation();runCheck(part.text);};
        frag.appendChild(span);activeHighlights.push(span);hitCount++;
      }
      // Preserve the unscanned tail of a node at the 30,000-character limit.
      if (node.nodeValue.length>raw.length) frag.appendChild(document.createTextNode(node.nodeValue.slice(raw.length)));
      node.parentNode.replaceChild(frag,node);
    }
    showToast(`Scanned up to 30,000 characters: <b>${hitCount}</b> passages marked. Facts are not checked.
      <button id="bmc-btn-clear-hl" style="margin-left:8px;padding:2px 8px;">Clear marks</button>`,12000);

    setTimeout(() => {
      const btn = document.querySelector('#bmc-btn-clear-hl');
      if (btn) btn.onclick = () => clearPageHighlights();
    }, 50);
  }

  // ── Rule Book 插件引擎 ──
  // 规则包从 server /api/pack 拉取（extension/rules-pack.json，热更新：admin
  // 保存后新扫描即用新规则，无需重载扩展）。缓存本页结果；拉不到就空跑。
  let packCache = null;
  async function ruleBookPacks() {
    if (packCache) return packCache;
    try {
      const r = await fetch(`${API}/api/pack`).then(x => x.json());
      if (r && r.pack && Array.isArray(r.pack.rules)) packCache = [r.pack];
    } catch (e) { /* server 不在线时 Rule Book 直接空跑 */ }
    return packCache || [];
  }

  function ruleBookScanSync(text, packs) {
    const findings = [];
    const exceptions = [];
    for (const pack of packs) {
      for (const rule of pack.rules || []) {
        let rx;
        try { rx = new RegExp(rule.re, 'gi' + (rule.re.includes('\\u') ? 'u' : '')); }
        catch (e) { console.warn('[tepat] bad rule regex', rule.id, e); continue; }
        let m;
        while ((m = rx.exec(text)) !== null) {
          if (m[0].length === 0) { rx.lastIndex++; continue; }
          const hit = {
            pack: pack.meta.id,
            rule: rule.id,
            conf: rule.conf,
            note: rule.note,
            span: m[0],
            start: m.index,
            end: m.index + m[0].length,
          };
          if (rule.conf === 'exception' || rule.noflag) exceptions.push(hit);
          else findings.push(hit);
        }
      }
    }
    // exception 抑制：与 exception 区间重叠的 finding 降级为 suppressed
    for (const f of findings) {
      f.suppressed = exceptions.some(x => x.rule !== f.rule &&
        f.start < x.end && x.start < f.end);
    }
    return { findings: findings.filter(f => !f.suppressed), suppressed: findings.filter(f => f.suppressed) };
  }

  async function runRuleBook(text, shouldCreatePanel = true) {
    const cleanText = sanitizeText(text);
    if (shouldCreatePanel || !panel) {
      panel = mkPanel('Rule Book', cleanText, 'check');
    } else {
      currentMode = 'check';
      panel.querySelector('.bmc-title-text').textContent = 'Rule Book';
      panel.querySelector('.bmc-search-input').value = cleanText;
    }
    const packs = await ruleBookPacks();
    if (!packs.length) {
      panelBody().innerHTML = '<div class="bmc-err-box"><div class="bmc-err-title">No rule packs (server unreachable?)</div></div>';
      return;
    }
    const r = ruleBookScanSync(cleanText, packs);
    // error 与 warn 分开呈现（用户裁决 2026-10-02）：regex 判不了语境的规则
    // 是"提醒不是判决"——warn 不占错误位，hover 看 tooltip 说明。
    const errors = r.findings.filter(f => f.conf === 'error');
    const warns = r.findings.filter(f => f.conf === 'warn');
    const notes = r.findings.filter(f => f.conf === 'note');
    const itemHtml = (f, kind) => {
      const dot = { error: '🔴', warn: '🟠', note: '🟡' }[kind];
      const label = { error: 'error', warn: 'needs context — hover for nuance', note: 'reminder' }[kind];
      const tip = TepatResults.escapeHtml(
        kind === 'warn'
          ? `${f.rule}: ${f.note}\nRegex tidak boleh menilai konteks — ini peringatan, bukan penghakiman. Rujuk Rule Book ${f.rule} sebelum memutuskan.`
          : `${f.rule}: ${f.note}`);
      return `<div class="bmc-item bmc-item-${kind} bmc-tip" data-tip="${tip.replace(/"/g, '&quot;').replace(/\n/g, ' ')}">
           <div class="bmc-item-main">${dot} <b>${TepatResults.escapeHtml(f.span)}</b>
             <span class="bmc-rule-id">[${f.rule}]</span></div>
           <div class="bmc-info-note">${TepatResults.escapeHtml(f.note)} · ${label}</div>
         </div>`;
    };
    const section = (title, arr, kind) => arr.length
      ? `<div class="bmc-sec-title">${title} (${arr.length})</div>` + arr.map(f => itemHtml(f, kind)).join('')
      : '';
    const suppressedNote = r.suppressed.length
      ? `<div class="bmc-foot">${r.suppressed.length} padanan diabaikan (kekecualian NF/LR)</div>` : '';
    panelBody().innerHTML = (errors.length || warns.length || notes.length)
      ? section('Errors', errors, 'error')
        + section('Needs context', warns, 'warn')
        + section('Reminders', notes, 'note')
        + suppressedNote
        + `<div class="bmc-foot">${errors.length} errors · ${warns.length} need context · ${notes.length} reminders · ${packs.length} packs · local check (offline)</div>`
      : `<div class="bmc-clean">✓ No findings according to the Rule Book. (${packs.map(p => p.meta.title).join('; ')})</div>`;
  }

  // ── 消息监听与快捷键 ──
  chrome.runtime.onMessage.addListener((m, sender, sendResponse) => {
    if (m.type === 'context-prpm') {
      runPrpm(m.text || '');
      sendResponse({ ok: true });
    } else if (m.type === 'context-check') {
      runCheck(m.text || '');
      sendResponse({ ok: true });
    } else if (m.type === 'context-rulebook') {
      runRuleBook(m.text || '');
      sendResponse({ ok: true });
    } else if (m.type === 'context-raise') {
      showRaiseForm(m.text || '');
      sendResponse({ ok: true });
    } else if (m.type === 'check-selection') {
      const text = getPageSelection();
      if (text && text.length >= 2) {
        if (text.includes(' ') || text.length > 25) {
          runCheck(text);
        } else {
          runPrpm(text);
        }
      } else {
        if (panel) {
          removeUI();
        } else {
          openBlankPanel();
        }
      }
      sendResponse({ ok: true });
    } else if (m.type === 'open-panel') {
      const sel = getPageSelection();
      if (sel) {
        if (sel.includes(' ') || sel.length > 25) {
          runCheck(sel);
        } else {
          runPrpm(sel);
        }
      } else {
        openBlankPanel();
      }
      sendResponse({ ok: true });
    } else if (m.type === 'scan-full-page') {
      scanFullPage();
      sendResponse({ ok: true });
    } else if (m.type === 'clear-highlights') {
      clearPageHighlights();
      sendResponse({ ok: true });
    }
    return false;
  });
})();
