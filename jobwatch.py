#!/usr/bin/env python3
"""jobwatch.py — one read-only health check over every registered scheduled job.

Silent when healthy. `check` prints nothing and exits 0 when every registered
job is fine; it prints findings and exits 1 when one is late, failed, or cannot
be evaluated. That inversion is deliberate: a daily "all green" wall of text
trains the eye to skip the surface, so absence of output *is* the healthy signal.

Three failure modes, and the order matters:

  1. Ran, failed, said so into the void. An error exists; nothing reads it.
  2. Stopped running entirely. No error is produced *at all* — an unloaded
     launchd agent, a disabled workflow, a detached deploy hook. Error-based
     monitoring cannot see this, because absence of errors is indistinguishable
     from success. This is why every check is primarily a *freshness* check.
  3. Ran, reported success, did the wrong thing. Needs a per-job assertion about
     the result, which only the job's author can write (`fail_if`, `expect`).

Lateness is measured in *awake* time for a job on this machine's own clock. A
`launchd` StartInterval only fires while the laptop is up, so a night's sleep
otherwise reports a healthy hourly job as eight hours late, every morning, until
you stop reading the surface. See `slept_between`.

Read-only by contract. This never restarts, repairs, re-enables or otherwise
mutates a job — a repair loop that misfires at 3am is a worse failure than the
one it fixes. The only files it writes are its own response cache. The one
process it runs that is not a read is `vercel whoami`, which refreshes the CLI's
own expired token in place; see `_vercel_token`.

Usage:
    jobwatch.py check                  # silent when healthy, exit 1 when not
    jobwatch.py check --json           # machine-readable, always exit 0
    jobwatch.py list                   # the full inventory, healthy or not
    jobwatch.py --offline check        # skip every network check
    jobwatch.py --only ID,ID check     # a subset, for debugging a registry entry
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "jobs.json"
EXAMPLE_REGISTRY = HERE / "jobs.example.json"
CACHE = HERE / "cache.json"

# Network answers are cached so the daily brief stays fast. A job that broke in
# the last three hours is not urgent; a brief that hangs for 20 seconds is.
NET_CACHE_TTL = dt.timedelta(hours=3)
NET_TIMEOUT = 15  # seconds, per call

VERCEL_AUTH = Path.home() / "Library/Application Support/com.vercel.cli/auth.json"
# The Vercel CLI's token is an 8-hour OAuth access token. Refresh a little early
# so a check starting just before the boundary does not race it.
VERCEL_TOKEN_MARGIN = dt.timedelta(minutes=5)
VERCEL_CLI_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

PMSET_TIMEOUT = 30  # seconds; reading the power log costs ~2s

OK, LATE, FAILED, UNKNOWN = "ok", "late", "failed", "unknown"
SEVERITY = {FAILED: 0, LATE: 1, UNKNOWN: 2, OK: 3}
MARK = {FAILED: "✗", LATE: "⏰", UNKNOWN: "?", OK: "·"}


class Result:
    def __init__(self, status, detail, last=None):
        self.status = status
        self.detail = detail
        self.last = last  # datetime of last known completion, or None


def worse(a: Result, b: Result) -> Result:
    return a if SEVERITY[a.status] <= SEVERITY[b.status] else b


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_interval(s):
    """'30m' / '6h' / '1d' / '2w' -> timedelta. None passes through."""
    if s is None:
        return None
    m = re.fullmatch(r"(\d+)([mhdw])", str(s).strip())
    if not m:
        raise ValueError(f"bad interval {s!r} (want 30m / 6h / 1d / 2w)")
    return dt.timedelta(**{UNITS[m.group(2)]: int(m.group(1))})


def expand(p):
    return Path(os.path.expanduser(p))


def tilde(p):
    p = str(p)
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def ago(then, now):
    if then is None:
        return "never"
    d = now - then
    secs = int(d.total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def human_dur(d):
    secs = int(d.total_seconds())
    if secs < 3600:
        return f"{secs // 60}m"
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h" if not m else f"{h}h{m:02d}m"


def _now():
    return dt.datetime.now()


# ---------------------------------------------------------------------------
# sleep — a laptop's clock is not a server's
# ---------------------------------------------------------------------------
# A `launchd` StartInterval job only fires while the machine is awake. One that
# slept 00:49 → 09:13 reports its hourly job as "8h ago, expected every 1h"
# while the job is perfectly healthy: agent loaded, last exit code 0, and simply
# no clock running. A surface that cries LATE every morning for that reason
# trains you to skip it, which is the one failure this whole thing exists to
# prevent. So lateness for a job on *this machine's* clock discounts the time
# the machine was not awake.
#
# `pmset -g log` is the source, and it costs ~2s — so it is read at most once
# per process, and only for a job that would otherwise be reported late. A
# healthy run never pays for it.
_PMSET_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) [+-]\d{4} (?P<kind>.{1,20}?)\s*\t")
# "Wake Requests" is written *while going to sleep* — it lists the wakes being
# scheduled, not a wake. Only these three mark the machine actually coming back.
_WAKE_KINDS = {"Wake", "DarkWake", "WakeDetails"}

_sleep_log = None


def _read_sleep_log():
    """([(asleep_from, awake_at)], earliest moment the log covers).

    Returns ([], None) on any failure. A checker that cannot read the power log
    falls back to plain wall-clock lateness — over-reporting is recoverable,
    guessing is not.
    """
    if sys.platform != "darwin":
        return [], None
    try:
        p = subprocess.run(["pmset", "-g", "log"], capture_output=True,
                           text=True, timeout=PMSET_TIMEOUT)
    except Exception:
        return [], None
    return _parse_sleep_log(p.stdout)


def _parse_sleep_log(text):
    spans, first, asleep_at = [], None, None
    for line in text.splitlines():
        m = _PMSET_RE.match(line)
        if not m:
            continue
        try:
            ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if first is None:
            first = ts
        kind = m.group("kind")
        if kind == "Sleep" and asleep_at is None:
            asleep_at = ts
        elif kind in _WAKE_KINDS and asleep_at is not None:
            spans.append((asleep_at, ts))
            asleep_at = None
    return spans, first


def sleep_log():
    global _sleep_log
    if _sleep_log is None:
        _sleep_log = _read_sleep_log()
    return _sleep_log


def slept_between(a, b):
    """(time the machine was asleep in [a, b], whether the log covers all of it).

    The second value matters: `pmset` keeps roughly a week. Beyond that we can
    only under-discount, and a finding that says so is honest where a confident
    number would not be.
    """
    spans, first = sleep_log()
    if first is None:
        return dt.timedelta(0), False
    total = dt.timedelta(0)
    for start, end in spans:
        lo, hi = max(start, a), min(end, b)
        if hi > lo:
            total += hi - lo
    return total, first <= a


def freshness(last, job, now, what="completed", local=False):
    """The mode-2 check. `last` is a naive local datetime, or None.

    `local` marks a job scheduled by this machine's own clock, so its lateness
    discounts time spent asleep.
    """
    every = parse_interval(job.get("every"))
    if every is None:
        return Result(OK, "no expected interval — outcome checked, freshness not")
    grace = parse_interval(job.get("grace")) or dt.timedelta(0)
    if last is None:
        return Result(LATE, f"no completed run found (expected every {job['every']})")

    gap, note = now - last, ""
    if local and gap > every + grace:
        # Only pay for the power log when the verdict would otherwise be LATE.
        slept, complete = slept_between(last, now)
        if slept > dt.timedelta(0):
            gap -= slept
            note = f", {human_dur(slept)} of it asleep"
            if not complete:
                note += " within the week the power log keeps"
    if gap > every + grace:
        return Result(LATE,
                      f"last {what} {last:%Y-%m-%d %H:%M} ({ago(last, now)}{note}) — "
                      f"expected every {job['every']}", last)
    return Result(OK, f"last {what} {ago(last, now)}{note}", last)


# ---------------------------------------------------------------------------
# cache — network answers only
# ---------------------------------------------------------------------------
def _cache_load():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _cache_save(data):
    try:
        CACHE.write_text(json.dumps(data, indent=1))
    except OSError:
        pass  # a read-only checker must not die because it cannot cache


class Net:
    """Cached network fetches, memoised per process and per cache file."""

    def __init__(self, offline=False, use_cache=True):
        self.offline = offline
        self.use_cache = use_cache
        self.disk = _cache_load() if use_cache else {}
        self.mem = {}
        self.dirty = False

    def get(self, key, fetch):
        if key in self.mem:
            return self.mem[key]
        if self.use_cache:
            hit = self.disk.get(key)
            if hit:
                try:
                    when = dt.datetime.fromisoformat(hit["at"])
                    if _now() - when < NET_CACHE_TTL:
                        self.mem[key] = hit["payload"]
                        return hit["payload"]
                except Exception:
                    pass
        if self.offline:
            raise Offline(f"offline: no fresh cache for {key}")
        payload = fetch()
        self.mem[key] = payload
        self.disk[key] = {"at": _now().isoformat(timespec="seconds"), "payload": payload}
        self.dirty = True
        return payload

    def flush(self):
        if self.dirty and self.use_cache:
            _cache_save(self.disk)


class Offline(Exception):
    pass


# ---------------------------------------------------------------------------
# check kinds
# ---------------------------------------------------------------------------
def check_log_block(job, net, now):
    """A log written as repeated blocks: one start marker, one done marker.

    Proves *completion*, not just execution — a run that started and died leaves
    a start marker with no done marker, which is a finding, not silence.
    """
    c = job["check"]
    path = expand(c["path"])
    if not path.exists():
        return Result(UNKNOWN, f"log not found: {tilde(path)}")
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return Result(UNKNOWN, f"cannot read {tilde(path)}: {e}")

    start_re = re.compile(c["start"], re.M)
    done_re = re.compile(c["done"], re.M)
    fail_re = re.compile(c["fail_if"], re.M) if c.get("fail_if") else None
    exp_re = re.compile(c["expect"], re.M) if c.get("expect") else None
    fmt = c.get("ts_format", "%Y-%m-%d %H:%M:%S")

    marks = list(start_re.finditer(text))
    if not marks:
        return Result(UNKNOWN,
                      f"no run marker in {tilde(path)} — the registry's `start` "
                      f"pattern no longer matches what the job writes")

    blocks = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        try:
            ts = dt.datetime.strptime(m.group("ts"), fmt)
        except (IndexError, ValueError):
            continue
        blocks.append((ts, text[m.start():end]))
    if not blocks:
        return Result(UNKNOWN, f"run markers in {tilde(path)} carry no parseable timestamp")

    last_ts, last_body = blocks[-1]
    done = [b for b in blocks if done_re.search(b[1])]
    last_done = done[-1][0] if done else None

    fresh = freshness(last_done, job, now, local=bool(c.get("launchd")))

    # Mode 1: the most recent run said it went wrong.
    if fail_re and fail_re.search(last_body):
        hit = fail_re.search(last_body).group(0).strip()[:120]
        return Result(FAILED, f"run {last_ts:%Y-%m-%d %H:%M} reported a failure: {hit}", last_done)
    # A start with no trailer has two causes that leave the identical shape on
    # disk: the run died, or the run is still going. Only one is a finding, and
    # launchd settles which without a guess — a live PID for the label means the
    # work is in flight. `correspondence-sync` runs a median 3m with a tail past
    # 45m on an hourly schedule, so a surface reading it mid-run is routine, and
    # an hourly false failure is exactly how a section earns its way into the
    # ignored pile — the one failure this whole checker exists to prevent.
    #
    # Bounded on purpose. A run still alive long past its grace is not in
    # flight, it is hung, and that IS a finding: otherwise a wedged job would be
    # tolerated forever and invisible, which is mode 2 wearing a live PID.
    #
    # Freshness is NOT skipped. An in-flight run whose last COMPLETION is days
    # old still reports late below, because "currently running" is not evidence
    # that anything has finished lately.
    in_flight = None
    if not done_re.search(last_body) and c.get("launchd"):
        pid = launchd_pid(c["launchd"])
        if pid:
            bound = (parse_interval(job.get("grace"))
                     or parse_interval(job.get("every"))
                     or IN_FLIGHT_MAX)
            if now - last_ts > bound:
                return Result(FAILED,
                              f"run {last_ts:%Y-%m-%d %H:%M} has been running "
                              f"{ago(last_ts, now)} (pid {pid}) — past its "
                              f"{human_dur(bound)} bound, so this is hung, not in flight",
                              last_done)
            in_flight = pid

    if not done_re.search(last_body) and in_flight is None:
        return Result(FAILED,
                      f"run {last_ts:%Y-%m-%d %H:%M} ({ago(last_ts, now)}) started and "
                      f"never completed — no `done` marker", last_done)

    # Mode 3: it finished, claimed success, and did the wrong thing. `expect` is
    # the positive half of `fail_if` — a pattern that MUST appear in a run that
    # actually did its job. A sync whose source directory moved finds nothing to
    # do, exits 0, logs no failure, and is indistinguishable from a healthy run
    # by every other check here. Only the job's author can say what its output
    # looks like when it worked, which is why this lives in the registry.
    if exp_re and in_flight is None and not exp_re.search(last_body):
        return Result(FAILED,
                      f"run {last_ts:%Y-%m-%d %H:%M} completed without "
                      f"{c.get('expect_what', 'its expected result')} — nothing in the "
                      f"run matches `{c['expect']}`", last_done)

    # A late log says the job stopped; the scheduler says why. Naming the cause
    # in the same line is the difference between a finding and a chore.
    if fresh.status == LATE and c.get("launchd"):
        cause = check_launchd({"check": {"kind": "launchd", "label": c["launchd"]}}, net, now)
        if cause.status != OK:
            fresh.detail += f" — and {cause.detail}"
    if in_flight is not None:
        fresh.detail += (f"; a run started {ago(last_ts, now)} is still in flight "
                         f"(pid {in_flight})")
    return fresh


def check_mtime(job, net, now):
    """Freshness of a produced artifact. Proof the job did its job, not that it ran."""
    c = job["check"]
    path = expand(c["path"])
    if not path.exists():
        return Result(UNKNOWN, f"artifact not found: {tilde(path)}")
    last = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return freshness(last, job, now, what="written", local=bool(c.get("launchd")))


def _gh(repo_dir, args):
    p = subprocess.run(["gh", *args], cwd=repo_dir, capture_output=True,
                       text=True, timeout=NET_TIMEOUT)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1:] or ["gh failed"])
    return json.loads(p.stdout)


RUN_FIELDS = "workflowName,conclusion,status,createdAt,databaseId,event"


def _gh_repo(net, repo):
    """Two calls answer most of a repo: every workflow's state, and recent runs."""
    d = expand(repo)

    def fetch():
        if not d.is_dir():
            raise RuntimeError(f"repo not found: {tilde(d)}")
        wfs = _gh(d, ["workflow", "list", "--all", "--json", "name,path,state"])
        runs = _gh(d, ["run", "list", "--limit", "200", "--json", RUN_FIELDS])
        return {"workflows": wfs, "runs": runs}

    return net.get(f"gh:{d}", fetch)


