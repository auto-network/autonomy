/* Shared approval presentation. Authority and execution remain with the caller.
 * Layout follows Design Studio bc4d034a-c4b1-43dd-b48e-f2b1fc65ff2c.
 * No secrets, policy evaluator, or signing implementation lives in this shell.
 */
let activeDialog;
let sequence = 0;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function icon(kind) {
  const paths = {
    link: ['M14 5h5v5M19 5l-9 9', 'M19 13v6H5V5h6'],
    working: ['M20 12a8 8 0 1 1-2.34-5.66', 'M20 4v5h-5'],
    success: ['M5 12.5 10 17.5 19 7'],
    failed: ['M6 6l12 12M18 6 6 18'],
    session: ['M8 21h8m-4-4v4m-6-10 3 3-3 3m6 0h5', 'M5 4h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2'],
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
  for (const d of paths[kind] || paths.failed) {
    const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path);
  }
  return svg;
}

export function localHref(value) {
  return typeof value === 'string' && /^\/(?!\/)/.test(value)
    && !/[\\\x00-\x20]/.test(value) ? value : null;
}

// Resolve the actual viewer route; organization names are not workspace IDs.
export async function requestingSession(session, label) {
  const result = { name: label || session || 'Requesting session' };
  if (!session) return result;
  try {
    const response = await fetch('/api/session/' + encodeURIComponent(session));
    const info = await response.json();
    if (response.ok && info.project) {
      result.href = '/session/' + encodeURIComponent(info.project) + '/' + encodeURIComponent(session);
    }
  } catch (_) { /* A missing viewer route must not become an invented link. */ }
  return result;
}

/**
 * review: user-facing title, intro, organization, target, requester, facts and
 * optional controls DOM. These describe the request, never its authority.
 * authorize receives openRoot's mount/signal plus onAuthenticated; it returns
 * the existing caller's decision. execute/decline retain their existing APIs.
 * result names the working/success states and the one fact to retain.
 */
