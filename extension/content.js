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
  let outsideClickListener = null;

  function removeUI() {
    if (outsideClickListener) {
      document.removeEventListener('pointerdown', outsideClickListener, true);
      outsideClickListener = null;
    }
    panel?.remove();
    panel = null;
    isPinned = false;
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') removeUI();
  });

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
    return sel.trim();
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
        <input type="text" class="bmc-search-input" placeholder="Taip perkataan untuk semak..." value="${esc(initialQuery)}">
        <div class="bmc-search-btns">
          <button class="bmc-pill-btn bmc-pill-prpm" title="Semak Kamus PRPM">PRPM</button>
          <button class="bmc-pill-btn bmc-pill-scan" title="Semak Peraturan Tatabahasa">Semak</button>
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

    btnGrab.onclick = () => {
      const sel = getPageSelection();
      if (sel) {
        input.value = sel;
        btnGrab.classList.add('bmc-btn-pop');
        setTimeout(() => btnGrab.classList.remove('bmc-btn-pop'), 300);
        if (currentMode === 'check' || sel.includes(' ')) {
          runCheck(sel, false);
        } else {
          runPrpm(sel, false);
        }
      } else {
        btnGrab.classList.add('bmc-btn-shake');
        setTimeout(() => btnGrab.classList.remove('bmc-btn-shake'), 400);
        const origPlaceholder = input.placeholder;
        input.placeholder = 'Sila pilih teks di laman dahulu!';
        input.focus();
        setTimeout(() => {
          if (input) input.placeholder = origPlaceholder;
        }, 1800);
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

    // 默认放在右上
    panel.style.right = '20px';
    panel.style.top = '80px';
    panel.style.left = 'auto';

    document.documentElement.appendChild(panel);
    setupDraggable(panel.querySelector('.bmc-head'), panel);
    attachOutsideClick();

    return panel;
  }

  function panelBody() { return panel?.querySelector('.bmc-body'); }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function cuteLoader(text = 'Menyemak…') {
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
    const rawWords = text.toLowerCase().split(/[^\p{L}'-]+/u)
      .map(w => w.trim())
      .filter(w => w.length >= 2 && w.length <= 40);
    const words = [...new Set(rawWords)].slice(0, 8);
    if (!words.length) return;

    const title = words.length === 1 ? `PRPM — ${words[0]}` : `PRPM (${words.length} kata)`;
    if (shouldCreatePanel || !panel) {
      panel = mkPanel(title, text, 'prpm');
    } else {
      currentMode = 'prpm';
      panel.querySelector('.bmc-title-text').textContent = title;
      panel.querySelector('.bmc-search-input').value = text;
    }

    const b = panelBody();
    b.innerHTML = words.map(w =>
      `<div class="bmc-item bmc-item-pending" id="bmc-p-${CSS.escape(w)}">
        <div class="bmc-item-main">
          <span class="bmc-word">${esc(w)}</span>
          <span class="bmc-badge bmc-badge-pending">…</span>
        </div>
      </div>`
    ).join('');

    for (const w of words) {
      const el = b.querySelector(`#bmc-p-${CSS.escape(w)}`);
      if (!el) continue;
      try {
        const r = await fetch(`${API}/api/prpm`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ word: w })
        }).then(x => x.json());

        el.classList.remove('bmc-item-pending');
        if (r.status === 'hit') {
          el.classList.add('bmc-item-hit');
          el.querySelector('.bmc-item-main').innerHTML = `
            <span class="bmc-word">${esc(w)}</span>
            <span class="bmc-badge bmc-badge-hit" title="Wujud dalam PRPM">✓</span>
          `;
          if (r.definition) {
            el.insertAdjacentHTML('beforeend', `<div class="bmc-def">${esc(r.definition)}…</div>`);
          }
        } else if (r.status === 'miss') {
          el.classList.add('bmc-item-miss');
          el.querySelector('.bmc-item-main').innerHTML = `
            <span class="bmc-word bmc-word-err">${esc(w)}</span>
            <span class="bmc-badge bmc-badge-miss" title="Tiada entri">✗</span>
          `;
          el.insertAdjacentHTML('beforeend', `<div class="bmc-miss-note">Tiada entri kamus ditemui</div>`);
        } else {
          el.classList.add('bmc-item-warn');
          el.querySelector('.bmc-item-main').innerHTML = `
            <span class="bmc-word">${esc(w)}</span>
            <span class="bmc-badge bmc-badge-warn" title="PRPM tidak dapat dicapai">?</span>
          `;
          el.insertAdjacentHTML('beforeend', `<div class="bmc-warn-note">Tidak dapat menghubungi PRPM</div>`);
        }
      } catch {
        el.classList.remove('bmc-item-pending');
        el.classList.add('bmc-item-warn');
        el.querySelector('.bmc-item-main').innerHTML = `
          <span class="bmc-word">${esc(w)}</span>
          <span class="bmc-badge bmc-badge-warn" title="Exe tidak berjalan">?</span>
        `;
        el.insertAdjacentHTML('beforeend', `<div class="bmc-warn-note">Tepat.exe tidak berjalan di latar belakang</div>`);
      }
    }
  }

  // ── 检查浮层 (Scan) ──
  async function runCheck(text, shouldCreatePanel = true) {
    if (shouldCreatePanel || !panel) {
      panel = mkPanel('Hasil Semakan', text, 'check');
    } else {
      currentMode = 'check';
      panel.querySelector('.bmc-title-text').textContent = 'Hasil Semakan';
      panel.querySelector('.bmc-search-input').value = text;
    }

    panelBody().innerHTML = cuteLoader('Menganalisis teks…');
    let r;
    try {
      r = await fetch(`${API}/api/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      }).then(x => x.json());
    } catch {
      panelBody().innerHTML = `
        <div class="bmc-err-box">
          <div class="bmc-err-title">Tepat.exe Tidak Aktif</div>
          <div class="bmc-err-desc">Sila pastikan aplikasi Tepat berjalan di komputer anda (localhost:${PORT}).</div>
        </div>
      `;
      return;
    }
    renderCheck(r, text);
  }

  function renderCheck(r, text) {
    const b = panelBody();
    const secs = [];

    // 黑名单搭配（确认为错）
    const bl = (r.blacklist_hits || []).map(h => `
      <div class="bmc-item bmc-item-miss">
        <div class="bmc-item-main">
          <div><span class="bmc-tag bmc-tag-err">Salah</span> <b class="bmc-word-err">${esc(h.bigram)}</b></div>
          <span class="bmc-badge bmc-badge-miss">✗</span>
        </div>
        <div class="bmc-miss-note">Gabungan perkataan ini disahkan tidak tepat.</div>
      </div>
    `);

    // 机械拼写错误
    const sp = (r.cold_words || []).filter(c => c.reason === 'spelling').map(c => `
      <div class="bmc-item bmc-item-miss">
        <div class="bmc-item-main">
          <div>
            <span class="bmc-tag bmc-tag-err">Ejaan</span>
            <b class="bmc-word-err">${esc(c.word)}</b>
            ${c.suggestion ? `<span class="bmc-arrow">→</span> <b class="bmc-sug">${esc(c.suggestion)}</b>` : ''}
          </div>
          <div class="bmc-actions">
            ${prpmBtn(c.word)}
            <span class="bmc-badge bmc-badge-miss">✗</span>
          </div>
        </div>
      </div>
    `);

    // 高置信度语法规则
    const hi = (r.rule_hits || []).filter(h => h.conf === 'high').map(h => `
      <div class="bmc-item bmc-item-miss">
        <div class="bmc-item-main">
          <div><span class="bmc-tag bmc-tag-err">${esc(h.rule)}</span> <b>${esc(h.span)}</b></div>
          <span class="bmc-badge bmc-badge-miss">✗</span>
        </div>
        <div class="bmc-miss-note">${esc(h.note)}</div>
      </div>
    `);

    // 未知生僻词 (cold)
    const cold = (r.cold_words || []).filter(c => c.reason === 'cold').map(c => `
      <div class="bmc-item bmc-item-warn">
        <div class="bmc-item-main">
          <div><span class="bmc-tag bmc-tag-warn">Jarang</span> <b>${esc(c.word)}</b></div>
          <div class="bmc-actions">
            ${prpmBtn(c.word)}
          </div>
        </div>
        <div class="bmc-warn-note">Perkataan tidak ditemui dalam korpus.</div>
      </div>
    `);

    // 中置信度语法规则
    const med = (r.rule_hits || []).filter(h => h.conf === 'medium').map(h => `
      <div class="bmc-item bmc-item-warn">
        <div class="bmc-item-main">
          <div><span class="bmc-tag bmc-tag-warn">${esc(h.rule)}</span> <b>${esc(h.span)}</b></div>
        </div>
        <div class="bmc-warn-note">${esc(h.note)}</div>
      </div>
    `);

    // 低置信度提醒
    const lo = (r.rule_hits || []).filter(h => h.conf === 'low').map(h => `
      <div class="bmc-item bmc-item-info">
        <div class="bmc-item-main">
          <div><span class="bmc-tag bmc-tag-info">${esc(h.rule)}</span> <b>${esc(h.span)}</b></div>
        </div>
        <div class="bmc-info-note">${esc(h.note)}</div>
      </div>
    `);

    // 句子冷密度
    const dens = (r.sentences || []).filter(s => s.total_seams >= 3 && s.density >= 0.6).map(s => `
      <div class="bmc-item bmc-item-info">
        <div class="bmc-item-main">
          <div><span class="bmc-tag bmc-tag-info">Ayat ${s.idx + 1}</span> <b>Gabungan kata ganjil</b></div>
          <span class="bmc-badge bmc-badge-info">${Math.round(s.density * 100)}%</span>
        </div>
        <div class="bmc-info-note">Banyak pasangan kata dalam ayat ini jarang ditemui dalam korpus; disarankan semak struktur ayat.</div>
      </div>
    `);

    if (bl.length) secs.push(`<div class="bmc-sec-title bmc-sec-err">Gabungan Tidak Sah</div>${bl.join('')}`);
    if (sp.length) secs.push(`<div class="bmc-sec-title bmc-sec-err">Ejaan Mencurigakan</div>${sp.join('')}`);
    if (hi.length) secs.push(`<div class="bmc-sec-title bmc-sec-err">Kesalahan Tatabahasa</div>${hi.join('')}`);
    if (cold.length) secs.push(`<div class="bmc-sec-title bmc-sec-warn">Kata Tidak Dikenali</div>${cold.join('')}`);
    if (med.length) secs.push(`<div class="bmc-sec-title bmc-sec-warn">Perlu Konteks</div>${med.join('')}`);
    if (lo.length) secs.push(`<div class="bmc-sec-title bmc-sec-info">Peringatan</div>${lo.join('')}`);
    if (dens.length) secs.push(`<div class="bmc-sec-title bmc-sec-info">Struktur Ayat</div>${dens.join('')}`);

    b.innerHTML = secs.length
      ? secs.join('') + `<div class="bmc-foot">${(r.cold_words || []).length} kata jarang · ${(r.rule_hits || []).length} peraturan dikesan</div>`
      : `<div class="bmc-clean-box">
          <div class="bmc-clean-icon">✓</div>
          <div class="bmc-clean-title">Tiada Isu Dikesan</div>
          <div class="bmc-clean-desc">Semua perkataan wujud dalam korpus dan mematuhi peraturan mekanikal.</div>
        </div>`;
  }

  function prpmBtn(word) {
    if (!prpmEnabled) return '';
    return `<button class="bmc-kamus-btn" data-w="${esc(word)}" title="Semak terus di Kamus PRPM">Kamus</button>`;
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
    if (r.status === 'hit') {
      btn.replaceWith(Object.assign(document.createElement('span'), {
        className: 'bmc-badge bmc-badge-hit',
        title: 'Wujud dalam PRPM',
        textContent: '✓'
      }));
      if (r.definition && item) {
        item.insertAdjacentHTML('beforeend', `<div class="bmc-def">${esc(r.definition)}…</div>`);
      }
    } else if (r.status === 'miss') {
      btn.replaceWith(Object.assign(document.createElement('span'), {
        className: 'bmc-badge bmc-badge-miss',
        title: 'Tiada dalam PRPM',
        textContent: '✗'
      }));
      if (item && !item.querySelector('.bmc-miss-note')) {
        item.insertAdjacentHTML('beforeend', `<div class="bmc-miss-note">Tiada entri ditemui dalam kamus</div>`);
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
        <label class="bmc-form-label">Teks yang ditandakan
          <div class="bmc-sel-preview">${esc(text.slice(0, 300))}${text.length > 300 ? '…' : ''}</div>
        </label>
        <label class="bmc-form-label">Jenis kesalahan
          <select id="bmc-type" class="bmc-input-select">
            <option value="spelling">Ejaan (spelling)</option>
            <option value="grammar">Tatabahasa (grammar)</option>
            <option value="terminology">Istilah (terminology)</option>
            <option value="collocation">Gabungan kata (collocation)</option>
            <option value="other">Lain-lain</option>
          </select>
        </label>
        <label class="bmc-form-label">Penjelasan (pilihan)
          <textarea id="bmc-why" class="bmc-input-textarea" rows="3" placeholder="Kenapa ini salah? / Bentuk yang betul?"></textarea>
        </label>
        <div class="bmc-row">
          <button id="bmc-save" class="bmc-btn-primary">Simpan</button>
          <button id="bmc-cancel" class="bmc-btn-secondary">Batal</button>
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
          panel.querySelector('#bmc-msg').textContent = `✓ Disimpan (${log.length} entri dalam log)`;
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

  // ── 消息监听与快捷键 ──
  chrome.runtime.onMessage.addListener((m, sender, sendResponse) => {
    if (m.type === 'context-prpm') {
      runPrpm(m.text || '');
      sendResponse({ ok: true });
    } else if (m.type === 'context-check') {
      runCheck(m.text || '');
      sendResponse({ ok: true });
    } else if (m.type === 'context-raise') {
      showRaiseForm(m.text || '');
      sendResponse({ ok: true });
    } else if (m.type === 'check-selection') {
      const text = getSelection()?.toString().trim();
      if (text && text.length >= 2) runCheck(text);
      sendResponse({ ok: true });
    }
    return false;
  });
})();