def _gh_workflow_runs(net, repo, wf_file):
    """Fallback for a workflow whose runs fall outside the repo-wide window — a
    chatty every-30-minutes job crowds a weekly one out of any shared page."""
    d = expand(repo)

    def fetch():
        return _gh(d, ["run", "list", "--workflow", wf_file, "--limit", "20",
                       "--json", RUN_FIELDS])

    return net.get(f"gh:{d}:{wf_file}", fetch)


def check_github_workflow(job, net, now):
    c = job["check"]
    try:
        data = _gh_repo(net, c["repo"])
    except Offline as e:
        return Result(UNKNOWN, str(e))
    except Exception as e:
        return Result(UNKNOWN, f"gh unavailable for {c['repo']}: {e}")

    wf = next((w for w in data["workflows"]
               if Path(w["path"]).name == c["workflow"]), None)
    if wf is None:
        return Result(FAILED,
                      f"workflow {c['workflow']} is not on the default branch any more "
                      f"— it cannot run at all")

    if wf["state"] != "active":
        return Result(FAILED,
                      f"workflow is {wf['state']} — it produces no runs and no errors, "
                      f"which is indistinguishable from health if nobody looks")

    runs = [r for r in data["runs"] if r["workflowName"] == wf["name"]]
    done = [r for r in runs if r["status"] == "completed"]
    if not done:
        try:
            runs = _gh_workflow_runs(net, c["repo"], c["workflow"])
            done = [r for r in runs if r["status"] == "completed"]
        except Offline as e:
            return Result(UNKNOWN, str(e))
        except Exception as e:
            return Result(UNKNOWN, f"gh could not list runs for {c['workflow']}: {e}")
    if not done:
        return Result(LATE, "the workflow is active but has never completed a run")

    last = done[0]
    when = dt.datetime.fromisoformat(last["createdAt"].replace("Z", "+00:00")) \
        .astimezone().replace(tzinfo=None)
    if last["conclusion"] not in ("success", "skipped"):
        streak = 0
        for r in done:
            if r["conclusion"] in ("success", "skipped"):
                break
            streak += 1
        return Result(FAILED,
                      f"last {streak} run(s) {last['conclusion']} — latest "
                      f"{when:%Y-%m-%d %H:%M} ({ago(when, now)}), run {last['databaseId']}",
                      when)
    if job.get("every") is None:
        return Result(OK, f"last run {last['conclusion']} {ago(when, now)} "
                          f"({when:%Y-%m-%d %H:%M})", when)
    return freshness(when, job, now, what="run")


