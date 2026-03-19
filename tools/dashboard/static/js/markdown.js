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
      const text = evaluate(expression);
      const html = DOMPurify.sanitize(marked.parse(text || ''), SECURE_CONFIG);
      el.classList.add('markdown-body');
      el.innerHTML = html;
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
    });
  });
});
