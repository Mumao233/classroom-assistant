(function (global) {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const forbidden = /\\(?:href|url|html\w*|includegraphics|class|style|def|gdef|edef|xdef|global|newcommand|renewcommand|let|futurelet)\b/i;
  function formula(source, display, original) {
    try {
      if (!global.katex || source.length > 4000 || forbidden.test(source)) throw new Error('unsupported');
      return global.katex.renderToString(source, {displayMode: display, throwOnError: true,
        trust: false, strict: 'error', maxExpand: 200, maxSize: 20, macros: Object.create(null)});
    } catch (_) {
      return `<span class="formula-fallback" role="note">公式无法渲染，保留原文：<code>${escape(original)}</code></span>`;
    }
  }
  function inline(text) {
    let out = '', cursor = 0;
    // Only formatting generated here and trusted local KaTeX output becomes HTML.
    const token = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\\\((.+?)\\\)|(?<!\\)\$([^$\n]+)\$/g;
    for (const match of text.matchAll(token)) {
      out += escape(text.slice(cursor, match.index));
      if (match[1] !== undefined) out += `<code>${escape(match[1])}</code>`;
      else if (match[2] !== undefined) out += `<strong>${escape(match[2])}</strong>`;
      else out += formula(match[3] ?? match[4], false, match[0]);
      cursor = match.index + match[0].length;
    }
    return out + escape(text.slice(cursor));
  }
  function render(value) {
    if (value == null || value === '') return '';
    const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    const lines = text.replace(/\r\n?/g, '\n').split('\n');
    const output = [];
    let code = null, math = null, mathClose = '', paragraph = [], list = '';
    const flush = () => { if (paragraph.length) { output.push(`<p>${paragraph.map(inline).join('<br>')}</p>`); paragraph = []; } };
    const closeList = () => { if (list) { output.push(`</${list}>`); list = ''; } };
    for (const line of lines) {
      if (code !== null) {
        if (/^\s*```/.test(line)) { output.push(`<pre><code>${escape(code.join('\n'))}</code></pre>`); code = null; }
        else code.push(line);
        continue;
      }
      if (math !== null) {
        const end = line.indexOf(mathClose);
        if (end >= 0) {
          math.push(line.slice(0, end));
          const source = math.join('\n');
          output.push(formula(source, true, (mathClose === '$$' ? '$$' : '\\[') + source + mathClose));
          math = null;
          if (line.slice(end + mathClose.length).trim()) paragraph.push(line.slice(end + mathClose.length));
        } else math.push(line);
        continue;
      }
      if (/^\s*```/.test(line)) { flush(); closeList(); code = []; continue; }
      const start = line.match(/^\s*(\$\$|\\\[)(.*)$/);
      if (start) {
        flush(); closeList(); mathClose = start[1] === '$$' ? '$$' : '\\]';
        const tail = start[2], end = tail.indexOf(mathClose);
        if (end >= 0) {
          output.push(formula(tail.slice(0, end), true, start[1] + tail.slice(0, end) + mathClose));
          if (tail.slice(end + mathClose.length).trim()) paragraph.push(tail.slice(end + mathClose.length));
        } else math = [tail];
        continue;
      }
      if (!line.trim()) { flush(); closeList(); continue; }
      const heading = line.match(/^#{1,6}\s+(.+)$/);
      const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (heading) { flush(); closeList(); output.push(`<h4>${inline(heading[1])}</h4>`); }
      else if (bullet || ordered) {
        flush();
        const kind = bullet ? 'ul' : 'ol';
        if (list !== kind) { closeList(); list = kind; output.push(`<${kind}>`); }
        output.push(`<li>${inline((bullet || ordered)[1])}</li>`);
      } else { closeList(); paragraph.push(line); }
    }
    flush(); closeList();
    if (code !== null) output.push(`<pre><code>${escape(code.join('\n'))}</code></pre>`);
    if (math !== null) output.push(`<span class="formula-fallback" role="note">公式未闭合，保留原文：<code>${escape((mathClose === '$$' ? '$$' : '\\[') + math.join('\n'))}</code></span>`);
    return output.join('');
  }
  function hydrate(root) {
    if (!root?.querySelectorAll) return;
    for (const element of root.querySelectorAll('.review-tex')) {
      const text = element.textContent || '';
      element.innerHTML = formula(text, element.getAttribute('data-display') === 'true', text);
      element.classList.remove('review-tex');
    }
  }
  function time(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds)) || Number(seconds) < 0) return '时间未知';
    const s = Math.floor(Number(seconds));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }
  global.ReviewRender = Object.freeze({render, hydrate, escape, time});
})(typeof window !== 'undefined' ? window : globalThis);