_vercel_tok = None


def _epoch(v):
    """Vercel mixes seconds and milliseconds between fields (`expiresAt` is in
    seconds, `disabledAt` in milliseconds). Anything past ~1973-in-milliseconds
    is far beyond any plausible seconds value, so the magnitude tells them
    apart."""
    return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v)


def _vercel_token():
    """Read the CLI's token, refreshing it first if it has expired.

    `auth.json` holds an **8-hour OAuth access token** with an `expiresAt` beside
    it, not a static key. Reading the field directly works right up until it
    does not, and then every Vercel check degrades to `unknown` — a routine,
    silent failure of exactly the class this checker exists to catch. There is a
    `refreshToken` in the same file, but rather than hand-roll the OAuth
    exchange we let the CLI refresh its own file: `vercel whoami` is a ~1s call
    that rewrites `auth.json` as a side effect. No new secret is introduced, and
    if the refresh token has itself expired the CLI fails loudly, so `unknown`
    becomes honest rather than routine.
    """
    global _vercel_tok
    if _vercel_tok:
        return _vercel_tok

    def read():
        d = json.loads(VERCEL_AUTH.read_text())
        return d.get("token"), d.get("expiresAt")

    try:
        tok, exp = read()
    except Exception as e:
        raise RuntimeError(f"no Vercel CLI auth ({tilde(VERCEL_AUTH)}): {e}")
    # No `expiresAt` at all means an older CLI wrote the file; trust the token
    # and let a 403 be the honest answer.
    if tok and (exp is None or _epoch(exp) - VERCEL_TOKEN_MARGIN > _now()):
        _vercel_tok = tok
        return tok

    # launchd hands jobs a bare /usr/bin:/bin PATH, so the Homebrew install is
    # invisible there — and the CLI's `#!/usr/bin/env node` shebang needs node on
    # the child's PATH too, not just a resolvable `vercel`.
    search = os.pathsep.join([os.environ.get("PATH", ""), *VERCEL_CLI_DIRS])
    cli = shutil.which("vercel", path=search)
    if not cli:
        raise RuntimeError("Vercel token expired and the `vercel` CLI is not on PATH "
                           "to refresh it")
    try:
        p = subprocess.run([cli, "whoami"], capture_output=True, text=True,
                           timeout=NET_TIMEOUT, env={**os.environ, "PATH": search})
    except Exception as e:
        raise RuntimeError(f"Vercel token expired and `vercel whoami` could not run: {e}")
    try:
        tok, exp = read()
    except Exception as e:
        raise RuntimeError(f"no Vercel CLI auth ({tilde(VERCEL_AUTH)}): {e}")
    if p.returncode != 0 or not tok or (exp is not None and _epoch(exp) <= _now()):
        why = ((p.stderr or p.stdout).strip().splitlines() or ["no reason given"])[-1]
        raise RuntimeError(f"Vercel token expired and could not be refreshed: {why}")
    _vercel_tok = tok
    return tok


