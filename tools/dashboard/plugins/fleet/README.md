# Fleet Machines plugin

The Fleet page renders one projection over existing runtime truth. It is
launched from the Dashboard's generic Identity-menu plugin slot and hosts no
approval decision surface: a pending admission row only opens the central
approval overlay. The page's own writes are root-signed ceremonies (machine
removal via the fleet-kick tombstone, connector unlock, invitation signing)
plus one plain Settings write (machine rename).

Status rendering is driven by ONE classifier (`machineState` in `page.js`):
every machine resolves to a single state id whose display word and note come
from a strings table. States without an honest server-side probe do not exist
in the classifier. The local machine's operational facts ride in the
projection's `localMachine` block (connector armed, code staleness, serving
certificate), each null when its probe is unavailable — null renders nothing,
never a guess.

| Visible value | Projection field | Authoritative source |
|---|---|---|
| Authorized | `summary.authorizedMachines` | Count of the personal-root-verified current roster |
| Connected | `summary.connectedMachines` | `null` until authenticated-channel presence exists |
| Last sync | `summary.lastSuccessfulSyncAt` | Maximum current-roster-epoch `fleet_sync_peer_state.last_success_ns` observed by this Dashboard |
| Requests | `summary.joinRequests` | Admission candidate rows still visible in Fleet |
| Machine identity and assignment | `machines[].machineId`, `assignment` | Effective root-signed `RosterEntry` |
| This machine | `machines[].isLocalMachine` | Roster machine ID compared with machine-local identity |
| Serves auto.network | `machines[].isTunnelServer` | Temporary singular tunnel-server selection |
| Sync counters and errors | `machines[]` observation fields | Machine-local `autonomy.machine.fleet-sync-telemetry` Settings; never synchronized |
| Pending / adding / failed | `machines[].standing` | Enrollment transport joined to the current generic approval by `sourceApprovalId` |
| Invitation | `invitation` | Newest usable registered signed Fleet invitation; copied as the real `AUTONOMY_FLEET_INVITE` bootstrap value |
| Observed activity | `activity` | Sum of current-roster peer observations on this Dashboard |

Steady-state Fleet pulls run at most once every 10 seconds. Each receiver keeps
the last fully verified source-journal transaction position in machine-local
telemetry, so an idle poll transfers only its terminal summary. The position is
advanced only after the stream count and digest verify; an interrupted stream
is safely replayed from the prior completed position.

An approval result never creates a machine. After the idempotent executor
commits signed roster evidence, the admission candidate disappears and the
verified roster entry becomes the real row. Declined requests disappear.
Multiple requests stay separate candidates; there is no Fleet-local approval
queue.

The plugin does not render a comparison PIN, notification, approval card,
Grant/Decline, execution control, retry, pagination, or approval history. It
also does not expose remove, publish, or cancel controls until their
browser-root command ceremonies exist. Unsupported presence is shown as “Not
reported,” never inferred from the scheduler's transient pull flag.

Design Studio source: stable design
`9f50de46-60f7-410c-8223-c11fce050951`, accepted revision
`25850afa-619f-41d0-b339-f822fed59fb8`.
