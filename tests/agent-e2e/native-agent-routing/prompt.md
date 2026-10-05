Please handle this small change through the configured subagents, not in the main conversation.

First use the `Explore` subagent to locate the file containing the application's greeting and report the current greeting. Then use the `worker-easy` subagent, without a per-call model override, to change that greeting from `hello` to `hello world`. Wait for both subagents to finish, verify the resulting file, and briefly report what changed.

For this routing test, also dispatch `worker-hard` without a model override to
assess whether moving the greeting to a database would introduce migration
risk, and dispatch `advisor` without a model override to independently review
whether the final greeting is correct. These last two tasks are read-only and
must not change files. Wait for all four subagents.