def _vercel_api(path, team_id=None):
    tok = _vercel_token()
    url = f"https://api.vercel.com{path}"
    if team_id:
        url += ("&" if "?" in url else "?") + f"teamId={team_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
        return json.load(r)


def _vercel_projects(net, team_slug):
    def fetch():
        teams = _vercel_api("/v2/teams").get("teams", [])
        tid = next((t["id"] for t in teams if t.get("slug") == team_slug), None)
        if tid is None:
            raise RuntimeError(f"no Vercel team with slug {team_slug!r}")
        d = _vercel_api("/v9/projects?limit=100", tid)
        return {p["name"]: p.get("crons") for p in d.get("projects", [])}

    return net.get(f"vercel:{team_slug}", fetch)


def _http_status(net, url):
    """Status code of a plain GET, cached like any other network answer."""
    def fetch():
        req = urllib.request.Request(url, headers={"User-Agent": "jobwatch"})
        try:
            with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
                return {"code": r.status}
        except urllib.error.HTTPError as e:
            return {"code": e.code}
        except Exception as e:
            return {"code": None, "error": f"{type(e).__name__}: {e}"}

    return net.get(f"http:{url}", fetch)


def check_vercel_cron(job, net, now):
    """What Vercel's API can and cannot tell you about a cron.

    It exposes the cron's *scheduled* state — enabled, present in the live
    deployment, and on what schedule. It exposes **no run history at all**: there
    is no public endpoint for cron invocations, and the deployment `events`
    endpoint returns build logs only, so a cron that 500s nightly leaves nothing
    this checker can read. Three things narrow that gap without inventing a
    second alerting system:

      * mode 2 — crons switched off, or a `vercel.json` path that never reached
        the live deployment;
      * `schedule` — the deployed cron expression against the one the registry
        expects, because an edit from `0 3 * * *` to `0 3 1 * *` turns a nightly
        job monthly and every other check here stays green;
      * `reachable` — a GET on a public, non-mutating URL of the same app. It
        catches the app being gone (404), broken at runtime (5xx) or
        billing-suspended (402). A suspended site can serve 402 for weeks with
        nothing else to say so. It cannot see inside a run that answered 200.

    The probe is never pointed at the cron path itself. Those endpoints mutate,
    and some guard on an env var that may be unset (`if (CRON_SECRET && …)`), so
    a probe could execute the very purge it is meant to be watching. Run
    outcomes proper stay with Vercel's own logs, which every `fix` links to.
    """
    c = job["check"]
    try:
        projects = _vercel_projects(net, c["team"])
    except Offline as e:
        return Result(UNKNOWN, str(e))
    except Exception as e:
        return Result(UNKNOWN, f"Vercel API unavailable: {e}")

    if c["project"] not in projects:
        return Result(FAILED, f"no Vercel project named {c['project']!r} in team {c['team']}")
    crons = projects[c["project"]]
    if not crons or not crons.get("definitions"):
        return Result(FAILED,
                      f"{c['project']} has no crons in its live deployment — "
                      f"vercel.json declares {c['path']}, the deployment carries none")
    if crons.get("disabledAt"):
        when = _epoch(crons["disabledAt"])
        return Result(FAILED, f"crons disabled {when:%Y-%m-%d} ({ago(when, now)}) — "
                              f"{c['path']} is not firing")
    live = next((d for d in crons["definitions"] if d["path"] == c["path"]), None)
    if live is None:
        paths = [d["path"] for d in crons["definitions"]]
        return Result(FAILED,
                      f"{c['path']} is declared in vercel.json but absent from the live "
                      f"cron set ({', '.join(paths)}) — the deploy carrying it never shipped")
    if c.get("schedule") and live.get("schedule") != c["schedule"]:
        return Result(FAILED,
                      f"{c['path']} runs on `{live.get('schedule')}` in the live "
                      f"deployment, not the `{c['schedule']}` this registry expects — "
                      f"a schedule edit changes the frequency silently")

    state = f"cron deployed and enabled on `{live.get('schedule')}`"
    if c.get("reachable"):
        try:
            probe = _http_status(net, c["reachable"])
        except Offline:
            return Result(UNKNOWN, f"{state}; {c['reachable']} not probed (offline)")
        ok_codes = c.get("reachable_ok") or [200]
        if probe.get("code") is None:
            return Result(FAILED,
                          f"{state}, but {c['reachable']} did not answer "
                          f"({probe.get('error')}) — the cron fires into an app that is "
                          f"not there")
        if probe["code"] not in ok_codes:
            return Result(FAILED,
                          f"{state}, but {c['reachable']} answered "
                          f"{probe['code']} (expected {'/'.join(str(x) for x in ok_codes)}) "
                          f"— the cron fires into an app that is not serving")
        state += f"; {c['reachable']} answers {probe['code']}"
    return Result(OK, state)


