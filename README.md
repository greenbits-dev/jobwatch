# jobwatch

**Does everything that is supposed to run, still run?**

One read-only checker over a registry of scheduled jobs: launchd agents, cron
scripts, GitHub Actions workflows, Vercel crons. It is **silent when healthy**.
`jobwatch.py check` prints nothing and exits 0 when every registered job is fine,
and prints findings and exits 1 when one is late, failed, or cannot be evaluated.

Single file, Python standard library only (tests pass on 3.9, 3.12, 3.13 and 3.14). Built for one person running
dozens of AI-agent jobs from a laptop, where "nothing broke" and "nothing ran"
look exactly the same.

```bash
cp jobs.example.json jobs.json      # then replace the examples with your jobs
python3 jobwatch.py check           # silent when healthy, findings + exit 1 when not
python3 jobwatch.py list            # the full inventory, healthy or not
python3 jobwatch.py check --json    # for a dashboard or a morning brief; always exits 0
python3 jobwatch.py --offline check # no network; those checks degrade to unknown
python3 test_jobwatch.py            # hermetic tests: no network, no real jobs needed
```

## The idea in four rules

1. **Freshness first.** Ask "when did this last *complete*?" before "did it error?"
2. **Every job has a trailer.** The runner writes a start line, and a done line
   only *after* the work exits. A run that started and died looks different from
   one that finished.
3. **Every entry has a `fix`.** The loader rejects a job without one. A check that
   cannot say what a human should do about it must not exist.
4. **One surface, silent when healthy.** An "all green" wall of text every morning
   trains you to skip it, and a surface you skip is not a surface.

## Why freshness first

A scheduled job fails in three ways, and only the first leaves an error:

1. **Ran, failed, and said so into the void.** A sync wrote `msg_too_long` to its
   log on four consecutive hourly runs. Nobody read the log, and a status page
   stayed ten hours stale.
2. **Stopped running entirely.** Five workflows were switched off by hand, and no
   log anywhere said so, because nothing ran. **Error-based monitoring cannot see
   this**: no errors looks exactly like success.
3. **Ran, reported success, and did the wrong thing.** A sync whose source folder
   moved found nothing to do, exited 0 and logged no failure.

Mode 2 is why every check is a freshness check first. Mode 3 needs an assertion
about the result, which only the job's author can write, so `log_block` has two
hooks that work in opposite directions:

- **`fail_if`**: a pattern that must be **absent**. One `FAILED <target>` line
  fails an otherwise successful run.
- **`expect`**: a pattern that must be **present**, describing what the log looks
  like when the job actually did something (`uploaded <file>`,
  `synced <x> -> <y>`).

**Validate an `expect` pattern against the whole log history before you ship it.**
Read every miss. A pattern that fires on a healthy run, such as a day with nothing
to commit, teaches you to ignore the surface, and that is the failure this tool
exists to prevent.

## The runner convention

[`examples/run-job.sh`](examples/run-job.sh) is the whole convention:

```
=== 2026-10-12 02:30:00 nightly-backup start ===
  uploaded notes.tar.gz
=== 2026-10-12 02:31:12 nightly-backup done rc=0 ===
```

Never `exec` the payload from the runner. After `exec`, nothing is left to write
the trailer. Existing jobs can keep their own markers, because each registry entry
carries its own regexes. The convention exists so a *new* job needs config, not
code.

## Laptops: sleep and DarkWake

Two macOS behaviors produced most of the false findings this tool has had, so
both are handled.

**Lateness is measured in awake time.** A launchd `StartInterval` job only fires
while the machine is awake, so a healthy hourly job reads "8h late" every morning.
For a job whose entry names a `launchd` label, jobwatch subtracts the sleep that
`pmset -g log` records. It does this only for a job that would otherwise be
reported late, so a healthy run never pays the ~2s cost. A job that genuinely
stopped is still late, and the finding states the discount.

**Jobs that fire in a DarkWake report false causes.** launchd runs a calendar job
in whatever wake is current, which on a laptop is often a 10-45 second
maintenance wake. The job is suspended mid-flight, and the break shows up as
whichever call was in progress:

