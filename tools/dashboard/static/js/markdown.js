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

// ── Embed resolution ──────────────────────────────────────────────
// ![[id]] embeds are resolved asynchronously via /api/resolve/{id}.
// Each embed gets a placeholder div that is filled in after the fetch.

const EMBED_RE = /!\[\[([^\]]+)\]\]/g;

function _createEmbedPlaceholder(embedId) {
  const wrapper = document.createElement('div');
  wrapper.className = 'embed-wrapper';
  wrapper.dataset.embedId = embedId;
  wrapper.innerHTML = '<div class="embed-skeleton" style="height:60px;background:var(--bg-secondary,#1a1a2e);border:1px solid var(--border,#333);border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--text-muted,#888);font-size:13px;">Loading embed…</div>';
  return wrapper;
}

// Exposed globally so source.js can reuse for direct rich-content view
window.renderRichEmbed = _renderEmbed;

function _renderEmbed(wrapper, data) {
  wrapper.innerHTML = '';
  wrapper.style.position = 'relative';
  wrapper.style.margin = '1rem 0';

  if (data.type === 'rich-content' && data.attachment_url) {
    // Rich-content note — iframe + toggle
    const hasAlt = data.alt_text && data.alt_text.trim();
    const showingHtml = { value: true };

    // Content area
    const contentArea = document.createElement('div');
    contentArea.className = 'embed-content';

    // Iframe view
    const iframe = document.createElement('iframe');
    iframe.setAttribute('data-testid', 'rich-content-iframe');
    iframe.src = data.attachment_url;
    iframe.sandbox = 'allow-same-origin';
    iframe.style.cssText = 'width:100%;border:none;border-radius:6px;min-height:200px;display:block;';
    // Auto-resize: kill scrollbar inside content, then match height
    iframe.addEventListener('load', () => {
      try {
        const d = iframe.contentDocument;
        d.documentElement.style.overflowY = 'hidden';
        d.documentElement.style.overflowX = 'auto';
        d.body.style.overflowY = 'hidden';
        d.body.style.overflowX = 'auto';
        const h = Math.max(d.documentElement.scrollHeight, d.body.offsetHeight);
        iframe.style.height = h + 'px';
      } catch (e) { /* cross-origin, ignore */ }
    });
    contentArea.appendChild(iframe);

    // Alt-text view (hidden by default)
    const altDiv = document.createElement('div');
    altDiv.className = 'embed-alt markdown-body';
    altDiv.style.display = 'none';
    if (hasAlt) {
      altDiv.innerHTML = DOMPurify.sanitize(marked.parse(data.alt_text), SECURE_CONFIG);
    }
    contentArea.appendChild(altDiv);

    wrapper.appendChild(contentArea);

    // Controls bar (top-left)
    const controls = document.createElement('div');
    controls.style.cssText = 'display:flex;gap:6px;margin-bottom:6px;';
    const btnStyle = 'font-size:11px;padding:3px 10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#8b949e;cursor:pointer;transition:all 0.15s ease;';
    const btnHover = (btn) => {
      btn.addEventListener('mouseenter', () => { btn.style.color = '#58a6ff'; btn.style.borderColor = '#58a6ff'; });
      btn.addEventListener('mouseleave', () => { btn.style.color = '#8b949e'; btn.style.borderColor = '#30363d'; });
    };

    if (hasAlt) {
      const toggle = document.createElement('button');
      toggle.setAttribute('data-testid', 'rich-toggle');
      toggle.textContent = 'Show Text';
      toggle.style.cssText = btnStyle;
      btnHover(toggle);
      toggle.addEventListener('click', () => {
        if (showingHtml.value) {
          iframe.style.display = 'none';
          altDiv.style.display = 'block';
          toggle.textContent = 'Show Diagram';
          showingHtml.value = false;
        } else {
          altDiv.style.display = 'none';
          iframe.style.display = 'block';
          toggle.textContent = 'Show Text';
          showingHtml.value = true;
        }
      });
      controls.appendChild(toggle);
    }

    // View Source link
    if (data.id) {
      const viewSrc = document.createElement('button');
      viewSrc.textContent = 'View Source';
      viewSrc.style.cssText = btnStyle;
      btnHover(viewSrc);
      viewSrc.addEventListener('click', () => { navigateTo('/graph/' + data.id.slice(0, 12)); });
      controls.appendChild(viewSrc);
    }

    wrapper.insertBefore(controls, wrapper.firstChild);

  } else if (data.type === 'attachment' && data.mime_type && data.mime_type.startsWith('image/')) {
    // Image attachment — img + alt toggle
    const hasAlt = data.alt_text && data.alt_text.trim();
    const showingImg = { value: true };

    const contentArea = document.createElement('div');
    contentArea.className = 'embed-content';

    const img = document.createElement('img');
    img.src = data.attachment_url;
    img.alt = data.alt_text || data.filename || '';
    img.style.cssText = 'max-width:100%;border-radius:6px;';
    contentArea.appendChild(img);

    const altDiv = document.createElement('div');
    altDiv.className = 'embed-alt markdown-body';
    altDiv.style.display = 'none';
    if (hasAlt) {
      altDiv.innerHTML = DOMPurify.sanitize(marked.parse(data.alt_text), SECURE_CONFIG);
    }
    contentArea.appendChild(altDiv);

    wrapper.appendChild(contentArea);

    // Controls bar (top-left)
    const controls = document.createElement('div');
    controls.style.cssText = 'display:flex;gap:6px;margin-bottom:6px;';
    const btnStyle = 'font-size:11px;padding:3px 10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#8b949e;cursor:pointer;transition:all 0.15s ease;';
    const btnHover = (btn) => {
      btn.addEventListener('mouseenter', () => { btn.style.color = '#58a6ff'; btn.style.borderColor = '#58a6ff'; });
      btn.addEventListener('mouseleave', () => { btn.style.color = '#8b949e'; btn.style.borderColor = '#30363d'; });
    };

    if (hasAlt) {
      const toggle = document.createElement('button');
      toggle.setAttribute('data-testid', 'rich-toggle');
      toggle.textContent = 'Show Alt';
      toggle.style.cssText = btnStyle;
      btnHover(toggle);
      toggle.addEventListener('click', () => {
        if (showingImg.value) {
          img.style.display = 'none';
          altDiv.style.display = 'block';
          toggle.textContent = 'Show Image';
          showingImg.value = false;
        } else {
          altDiv.style.display = 'none';
          img.style.display = 'block';
          toggle.textContent = 'Show Alt';
          showingImg.value = true;
        }
      });
      controls.appendChild(toggle);
    }

    wrapper.insertBefore(controls, wrapper.firstChild);

  } else if (data.type === 'attachment') {
    // Non-image attachment — download link
    const link = document.createElement('a');
    link.href = data.attachment_url;
    link.textContent = data.filename || 'Download attachment';
    link.className = 'text-indigo-400 hover:underline';
    link.setAttribute('download', '');
    if (data.alt_text) {
      const desc = document.createElement('p');
      desc.textContent = data.alt_text;
      desc.style.cssText = 'font-size:13px;color:var(--text-muted,#888);margin-top:4px;';
      wrapper.appendChild(link);
      wrapper.appendChild(desc);
    } else {
      wrapper.appendChild(link);
    }

  } else if (data.type === 'note' && data.content) {
    // Plain note embed — render inline as markdown
    const div = document.createElement('div');
    div.className = 'embed-note markdown-body';
    div.style.cssText = 'border-left:3px solid var(--border,#333);padding-left:1rem;margin:0.5rem 0;';
    div.innerHTML = DOMPurify.sanitize(marked.parse(data.content), SECURE_CONFIG);
    wrapper.appendChild(div);

  } else {
    // Fallback — error or unknown type
    wrapper.innerHTML = '<div style="color:var(--text-muted,#888);font-size:13px;padding:8px;border:1px dashed var(--border,#333);border-radius:4px;">Embed not found</div>';
  }
}