# A run still going and a run that died leave the identical shape on disk: a
# start marker, no trailer. Ceiling on how long "still going" stays credible
# when the registry entry gives no better bound.
IN_FLIGHT_MAX = dt.timedelta(hours=1)

_pid_cache = {}


def launchd_pid(label):
    """PID of a currently-running launchd job, or None. `launchctl list` prints
    PID<TAB>Status<TAB>Label, with `-` in the PID column when nothing is running.
    """
    if label in _pid_cache:
        return _pid_cache[label]
    pid = None
    try:
        p = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        for line in p.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) >= 3 and cols[-1] == label and cols[0].strip().isdigit():
                pid = int(cols[0])
                break
    except Exception:
        pid = None
    _pid_cache[label] = pid
    return pid


def check_launchd(job, net, now):
    """Is the agent even loaded? A plist on disk that launchd does not know about
    is the purest mode-2 failure: the file looks installed and nothing runs.
    """
    c = job["check"]
    label = c["label"]
    plist = expand(c.get("plist", f"~/Library/LaunchAgents/{label}.plist"))
    if not plist.exists():
        return Result(FAILED, f"plist missing: {tilde(plist)}")
    try:
        p = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
    except Exception as e:
        return Result(UNKNOWN, f"launchctl unavailable: {e}")
    if not any(line.endswith("\t" + label) or line.split("\t")[-1] == label
               for line in p.stdout.splitlines()):
        return Result(FAILED,
                      f"{label} is not loaded in launchd — the plist is on disk and "
                      f"nothing is running it")
    return Result(OK, "agent loaded")


