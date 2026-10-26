---
name: adopting-an-external-api
description: Use when about to call an external HTTP API, service or site for the first time in a project — writing a client, crawler, scraper, sync job, bulk fetch or one-off probe against someone else's server; adding a new host to existing tooling; or when a task involves pulling more than a handful of requests worth of data from a third party.
---

# Adopting an External API

Before the first request to a host you have not used here, establish the rate it
tolerates, register a debounced sink at that rate, and point your code at the
sink. The sink URL — not a delay argument — is what carries the cadence.

## Why the address and not a parameter

A delay parameter is a knob: a caller can pass a weaker value and still succeed,
so compliance depends on every caller choosing well every time. An address is
binary. Point at the sink and the cadence applies, whether the caller is a
crawler, a diagnostic one-liner, a subagent, or a detached background script.

## Steps

### 1. Investigate the rate BEFORE any traffic

Find the published limit and record where it came from:

- `robots.txt` — `Crawl-Delay`, and any `Disallow` covering your path
- API docs — requests/minute, quotas, burst rules, cost-weighted limits
- Response headers on a single manual request — `X-RateLimit-*`, `Retry-After`
- Terms of use, if the data is not obviously public

No published limit means an unmeasured host, which gets the conservative
default, not a guess in your favour. Where the number came from goes in `--note`.

### 2. Register the sink

```bash
egress-sink add https://api.example.com --interval 5 --note "robots.txt Crawl-Delay: 2, doubled"
```

It prints the proxy URL and the effective interval. Intervals only ever ratchet
stricter — re-registering faster is ignored, by design. The upstream URL is
deliberately not printed, because this output gets pasted into code.

### 3. Hardcode the sink URL as the default

Put the printed URL in the ONE place your client resolves its base URL — the
constant, the config default, the helper's fallback. Not a parameter with the
upstream as its default. Not an environment variable that falls back to the
upstream.

Verify nothing kept the direct address:

```bash
lint-direct-egress .
```

It exits 1 on any registered upstream host found in code. Wire it into the
project's verify command so this cannot rot.

### 4. Propagate it

Record the sink URL in the task's mission or brief, so subagents and later
sessions inherit the address rather than rediscovering the upstream. Briefing a
subagent to "be careful with rate limits" does not survive handoff; giving it a
URL does.

### 5. Prove the client on a bounded sample

Before any bulk run, verify pagination and termination on a small window:
a short page mid-run is not end-of-data, offset caps, totals that over-count,
empty windows, duplicates at boundaries. A correctness bug multiplies traffic —
a silent one turns a single run into a repair pass at full scale.

## When the sink refuses

A `403` from the sink is a hard stop, not a retryable error. It means a cooldown
is active, the budget is spent, or the queue is deeper than the wait ceiling.

- Do not retry, and do not shorten the interval.
- Do not point at the upstream "just for this one call".
- Do not start a second process to work around a busy one.
- Widen a limit only by editing the egress policy file, deliberately.

If the upstream itself pushed back (403 challenge, 429, quota mail), the sink
records it for every client on the machine, not just the one that saw it.

## Red flags

| Thought | Reality |
|---|---|
| "It's one quick probe, not a crawl" | The host sees aggregate. Probes are traffic. |
| "I'll add the throttle once it works" | The unthrottled version is what runs during debugging, which is when volume spikes. |
| "The sink is down, I'll use the real URL meanwhile" | That is the bypass. Restart the sink. |
| "I'll pass a smaller interval just for this run" | Intervals ratchet stricter only; wanting the knob is the signal. |
| "My script has its own delay" | Two polite processes are twice the rate. Only a shared address is shared. |
| "No published limit, so anything goes" | Unmeasured means conservative, not permitted. |
