/* Operation framing over a relaykit SecureChannel — PROVISIONAL SHIM.
 *
 * This is a join-local stand-in for the shared `sendOp` that relay is
 * extracting into the origin-neutral relaykit/core module (surface frozen at
 * graph://0856b482-cd3). It is pinned to that exact wire shape so the day
 * relaykit/core is served own-origin from the dashboard, THIS FILE IS DELETED
 * and its one importer re-points at core's `sendOp` with no other change.
 *
 * Like core's `sendOp`, it carries NO claim/persona/ceremony semantics: it
 * canonicalises a {v:1, op, ...} request, sends exactly one SecureChannel
 * message, reads exactly one reply, and validates only the envelope
 * (JSON / object / version). What the operation fields MEAN is the caller's.
 */
import { canonicalJson } from '../ceremony/primitives.js';

const te = new TextEncoder();
const td = new TextDecoder();

/* sendOp(channel, request) -> Promise<object>
 *
 * request : a {v:1, op:string, ...} object. Serialised as canonical JSON
 *           bytes terminated by '\n' (relaykit/core wire contract).
 * reply   : the org's `canonical_json({v:1, ...result}) + '\n'`, parsed and
 *           returned verbatim. `result.status` (the LEDGER truth) is the
 *           caller's to read; a thrown error here is LINK truth (the channel
 *           itself failed — offline, revoked, or malformed), and callers must
 *           keep the two apart.
 */
export async function sendOp(channel, request) {
  if (
    !channel
    || typeof channel.sendMessage !== 'function'
    || typeof channel.recvMessage !== 'function'
  ) {
    throw new Error('sendOp requires a SecureChannel');
  }
  if (
    !request
    || typeof request !== 'object'
    || request.v !== 1
    || typeof request.op !== 'string'
    || !request.op
  ) {
    throw new Error('sendOp request must be a {v:1, op:string, ...} object');
  }
  await channel.sendMessage(te.encode(`${canonicalJson(request)}\n`));
  const raw = await channel.recvMessage();
  if (!(raw instanceof Uint8Array)) {
    throw new Error('channel reply must be bytes');
  }
  let reply;
  try {
    reply = JSON.parse(td.decode(raw));
  } catch (_) {
    throw new Error('channel reply is not valid JSON');
  }
  if (
    !reply
    || typeof reply !== 'object'
    || Array.isArray(reply)
    || reply.v !== 1
  ) {
    throw new Error('channel reply must be a {v:1, ...} object');
  }
  return reply;
}