# `pmset -g sched` is a ~0.1s local call; cache it like the sleep log so a
# process asking twice pays once. None = not read yet.
_pmset_sched = None


def pmset_sched():
    global _pmset_sched
    if _pmset_sched is None:
        try:
            p = subprocess.run(["pmset", "-g", "sched"], capture_output=True,
                               text=True, timeout=PMSET_TIMEOUT)
            _pmset_sched = p.stdout
        except Exception:
            _pmset_sched = ""
    return _pmset_sched


# pmset prints `wakeorpoweron` back as `wakepoweron`; both are wake-class, and
# a plain `wake` also satisfies "the Mac is awake when the job fires".
_WAKE_EVENTS = ("wakepoweron", "wakeorpoweron", "wake")


def check_pmset_repeat(job, net, now):
    """Is the daily scheduled wake still armed?

    Not a job that does work — a *precondition* six jobs depend on. Without it
    launchd fires them inside a 10-45s maintenance DarkWake and they fail as
    DNS/OAuth/TLS/git errors that each name a different, wrong root cause (see
    README, "Jobs that fire in a DarkWake"). macOS keeps exactly one repeat
    schedule: an OS update, an NVRAM reset, a migration to a new Mac, or one
    stray `pmset repeat cancel` clears it, and NOTHING says so. Every one of the
    six then quietly goes back to failing the way it did on 2026-08-27.

    That is the purest mode-2 shape in this registry: the absence of the thing
    produces no error at all, only the return of six misleading ones.
    """
    c = job["check"]
    out = pmset_sched()
    if not out:
        return Result(UNKNOWN, "pmset unavailable — cannot tell whether the wake is armed")

    body = out.split("Repeating power events:", 1)
    if len(body) == 1:
        return Result(FAILED,
                      "no repeating power event at all — the daily scheduled wake is "
                      "gone, so every calendar job is back to firing inside whatever "
                      "DarkWake happens to be current")
    # The repeat block ends at the one-off list, or at the end of the output.
    block = body[1].split("Scheduled power events:", 1)[0]
    lines = [l.strip() for l in block.splitlines() if l.strip()]

    wake = next((l for l in lines
                 if l.split(" ", 1)[0].lower() in _WAKE_EVENTS), None)
    if wake is None:
        return Result(FAILED,
                      f"a repeat schedule exists but none of it is a wake: "
                      f"{'; '.join(lines) or 'empty'} — macOS keeps only ONE repeat "
                      f"schedule, so setting a sleep repeat silently replaced the wake")

    at = c.get("at")
    if at and at.lower().replace(" ", "") not in wake.lower().replace(" ", ""):
        return Result(FAILED,
                      f"scheduled wake moved: expected {at}, `pmset -g sched` says "
                      f"`{wake}`. The jobs it protects run 07:15-08:00, so a wake "
                      f"that drifted past them protects nothing")

    days = c.get("days")
    if days and days.lower() not in wake.lower():
        return Result(FAILED,
                      f"scheduled wake now runs `{wake}`, not `{days}` — the days it "
                      f"stopped covering are days the jobs fire into a DarkWake")

    return Result(OK, f"scheduled wake armed: {wake}")


