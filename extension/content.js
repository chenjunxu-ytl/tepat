/* content.js — Tepat content script
 *
 * 交互模型（用户裁决 2026-10-01）：
 *  1. 选中文字 → 浮条出现「✓ Semak / ✗ Raise Error」
 *  2. Semak → 调本地 exe /api/scan，浮层按置信度浅→深渲染：
 *       深: spelling 后缀规律错 / blacklist bigram / high 规则
 *       中: cold 零频词 / medium 规则
 *       浅: low 规则提醒 / 句子冷密度
 *  3. PRPM 由用户决定：浮层里每个词旁有「Kamus」按钮（若 popup 开关
 *     允许），点击 → /api/prpm → 三态 + 释义展示
 *  4. Raise Error → 表单（错误类型 + 可选解释）→ chrome.storage.local
 *     日志，popup 里可导出 JSON
 *
 * 不自动整页扫描：由用户选中触发（误报 annoy 值为零）。
 */
(() => {
  const PORT = 8377;
  const API = `http://127.0.0.1:${PORT}`;
  let prpmEnabled = true;  // 与 popup 开关同步

  chrome.storage.sync.get({ prpmEnabled: true }, (s) => { prpmEnabled = s.prpmEnabled; });
  chrome.storage.onChanged.addListener((ch) => {
    if (ch.prpmEnabled) prpmEnabled = ch.prpmEnabled.newValue;
  });

  // ── 浮层（选中浮条已移除，右键菜单直达功能）──
  let panel = null;

  function removeUI() {
    panel?.remove(); panel = null;
  }

  // ── 选中浮条已移除（用户裁决 2026-10-02：右键菜单替代）──
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') removeUI(); });

  // ── 右键菜单动作（菜单项在 background.js 里注册，这里处理点击）──
  // 用户裁决 2026-10-02：不要选中浮条，右键菜单直达功能。
  chrome.runtime.onMessage.addListener((m, sender, sendResponse) => {
    if (m.type === 'context-prpm') {
      removeUI();
      runPrpm(m.text);
      sendResponse({ ok: true });
    } else if (m.type === 'context-check') {
      removeUI();
      runCheck(m.text);
      sendResponse({ ok: true });
    } else if (m.type === 'context-raise') {
      removeUI();
      showRaiseForm(m.text);
      sendResponse({ ok: true });
    }
    return false;
  });

  // ── PRPM 直查浮层（右键菜单触发）──
  async function runPrpm(text) {
    const words = [...new Set(
      text.toLowerCase().split(/[^\p{L}'-]+/u)
        .map(w => w.trim())
        .filter(w => w.length >= 3 && w.length <= 40)
    )].slice(0, 8);  // 右键直查：上限 8 词防 flood
    if (!words.length) return;
    panel = mkPanel('PRPM — ' + (words.length === 1 ? words[0] : `${words.length} kata`));
    const b = panelBody();
    b.innerHTML = words.map(w =>
      `<div class="bmc-item" id="bmc-p-${CSS.escape(w)}"><b>${esc(w)}</b> — <span class="bmc-k-unreach">menyemak…</span></div>`
    ).join('');
    // 串行查（exe 端全局锁兜底，前端不再排队等待）
    for (const w of words) {
      const el = b.querySelector(`#bmc-p-${CSS.escape(w)}`);
      try {
        const r = await fetch(`${API}/api/prpm`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ word: w })
        }).then(x => x.json());
        let tag;
        if (r.status === 'hit') {
          tag = '<span class="bmc-k-hit">✓ ada entri</span>';
          if (r.definition) el.insertAdjacentHTML('beforeend', `<div class="bmc-def">${esc(r.definition)}…</div>`);
        } else if (r.status === 'miss') {
          tag = '<span class="bmc-k-miss">✗ tiada entri</span>';
        } else {
          tag = '<span class="bmc-k-unreach">? PRPM tidak dapat dihubungi</span>';
        }
        el.querySelector('span').outerHTML = tag;
      } catch {
        el.querySelector('span').outerHTML = '<span class="bmc-k-unreach">? exe tidak berjalan</span>';
      }
    }
  }

  // ── 检查浮层 ──
  async function runCheck(text) {
    panel = mkPanel('Hasil Semakan');
    panel.querySelector('.bmc-body').innerHTML = '<div class="bmc-loading">menyemak…</div>';
    let r;
    try {
      r = await fetch(`${API}/api/scan`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      }).then(x => x.json());
    } catch {
      panelBody().innerHTML =
        `<div class="bmc-err">Tepat.exe tidak berjalan.<br>Mulakan aplikasi itu dahulu (localhost:${PORT}).</div>`;
      return;
    }
    renderCheck(r, text);
  }

  function renderCheck(r, text) {
    const b = panelBody();
    const secs = [];
    const bl = (r.blacklist_hits || []).map(h =>
      `<div class="bmc-item deep">🔴 <b>${esc(h.bigram)}</b> — gabungan kata disahkan salah (blacklist)</div>`);
    const sp = (r.cold_words || []).filter(c => c.reason === 'spelling').map(c =>
      `<div class="bmc-item deep">🔴 <b>${esc(c.word)}</b> — corak ejaan ms/id
        ${c.suggestion ? `→ <b>${esc(c.suggestion)}</b>` : ''}
        ${prpmBtn(c.word)}</div>`);
    const hi = (r.rule_hits || []).filter(h => h.conf === 'high').map(h =>
      `<div class="bmc-item deep">🔴 <b>${esc(h.span)}</b> [${h.rule}] ${esc(h.note)}</div>`);
    const cold = (r.cold_words || []).filter(c => c.reason === 'cold').map(c =>
      `<div class="bmc-item mid">🟠 <b>${esc(c.word)}</b> — perkataan ini tidak dikenali (tiada dalam senarai kata)
        ${prpmBtn(c.word)}</div>`);
    const med = (r.rule_hits || []).filter(h => h.conf === 'medium').map(h =>
      `<div class="bmc-item mid">🟠 <b>${esc(h.span)}</b> [${h.rule}] ${esc(h.note)}</div>`);
    const lo = (r.rule_hits || []).filter(h => h.conf === 'low').map(h =>
      `<div class="bmc-item low">🟡 <b>${esc(h.span)}</b> [${h.rule}] ${esc(h.note)}</div>`);
    const dens = (r.sentences || []).filter(s => s.total_seams >= 3 && s.density >= 0.6)
      .map(s => `<div class="bmc-item low">🟡 Ayat ${s.idx + 1} — gabungan kata dalam ayat ini jarang ditemui dalam korpus; semak struktur ayat</div>`);

    if (bl.length) secs.push(`<h4>Gabungan Disahkan Salah</h4>${bl.join('')}`);
    if (sp.length) secs.push(`<h4>Ejaan (corak mekanikal)</h4>${sp.join('')}`);
    if (hi.length) secs.push(`<h4>Peraturan (keyakinan tinggi)</h4>${hi.join('')}`);
    if (cold.length) secs.push(`<h4>Perkataan Tidak Dikenali</h4>${cold.join('')}`);
    if (med.length) secs.push(`<h4>Peraturan (perlu konteks)</h4>${med.join('')}`);
    if (lo.length) secs.push(`<h4>Peringatan Umum</h4>${lo.join('')}`);
    if (dens.length) secs.push(`<h4>Ayat Mencurigakan</h4>${dens.join('')}`);

    b.innerHTML = secs.length
      ? secs.join('') + `<div class="bmc-foot">${(r.cold_words || []).length} kata tidak dikenali · ${(r.rule_hits || []).length} peraturan · ${(r.blacklist_hits || []).length} blacklist</div>`
      : `<div class="bmc-clean">✓ Tiada isu mekanikal dikesan. (Semakan wujudan kata sahaja — bukan penghakiman tatabahasa.)</div>`;
  }

  function prpmBtn(word) {
    if (!prpmEnabled) return '';
    return ` <button class="bmc-kamus" data-w="${esc(word)}">📖 Kamus</button>`;
  }

  // PRPM 询问 + 结果（用户裁决：选中时决定是否查，结果就地展示）
  panelBody()?.addEventListener?.('click', () => {});
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest?.('.bmc-kamus');
    if (!btn) return;
    const w = btn.dataset.w;
    btn.disabled = true; btn.textContent = '📖 …';
    let r;
    try {
      r = await fetch(`${API}/api/prpm`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word: w })
      }).then(x => x.json());
    } catch { r = { status: 'unreachable' }; }
    const item = btn.closest('.bmc-item');
    let tag, def = '';
    if (r.status === 'hit') {
      tag = '<span class="bmc-k-hit">✓ Kamus Dewan: wujud</span>';
      def = r.definition ? `<div class="bmc-def">${esc(r.definition)}…</div>` : '';
    } else if (r.status === 'miss') {
      tag = '<span class="bmc-k-miss">✗ Tiada entri kamus</span>';
    } else {
      tag = '<span class="bmc-k-unreach">? PRPM tidak boleh dihubungi — TIDAK bermakna salah eja</span>';
    }
    btn.replaceWith(Object.assign(document.createElement('span'), { innerHTML: tag }));
    if (def) item.insertAdjacentHTML('beforeend', def);
  });

  // ── Raise Error 表单 ──
  function showRaiseForm(text) {
    panel = mkPanel('Raise Error');
    panelBody().innerHTML = `
      <div class="bmc-raise-form">
        <label>Teks yang ditandakan<div class="bmc-sel">${esc(text.slice(0, 300))}${text.length > 300 ? '…' : ''}</div></label>
        <label>Jenis kesalahan
          <select id="bmc-type">
            <option value="spelling">Ejaan (spelling)</option>
            <option value="grammar">Tatabahasa (grammar)</option>
            <option value="terminology">Istilah (terminology)</option>
            <option value="collocation">Gabungan kata (collocation)</option>
            <option value="other">Lain-lain</option>
          </select></label>
        <label>Penjelasan (pilihan)<textarea id="bmc-why" rows="3" placeholder="Kenapa ini salah? / bentuk yang betul?"></textarea></label>
        <div class="bmc-row">
          <button id="bmc-save" class="bmc-b bmc-check">Simpan</button>
          <button id="bmc-cancel" class="bmc-b">Batal</button>
        </div>
        <div id="bmc-msg" class="bmc-msg"></div>
      </div>`;
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

  // ── 工具 ──
  function mkPanel(title) {
    removeUI();
    panel = document.createElement('div');
    panel.className = 'bmc-panel';
    panel.innerHTML = `<div class="bmc-head"><span>${title}</span><button class="bmc-x">✕</button></div><div class="bmc-body"></div>`;
    panel.querySelector('.bmc-x').onclick = removeUI;
    // 放在视口右上，避开选区
    panel.style.right = '16px'; panel.style.top = '80px'; panel.style.left = 'auto';
    document.documentElement.appendChild(panel);
    return panel;
  }
  function panelBody() { return panel?.querySelector('.bmc-body'); }
  function esc(s) {
    return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  // 快捷键 Ctrl+Shift+B 检查当前选区
  chrome.runtime.onMessage.addListener((m) => {
    if (m.type === 'check-selection') {
      const text = getSelection()?.toString().trim();
      if (text && text.length >= 3) runCheck(text);
    }
  });
})();
