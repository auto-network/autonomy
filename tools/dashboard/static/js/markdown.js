// x-markdown Alpine custom directive
// Renders markdown with DOMPurify sanitization, highlight.js syntax highlighting,
// and secure link handling (internal SPA links via navigateTo, external in new tab).
//
// Usage: <div x-markdown="expression"></div>
// The expression should evaluate to a markdown string.

// DOMPurify deliberately strips unknown app schemes. Preserve exactly one
// user-gesture shape: an anchor that runs an installed Apple Shortcut. A
// tag-aware hook is important here — a broad URI-regex exception would also
// admit shortcuts: in passive attributes such as <img src>, allowing content
// insertion alone to attempt an app launch.
function _isAllowedExternalAppHref(href) {
  return /^shortcuts:\/\/run-shortcut(?:[/?#]|$)/i.test(href || '');
}

// Resolve artifact addresses against the viewer's session, never against the
// browser's filesystem or whichever session currently owns dictation.
function _artifactHref(href, session, baseUrl) {
  if (href.startsWith('#') && !baseUrl) return href;
  const graph = /^graph:\/\/([0-9a-f]{8}[-0-9a-f]*)([?#].*)?$/i.exec(href);
  if (graph) return '/graph/' + graph[1] + (graph[2] || '');
  if (href.startsWith('/workspace/output/')) {
    if (!session) return '';
    let path;
    try { path = decodeURIComponent(href.slice('/workspace/output/'.length)); }
    catch (_) { return ''; }
    path = path.replace(/:\d+(?::\d+)?$/, '');
    if (!path || path.split('/').some(p => p === '..' || p === '.') || /[\\\x00]/.test(path)) return '';
    return '/api/session/' + encodeURIComponent(session) + '/output/' + path.split('/').map(encodeURIComponent).join('/');
  }
  let url;
  try { url = new URL(href, baseUrl || window.location.href); }
  catch (_) { return href; }
  const artifactPath = /^\/(?:graph|source|design|present|mission|bead|session)\//.test(url.pathname)
    || /^\/api\/session\/[^/]+\/output\//.test(url.pathname);
  // Agents have historically printed their container's Tailnet name and
  // internal dashboard aliases. Only known artifact routes are rebased;
  // ordinary external web links retain their destination.
  const dashboardAlias = /^(?:localhost|127\.0\.0\.1|host\.docker\.internal|dashboard)$/.test(url.hostname)
    || /^[a-z0-9-]+\.tail[a-z0-9]+\.ts\.net$/i.test(url.hostname);
  if (url.origin === window.location.origin ||
      (artifactPath && dashboardAlias && /^https?:$/.test(url.protocol) && (!url.port || url.port === '8080'))) {
    return url.pathname + url.search + url.hash;
  }
  return href;
}

function _bindMarkdownLinks(el, scope, baseUrl) {
  scope = scope || {};
  el.querySelectorAll('a[href]').forEach(a => {
    const raw = a.getAttribute('href');
    let href = _artifactHref(raw, scope._tmuxSession, baseUrl);
    // Match the session header's existing Design Studio contribution action:
    // linked entry supplies the originating session and Studio already keeps
    // it across revisions and exposes its Back to session control.
    if (/^\/design\/[^/?#]+(?:[?#]|$)/.test(href) &&
        /^[A-Za-z0-9._-]{1,160}$/.test(scope._tmuxSession || '')) {
      const designUrl = new URL(href, window.location.origin);
      designUrl.searchParams.set('from_session', scope._tmuxSession);
      href = designUrl.pathname + designUrl.search + designUrl.hash;
    }
    if (!href) { a.removeAttribute('href'); return; }
    a.setAttribute('href', href);
    if (_isAllowedExternalAppHref(href)) {
      a.setAttribute('rel', 'noopener noreferrer');
      a.removeAttribute('target');
      a.setAttribute('data-external-app', 'shortcuts');
      if (!a.title) a.title = 'Open in Shortcuts';
      return;
    }
    if (/^[a-z][a-z0-9+.\-]*:/i.test(href) && !/^(?:https?|mailto|tel|callto|sms|cid|xmpp):/i.test(href)) {
      a.removeAttribute('href');
      return;
    }
    if (href.startsWith('/') && !href.startsWith('//')) {
      a.removeAttribute('target');
      a.addEventListener('click', e => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || a.hasAttribute('download')) return;
        if (/^\/api\/session\/[^/]+\/output\//.test(href) && typeof scope.openOutputLink === 'function') {
          e.preventDefault();
          scope.openOutputLink(href);
        } else if (!href.startsWith('/api/')) {
          e.preventDefault();
          if (typeof scope.closeLightbox === 'function') scope.closeLightbox();
          navigateTo(href);
        }
      });
    } else if (!href.startsWith('#')) {
      a.setAttribute('rel', 'noopener noreferrer');
      a.setAttribute('target', '_blank');
    }
  });
}
window.AutonomyMarkdownLinks = { resolve: _artifactHref, bind: _bindMarkdownLinks };

DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
  if (node.nodeName === 'A' && data.attrName === 'href' && /^graph:\/\//i.test(data.attrValue)) {
    data.attrValue = _artifactHref(data.attrValue);
  }
  if (node.nodeName === 'A' && data.attrName === 'href' &&
      _isAllowedExternalAppHref(data.attrValue)) {
    data.forceKeepAttr = true;
  }
});

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

    // View Source link (hidden on direct view — already on the source page)
    if (data.id && !data._directView) {
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
  Alpine.directive('markdown', (el, { expression, modifiers }, { effect, evaluate }) => {
    const hasAttachments = modifiers.includes('attachments');
    effect(() => {
      let text = evaluate(expression) || '';
      // Rewrite graph:// image refs BEFORE DOMPurify (which strips unknown protocols)
      // Only in .attachments mode — otherwise graph:// should render literally
      if (hasAttachments) {
        text = text.replace(/!\[([^\]]*)\]\(graph:\/\/([^)]+)\)/g, '![$1](/api/attachment/$2)');
      }

      // Replace ![[id]] embeds with placeholder markers BEFORE markdown parsing
      // We use a unique HTML comment that survives marked.parse + DOMPurify
      // Only in .attachments mode — otherwise ![[id]] should render literally
      const embedIds = [];
      if (hasAttachments) {
        text = text.replace(EMBED_RE, (match, id) => {
          embedIds.push(id);
          return `<p data-embed-placeholder="${DOMPurify.sanitize(id)}"></p>`;
        });
      }

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
      // Linkify references — bead IDs, graph:// URIs, source IDs
      // Each rule: regex source contributes one capture group; group index maps to href/text
      const LINK_RULES = [
        { group: 1, href: id => '/bead/' + id,   display: (full, id) => id },    // auto-xxxx
        { group: 2, href: id => '/graph/' + id,  display: (full, id) => full },  // graph://xxxxx
        { group: 3, href: id => '/graph/' + id,  display: (full, id) => id },    // 9e1a2361-405
      ];
      //                       group 1: bead                        group 2: graph:// URI               group 3: source ID
      const COMBINED_RE = /\b(auto-[a-z0-9]{2,8}(?:\.[0-9]+)*)(?!-)\b|graph:\/\/([0-9a-f]{8}[-0-9a-f]*)|\b([0-9a-f]{8}-[0-9a-f]{3})\b/g;
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
      const textNodes = [];
      while (walker.nextNode()) textNodes.push(walker.currentNode);
      for (const node of textNodes) {
        if (node.parentElement && node.parentElement.closest('a, pre')) continue;
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
          frag.appendChild(a);
          last = m.index + m[0].length;
        }
        if (last < node.textContent.length) frag.appendChild(document.createTextNode(node.textContent.slice(last)));
        node.parentNode.replaceChild(frag, node);
      }

      const scope = typeof Alpine.$data === 'function' ? Alpine.$data(el) : {};
      const baseUrl = el.classList.contains('sc-va-lightbox-md') && scope.lightboxSrc
        ? new URL(scope.lightboxSrc, window.location.href).href : undefined;
      _bindMarkdownLinks(el, scope, baseUrl);

      // Async-resolve embeds
      if (embedIds.length > 0) {
        _resolveEmbeds(el);
      }
    });
  });
});
