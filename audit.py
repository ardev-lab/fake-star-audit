#!/usr/bin/env python3
"""
fake-star-audit — transparent, rule-based GitHub fake-star detector.

Audits a repo's stargazers across 5 deterministic axes plus extended signals,
and reports a LOW / MEDIUM / HIGH risk verdict with full evidence. No machine
learning, no black box: every flag is explainable.

It inspects TWO windows of stargazers, because injection shows up in different
places:
  - earliest (oldest up to 100): catches bootstrap injection at repo creation
    (e.g. "100 stars in 33 minutes" right after the repo is born).
  - latest (most-recent 30): catches retrospective injection / ongoing drip.
An axis is flagged if it trips in EITHER window.

Design principles (see SPEC.md):
  - transparent : each rule is an inspectable list
  - deterministic: same input -> same output (no random sampling)
  - conservative : LOW by default; HIGH only on strong signatures
  - anonymous    : uses the unauthenticated GitHub API; never reads a token
                   or any environment variable; never writes files.

Usage:
  python3 audit.py --repo owner/name
  python3 audit.py --repo owner/name --json
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from statistics import median, pstdev, mean

TOOL_VERSION = "0.1.0"
TOOL_URL = "https://github.com/Armada735/fake-star-audit"
API = "https://api.github.com"
UA = "fake-star-audit/%s (+%s)" % (TOOL_VERSION, TOOL_URL)

# Suffix-farm vocabulary observed in production. New variants emerge ~1-3/month,
# so we also do frequency-based trailing-token detection below.
KNOWN_SUFFIXES = [
    "-bot", "-oss", "-cmd", "-max", "-create", "-ctrl", "-source", "-bit",
    "-svg", "-hub", "-prog", "-commits", "-cell", "-pixel", "-boop", "-arch",
    "-lab", "-lgtm", "-ph", "-maker", "-hue", "-del", "-netizen", "-eng",
    "-blip", "-glitch", "-jpg", "-png", "-star", "-art", "-crypto", "-coder",
    "-beep", "-ship-it", "-spec", "-rgb", "-a11y", "-creator", "-cloud", "-web",
]

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TRAILING_TOKEN_RE = re.compile(r"-([a-z0-9]+)$", re.IGNORECASE)


class AuditError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def is_backfilled(times):
    """True if timestamps look backfilled / low-resolution (many identical
    values). Stars predating GitHub's starred_at tracking (~2012) get a single
    bulk timestamp, which makes every timing axis fire spuriously. Timing
    analysis is meaningless on such data, so axes skip it."""
    if len(times) < 4:
        return False
    return len(set(times)) < max(5, int(0.10 * len(times)))


def gh_get(url, accept=None, timeout=10):
    """GET a GitHub API URL anonymously. Returns (status, headers, parsed_json)."""
    headers = {"User-Agent": UA, "Accept": accept or "application/vnd.github+json"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            hdrs = {k: v for k, v in resp.headers.items()}
            try:
                data = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError) as e:
                raise AuditError("parse_error", "GitHub returned non-JSON: %s" % e)
            return resp.status, hdrs, data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise AuditError("not_found", "Repo or user not found (or private).")
        if e.code == 403:
            if e.headers.get("X-RateLimit-Remaining") == "0":
                raise AuditError("rate_limited",
                                 "GitHub anonymous rate limit exhausted (60/h). Retry later.")
            raise AuditError("forbidden", "GitHub returned 403 (access forbidden).")
        if e.code == 422:
            # stargazers pagination cap (>40k stars) lands here
            raise AuditError("pagination_capped",
                             "GitHub paginated past its cap (repo too large for last-page fetch).")
        raise AuditError("http_%d" % e.code, "GitHub HTTP error %d." % e.code)
    except (urllib.error.URLError, TimeoutError) as e:
        raise AuditError("network_error", "Network failure: %s" % e)


# --------------------------------------------------------------------------
# Data fetching (<= 4 API calls total)
# --------------------------------------------------------------------------

def _normalize_stars(data):
    out = []
    for item in (data or []):
        if not isinstance(item, dict):
            continue
        u = item.get("user") or {}
        out.append({"login": u.get("login"), "id": u.get("id"),
                    "starred_at": item.get("starred_at")})
    return out


def fetch_repo(owner, name, timeout):
    _, _, d = gh_get("%s/repos/%s/%s" % (API, owner, name), timeout=timeout)
    if not isinstance(d, dict):
        raise AuditError("unexpected", "Unexpected repo metadata shape.")
    return d


def fetch_stargazer_windows(owner, name, timeout=10):
    """Return (earliest, latest, capped).

    earliest = oldest up to 100 stargazers (bootstrap-injection window).
    latest   = most-recent up to 30 stargazers (drip / retrospective window).
    capped   = True if the repo is too large to reach the last page.
    """
    accept = "application/vnd.github.star+json"
    url = "%s/repos/%s/%s/stargazers?per_page=100" % (API, owner, name)
    _, hdrs, data = gh_get(url, accept=accept, timeout=timeout)
    earliest = _normalize_stars(data)  # oldest-first
    link = hdrs.get("Link", "") or hdrs.get("link", "")
    m = re.search(r"[?&]page=(\d+)>;\s*rel=\"last\"", link)
    capped = False
    latest = earliest[-30:]
    if m and int(m.group(1)) > 1:
        last = int(m.group(1))
        if last > 400:  # GitHub stargazer pagination cap
            capped = True
        else:
            try:
                url2 = "%s/repos/%s/%s/stargazers?per_page=100&page=%d" % (API, owner, name, last)
                _, _, data2 = gh_get(url2, accept=accept, timeout=timeout)
                latest = _normalize_stars(data2)[-30:]
            except AuditError as e:
                if e.code == "pagination_capped":
                    capped = True
                else:
                    raise
    return earliest, latest, capped


def fetch_owner(owner, timeout):
    try:
        _, _, d = gh_get("%s/users/%s" % (API, owner), timeout=timeout)
        return d if isinstance(d, dict) else {}
    except AuditError:
        return {}  # supplementary; degrade gracefully


# --------------------------------------------------------------------------
# Axes (5) — each takes a list of stargazers and returns a finding dict
# --------------------------------------------------------------------------

def axis_page1_sliding_window(stars):
    times = sorted(t for t in (parse_iso(s["starred_at"]) for s in stars) if t)
    if len(times) < 2:
        return {"flag": False, "evidence": "insufficient timestamps", "span_hours": None}
    if is_backfilled(times):
        return {"flag": False, "evidence": "backfilled/low-resolution timestamps; skipped",
                "span_hours": None}
    span_h = (times[-1] - times[0]).total_seconds() / 3600.0
    n = len(times)
    rate = (n / span_h) if span_h > 0 else float("inf")
    # BURST injection (strong fake signal): many stars in a very short window.
    # Organic launches ramp over hours/days; a bootstrap dump packs 50+ stars
    # into <2h. This is the primary, flag-worthy condition.
    burst = n >= 50 and span_h < 2.0
    # DECELERATED (informational only, not flagged): oldest stars spread over a
    # long time is normal for healthy aging repos -> would false-positive.
    decel = span_h > 720
    if burst:
        ev = "BURST: %d stars in %.2fh (~%.0f stars/h)" % (n, span_h, rate)
    elif decel:
        ev = "decelerated: %d stars span %.1fh (informational, not a fake signal)" % (n, span_h)
    else:
        ev = "%d stars span %.1fh" % (n, span_h)
    return {"flag": bool(burst), "evidence": ev, "span_hours": round(span_h, 1)}


def axis_suffix_farm(stars):
    logins = [s["login"] for s in stars if s.get("login")]
    if not logins:
        return {"flag": False, "evidence": "no logins", "matched_suffixes": [], "_frac": 0.0}
    matched, hits, token_freq = set(), 0, {}
    for lg in logins:
        low = lg.lower()
        if any(low.endswith(suf) for suf in KNOWN_SUFFIXES):
            for suf in KNOWN_SUFFIXES:
                if low.endswith(suf):
                    matched.add(suf)
                    break
            hits += 1
        tok = TRAILING_TOKEN_RE.search(lg)
        if tok:
            t = tok.group(1).lower()
            token_freq[t] = token_freq.get(t, 0) + 1
    n = len(logins)
    freq_farm = [t for t, c in token_freq.items() if c / n > 0.30 and c >= 3]
    frac = hits / n
    flag = frac > 0.30 or bool(freq_farm)
    ev = "%d/%d logins match farm suffixes (%.0f%%)" % (hits, n, frac * 100)
    if freq_farm:
        ev += "; trailing-token cluster: %s" % ", ".join(sorted(freq_farm))
    return {"flag": flag, "evidence": ev,
            "matched_suffixes": sorted(matched) + ["-" + t for t in sorted(freq_farm)],
            "_frac": frac}


def axis_sequential_id_cluster(stars):
    seq = [(parse_iso(s["starred_at"]), s["id"]) for s in stars
           if s.get("id") is not None and parse_iso(s["starred_at"])]
    seq.sort(key=lambda x: x[0])
    ids = [i for _, i in seq]
    if len(ids) < 4:
        return {"flag": False, "evidence": "insufficient ids",
                "id_span": (max(ids) - min(ids)) if ids else None}
    run = None
    for i in range(len(ids) - 3):
        w = ids[i:i + 4]
        if max(w) - min(w) < 200000:
            run = w
            break
    flag = run is not None
    ev = ("4+ time-consecutive stargazers within id range <200k (run min %d)" % min(run)) \
        if flag else "no dense sequential-id run"
    return {"flag": flag, "evidence": ev, "id_span": max(ids) - min(ids)}


def axis_same_second_cluster(stars):
    times = sorted(t for t in (parse_iso(s["starred_at"]) for s in stars) if t)
    if len(times) < 4:
        return {"flag": False, "evidence": "insufficient timestamps", "max_density": 0}
    if is_backfilled(times):
        return {"flag": False, "evidence": "backfilled/low-resolution timestamps; skipped",
                "max_density": 0}
    max_density, j = 1, 0
    for i in range(len(times)):
        while (times[i] - times[j]).total_seconds() > 30:
            j += 1
        max_density = max(max_density, i - j + 1)
    flag = max_density >= 4
    return {"flag": flag, "evidence": "max %d stars within a 30s window" % max_density,
            "max_density": max_density}


def axis_interstar_gap_regularity(stars):
    times = sorted(t for t in (parse_iso(s["starred_at"]) for s in stars) if t)
    if len(times) < 20:
        return {"flag": False, "evidence": "need >=20 timestamps (have %d)" % len(times),
                "gap_cv": None, "gap_median_sec": None}
    if is_backfilled(times):
        return {"flag": False, "evidence": "backfilled/low-resolution timestamps; skipped",
                "gap_cv": None, "gap_median_sec": None}
    gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    gaps = [g for g in gaps if g >= 0]
    if len(gaps) < 2:
        return {"flag": False, "evidence": "insufficient gaps", "gap_cv": None,
                "gap_median_sec": None}
    m = mean(gaps)
    cv = (pstdev(gaps) / m) if m > 0 else 0.0
    med = median(gaps)
    flag = cv < 0.6 and med < 90 and len(times) >= 20
    return {"flag": flag,
            "evidence": "gap CV %.2f, median %.0fs over %d stars" % (cv, med, len(times)),
            "gap_cv": round(cv, 2), "gap_median_sec": round(med, 1)}


AXES = [
    ("page1_sliding_window", axis_page1_sliding_window),
    ("suffix_farm", axis_suffix_farm),
    ("sequential_id_cluster", axis_sequential_id_cluster),
    ("same_second_cluster", axis_same_second_cluster),
    ("interstar_gap_regularity", axis_interstar_gap_regularity),
]


def compute_axes(stars):
    return {name: fn(stars) for name, fn in AXES}


def skipped_axes(reason):
    """All axes marked not-applicable (used when a window is intentionally not
    analysed, e.g. the earliest window of an old repo)."""
    return {name: {"flag": False, "evidence": reason, "_frac": 0.0} for name, _ in AXES}


def merge_axes(earliest_axes, latest_axes):
    """An axis is flagged if it trips in either window. Evidence is annotated
    with which window(s) tripped."""
    merged = {}
    for name, _ in AXES:
        e, l = earliest_axes[name], latest_axes[name]
        flag = bool(e.get("flag") or l.get("flag"))
        which = []
        if e.get("flag"):
            which.append("earliest: " + e.get("evidence", ""))
        if l.get("flag"):
            which.append("latest: " + l.get("evidence", ""))
        if not which:
            which.append("earliest: %s | latest: %s" % (e.get("evidence", ""), l.get("evidence", "")))
        merged[name] = {"flag": flag, "evidence": " || ".join(which),
                        "earliest": {k: v for k, v in e.items() if k != "_frac"},
                        "latest": {k: v for k, v in l.items() if k != "_frac"}}
    return merged


# --------------------------------------------------------------------------
# Extended signals (6)
# --------------------------------------------------------------------------

def extended_signals(repo, owner_obj, suffix_frac):
    stars = repo.get("stargazers_count", 0) or 0
    forks = repo.get("forks_count", 0) or 0
    created = parse_iso(repo.get("created_at"))
    pushed = parse_iso(repo.get("pushed_at"))
    now = datetime.now(timezone.utc)
    age_h = ((now - created).total_seconds() / 3600.0) if created else None

    fork_star_ratio = round(forks / stars, 3) if stars > 0 else None
    fork_star_inverted = bool(stars > 0 and forks >= stars)

    owner_created = parse_iso(owner_obj.get("created_at"))
    owner_age_days = ((now - owner_created).total_seconds() / 86400.0) if owner_created else None
    public_repos = owner_obj.get("public_repos")
    mass_creation_owner = bool(
        public_repos is not None and public_repos >= 10
        and owner_age_days is not None and owner_age_days < 30)

    never_repushed = bool(created and pushed and (pushed - created).total_seconds() < 3600)
    velocity = (stars / age_h) if (age_h and age_h > 0) else 0.0
    single_repo_mass_injection = bool(
        public_repos == 1 and velocity > 30 and suffix_frac > 0.50 and never_repushed)

    is_org = owner_obj.get("type") == "Organization"
    followers = owner_obj.get("followers", 0) or 0
    repo_age_days = (age_h / 24.0) if age_h is not None else None
    trusted_org_parasitism = bool(
        is_org and owner_age_days is not None and owner_age_days > 730
        and followers > 500 and repo_age_days is not None and repo_age_days < 7
        and never_repushed and fork_star_inverted)

    return {
        "fork_star_ratio": fork_star_ratio,
        "fork_star_inverted": fork_star_inverted,
        "batch_convergence_candidate": False,  # cross-repo scan = out of scope (MVP)
        "mass_creation_owner": mass_creation_owner,
        "single_repo_mass_injection": single_repo_mass_injection,
        "trusted_org_parasitism": trusted_org_parasitism,
    }


def decide_verdict(merged_axes, ext):
    n = sum(1 for a in merged_axes.values() if a.get("flag"))
    hard = (ext["mass_creation_owner"] or ext["single_repo_mass_injection"]
            or ext["trusted_org_parasitism"])
    if hard or n >= 3 or (n >= 2 and ext["batch_convergence_candidate"]):
        return "HIGH", n
    if n == 2 or (n >= 1 and any(
            ext[k] for k in ("fork_star_inverted", "mass_creation_owner",
                             "single_repo_mass_injection", "trusted_org_parasitism"))):
        return "MEDIUM", n
    return "LOW", n


def build_summary(verdict, merged_axes, ext, n):
    flagged = [k for k, a in merged_axes.items() if a.get("flag")]
    hard = [k for k in ("mass_creation_owner", "single_repo_mass_injection",
                        "trusted_org_parasitism") if ext[k]]
    if verdict == "LOW":
        return "looks organic; %d/5 axes flagged, no hard extended signals" % n
    parts = []
    if flagged:
        parts.append("axes: " + ", ".join(flagged))
    if hard:
        parts.append("hard signals: " + ", ".join(hard))
    if ext["fork_star_inverted"]:
        parts.append("fork>=star (inverted)")
    return "; ".join(parts) or "see axes"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def audit(repo_slug, timeout=10):
    owner, name = repo_slug.split("/", 1)
    repo = fetch_repo(owner, name, timeout)
    if repo.get("private"):
        raise AuditError("scope_out_of_range", "Repo is private; tool audits public repos only.")
    earliest, latest, capped = fetch_stargazer_windows(owner, name, timeout)
    owner_obj = fetch_owner(owner, timeout)

    created = parse_iso(repo.get("created_at"))
    age_h = ((datetime.now(timezone.utc) - created).total_seconds() / 3600.0) if created else None
    age_days = (age_h / 24.0) if age_h is not None else None
    # The earliest window catches *bootstrap injection*, which only happens on
    # young repos. On old repos the oldest stars are backfilled (single bulk
    # timestamp) and from early low-numbered accounts, which makes the timing
    # and sequential-id axes fire spuriously. So only analyse earliest if young.
    use_earliest = age_days is None or age_days < 90
    earliest_axes = compute_axes(earliest) if use_earliest \
        else skipped_axes("earliest-window skipped (repo age > 90d; avoids backfill artifacts)")
    latest_axes = compute_axes(latest)
    merged = merge_axes(earliest_axes, latest_axes)
    suffix_frac = max(earliest_axes["suffix_farm"].get("_frac", 0.0),
                      latest_axes["suffix_farm"].get("_frac", 0.0))
    ext = extended_signals(repo, owner_obj, suffix_frac)
    verdict, n = decide_verdict(merged, ext)

    warnings = []
    if age_h is not None and age_h < 1:
        warnings.append("repo is <1h old; signals are unstable")
    if not use_earliest:
        warnings.append("repo age > 90d: earliest-window analysis skipped (backfill-safe)")
    if capped:
        warnings.append("repo too large to reach last page; 'latest' window falls back to oldest")
    dated = len([s for s in latest if parse_iso(s["starred_at"])])
    if dated < 20:
        warnings.append("fewer than 20 dated stargazers in latest window; gap-regularity axis inactive")

    return {
        "tool_version": TOOL_VERSION,
        "tool_url": TOOL_URL,
        "repo": repo_slug,
        "fetch_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_metadata": {
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks_count"),
            "subscribers": repo.get("subscribers_count"),
            "size_kb": repo.get("size"),
            "language": repo.get("language"),
            "created_at": repo.get("created_at"),
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "license": (repo.get("license") or {}).get("spdx_id"),
            "topics": repo.get("topics", []),
        },
        "windows": {"earliest_count": len(earliest), "latest_count": len(latest)},
        "axes": merged,
        "extended_signals": ext,
        "risk_verdict": verdict,
        "evidence_summary": build_summary(verdict, merged, ext, n),
        "warnings": warnings,
    }


def human_report(r):
    icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(r["risk_verdict"], "")
    md, out = r["repo_metadata"], []
    out.append("%s  %s  —  risk: %s" % (icon, r["repo"], r["risk_verdict"]))
    out.append("    %s★ / %s forks / %s / age %sh / %s" % (
        md["stars"], md["forks"], md["language"], md["age_hours"], md["license"]))
    out.append("    windows: earliest=%d, latest=%d" % (
        r["windows"]["earliest_count"], r["windows"]["latest_count"]))
    out.append("    %s" % r["evidence_summary"])
    out.append("    axes (flagged if either window trips):")
    for k, a in r["axes"].items():
        mark = "FLAG" if a.get("flag") else "ok  "
        out.append("      [%s] %-26s %s" % (mark, k, a.get("evidence", "")))
    ext_on = [k for k, v in r["extended_signals"].items() if v is True]
    if ext_on:
        out.append("    extended signals: " + ", ".join(ext_on))
    for w in r["warnings"]:
        out.append("    ! %s" % w)
    out.append("    — audited by fake-star-audit · %s" % TOOL_URL)
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description="Transparent rule-based GitHub fake-star audit.")
    p.add_argument("--repo", required=True, metavar="owner/name",
                   help="GitHub repo to audit, e.g. facebook/react")
    p.add_argument("--json", action="store_true", help="emit raw JSON instead of human report")
    p.add_argument("--timeout", type=int, default=10, help="per-request timeout seconds (default 10)")
    args = p.parse_args(argv)

    if not REPO_RE.match(args.repo):
        err = {"error": "invalid_input", "message": "repo must be 'owner/name' (alnum . _ - only)"}
        print(json.dumps(err) if args.json else "error: %s" % err["message"], file=sys.stderr)
        return 2
    try:
        result = audit(args.repo, timeout=args.timeout)
    except AuditError as e:
        err = {"error": e.code, "message": e.message}
        print(json.dumps(err) if args.json else "error (%s): %s" % (e.code, e.message), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else human_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
