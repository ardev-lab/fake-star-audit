---
name: fake-star-audit
description: Check whether a GitHub repository's stars look fake or bot-injected. Runs a transparent, dependency-free rule-based audit (5 axes over two stargazer windows) and reports LOW / MEDIUM / HIGH with evidence. Use when the user asks if a repo is fake-starred, has bot stars, or whether its star count is trustworthy / real.
---

This skill audits a GitHub repository's stargazers for signs of fake-star
injection and returns an explainable risk verdict. It wraps `audit.py`, a
single-file, zero-dependency, anonymous-API tool (no token, no file writes).

## When to use

Trigger when the user asks anything like:
- "is `owner/repo` fake-starred?" / "are these stars real?"
- "check the star authenticity of this GitHub repo"
- "this repo has 5k stars but looks sketchy — verify it"

## How to run

`audit.py` lives at the repository root (one directory above this skill). Run:

```bash
python3 /path/to/fake-star-audit/audit.py --repo <owner>/<name> --json
```

- Requires only Python 3.10+ and outbound HTTPS to `api.github.com`.
- Uses the anonymous GitHub API (60 req/h). Each audit costs 3–4 requests.
- If you only have the owner/name from a URL like `https://github.com/a/b`,
  pass `--repo a/b`.

## How to interpret the JSON

Key fields:
- `risk_verdict`: `LOW` / `MEDIUM` / `HIGH`.
- `axes`: the 5 detection axes, each with `flag` (bool) and `evidence` (string,
  annotated by which window — `earliest` = bootstrap window, `latest` = recent drip).
- `extended_signals`: booleans like `fork_star_inverted`, `single_repo_mass_injection`,
  `trusted_org_parasitism` (any hard one forces HIGH).
- `warnings`: caveats (e.g. repo too large to page to newest stars).

Verdict logic: HIGH = 3+ axes or a hard signal; MEDIUM = 2 axes or 1 axis + a
signal; LOW = 0–1 axes and no hard signals. It is conservative by design.

## How to respond to the user

Give a short, plain-language summary, then the *reason*:

> **HIGH risk.** 100 stars landed in the first 33 minutes after the repo was
> created, with near-sequential account IDs — a bootstrap-injection pattern,
> not organic growth.

Always cite the specific flagged axes / evidence. If the verdict is LOW, say so
plainly ("stars look organic on the sampled windows") and mention it's a page-1
sample, not a full-history proof. Never present a verdict as definitive proof of
fraud — it's a heuristic signal; point the user to the evidence.

## On errors

- `rate_limited`: the anonymous 60/h budget is spent; tell the user to retry in
  up to an hour.
- `not_found`: repo is private or doesn't exist.
- `pagination_capped` / `warnings` mentioning size: very large repo; the recent
  window falls back to oldest stars — note this caveat in your answer.

This skill only audits public repositories using public data.
