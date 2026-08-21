# Dashboard plugin starter

This directory is a deliberately small, disabled plugin. Copy it when starting
a dashboard plugin; do not turn it into the application you are building.

## Backend contract

`plugin.yaml` declares Python entrypoints, but the loader does not invent API
paths or authorization policy. Keep a plugin's routes under
`/api/plugins/<plugin-id>/...` and guard every non-public handler explicitly.

The dashboard's API identity middleware runs around core and plugin routes. It
classifies the caller once and resolves one trusted organization scope:

- an organization session bearer is forced to the bearer's organization;
- an authenticated dashboard operator or local host session may select an
  organization with `X-Graph-Org`;
- a conflicting header cannot widen an organization session's scope;
- an organization name without a credential is not authentication.

Use `require_authenticated_api_caller(request)` at the top of the handler, then
read the resolved scope with `organization_scope_from_request(request)`. Reject
`None` when the endpoint requires an organization. Never re-parse an `org`
query parameter, JSON field, or header inside the handler.

Pass the resolved organization explicitly to Settings. Use an owning-database
read (`read_set_key(..., peers=[])` or `read_owned_set`) when the plugin must not
compose peer organizations' published rows. The example schema declares
`@home("organization")`; its raw-only publication band additionally prevents
those rows from ever entering federated read-through.

The example backend returns this bounded shape:

```json
{
  "organization": "example-org",
  "record": {
    "key": "current",
    "message": "Hello from Settings",
    "updated_at": "2026-08-21T00:00:00Z"
  }
}
```

`record` is `null` when the organization has not written one. Replace the
example set and response with the real application's contract.
