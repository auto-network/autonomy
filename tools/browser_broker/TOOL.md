# Browser broker

Lends an agent session a real, visible web browser: one container per lease,
driven through structured commands, with sign-in performed on the caller's
behalf so the caller never sees a credential. Design of record:
graph://c330323d-986; epic `auto-8q7oe`.

## Modules

- `profiles.py` — persistent browser profiles (`auto-0skxh`). A profile's path
  is `<browser_profiles store>/<org>/<workspace>/<name>`, built from the
  caller's authenticated organization and workspace plus a short validated
  name; the caller never supplies a path. Also the profile's container name
  (`brw-p-<16 hex>`, which gives a profile a single owner) and its `/profile`
  mount.

## Storage

`browser_profiles` (`BROWSER_PROFILES_DIR`, default `data/browser-profiles/`)
is declared in `tools/data_paths.py::STORE_MANIFEST`. Its backup action is
`exclude`: profiles hold site sign-in cookies, and a copy of a running
profile would be torn. A lost profile costs one fresh sign-in.