def check_self(job, net, now, results=None):
    """Who watches the watcher.

    The checker cannot go stale independently — it runs on demand from whatever
    surface reads it, so "the checker stopped" and "the surface stopped" are the
    same event, and the surface is responsible for saying so (see README).
    What *can* rot silently is the registry: a log that moved, a workflow that
    was renamed, a `gh` that lost its auth. Every one of those degrades a real
    check to `unknown` while the surface stays quiet. So the self-check is:
    how many registered jobs could not be evaluated at all?
    """
    blind = [r for r in (results or []) if r[1].status == UNKNOWN]
    if blind:
        by_reason = {}
        for jid, res in blind:
            by_reason.setdefault(res.detail.split(":")[0][:60], []).append(jid)
        parts = "; ".join(f"{r} ({', '.join(ids)})" for r, ids in by_reason.items())
        return Result(FAILED,
                      f"{len(blind)} of {len(results)} registered job(s) could not be "
                      f"evaluated — this checker is partly blind: {parts}")
    return Result(OK, f"all {len(results or [])} registered job(s) evaluated")


KINDS = {
    "log_block": check_log_block,
    "mtime": check_mtime,
    "github_workflow": check_github_workflow,
    "vercel_cron": check_vercel_cron,
    "launchd": check_launchd,
    "pmset_repeat": check_pmset_repeat,
    "self": check_self,
}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
REQUIRED = ("id", "what", "trigger", "notices", "fix", "check")


