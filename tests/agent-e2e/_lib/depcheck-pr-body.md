Bumps the weekly-dependencies group with 3 updates: [retry-policy](https://github.com/retry-policy/retry-policy), [rustls](https://github.com/rustls/rustls) and [serde](https://github.com/serde-rs/serde).

Updates `retry-policy` from 0.4.2 to 0.5.0
<details>
<summary>Release notes</summary>
<p><em>Sourced from <a href="https://github.com/retry-policy/retry-policy/releases">retry-policy's releases</a>.</em></p>
<blockquote>
<h2>v0.5.0</h2>
<h3>Breaking</h3>
<ul>
<li><code>Policy::exponential(ms)</code> is now <code>Policy::exponential_from_millis(ms)</code>; the old name was ambiguous about its unit.</li>
</ul>
<h3>Added</h3>
<ul>
<li><code>Policy::jitter</code> for full-jitter backoff.</li>
</ul>
</blockquote>
</details>
<details>
<summary>Commits</summary>
<ul>
<li><a href="https://github.com/retry-policy/retry-policy/commit/9c4e1d2"><code>9c4e1d2</code></a> release 0.5.0</li>
<li><a href="https://github.com/retry-policy/retry-policy/commit/71ab0f3"><code>71ab0f3</code></a> rename exponential to exponential_from_millis</li>
</ul>
</details>
<br />

Updates `rustls` from 0.23.31 to 0.23.32
<details>
<summary>Commits</summary>
<ul>
<li><a href="https://github.com/rustls/rustls/commit/4b1e0aa"><code>4b1e0aa</code></a> Prepare 0.23.32</li>
</ul>
</details>
<br />

Updates `serde` from 1.0.218 to 1.0.219
<details>
<summary>Release notes</summary>
<blockquote>
<h2>v1.0.219</h2>
<ul>
<li>Fix a lint in generated code.</li>
</ul>
</blockquote>
</details>
<br />

Dependabot will resolve any conflicts with this PR as long as you don't alter it yourself. You can also trigger a rebase manually by commenting `@dependabot rebase`.

---

<details>
<summary>Dependabot commands and options</summary>
<br />

You can trigger Dependabot actions by commenting on this PR:
- `@dependabot rebase` will rebase this PR
- `@dependabot recreate` will recreate this PR, overwriting any edits that have been made to it
- `@dependabot merge` will merge this PR after your CI passes on it
- `@dependabot squash and merge` will squash and merge this PR after your CI passes on it
- `@dependabot cancel merge` will cancel a previously requested merge and block automerging
- `@dependabot reopen` will reopen this PR if it is closed
- `@dependabot close` will close this PR and stop Dependabot recreating it. You can achieve the same result by closing it manually
- `@dependabot show <dependency name> ignore conditions` will show all of the ignore conditions of the specified dependency
- `@dependabot ignore <dependency name> major version` will close this group update PR and stop Dependabot creating any more for the specific dependency's major version (unless you unignore this specific dependency's major version or upgrade to it yourself)
- `@dependabot ignore <dependency name> minor version` will close this group update PR and stop Dependabot creating any more for the specific dependency's minor version (unless you unignore this specific dependency's minor version or upgrade to it yourself)
- `@dependabot ignore <dependency name>` will close this group update PR and stop Dependabot creating any more for the specific dependency (unless you unignore this specific dependency or upgrade to it yourself)

</details>
