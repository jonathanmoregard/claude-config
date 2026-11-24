# One root cause

The failure this stops: multiple fixes haphazardly patch the same unaddressed
root cause, instead of solving it once and for all. Each round's sibling line
finds more sites, each site gets its own patch, the patches start to disagree,
and the cause is named only at the cap.

## 1. Spot it

At every adjudication, before any edit, sort the reproduced findings by why
they fail, not where. Include earlier rounds' findings, fixed or not, and
what sibling lines turned up. It applies when two or more of them fail for
one reason that no fix so far has removed: every fix made, or about to be
made, changes a call site while the code producing the failure stays as it
is. Signs:

1. a sibling line keeps finding more sites of a shape already fixed;
2. site fixes disagree with each other (a different status, fallback or
   message for the same fact);
3. a reviewer, or a design finding, names the shared cause.

No count is needed: two findings in round 1 qualify, and a shrinking count
does not clear it. Findings that merely share a file or a hunt class, but fail
for separate reasons, do not.

## 2. Name it

Before acting, write two sentences: the shared cause, and the one change that
removes it for every site, the unreported ones included. List the known sites.

## 3. Act once

1. **Within the diff's scope** (the cause sits in code this branch added or
   changed, and removing it keeps the acceptance criteria): make that change
   once, at the cause. Callers adapting to it are part of the one change.
   Replace earlier site patches it makes redundant or that disagree with it.
   Every reproducer stays as a regression test.
2. **Otherwise** (the cause predates the branch, or removing it would grow the
   branch, change a contract, or touch CI or dependencies): patch no site,
   not even the ones the diff caused, and run no further round. Put it to the
   user as one closed question with a recommendation, for example: "Remove
   <cause> at <file:line> on this branch (recommended), or ship the site
   patches and track the cause separately?" Unattended: write the question to
   `<notes>/pending_for_human.md` and stop with "not converged; root cause
   named".
3. After the root change lands, run the gates and every reproducer, then a
   fresh round over the whole diff. Its sibling line names the cause, not a
   site: "find any site that still works around <cause>".

## 4. What it overrides

In this situation, this file wins over:

1. "Reproduced, and caused by the diff: fix it now": the sites are fixed by
   the one change, not one by one.
2. "Reproduced but pre-existing and unrelated: record it separately": judge
   the cause, not each site. A pre-existing site sharing the cause is related;
   whether removing the cause grows the branch is decided once, in step 3.
3. "Design finding: never auto-fix it": when the reproduced findings are the
   evidence for a design finding's cause, it is handled here. A design finding
   with no reproduced instance stays a taste call.
4. Global rule, `CLAUDE.md` "Build + review": this is one of its stop
   triggers.

The count-based breaker (`rabbit-hole.md`) is separate: it fires on counts and
asks an advisor about the mechanism. This rule needs neither; you see the
shape while adjudicating. A cause already removed here is not a thread for it.

## 5. Ledger and report

Each finding's row keeps its outcome; `fixed` is `yes` once the root change
lands, `open` while the question waits. The report names the cause, the one
change, and whether it was made or asked.
