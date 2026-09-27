---
name: operator-qa
description: Walk the operator through a set of open decisions one question at a time, record each answer in the note or bead that owns the decision, update the dependent beads, and then continue the unblocked work without further prompting. Use whenever work is waiting on decisions only the operator can make.
---

# Operator Q&A

Use this skill when work cannot continue until the operator makes one or more decisions. It covers preparing the questions, asking them, recording the answers, and returning to autonomous work afterwards.

The operator has stated the core requirement several times: ask one question at a time, in a few sentences, with the least background needed to answer it. The Mission Control writing rules state the same thing: an ask is one answerable question that names its options, states the consequence of each option, and contains no internal identifiers. Every sentence also follows the Mandatory communication and writing style guide (graph://7ba682b3-7e1).

## Before the first question

1. List every open decision, and name the note or bead that owns each one. That note or bead is where the answer is recorded.
2. Remove any decision the operator has already made. Search `graph attention --search "<topic>"` and read the owning note's comments. If you find an answer, record it and do not ask it again.
3. Verify every premise of every remaining decision by direct observation before you write the question. A premise is any statement of fact the question rests on: "no UI exists for X", "Y has only run in simulation", "Z is blocked on W". For each one, open the code, the database, the log or the running system and confirm it now. A bead description, a graph note, a worklist, or another session's report is a claim, not an observation; it is stale the moment the code it describes changes, so check it against the current tree (`git log -S` on the relevant file shows whether a control landed after the claim was written). If a premise turns out false, the decision may shrink or vanish; rewrite or drop it. Note beside each question which observation backs each premise, so you can answer "how do you know?" without a second look.
4. Order the remaining decisions so that a decision which changes the options of another decision comes first.
5. For each decision, write the options, the consequence of each option in one sentence, and your recommendation with its reason in one sentence. Consequences describe what the option does or rules out, never how long it takes.

## Asking one question

Send exactly one question per message, in this shape:

```
<Question in one sentence, ending with a question mark.>

A. <Option> — <its consequence, one sentence>.
B. <Option> — <its consequence, one sentence>.
C. Something else — say what.

I recommend <letter>, because <reason in one sentence>.
```

Rules for the question:
- Keep it under about eight lines.
- Give only the background the choice needs. If the operator needs more, they will ask.
- Do not give time or effort estimates, in the question, the options or the recommendation.
- Do not use bead IDs, section numbers, option codes from a design note, or any other label the operator would have to look up. The letters A, B and C are defined in the message itself.
- Do not bundle two decisions into one question, and do not list the remaining questions.
- Every fact in the question and its options is one you observed yourself in this session. Never carry an unverified claim from a bead, note or peer session into a question; a false premise costs the operator a correction round that a single grep would have saved.
- The operator may answer with a letter, with their own words, or with a correction to the question. Treat all three as answers. A correction that exposes a false premise is a defect in your preparation: fix the record that carried the false claim (close or amend the bead, comment on the note) before asking the corrected question.

## After each answer

1. If the answer is clear, record it immediately. Post a comment on the owning note or bead that states the decision and quotes the operator's words exactly.
2. In the same reply, confirm the recorded decision in one sentence, then ask the next question.
3. If the answer is unclear, ask one clarifying question about that decision only. Do not move on until it is clear.
4. If an answer changes the options of a later decision, rewrite that later question before asking it.
5. If the operator says to stop, stop, and report which decisions remain open.

## After the last answer

1. Write a new version of the owning note that states every answer in its body, integrating the answer comments, as the Note revision protocol requires (graph://843a8137-3c7).
2. Update every bead whose content depended on an answer: its body, its acceptance criteria, and any threshold or scope it states.
3. Close the bead that tracked the decisions, with a reason that names the note version recording them.
4. Tell the operator, in a few sentences, what was decided and which work starts now.
5. Start the unblocked work without asking for permission again. The decisions were the only thing blocking it, and the operator's answers authorize it.

## Recording in Mission Control

If the work belongs to a Mission Control mission, also record each decision there. Add a `question` item with `graph mission add`, then record the operator's answer with `graph mission answer`. The note or bead remains the primary record.
