/* Shared, escaped result rendering and exact occurrence highlighting. */
(() => {
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const weight = {error:3, warning:2, info:1};
  const category = {spelling:'Spelling', grammar:'Grammar', terminology:'Terminology / usage', register:'Register'};
  function html(r) {
    if (r.error) return `<div class="bmc-err-box">${esc(r.error)}</div>`;
    if (r.engine !== 'evidence-v2') return '<div class="bmc-err-box">Please use the latest Tepat version with the cleaned evidence database.</div>';
    const sections = [];
    sections.push('<div class="bmc-foot">Spelling & terminology: limited evidence · Grammar: local rules and context · Facts: not checked.</div>');
    if (r.coverage?.unsupported_script) sections.push('<div class="bmc-warn-note">Teks mengandungi tulisan di luar skop Latin yang belum disemak sepenuhnya.</div>');
    for (const level of ['error','warning','info']) {
      const issues = (r.issues || []).filter(i => i.level === level);
      if (!issues.length) continue;
      const cls = {error:'miss',warning:'warn',info:'info'}[level];
      sections.push(`<div class="bmc-sec-title">${{error:'Specific rules',warning:'Needs review',info:'Needs context'}[level]}</div>`);
      for (const i of issues) {
        const word = i.span.trim();
        sections.push(`<div class="bmc-item bmc-item-${cls}"><div class="bmc-item-main"><div><span class="bmc-tag">${esc(category[i.category] || i.category)}</span> <b>${esc(word)}</b>${i.suggestion ? ` <span class="bmc-arrow">→</span> <b class="bmc-sug">${esc(i.suggestion)}</b>` : ''}</div></div><div class="bmc-${cls}-note">${esc(i.note)}</div><div class="bmc-actions"><button class="bmc-evidence-btn" data-q="${esc(word)}">Local evidence</button>${!/\s/.test(word) ? `<button class="bmc-kamus-btn" data-w="${esc(word)}">PRPM</button>` : ''}<a class="bmc-search-link" href="https://www.google.com/search?q=${encodeURIComponent(word + ' bahasa Melayu DBP')}" target="_blank" rel="noopener noreferrer">Search web</a></div></div>`);
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