| in flight when it slept | what the log said | what it reads as |
|---|---|---|
| DNS lookup | `nodename nor servname provided` | the host is gone |
| OAuth refresh | `token_valid=false`, no error | the token was revoked |
| TLS handshake | `no native root CA certificates found` | the trust store is corrupt |
| git transfer | `Operation too slow` | the remote is down |

Four wrong root causes, one real cause. Two days went into chasing a token that
was valid the whole time. The runner therefore re-execs itself under
`caffeinate -i`, and a real scheduled wake (`sudo pmset repeat ...`) covers the
start. macOS keeps exactly one repeat schedule, and an OS update can clear it
silently, so the wake is registered as a job too (`pmset_repeat`). **Register the
thing your jobs silently depend on, not just the jobs.**

## Check kinds

| `kind` | Catches | Fields |
|---|---|---|
| `log_block` | modes 1, 2, 3 | `path`, `start` (must capture `(?P<ts>…)`), `done`, optional `fail_if`, `expect` + `expect_what`, `ts_format`, `launchd` |
| `mtime` | mode 2, via an artifact's freshness | `path`, optional `launchd` |
| `github_workflow` | modes 1, 2, including `disabled_manually`, which GitHub never alerts on | `repo` (local clone), `workflow` (file name) |
| `vercel_cron` | mode 2: cron switched off, a path that never reached the live deployment, a schedule quietly edited, an app that stopped answering | `team`, `project`, `path`, optional `schedule`, `reachable` + `reachable_ok` |
| `launchd` | mode 2: a plist on disk that launchd does not know about | `label`, optional `plist` |
| `pmset_repeat` | mode 2 on a precondition: the scheduled wake other jobs need | optional `at`, `days` |
| `self` | the registry going blind | (none) |

`every` is the longest acceptable gap between completions and `grace` is slack on
top of it. Set `every` to `null` for a job with no interval (push-triggered CI):
only its outcome is checked, because silence is legitimate there.

`audience` is optional. `--audience me` filters to one owner's jobs, so each
surface shows the jobs its reader can act on.

## Who watches the watcher

The checker runs only when a surface asks it to, so "the checker stopped" and "the
surface stopped" are the same event. Whatever surface renders it should show a
checker that failed to run as a warning, never as nothing.

What can rot silently is the registry. A log moves, a workflow is renamed, `gh`
loses its auth, and each of those quietly turns a real check into `unknown` while
the surface stays quiet. The `self` check fails when any other job could not be
evaluated, because a partly blind checker looks just like a working one.

If everything runs on one laptop, add one check that runs somewhere else, such as
a scheduled GitHub Action that fails when your dashboard has not been updated in
36 hours. A closed lid otherwise produces perfect silence.

## Boundaries

- **Read-only.** It never restarts, re-enables or repairs a job. A repair loop that
  misfires at 3am is worse than the failure it fixes. The only file it writes is
  `cache.json`. The one non-read it runs is `vercel whoami`, which refreshes the
  Vercel CLI's own 8-hour token in place.
- **No secrets in the registry.** Paths and endpoints only. Credentials stay in the
  `gh` and `vercel` CLIs' own auth stores.
- **Network answers are cached for 3 hours**, so a morning brief stays fast. Use
  `--no-cache` when debugging.
- **It does not duplicate GitHub's or Vercel's alerts.** It checks what their
  alerting cannot report: a workflow switched off, or a cron that never reached a
  deployment. Vercel exposes no API for cron run outcomes, so a cron that returns
  500 nightly is still only visible in Vercel's logs.
- **`reachable` is never pointed at a cron path.** Cron endpoints mutate, and some
  guard on an env var that may be unset, so a probe could run the very job it is
  watching.
- **macOS-specific parts** (`launchd`, `pmset`) degrade to `unknown` elsewhere. The
  log, mtime, GitHub and Vercel checks are portable.

## License

MIT. See [LICENSE](LICENSE).
