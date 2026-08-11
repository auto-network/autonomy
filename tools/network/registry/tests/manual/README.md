# Manual browser harnesses

Self-contained pages for behaviour that only a real browser can answer, and
that headless Chromium alone cannot settle — chiefly WebKit/iOS.

Serve one over HTTPS and open it on the device. Each page reports PASS/FAIL
into its own DOM, so no devtools are needed; a screenshot is a complete result.

The quickest way to get one onto a phone from an agent container:

```bash
cp tools/network/registry/tests/manual/<page>.html /workspace/output/
# then, on the device, over the tailnet:
#   https://<host>.<tailnet>.ts.net:8080/api/session/<tmux-name>/output/<page>.html
```

## `srcdoc-base-fragment.html`

Answers: **does `<base href="about:srcdoc">` give native in-document fragment
navigation inside a sandboxed opaque-origin iframe?**

Two frames, `sandbox="allow-scripts"` with no `allow-same-origin` — the same
isolation the relay bootloader uses for an artifact. They differ by exactly one
line of HTML.

Result, 2026-08-11, Chromium 1xx headless **and** iOS Safari (real tap):

| case | `document.baseURI` | `#target` resolves to | outcome |
|---|---|---|---|
| A — no `<base>` | the parent page URL | parent URL`#target` | frame navigates away |
| B — with `<base>` | `about:srcdoc` | `about:srcdoc#target` | scrolls in place |

Under CSP `base-uri 'none'` the `<base>` is silently ignored and `baseURI` falls
back to the parent — which is why `_BOOTLOADER_CSP` needs `base-uri about:` for
the fix to take effect.

**Read the banner first.** The page asserts its own
`document.baseURI === location.href` and shows red if not. This matters: the
first version of this harness injected the case labels — which contain a literal
`<base …>` tag — with `innerHTML`, so the parser installed a real `<base>` in the
*parent* document. srcdoc frames inherit the parent's base, so the no-base
control passed and the whole run was meaningless. **A control that passes is a
broken test, not a working feature.** Any harness added here needs an equivalent
self-check.

Re-run this when bumping the minimum supported Safari/iOS.


## `webkit-compose-test.html` (+ `mc-bootstrap.js`)

Answers: **does a composed Mission document behave as an ordinary document
in a sandboxed opaque-origin frame, on this engine?**

Composition is doctype + `<base href="about:srcdoc">` + the platform bootstrap,
prepended to the coordinator's complete HTML and assigned as `srcdoc`. The page
drives the **real** 1.87 MB OSS Insights document, not a fixture — it has
`<html lang>`, global `body{}` CSS, two `DOMContentLoaded` registrations, a
`load` listener, 64 fragment anchors, and `scroll-behavior:smooth`.

Fetches `pillar-sample.html` from this directory. That fixture is committed and
pinned: pillar `cfd645a0-09e5-4094-9010-6c1f9e946dd2`, revision
`d81c4ef9-91c6-4d75-84fc-2da0f18fd4a2` (seq 2), 1 871 081 bytes, sha256
`5d666db3fbded94234cf30432aa06dae6e5d7275fa90c086b3e164e31c2dc76a`. Re-export
with `mission_control_db.get_current_pillar_site(<pillar_id>)["html"]` only if
the pin is updated with it — an unpinned fixture makes the recorded result
unreproducible.

Two phases, 14 checks each. Phase 2 replaces the document with
`document.open/write/close` and re-runs every check on the replacement, plus
confirms the new bootstrap established a fresh `MessagePort`.

Result 2026-08-11: **14/14 both phases, Chromium headless and iOS Safari.**
Standards mode, authored `<html lang>`, author CSS, the author's own script
(`window.OQ_DATA`, 36 entries), bootstrap-runs-first, parse ordering,
`DOMContentLoaded`, `load`, six controls mounted in closed shadow roots, native
fragment scrolling, and no leaked global.

Two harness traps worth knowing, both of which produced false failures first:

- **Measure where the target lands, not whether `scrollY` changed.** A fragment
  already at the top of the document cannot move the scroll position.
- **The author's CSS governs the landing.** `scroll-behavior:smooth` means a
  sample taken 400 ms after the click catches the animation mid-flight, and
  `scroll-margin-top:20px` means the target settles at 20, not 0. Both are the
  author's styling being honoured — evidence the design works, not against it.

## `anchor-mount-test.html`

Answers: **where does the platform mount a control at `data-mc-anchor` without
disturbing author CSS?**

Measured, against a baseline showing all five author rules matching first:
mounting as a next **sibling** breaks adjacent-sibling (`+`) rules; mounting
**inside** the anchored element broke none. Scope: selector matching only, not
layout — an extra child still affects flex/grid geometry, `:empty` and
`:only-child`.


## `write-fragment-min.html`

Answers: **does `#fragment` navigation still work in a document created by
`document.open/write/close`?** That is the mechanism for every screen change
after the first, so an author's in-page anchors depend on it.

Minimal by design — no MessageChannel, no polling, two lines of output. It was
written because the larger harness gave an intermittent answer and could not be
trusted.

Result 2026-08-11, Chromium headless and iOS Safari: **both generations
SCROLLED**, target 1214 → 0.

It also surfaced the behaviour the design now depends on: **`document.write`
inherits the previous document's scroll offset** (1214 px carried into the
replacement). The runtime must reset scroll when it writes a new screen.

Trap this harness exists to avoid: asserting that the target *moved*. With an
inherited scroll offset the target can already be at the destination, so a
working browser reports no movement. Assert where the target **lands**, not
that it travelled.
