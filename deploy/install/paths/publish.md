# Path: publish and share

For the user who wants their work visible beyond their machine — without
giving anyone an account on it.

1. Anything in the graph (a note, a design, an artifact) can be published
   as a share link: an opaque URL whose token is the capability. Viewers
   need no Autonomy and no account.
2. Publishing is consent-gated per action and revocable per link; a
   revoked-then-republished link is a new confidentiality domain.
3. Demo it end-to-end: publish the "how I set this up" note, open the
   link in a private browser window, then show the revoke.
4. The relay that serves links never holds content keys — what it can't
   read, it can't leak. (Receipt-minded users: the E2E channel design is
   in the repo; offer the read.)

Done when: one link published, opened from "outside," and revoked, with
the user driving the mint and revoke.
