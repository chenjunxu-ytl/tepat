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
        btnGrab.title = 'Aktifkan mod auto-ambil teks (pilih teks terus dihantar ke sini)';
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
    panel = mkPanel('Tepat — Semakan', '', 'prpm');
    const b = panelBody();
    if (b) {
      b.innerHTML = `
        <div class="bmc-item" style="text-align:center;padding:16px 12px;color:#64748b;">
          <div style="font-size:22px;margin-bottom:6px;">📖</div>
          <div style="font-weight:600;color:#1e293b;margin-bottom:4px;">Taip perkataan atau ayat di atas</div>
          <div style="font-size:12px;">Tekan <b>PRPM</b> untuk semak kamus atau <b>Semak</b> untuk tatabahasa.</div>
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

    const words = [...new Set(rawWords)].slice(0, 8);
    if (!words.length) return;

    const title = words.length === 1 ? `PRPM — ${words[0]}` : `PRPM (${words.length} kata)`;
    if (shouldCreatePanel || !panel) {
      panel = mkPanel(title, cleanText, 'prpm');
    } else {
      currentMode = 'prpm';
      panel.querySelector('.bmc-title-text').textContent = title;
      panel.querySelector('.bmc-search-input').value = cleanText;
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
        const isSuggestion = r.status === 'warn' ||
          (r.definition && /adakah anda bermaksud/i.test(r.definition));

        if (isSuggestion) {
          el.classList.add('bmc-item-warn');
          el.querySelector('.bmc-item-main').innerHTML = `
            <span class="bmc-word">${esc(w)}</span>
            <span class="bmc-badge bmc-badge-warn" title="Cadangan perkataan terdekat">?</span>
          `;
          if (r.definition) {
            el.insertAdjacentHTML('beforeend', `<div class="bmc-warn-note">${esc(r.definition)}</div>`);
          } else {
            el.insertAdjacentHTML('beforeend', `<div class="bmc-warn-note">Tiada entri tepat, ada cadangan berkaitan</div>`);
          }
        } else if (r.status === 'hit') {
          el.classList.add('bmc-item-hit');
          el.querySelector('.bmc-item-main').innerHTML = `
            <span class="bmc-word">${esc(w)}</span>
            <span class="bmc-badge bmc-badge-hit" title="Wujud dalam PRPM">✓</span>
          `;
          if (r.definition) {
            el.insertAdjacentHTML('beforeend', `<div class="bmc-def">${esc(r.definition)}</div>`);
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
    const cleanText = sanitizeText(text);
    if (shouldCreatePanel || !panel) {
      panel = mkPanel('Hasil Semakan', cleanText, 'check');
    } else {
      currentMode = 'check';
      panel.querySelector('.bmc-title-text').textContent = 'Hasil Semakan';
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
          <div class="bmc-err-title">Tepat.exe Tidak Aktif</div>
          <div class="bmc-err-desc">Sila pastikan aplikasi Tepat berjalan di komputer anda (localhost:${PORT}).</div>
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
      target.innerHTML = rows.length ? rows.join('') : 'Tiada bukti tempatan ditemui; semak sumber luar.';
    } catch { showToast('Bukti tempatan tidak dapat dimuatkan.',3000); }
    finally { btn.disabled=false; }
  });

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
    if (!fullText.trim()) { showToast('Tiada teks sesuai ditemui.',3000);return; }
    let r;
    try {
      r=await fetch(`${API}/api/scan`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:fullText})}).then(x=>x.json());
      if (r.error || r.engine!=='evidence-v2') throw new Error(r.error || 'Versi Tepat perlu dikemas kini.');
    } catch (e) { showToast(esc(e.message || 'Semakan tidak selesai.'),4000);return; }
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
    showToast(`Imbasan sehingga 30,000 aksara: <b>${hitCount}</b> bahagian ditandakan. Fakta belum disemak.
      <button id="bmc-btn-clear-hl" style="margin-left:8px;padding:2px 8px;">Padam Serlah</button>`,12000);

    setTimeout(() => {
      const btn = document.querySelector('#bmc-btn-clear-hl');
      if (btn) btn.onclick = () => clearPageHighlights();
    }, 50);
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
