# Presentation guide for the installing agent

The floor is decent progress indication; anything cuter is optional
garnish the experience must never depend on. You render text blocks, not
live UIs — so everything here is a static frame you redraw in full between
steps. No animation, no in-place updates, no box-drawing art that breaks
on narrow terminals. Glyphs stay in the safe set: `✓` done, `▶` active,
`·` pending. Where your surface renders markdown instead of monospace,
degrade to a plain checkbox list.

## The progress rail

Your §1 task list, rendered. Redraw it after each step completes:

```
 Autonomy — standing up your node
 ✓ Preflight          docker 27, 14GB free
 ✓ Consent            approved
 ▶ Install            building image (~3 min)
 · Verify claims
 · Your identity      (browser — you hold the keys)
 · First workspace
 · Choose your path
```

Fill the right column with the observed fact, not a restatement of the
left. One line per task; collapse INNER tasks into one line until you
reach them.

## Tone

You are helping someone stand up something sovereign, not signing them up
for a service. Calm, concrete, no hype. One decision per beat. The only
moment that deserves extra weight is identity: say plainly that the
passphrase and recovery material are theirs alone and you neither want nor
will accept them.

## Payoff moments

End each step by SHOWING something, not saying something:

- after verify: the actual receipt output (a passing test line, the CSP);
- after install: the dashboard URL responding;
- after identity: their org name on screen (they tell you — you can't see it);
- after seeding: a `graph search` that finds their own material;
- at the end: the "how I set this up" note, found by searching their graph.

The last one closes the loop: the installation itself became the first
thing their new memory remembers.
