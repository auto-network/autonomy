// x-markdown Alpine custom directive
// Renders markdown with DOMPurify sanitization, highlight.js syntax highlighting,
// and secure link handling (internal SPA links via navigateTo, external in new tab).
//
// Usage: <div x-markdown="expression"></div>
// The expression should evaluate to a markdown string.

const SECURE_CONFIG = {
  ALLOWED_TAGS: ['h1','h2','h3','h4','h5','h6','p','br','hr','ul','ol','li',
                 'blockquote','pre','code','em','strong','del','a','img',
                 'table','thead','tbody','tr','th','td','sup','sub','details','summary'],
  ALLOWED_ATTR: ['href','src','alt','title','class','id','colspan','rowspan','align'],
  ALLOW_DATA_ATTR: false,
  ADD_ATTR: ['target'],
  FORBID_TAGS: ['script','style','iframe','object','embed','form','input',
                'textarea','select','meta','link'],
  FORBID_ATTR: ['onerror','onload','onclick','onmouseover','onfocus','onblur','style'],
};

document.addEventListener('alpine:init', () => {
  Alpine.directive('markdown', (el, { expression }, { effect, evaluate }) => {
    effect(() => {
      let text = evaluate(expression) || '';
      // Rewrite graph:// image refs BEFORE DOMPurify (which strips unknown protocols)
      text = text.replace(/!\[([^\]]*)\]\(graph:\/\/([^)]+)\)/g, '![$1](/api/attachment/$2)');
      const html = DOMPurify.sanitize(marked.parse(text), SECURE_CONFIG);
      el.classList.add('markdown-body');
      el.innerHTML = html;
      // Wrap tables in horizontally-scrollable containers for mobile
      el.querySelectorAll('table').forEach(function(t) {
        var wrapper = document.createElement('div');
        wrapper.className = 'md-table-scroll';
        t.parentNode.insertBefore(wrapper, t);
        wrapper.appendChild(t);
      });
      // $nextTick not available in directive context — use queueMicrotask
      queueMicrotask(() => {
        el.querySelectorAll('pre code').forEach(b => hljs.highlightElement(b));
      });
      // Post-process links
      el.querySelectorAll('a').forEach(a => {
        const href = a.getAttribute('href') || '';
        if (/^javascript:|^data:/i.test(href)) {
          a.removeAttribute('href');
        } else if (href.startsWith('/')) {
          // Internal SPA link — use navigateTo() (app.js)
          a.addEventListener('click', e => { e.preventDefault(); navigateTo(href); });
        } else {
          a.setAttribute('rel', 'noopener noreferrer');
          a.setAttribute('target', '_blank');
        }
      });
      // Linkify references — bead IDs, graph:// URIs, source IDs
      // Each rule: regex source contributes one capture group; group index maps to href/text
      const LINK_RULES = [
        { group: 1, href: id => '/bead/' + id,   display: (full, id) => id },    // auto-xxxx
        { group: 2, href: id => '/graph/' + id,  display: (full, id) => full },  // graph://xxxxx
        { group: 3, href: id => '/graph/' + id,  display: (full, id) => id },    // 9e1a2361-405
      ];
      //                       group 1: bead              group 2: graph:// URI               group 3: source ID
      const COMBINED_RE = /\b(auto-[a-z0-9]{2,8})\b|graph:\/\/([0-9a-f]{8}[-0-9a-f]*)|\b([0-9a-f]{8}-[0-9a-f]{3})\b/g;
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
      const textNodes = [];
      while (walker.nextNode()) textNodes.push(walker.currentNode);
      for (const node of textNodes) {
        if (node.parentElement && (node.parentElement.tagName === 'A' || node.parentElement.tagName === 'PRE' || node.parentElement.closest('pre'))) continue;
        if (!COMBINED_RE.test(node.textContent)) continue;
        COMBINED_RE.lastIndex = 0;
        const frag = document.createDocumentFragment();
        let last = 0;
        let m;
        while ((m = COMBINED_RE.exec(node.textContent)) !== null) {
          const rule = LINK_RULES.find(r => m[r.group] !== undefined);
          if (!rule) continue;
          const id = m[rule.group];
          if (m.index > last) frag.appendChild(document.createTextNode(node.textContent.slice(last, m.index)));
          const a = document.createElement('a');
          a.href = rule.href(id);
          a.textContent = rule.display(m[0], id);
          a.className = 'text-indigo-400 hover:underline';
          a.addEventListener('click', e => { e.preventDefault(); navigateTo(a.getAttribute('href')); });
          frag.appendChild(a);
          last = m.index + m[0].length;
        }
        if (last < node.textContent.length) frag.appendChild(document.createTextNode(node.textContent.slice(last)));
        node.parentNode.replaceChild(frag, node);
      }
    });
  });
});
