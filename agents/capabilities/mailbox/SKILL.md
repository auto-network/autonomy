---
name: mailbox
description: The organization's mailbox (broker-backed, no credentials in the workspace). List and read mail read-only, wait for a sign-in code or verification link, and send email only after the operator approves each message.
---

# Mailbox capability: agent skill

Implementation: `autonomy/mailbox` · Contract: `mailbox@1` · Delivery:
`mounted_tools` (the `mail-*` commands below are on PATH).

## Security model

These tools hold **no mailbox password**. The dashboard reads the mailbox
host-side, with the folder opened **read-only**: listing or reading never marks
a message read, moves it or deletes it. Sending is staged as an operator
approval: the exact message opens on the operator's dashboard, `mail-send`
**blocks** until they decide, and the email is sent host-side only after an
approval. A decline exits non-zero; confirm intent with the user before
retrying, and never loop on a declined send.

## Commands

```bash
mail-list                                   # newest 20 messages
mail-list --to agent+claude@auto.network --newer-than 30m
mail-read 42                                # one message: text, links, likely codes
mail-wait --to agent+codex@auto.network --timeout 5m
                                            # block until a matching message arrives,
                                            # then print it (codes and links first)
mail-send --to someone@example.com --subject "Hello" -f body.txt
                                            # waits for operator approval
mail-list --probe                           # check the broker and mailbox
```

Filters (`--to`, `--from`, `--subject`, `--text`) match substrings, as IMAP
SEARCH does. `mail-wait` only accepts messages received in the last 15 minutes
unless you pass `--newer-than`, so a stale code is never returned.

## Sign-in codes and verification links

1. Note the time, then trigger the email (for example, a sign-in page's
   "email me a code").
2. `mail-wait --to <the address you entered> --timeout 5m`.
3. Use the code or link it prints. `Codes` lists every 4 to 8 digit number in
   the subject and body: pick the one the message calls the code.

Plus-addresses deliver to the same mailbox, so use a distinct one per service
(`agent+claude@...`, `agent+codex@...`) and filter on it.
