---
name: spec-splitter
description: Use this skill whenever the user asks to split, break up, decompose, or chunk a large spec (specification) into smaller ones. Also use it when the user says a spec is too big, needs to be broken into pieces, or wants to create multiple specs from one. This skill enforces strict what/why framing throughout the split — it prevents the model from sneaking implementation details into the resulting sub-specs.
---

# Spec Splitter

Your job is to take a large spec and produce several smaller ones, each describing a distinct, user-visible deliverable. The split should feel natural to a product person reading it — not a code refactor, not a task breakdown, but a decomposition of *what the user experiences* into coherent, shippable chunks.

## The cardinal rule

Every sentence you write must survive this test: **could you say this exact thing in a spec for a different tech stack?**

If not, cut it. Specifically, sub-specs must never contain:

- Framework or library names (Retrofit, Compose, Room, Hilt, etc.)
- File paths, package names, class names, method names
- Code blocks, type definitions, algorithms, data structures
- Test code or assertions about code behaviour
- Implementation mechanisms or architectural decisions

These are all "how." They belong in a design doc, not a spec. The spec describes *what the user sees and does*, and *why that matters*.

## What makes a good split

Look for natural seams in the parent spec — places where:

- A different type of user action begins (e.g. setup vs. daily use)
- A distinct user-visible feature can stand alone and be evaluated independently
- One chunk could ship and be useful even if another is delayed
- The acceptance criteria in one area don't depend on the acceptance criteria in another

Avoid splits that produce:
- A chunk that is only infrastructure and has no user-visible outcome ("set up the database layer")
- A chunk so thin it can't be meaningfully evaluated against real user behaviour
- Chunks that must ship together to be useful — if they're truly inseparable, keep them together

## Format for each sub-spec

Follow the same structure as the parent spec. Use this template:

```
# NNN — Title

**Purpose:** One sentence. What is this spec delivering? Frame it as a user capability, not a feature description.

**Why it matters:** One to three sentences. Why does this specific capability matter to the user? What breaks or stays bad without it?

## Behaviour

[Describe what the user sees, hears, and does. Use subsections if the behaviour has multiple modes or flows. Write as if narrating a user's experience — no technical vocabulary.]

## Acceptance criteria

[A list of observable outcomes. Each criterion is a thing you could verify by watching a real user interact with the product, without reading the code. Starts with a subject, then a verb — "Opening X shows...", "Tapping Y takes the user to..."]

## Edge cases

[Stated in product terms. "The user types a very long phrase" not "String.length > 512". Each case names a real-world situation and states what the user should experience.]

## Out of scope

[Optional. What related-sounding things this spec explicitly does not cover, so the implementer doesn't over-build.]
```

## Checklist before writing any sub-spec

Ask yourself:

1. Does the title describe a user-visible thing, or an engineering task?
2. Is "Purpose" framed as what the user gains, not what the system does?
3. Are all Behaviour subsections narrating user experience?
4. Can every acceptance criterion be verified by a human tester without code access?
5. Are edge cases described in terms of real user situations?

If any answer is no, rewrite before continuing.

## Numbering

If the parent spec has a number, assign sequential numbers to the sub-specs starting from the next available number in the spec directory. If the project has a naming scheme, follow it. Ask the user if unsure.

## Announce the decomposition plan first

Before writing any sub-spec, present your proposed split as a short list:

- Spec NNN — [title]: [one-sentence description of what the user gains]
- Spec NNN — [title]: [one-sentence description of what the user gains]

Ask the user to confirm this is the right decomposition. Only write the full specs after they've agreed on the structure. This saves everyone time if the split is wrong.