def load_registry(path=REGISTRY):
    data = json.loads(Path(path).read_text())
    jobs = data["jobs"]
    seen = set()
    for j in jobs:
        missing = [k for k in REQUIRED if not j.get(k)]
        if missing:
            raise SystemExit(f"registry: job {j.get('id', '?')} missing {', '.join(missing)}")
        if j["id"] in seen:
            raise SystemExit(f"registry: duplicate id {j['id']}")
        seen.add(j["id"])
        if j["check"]["kind"] not in KINDS:
            raise SystemExit(f"registry: {j['id']} has unknown check kind {j['check']['kind']}")
        # `fix` is required because a check that cannot say what a human would do
        # about it should not exist — it only trains the eye to skip the surface.
        parse_interval(j.get("every"))
        parse_interval(j.get("grace"))
    return jobs


def run_all(jobs, net, now, only=None):
    results, deferred = [], []
    for j in jobs:
        if only and j["id"] not in only:
            continue
        if j["check"]["kind"] == "self":
            deferred.append(j)
            continue
        try:
            res = KINDS[j["check"]["kind"]](j, net, now)
        except Exception as e:  # a broken check must not take the surface down
            res = Result(UNKNOWN, f"check raised {type(e).__name__}: {e}")
        results.append((j, res))
    for j in deferred:
        res = check_self(j, net, now, [(job["id"], r) for job, r in results])
        results.append((j, res))
    return results


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_check(args):
    jobs = load_registry(args.registry)
    if getattr(args, "audience", None):
        jobs = [j for j in jobs if j.get("audience") == args.audience]
    net = Net(offline=args.offline, use_cache=not args.no_cache)
    now = _now()
    results = run_all(jobs, net, now, only=set(i.strip() for i in args.only.split(",")) if args.only else None)
    net.flush()

    bad = [(j, r) for j, r in results if r.status != OK]
    bad.sort(key=lambda x: (SEVERITY[x[1].status], x[0]["id"]))

    if args.json:
        print(json.dumps({
            "checked_at": now.isoformat(timespec="seconds"),
            "total": len(results),
            "healthy": len(results) - len(bad),
            "findings": [{"id": j["id"], "status": r.status, "what": j["what"],
                          "detail": r.detail, "fix": j["fix"],
                          "audience": j.get("audience", "")} for j, r in bad],
        }, indent=1))
        return 0

    if not bad:
        return 0  # silent when healthy — absence of output is the signal

    counts = {}
    for _, r in bad:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = ", ".join(f"{n} {s}" for s, n in
                        sorted(counts.items(), key=lambda kv: SEVERITY[kv[0]]))
    print(f"## Automation health — {summary} of {len(results)} registered job(s)")
    for j, r in bad:
        print(f"  {MARK[r.status]} {j['id']} — {j['what']}")
        print(f"      {r.detail}")
        print(f"      fix: {j['fix']}")
    return 1


def cmd_list(args):
    jobs = load_registry(args.registry)
    if getattr(args, "audience", None):
        jobs = [j for j in jobs if j.get("audience") == args.audience]
    net = Net(offline=args.offline, use_cache=not args.no_cache)
    now = _now()
    results = run_all(jobs, net, now, only=set(i.strip() for i in args.only.split(",")) if args.only else None)
    net.flush()
    print(f"# Registered automated jobs — {now:%Y-%m-%d %H:%M}\n")
    for j, r in results:
        print(f"{MARK[r.status]} {j['id']}  [{r.status}]")
        print(f"    what      {j['what']}")
        print(f"    trigger   {j['trigger']}")
        print(f"    expected  every {j.get('every') or '(no interval — outcome only)'}"
              + (f" (+{j['grace']} grace)" if j.get("grace") else ""))
        print(f"    proof     {j.get('proof', j['check']['kind'])}")
        print(f"    notices   {j['notices']}")
        print(f"    state     {r.detail}")
        print()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--registry", default=str(REGISTRY))
    ap.add_argument("--offline", action="store_true",
                    help="skip network checks; they degrade to unknown")
    ap.add_argument("--no-cache", action="store_true", help="bypass the response cache")
    ap.add_argument("--only", help="comma-separated job ids to check, e.g. "
                                   "--only status-sync,watch-self")
    ap.add_argument("--audience", help="only jobs with this audience, e.g. "
                                       "--audience personal. Every job carries one; "
                                       "the surface that shows a job is the surface "
                                       "its owner reads, and a job shown nowhere is "
                                       "the failure this tool exists to prevent.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="silent when healthy, exit 1 when not")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)
    l = sub.add_parser("list", help="the full inventory, healthy or not")
    l.set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
