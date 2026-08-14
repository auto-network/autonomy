# Experience Report Template

After completing your task, write `experience_report.md` in your workspace.
This captures operational feedback for future agents and the knowledge graph.

## Format

```markdown
# Experience Report: [bead-id]

## What Worked
- Tools, approaches, or patterns that were effective

## What Didn't Work
- Dead ends, failed approaches, unexpected obstacles

## Pitfalls
- Things future agents should watch out for
- Include enough context that someone unfamiliar can understand

## Functional Proof
- (Runtime-critical beads only.) The reference to the REAL run's evidence —
  a screenshot, log tail, or transcript of the change on the actual user path.
- Record it as a `functional-proof: <ref>` marker line, where `<ref>` is a
  graph attachment id, a `/workspace/output` path, or a run log. It belongs in
  the bead's close reason or a bead note (and, for dispatched beads, as a
  `functional_artifacts` entry in decision.json).
- The pipeline-driven way to produce one: write an executable
  `functional_check.sh` into your output dir (next to decision.json). Exit 0 =
  proven; stdout IS the transcript. The dispatcher runs it via `smoke.py`,
  tees stdout to `functional_check.log`, and the gate accepts that log.
- A green test suite is NOT functional proof for a runtime change — omit this
  section only when the bead is not runtime-critical.

## Tool Feedback
- CLI tools that were confusing, slow, or missing features
- Suggested improvements

## Discovered Work
- New tasks or issues found during execution
- Include suggested priority and labels
```

Keep it concise. Focus on what's non-obvious — future agents can read the code,
but they can't read your mind about why you chose approach A over approach B.