export function openApprovalDialog({ review, authorize, execute, decline,
  result, retained = false, onClose = () => {} }) {
  if (activeDialog && !activeDialog.close()) throw new Error('Another approval is still finishing. Open this request when it completes.');
  if (!document.getElementById('approval-dialog-style')) {
    const link = element('link'); link.id = 'approval-dialog-style'; link.rel = 'stylesheet';
    link.href = '/static/css/approval-dialog.css'; document.head.append(link);
  }
  const previousFocus = document.activeElement;
  const host = element('div', 'approval-dialog');
  const sheet = element('div', 'approval-sheet');
  sheet.setAttribute('role', 'dialog'); sheet.setAttribute('aria-modal', 'true');
  sheet.tabIndex = -1;
  const headingId = 'approval-title-' + ++sequence;
  sheet.setAttribute('aria-labelledby', headingId);
  host.append(sheet); document.body.append(host);
  let phase = 'review', timer, controller, layer, disposed = false;
  const brand = element('div', 'approval-brand');
  const organizationImage = review.organization?.image;
  // Org profiles now carry their normalized, portable icon as a data URI.
  const portableIcon = typeof organizationImage === 'string' && organizationImage.length <= 24000
    && /^data:image\/(?:webp|png|jpeg);base64,[A-Za-z0-9+/]+=*$/.test(organizationImage);
  if (localHref(organizationImage) || portableIcon) {
    const image = element('img'); image.src = review.organization.image; image.alt = '';
    image.onerror = () => image.remove(); brand.append(image);
  }
  brand.append(element('span', '', review.organization?.name || 'Personal approval'));
  const closeButton = element('button', 'approval-close', '×');
  closeButton.type = 'button'; closeButton.setAttribute('aria-label', 'Close approval');
  brand.append(closeButton); sheet.append(brand);
  const content = element('div', 'approval-review');
  const heading = element('h1', '', review.title); heading.id = headingId;
  content.append(heading, element('p', 'approval-intro', review.intro));
  function appendLink(parent, fact, label) {
    const href = localHref(fact?.href);
    if (!href) return;
    const link = element('a', 'approval-link', label); link.href = href;
    link.addEventListener('click', event => { if (!close()) event.preventDefault(); });
    parent.append(link);
  }
  if (review.target) {
    const target = element('div', 'approval-target');
    target.append(element('p', 'approval-eyebrow', review.target.type),
      element('p', 'approval-target-name', review.target.name),
      element('p', 'approval-byline', review.target.byline));
    appendLink(target, review.target, '↗');
    target.querySelector('a')?.replaceChildren(icon('link'));
    target.querySelector('a')?.setAttribute('aria-label', 'Open ' + review.target.type);
    content.append(target);
  }
  if (review.facts?.length) {
    const facts = element('div', 'approval-facts');
    for (const [name, value] of review.facts) {
      const row = element('div', 'approval-fact');
      row.append(element('span', '', name), element('span', '', value)); facts.append(row);
    }
    content.append(facts);
  }
  if (review.requester) {
    const href = localHref(review.requester.href);
    const requester = element(href ? 'a' : 'div', 'approval-requester');
    if (href) { requester.href = href; requester.onclick = event => { if (!close()) event.preventDefault(); }; }
    const text = element('div');
    text.append(element('p', 'approval-eyebrow', 'Requesting session'),
      element('p', 'approval-name', review.requester.name));
    const avatar = element('span', 'approval-avatar'); avatar.append(icon('session'));
    requester.append(avatar, text);
    content.append(requester);
  }
  if (review.controls) content.append(review.controls);
  sheet.append(content);
  const auth = element('div', 'approval-auth'); auth.hidden = true;
  auth.setAttribute('role', 'group'); auth.setAttribute('aria-label', 'Authorization');
  const authContent = element('div', 'approval-auth-content'); auth.append(authContent);
  sheet.append(auth);
  const error = element('p', 'approval-error'); error.setAttribute('role', 'alert'); error.hidden = true;
  sheet.append(error);
  const outcome = element('div', 'approval-result'); outcome.hidden = true;
  outcome.setAttribute('aria-live', 'polite');
  const mark = element('div', 'approval-result-mark'); mark.setAttribute('aria-hidden', 'true');
  const outcomeTitle = element('h2'); const copy = element('p', 'approval-intro');
  const receipt = element('div', 'approval-receipt');
  receipt.append(element('p', 'approval-target-name', result.fact.name),
    element('p', 'approval-byline', result.fact.byline));
  appendLink(receipt, result.fact, result.fact.linkLabel || 'Open');
  outcome.append(mark, outcomeTitle, copy, receipt); sheet.append(outcome);
  const buttons = element('div', 'approval-buttons');
  const secondary = element('button', 'approval-secondary', decline ? 'Decline' : 'Close'); secondary.type = 'button';
  const primary = element('button', 'approval-primary', 'Authorize'); primary.type = 'button';
  if (retained) primary.classList.add('cached');
  buttons.append(secondary, primary); sheet.append(buttons);
  if (review.unavailable) { error.hidden = false; error.textContent = review.unavailable; primary.disabled = true; }

  function restoreLayer() {
    if (layer) { sheet.insertBefore(auth, error); layer.remove(); layer = null; content.inert = false; }
    auth.hidden = true; authContent.replaceChildren(); sheet.classList.remove('auth-open');
  }
  function back() {
    if (!['auth', 'preparing', 'grace'].includes(phase)) return;
    controller?.abort(); clearTimeout(timer); restoreLayer(); phase = 'review';
    primary.classList.remove('pending'); primary.hidden = false; primary.disabled = !!review.unavailable;
    primary.textContent = 'Authorize'; secondary.textContent = decline ? 'Decline' : 'Close';
    primary.focus({ preventScroll: true });
  }
  function close(force = false) {
    if (phase === 'working' && !force) return false;
    if (disposed) return true;
    disposed = true; clearTimeout(timer); controller?.abort(); layer?.remove(); host.remove();
    document.removeEventListener('keydown', onKey, true);
    if (activeDialog === api) activeDialog = null;
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    onClose(); return true;
  }
  function mount() {
    if (disposed || controller.signal.aborted) throw new DOMException('Cancelled', 'AbortError');
    const top = sheet.getBoundingClientRect().top;
    phase = 'auth'; primary.hidden = true; secondary.textContent = 'Back';
    auth.hidden = false;
    if (window.matchMedia('(max-width: 767px)').matches) {
      layer = element('div', 'approval-auth-overlay');
      const panel = element('div', 'approval-auth-panel'); panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true'); panel.setAttribute('aria-label', 'Authorize');
      const header = element('div', 'approval-auth-header', 'Authorize');
      const dismiss = element('button', 'approval-close', '×'); dismiss.type = 'button';
      dismiss.setAttribute('aria-label', 'Back to request'); dismiss.onclick = back;
      header.append(dismiss); panel.append(header, auth); layer.append(panel); host.append(layer);
      content.inert = true; layer.onclick = event => { if (event.target === layer) back(); };
      dismiss.focus({ preventScroll: true });
      if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) panel.animate?.(
        [{ transform: 'translateY(24px)', opacity: 0 }, { transform: 'translateY(0)', opacity: 1 }], { duration: 180, easing: 'ease-out' });
    } else {
      host.style.alignItems = 'flex-start'; host.style.paddingTop = top + 'px';
      sheet.style.maxHeight = `calc(100dvh - ${top + 24}px)`;
      sheet.classList.add('auth-open');
      window.requestAnimationFrame?.(() => {
        if (phase !== 'auth' || disposed || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        auth.animate?.([{ height: '0px', opacity: 0 }, { height: auth.offsetHeight + 'px', opacity: 1 }],
          { duration: 180, easing: 'ease-out' });
      });
    }
    return authContent;
  }
  function showResult(state, title, description) {
    restoreLayer(); content.hidden = true; outcome.hidden = false; error.hidden = true;
    primary.hidden = true; secondary.hidden = state === 'working'; secondary.textContent = 'Done';
    closeButton.disabled = state === 'working';
    mark.className = 'approval-result-mark ' + state;
    mark.replaceChildren(icon(state));
    outcomeTitle.textContent = title; copy.textContent = description || '';
    sheet.setAttribute('aria-label', title); sheet.removeAttribute('aria-labelledby');
  }
  async function run() {
    phase = 'preparing'; primary.disabled = true; secondary.textContent = 'Back';
    primary.classList.remove('pending'); error.hidden = true;
    const attempt = controller = new AbortController();
    try {
      const decision = await authorize({ mount, signal: attempt.signal,
        onAuthenticated() {
          if (attempt.signal.aborted || disposed) return;
          phase = 'working'; showResult('working', result.working, result.copy);
        } });
      if (attempt.signal.aborted || disposed) return;
      phase = 'working'; showResult('working', result.working, result.copy);
      await execute(decision);
      if (disposed) return;
      phase = 'complete'; showResult('success', result.success, result.copy);
    } catch (failure) {
      if (attempt.signal.aborted || disposed) return;
      if (phase === 'working') {
        phase = 'complete'; showResult('failed', 'Could not complete the request', failure.message);
      } else {
        back();
        if (failure.message !== 'Approval cancelled.') { error.textContent = failure.message || 'Could not authorize.'; error.hidden = false; }
      }
    }
  }
  primary.onclick = () => {
    if (phase === 'grace') { back(); return; }
    if (phase !== 'review') return;
    if (retained) {
      phase = 'grace'; primary.classList.add('pending'); primary.textContent = 'Authorizing…';
      secondary.textContent = 'Cancel'; timer = setTimeout(run, 1500);
    } else run();
  };
  secondary.onclick = async () => {
    if (['auth', 'preparing', 'grace'].includes(phase)) { back(); return; }
    if (phase === 'complete' || !decline) { close(); return; }
    if (phase === 'review') { phase = 'confirm-decline'; primary.hidden = true; secondary.textContent = 'Decline request'; error.textContent = 'Decline this request? Close to leave it pending.'; error.hidden = false; return; }
    if (phase !== 'confirm-decline') return;
    phase = 'working'; showResult('working', 'Declining request', '');
    try { await decline(); phase = 'complete'; showResult('declined', 'Request declined', ''); }
    catch (failure) { phase = 'complete'; showResult('failed', 'Could not decline request', failure.message); }
  };
  closeButton.onclick = () => ['auth', 'preparing', 'grace'].includes(phase) ? back() : close();
  host.onclick = event => { if (event.target === host) closeButton.onclick(); };
  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeButton.onclick(); }
    if (event.key !== 'Tab') return;
    const root = layer || sheet;
    const available = [...root.querySelectorAll('button,a[href],input,select,[tabindex="0"]')]
      .filter(node => !node.disabled && !node.closest('[hidden]') && !node.closest('[inert]'));
    const first = available[0], last = available.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  }
  document.addEventListener('keydown', onKey, true);
  const api = { close: () => close(), dispose: () => close(true) }; activeDialog = api; sheet.focus({ preventScroll: true }); return api;
}
