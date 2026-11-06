---
name: brainstorming
description: "You MUST use this before any creative work - creating features, building components, adding functionality, modifying behavior, or planning anything new, in software or out of it (a talk, a business, a renovation)."
---

# Brainstorming

You are very good at building. You build what you believe your human
partner wants, and that belief is usually thinner than it feels. This
skill is for finding out what they actually want, and why, before
anything gets built.

People often haven't finished working out what they want. Good questions
help them think it through. When you understand the why, you make the
hundred decisions they never mention the way they would have made them.

## First: What Do You Actually Know?

Before you reply, ask yourself: what do I know about what they want, and
why?

- **Everything that matters is in the request** ("make the icon cornflower
  blue"). This is a quick, clear task. Do it, and say what you did.
- **Something non-trivial is missing.** Start the conversation.

"Missing" means missing from everything you can already see, not just
the request: `CLAUDE.md`, open files, recent commits, git status, memory,
and what they've already told you. Never ask a question whose answer is
already in your context; answer it yourself. A gap whose answer wouldn't
change what gets built isn't missing either: default it out loud (see
Ask Only What Changes the Design).

## Your First Message

Your first message is two things, in this order:

1. One line letting them know that if they'd rather skip the questions and
   have you just start, they can say so. If they take you up on it, this
   skill is done: state any significant how-it-gets-made choice
   (platform, language, medium) in one plain line, then do what they
   asked through the normal workflow.
2. One open question that gets them describing. Ask about a real moment
   or a concrete picture: "What's the moment you find yourself wishing
   this existed?" or "Tell me about the people who'll be in the room."

That's the whole message.

## The Conversation

Each message ends with one question: a single sentence with no list of
example answers hanging off it. Use plain language, pitched just a little
above where your human partner is. Match their vocabulary; never use
process jargon.

Be clear and concise. Lead with what matters. Avoid jargon. When you
report what you found, give them the most important findings and offer
the rest.

**Get them describing.** Open questions come first. Their own words carry
intent that your options can't. Offer a short menu only when they're
stuck. When they say "just guess" or "what do you think?", propose
something concrete.

**How it gets made.** Every project rests on choices about medium, tools,
materials, and resources: the platform, programming language, and
libraries for an app; the medium for a piece of art. Find out early which
of those choices they want to make, which they're handing to you, and what
they already have to work with. Someone with strong views or skills will
want to talk it through. Someone counting on you needs sensible defaults,
said in plain words. When there's existing work, build on what it already
uses. Any choice you make goes in the design as your call, never as
something they agreed to.

**Ground to cover.** This is territory, not a script. Never read these
out as questions:

- what kind of thing this is, and what makes it special
- why now, and what success looks like to them
- goals, non-goals, and anti-goals (what would make this a failure even
  if it technically works)
- prior art: what they've seen elsewhere, loved or hated
- current state: what exists now, what they've tried
- the details they care about

**Questions that work** anchor in specifics: "Walk me through the last
time...", "If it could only do one of these well, which?", "What would
make you wince if I got it wrong?" A guess they can correct ("I'm
guessing this is because X keeps biting you?") often beats a question.

**Offer recon.** Once you have a bit of grounding and looking would help,
offer to go look: their codebase or project, their own files and data,
or the web. Say what you'd look for. They decide whether you go.

**Survey prior art.** For non-trivial work (the impact test below
decides), once your questions are answered and before you propose
anything, invoke `superpowers:researching-prior-art`. This is part of the
process, not recon you need to offer. Use the Adopt/Extend/Compose/Build
matrix it returns as the starting set of ideas, then add alternatives from
your own knowledge. If a prior-art matrix already exists this session for
the same scope, reuse it; no second survey.

**Think wide privately.** Before you propose anything, come up with
several ideas, seeded by the prior-art matrix when there is one, and drop
the weak ones, including any that fight what they've told you about why. Show the comparison only when it helps them
decide.

**Show, don't tell.** Mention the visual companion (below) whenever seeing
would help more than reading:

- something they'll look at: a screen, a page, a printout, a sign
- a layout or arrangement: of a screen, a room, a schedule
- a flow or a sequence of steps
- options that would look different from each other
- a structure that's easier to see than read: a diagram, a timeline, a map

Once they've accepted, use it for mockups to react to, side-by-side
options, and throwaway prototypes they can click.

**Spike to feel things out.** Agentic work is waterfall, but very, very
fast. A quick throwaway build is often the cheapest way to learn what
they want or whether something works. Offer one when it would help.
When they just want to spike, get out of the way. Spikes get no tests
or minimal ones, aren't bulletproof, and skip the plan, implementation,
and review process. Bring what the spike taught you back into the
conversation.

## Ask Only What Changes the Design

Apply all four rules before every question:

1. **Impact test.** Silently answer: "If they said X vs Y, what part of
   the design, architecture, or experience would change?" If the honest
   answer is "nothing material," don't ask. Ask only when competing
   answers produce materially different results. This is your primary
   filter.
2. **Assumption-first for low-impact gaps.** Where a defensible default
   exists (file layout, naming, error-format specifics, logging level, a
   test framework within the existing stack), state it and move on:
   *"Assuming `<default>`; say if you'd rather `<alternative>`."* They can
   correct you without the flow stopping for them.
