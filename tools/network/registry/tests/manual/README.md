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
