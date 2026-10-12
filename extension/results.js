/* Shared, escaped result rendering and exact occurrence highlighting. */
(() => {
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const weight = {error:3, warning:2, info:1};
  const category = {spelling:'Spelling', grammar:'Grammar', terminology:'Terminology / usage', register:'Register'};
  // icon 按钮（右上角）：evidence / PRPM / web search；Wrong(flag) 只在
  // extension 环境（chrome.runtime）渲染——web Check tab 没有 flag 通道。
  const inExt = typeof chrome !== 'undefined' && chrome.runtime?.id;
  function cornerBtns(word, origin) {
    // data-tip → CSS 即时 tooltip（原生 title 延迟 1s 太迟钝）。
    // Local evidence 已删（用户裁决 2026-10-12）：无 evidence 库的部署点了
    // 只会报错，查询需求由两个 web 搜索 icon 覆盖。
    const one = (cls, tip, svg, attrs='') => `<button class="${cls}" ${attrs} data-tip="${esc(tip)}" aria-label="${esc(tip)}"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${svg}</svg></button>`;
    return `<div class="bmc-item-corner">`
      + `<a class="bmc-search-link" data-tip="${esc(`Is "${word}" correct?`)}" aria-label="${esc(`Is "${word}" correct?`)}" href="https://www.google.com/search?q=${encodeURIComponent(`is "${word}" correct`)}" target="_blank" rel="noopener noreferrer"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14"/></svg></a>`
      + (inExt ? one('bmc-wrong-btn', `Wrong flag?${origin ? ` (${origin})` : ''}`, '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>', `data-word="${esc(word)}" data-rule="${esc(origin || '')}"`) : '')
      + `</div>`;
  }
  function html(r) {
    if (r.error) return `<div class="bmc-err-box">${esc(r.error)}</div>`;
    if (!r.engine) return '<div class="bmc-err-box">Server did not return a result — is Tepat running?</div>';
    const sections = [];
    if (r.coverage?.unsupported_script) sections.push('<div class="bmc-warn-note">Teks mengandungi tulisan di luar skop Latin yang belum disemak sepenuhnya.</div>');
    for (const level of ['error','warning','info']) {
      const issues = (r.issues || []).filter(i => i.level === level);
      if (!issues.length) continue;
      const cls = {error:'miss',warning:'warn',info:'info'}[level];
      sections.push(`<div class="bmc-sec-title">${{error:'Specific rules',warning:'Needs review',info:'Needs context'}[level]}</div>`);
      for (const i of issues) {
        const word = i.span.trim();
        // 类别 tag：grammar 是面板主体（不需要自报家门），只标出
        // spelling/terminology/register 这类特殊来源
        const tag = i.category && i.category !== 'grammar' ? `<span class="bmc-tag">${esc(category[i.category] || i.category)}</span> ` : '';
        sections.push(`<div class="bmc-item bmc-item-${cls}">${cornerBtns(word, i.origin)}<div class="bmc-item-main"><div>${tag}<b>${esc(word)}</b>${i.suggestion ? ` <span class="bmc-arrow">→</span> <b class="bmc-sug">${esc(i.suggestion)}</b>` : ''}</div></div><div class="bmc-${cls}-note">${esc(i.note)}</div></div>`);
      }
    }
    if (!(r.issues || []).length) sections.push('<div class="bmc-clean-box"><b>No signals detected within this check.</b><div>Whole-sentence accuracy and facts remain unverified.</div></div>');
    for (const warning of r.config_warnings || []) sections.push(`<div class="bmc-warn-note">${esc(warning)}</div>`);
    sections.push(`<div class="bmc-foot">${r.total_words || 0} words · ${(r.issues || []).length} signals · ${r.elapsed_ms || 0} ms</div>`);
    return sections.join('');
  }
  function segments(text, issues, offset=0) {
    const relevant = issues.filter(i => i.highlight !== false && Number.isInteger(i.start) && Number.isInteger(i.end) && i.end>offset && i.start<offset+text.length);
    const boundaries = new Set([0,text.length]);
    for (const i of relevant) { boundaries.add(Math.max(0,i.start-offset)); boundaries.add(Math.min(text.length,i.end-offset)); }
    const points=[...boundaries].sort((a,b)=>a-b), out=[];
    for (let i=0;i<points.length-1;i++) {
      const start=points[i],end=points[i+1];
      const matches=relevant.filter(h=>h.start<offset+end && h.end>offset+start).sort((a,b)=>(weight[b.level]||0)-(weight[a.level]||0));
      out.push({text:text.slice(start,end),start,end,level:matches[0]?.level || null,notes:matches.map(h=>h.note),issues:matches});
    }
    return out;
  }
  const api={html,segments,esc};
  if (typeof module !== 'undefined') module.exports=api;
  else globalThis.TepatResults=api;
})();