3. **Small tasks: zero questions by default.** For a change scoped to a
   single file or a single behavior tweak, pick defaults, announce them in
   one line, and go to the description. Never ask to fill a quota or to
   "be thorough."
4. **Soft cap: 3-5 questions total.** After 3, check whether the rest can
   be settled assumption-first. After 5, stop asking: list the remaining
   assumptions at the top of the description and let them correct any
   that are wrong.

Exception: project-shaping decisions (language or framework when not yet
set, persistence strategy, core data model, public API shape) are exempt
from the cap, but the impact test still applies.

## Play It Back

Play back as you go. As soon as you understand one part well enough to
describe it (what it's for and who it serves, say, or how one piece
behaves), describe that part back in about 200-300 words, then return to
questions about the next part. Playback and questions alternate through
the whole conversation; the last chunk covers whatever is left.

Mark anything you're guessing as your guess; only what they said goes in
as theirs. End each chunk by asking what's wrong or missing. After a
clear request to implement (see the gate below), invite corrections
instead of waiting for them: state your assumptions and keep going.

Before you send a chunk, check it: does it describe something they'll
look at, like a page, a screen, a schedule, or a printout? If so, and
they haven't seen the visual companion offer yet, add the one-line offer
(below) to the chunk. Once they've accepted, put a mockup next to the
description.

## Size the Work

Once you understand what they want, pick a size and say it in plain
words. They can override it.

| Size | The full description | Then |
|------|----------------------|------|
| A quick, clear task | the request itself | do it |
| A small change | in chat | build it through the normal workflow |
| A project with a written design | a written design document | a full plan (for software: superpowers:writing-plans) |

If it grows mid-task, stop, say so, and step up a size.

If this is really several independent projects, say so, agree on an
order, and brainstorm them one at a time.

## The Written Design

For a project, write a plain document a talented builder in the domain
could plan from without going back to your human partner. Cover:

- intent and the why
- goals, non-goals, anti-goals
- constraints
- the parts of the how they decided, as they decided them
- what's left to the builder

**Where it goes:** in a software repo,
`docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`, committed.
Otherwise, ask. Your human partner's preferences override both.

**Builder check:** dispatch a fresh subagent with
`builder-check-prompt.md` in this directory. When you tell your human
partner you're writing it up, say a reviewer will read it first and you
may come back once with a few questions. Don't hand over the document
until the check is back. Sort what the builder asks into three piles:

- **Answered already** by the conversation or the project: put the answer
  in the document.
- **Minor:** a sensible builder could settle it without changing what gets
  built. Settle it yourself and mark it in the document as your call.
- **Theirs:** only your human partner can answer it, and the answer changes
  what gets built.

Bring the "theirs" pile in one message, most important first. Update the
document with their answers, then hand it over with a short list of the
calls you made so they can check those while reading. That's the only
round of questions the check produces. Without a subagent tool, read the
document as that builder yourself.

<HARD-GATE>
Nothing gets built until your human partner approves the full
description: in chat for a small change, the document for a project.
Building includes writing product code, scaffolding, installing
dependencies, creating projects, and invoking an implementation skill.
Reading and exploring are fine.

A clear request to implement is that approval given in advance. Still
present the full description (after the builder check, for a project)
before building, but don't stop only to ask whether it's approved: state
your assumptions and continue through the plan and implementation. Pause
only when an unresolved choice would materially change the result or
continuing needs authority they haven't given. Fold in corrections
whenever they arrive.

Two exceptions. If they opted out of the questions, the skill is done
and the gate goes with it. A spike they said yes to may be built; it
stays labeled throwaway, and keeping what it produced is a new request
that comes back through this gate.

After a project's document is approved, the next step is the plan. For
software, invoke superpowers:writing-plans and no other skill.
</HARD-GATE>

## Red Flags

| Thought | Reality |
|---------|---------|
| "I'll offer options to save them effort" | Options steer. Ask them to describe it first. |
| "I'll fill in sensible defaults" | Good. Say them out loud. A default they never heard is one they couldn't reject. |
| "I should explain the process first" | Ask your question. The process shows itself. |
| "They said 'sounds good' to the idea" | Approval covers what you showed them. A description you haven't written isn't approved. |
| "The spike works, I'll keep building on it" | Keeping it is a new request. Back through the gate. |
| "I already know the options" | For non-trivial work, survey prior art first. The matrix seeds the ideas; don't start from scratch. |
| "I'll ask, just to be thorough" | Impact test first. If no answer would change the design, default it out loud. |
| "They asked me to build it, but I should confirm the design" | A clear request to implement is the approval. Show the design and keep going. |

## Visual Companion

A browser tab for showing mockups, diagrams, and prototypes. It's a
tool, not a mode: accepting it doesn't send every question to the
browser.

**Offer it just-in-time, inline.** Default to text. Offer it the first
time one of the moments above comes up, never upfront, as one line in
your normal message while you carry on in text:

> "This might be easier if I show you. Want me to open a browser tab with some mockups?"

Never send a standalone offer that holds back other content or pauses
the work. Use it only once they've accepted. If they decline or ignore
it, stay in text and don't offer again unless they raise it.

**Per question, ask: would they understand this better by seeing it?**
Mockups, layouts, diagrams, and side-by-side designs go in the browser.
Requirements, scope, tradeoffs, and conceptual choices stay in text. A
question about a UI topic isn't automatically a visual question.

If they accept, read `visual-companion.md` in this directory before
starting the server.