async function _resolveEmbeds(el) {
  const placeholders = el.querySelectorAll('.embed-wrapper[data-embed-id]');
  for (const wrapper of placeholders) {
    const embedId = wrapper.dataset.embedId;
    try {
      const resp = await fetch('/api/resolve/' + encodeURIComponent(embedId));
      if (resp.ok) {
        const data = await resp.json();
        _renderEmbed(wrapper, data);
      } else {
        wrapper.innerHTML = '<div style="color:var(--text-muted,#888);font-size:13px;">Embed not found: ' + DOMPurify.sanitize(embedId) + '</div>';
      }
    } catch (e) {
      wrapper.innerHTML = '<div style="color:var(--text-muted,#888);font-size:13px;">Failed to load embed</div>';
    }
  }
}

document.addEventListener('alpine:init', () => {
  Alpine.directive('markdown', (el, { expression }, { effect, evaluate }) => {
    effect(() => {
      let text = evaluate(expression) || '';
      // Rewrite graph:// image refs BEFORE DOMPurify (which strips unknown protocols)
      text = text.replace(/!\[([^\]]*)\]\(graph:\/\/([^)]+)\)/g, '![$1](/api/attachment/$2)');

      // Replace ![[id]] embeds with placeholder markers BEFORE markdown parsing
      // We use a unique HTML comment that survives marked.parse + DOMPurify
      const embedIds = [];
      text = text.replace(EMBED_RE, (match, id) => {
        embedIds.push(id);
        return `<p data-embed-placeholder="${DOMPurify.sanitize(id)}"></p>`;
      });

      const embedConfig = embedIds.length > 0 ? {
        ...SECURE_CONFIG,
        // Allow data-embed-placeholder through DOMPurify for embed placeholders
        ALLOWED_ATTR: [...SECURE_CONFIG.ALLOWED_ATTR, 'data-embed-placeholder'],
        ADD_ATTR: [...(SECURE_CONFIG.ADD_ATTR || []), 'data-embed-placeholder'],
      } : SECURE_CONFIG;
      const html = DOMPurify.sanitize(marked.parse(text), embedConfig);
      el.classList.add('markdown-body');
      el.innerHTML = html;

      // Replace placeholder <p> elements with actual embed wrappers
      el.querySelectorAll('p[data-embed-placeholder]').forEach(p => {
        const embedId = p.dataset.embedPlaceholder;
        if (embedId) {
          const wrapper = _createEmbedPlaceholder(embedId);
          p.parentNode.replaceChild(wrapper, p);
        }
      });

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

      // Async-resolve embeds
      if (embedIds.length > 0) {
        _resolveEmbeds(el);
      }
    });
  });
});
