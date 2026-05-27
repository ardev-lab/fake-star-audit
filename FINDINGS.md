# Findings: auditing the fastest-growing new repos

I ran `fake-star-audit` over a reproducible slice of GitHub — the most-starred
public repositories **created in the last 90 days**. New projects are where
purchased stars tend to cluster, because the whole point of buying stars is to
fake *early* traction.

## Method
- Population: GitHub Search `created:>2026-02-26 stars:>1000`, sorted by stars (684 repos matched).
- Sample: the top 12 by star count (range: 20k–192k stars).
- Tool: `fake-star-audit` — standard-library only, anonymous GitHub API, 5 timing/identity
  axes plus extended signals. Every verdict prints the per-rule evidence behind it. No token, no dependencies.
- Date: 2026-05-27 (UTC).

## Result

| verdict | count | meaning |
|---|---|---|
| LOW    | 9 / 12 | looks organic |
| MEDIUM | 3 / 12 | two signatures consistent with purchased stars |
| HIGH   | 0 / 12 | — |

The 3 MEDIUM repos each tripped the **same two axes**: a **page-1 sliding-window burst**
(a dense spike of early stars in a short window) and **same-second star clusters**
(multiple stars recorded within a single second — hard to do by hand).

These are *signatures consistent with* star purchasing — **not proof**. A MEDIUM means
"worth a closer look," not a verdict of guilt. Repo names are withheld on purpose:
the point here is the method and the base rate, not an accusation.

## Check any repo yourself

Zero install, zero dependencies — it's one file:

```sh
curl -sO https://raw.githubusercontent.com/Armada735/fake-star-audit/main/audit.py
python3 audit.py --repo owner/name
```

Or from PyPI (the CLI entry point is `fake-star-audit-cli`):

```sh
pipx run --spec fake-star-audit fake-star-audit-cli --repo owner/name
```

Every report shows the evidence behind the verdict, so you can judge it — and disagree — for yourself.

— produced with fake-star-audit · https://github.com/Armada735/fake-star-audit
