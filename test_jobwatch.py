#!/usr/bin/env python3
"""Tests for jobwatch.py.

Deliberately hermetic: every test builds its own log fixture in a tmpdir and
uses a fixture registry. Nothing here reads a private repo, hits the network, or
depends on this machine's real jobs — the checker has to be testable on a clone
that has none of them.

Run: python3 test_jobwatch.py
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobwatch as watch  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"

# Captured before any test swaps them out, so a failing test cannot leave the
# module monkeypatched for the next one.
_real_vercel_projects = watch._vercel_projects
_real_http_status = watch._http_status
_real_auth_path = watch.VERCEL_AUTH


def job(**over):
    j = {
        "id": "fixture", "what": "a fixture job", "trigger": "test",
        "notices": "the test", "fix": "nothing — this is a fixture",
        "every": "1h", "grace": "1h",
        "check": {
            "kind": "log_block", "path": None,
            "start": r"^=== (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) run ===",
            "done": r"^=== \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} done rc=0 ===",
            "fail_if": r"^\s*FAILED \S+",
        },
    }
    check = over.pop("check", None)
    j.update(over)
    if check:
        j["check"].update(check)
    return j


def write_log(path, runs):
    """runs: list of (datetime, body_lines, completed)."""
    out = []
    for when, body, completed in runs:
        out.append(f"=== {when:{FMT}} run ===")
        out.extend(body)
        if completed:
            out.append(f"=== {when + dt.timedelta(seconds=30):{FMT}} done rc=0 ===")
    Path(path).write_text("\n".join(out) + "\n")


class LogBlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "sync.log"
        self.now = dt.datetime(2026, 7, 30, 12, 0, 0)
        self.net = watch.Net(offline=True, use_cache=False)
        self.addCleanup(self.tmp.cleanup)

    def run_check(self, j):
        j["check"]["path"] = str(self.log)
        return watch.check_log_block(j, self.net, self.now)

    def test_recent_completed_run_is_ok(self):
        write_log(self.log, [(self.now - dt.timedelta(minutes=20), ["  did a thing"], True)])
        self.assertEqual(self.run_check(job()).status, watch.OK)

    def test_stale_completed_run_is_late(self):
        """Mode 2 — the job stopped. No error exists anywhere; only the gap shows it."""
        write_log(self.log, [(self.now - dt.timedelta(days=2), ["  did a thing"], True)])
        r = self.run_check(job())
        self.assertEqual(r.status, watch.LATE)
        self.assertIn("expected every 1h", r.detail)

    def test_started_and_never_finished_is_failed(self):
        """The 2026-07-13 weekly-insights case: a start marker, no done marker."""
        write_log(self.log, [(self.now - dt.timedelta(minutes=10), ["  half a thing"], False)])
        r = self.run_check(job())
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("never completed", r.detail)

    def test_fail_if_beats_a_completed_run(self):
        """Mode 1 — a multi-channel sync. The run 'completed'; one channel did not."""
        write_log(self.log, [(self.now - dt.timedelta(minutes=10),
                              ["  FAILED client-a -> C0B: msg_too_long"], True)])
        r = self.run_check(job())
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("client-a", r.detail)

    def test_last_run_failed_but_an_older_one_succeeded(self):
        write_log(self.log, [
            (self.now - dt.timedelta(hours=3), ["  fine"], True),
            (self.now - dt.timedelta(minutes=5), ["  FAILED x -> y: boom"], True),
        ])
        r = self.run_check(job())
        self.assertEqual(r.status, watch.FAILED)
        self.assertIsNotNone(r.last)  # still reports the last good completion

    def test_missing_log_is_unknown_not_an_error(self):
        r = self.run_check(job())
        self.assertEqual(r.status, watch.UNKNOWN)
        self.assertIn("log not found", r.detail)

    def test_start_pattern_that_no_longer_matches_is_unknown(self):
        """A registry that has drifted from what the job writes is a blind check,
        and must say so rather than reading as healthy."""
        self.log.write_text("nothing that looks like a run marker\n")
        r = self.run_check(job())
        self.assertEqual(r.status, watch.UNKNOWN)
        self.assertIn("no longer matches", r.detail)

    def test_no_interval_means_freshness_is_not_checked(self):
        write_log(self.log, [(self.now - dt.timedelta(days=400), ["  fine"], True)])
        self.assertEqual(self.run_check(job(every=None, grace=None)).status, watch.OK)


class PmsetRepeat(unittest.TestCase):
    """The scheduled wake is a precondition, not a job, and its absence is silent."""

    ARMED = ("Repeating power events:\n"
             "  wakepoweron at 7:10AM every day\n"
             "Scheduled power events:\n"
             " [0]  wake at 08/28/2026 00:10:20 by 'com.apple.alarm'\n")

    def setUp(self):
        self.now = dt.datetime(2026, 8, 27, 13, 0, 0)
        self.net = watch.Net(offline=True, use_cache=False)
        self.addCleanup(setattr, watch, "_pmset_sched", None)

    def run_check(self, sched, **over):
        watch._pmset_sched = sched
        j = job(check={"kind": "pmset_repeat", "at": "7:10AM", "days": "every day"})
        j["check"].update(over)
        return watch.check_pmset_repeat(j, self.net, self.now)

    def test_armed_is_ok(self):
        r = self.run_check(self.ARMED)
        self.assertEqual(r.status, watch.OK)
        self.assertIn("7:10AM", r.detail)

    def test_no_repeat_block_at_all_is_a_finding(self):
        """An NVRAM reset or a stray `pmset repeat cancel` leaves exactly this."""
        r = self.run_check("Scheduled power events:\n [0]  wake at 08/28/2026 by 'x'\n")
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("no repeating power event", r.detail)

    def test_a_sleep_repeat_that_replaced_the_wake_is_a_finding(self):
        """macOS keeps ONE repeat schedule — setting a sleep silently drops the wake."""
        r = self.run_check("Repeating power events:\n  sleep at 11:00PM every day\n")
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("none of it is a wake", r.detail)

    def test_a_wake_that_drifted_past_the_jobs_is_a_finding(self):
        r = self.run_check("Repeating power events:\n  wakepoweron at 9:30AM every day\n")
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("moved", r.detail)

    def test_days_narrowing_is_a_finding(self):
        """Weekdays-only means the weekend jobs are back in the dark."""
        r = self.run_check("Repeating power events:\n  wakepoweron at 7:10AM weekdays only\n")
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("weekdays only", r.detail)

    def test_plain_wake_also_satisfies_it(self):
        """`wake` and `wakeorpoweron` both leave the Mac awake when the job fires."""
        r = self.run_check("Repeating power events:\n  wake at 7:10AM every day\n")
        self.assertEqual(r.status, watch.OK)

    def test_unreadable_pmset_is_unknown_not_ok(self):
        self.assertEqual(self.run_check("").status, watch.UNKNOWN)


class InFlight(unittest.TestCase):
    """A start with no trailer: died, or still running. Only one is a finding.

    Before this, an hourly job whose runs take minutes reported FAILED every
    time a surface happened to read the log mid-run. A section that cries wolf
    hourly is a section you stop reading, which is the failure the whole
    checker exists to prevent.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "sync.log"
        self.now = dt.datetime(2026, 7, 30, 12, 0, 0)
        self.net = watch.Net(offline=True, use_cache=False)
        self.addCleanup(self.tmp.cleanup)
        watch._pid_cache.clear()
        self.addCleanup(watch._pid_cache.clear)

    def run_check(self, j, pid):
        j["check"]["path"] = str(self.log)
        j["check"]["launchd"] = "com.example.job"
        watch._pid_cache["com.example.job"] = pid
        return watch.check_log_block(j, self.net, self.now)

    def test_live_pid_inside_grace_is_not_a_finding(self):
        write_log(self.log, [
            (self.now - dt.timedelta(minutes=70), ["  ok"], True),
            (self.now - dt.timedelta(minutes=4), ["  working"], False),
        ])
        r = self.run_check(job(every="1h", grace="3h"), pid=4242)
        self.assertEqual(r.status, watch.OK)
        self.assertIn("in flight", r.detail)

    def test_no_pid_is_still_a_finding(self):
        """The 2026-07-13 weekly-insights case: started, died, no trailer."""
        write_log(self.log, [(self.now - dt.timedelta(minutes=4), ["  working"], False)])
        r = self.run_check(job(every="1h", grace="3h"), pid=None)
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("never completed", r.detail)

    def test_live_pid_past_grace_is_hung_not_in_flight(self):
        """Otherwise a wedged job is tolerated forever: mode 2 wearing a PID."""
        write_log(self.log, [(self.now - dt.timedelta(hours=5), ["  working"], False)])
        r = self.run_check(job(every="1h", grace="3h"), pid=4242)
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("hung", r.detail)

    def test_in_flight_does_not_mask_staleness(self):
        """Currently running is not evidence that anything finished lately."""
        write_log(self.log, [
            (self.now - dt.timedelta(days=4), ["  ok"], True),
            (self.now - dt.timedelta(minutes=4), ["  working"], False),
        ])
        r = self.run_check(job(every="1h", grace="3h"), pid=4242)
        self.assertEqual(r.status, watch.LATE)

    def test_expect_is_not_judged_on_a_half_written_run(self):
        """The summary line is written last; absence mid-run proves nothing."""
        write_log(self.log, [
            (self.now - dt.timedelta(minutes=70), ["  synced a -> b"], True),
            (self.now - dt.timedelta(minutes=4), ["  working"], False),
        ])
        j = job(every="1h", grace="3h",
                check={"expect": r"^\s*synced \S+ -> ", "expect_what": "a sync"})
        self.assertEqual(self.run_check(j, pid=4242).status, watch.OK)

    def test_fail_if_still_fires_on_an_in_flight_run(self):
        """A run that has already logged a failure is a finding now, not later."""
        write_log(self.log, [(self.now - dt.timedelta(minutes=4), ["  FAILED gmail"], False)])
        self.assertEqual(self.run_check(job(every="1h", grace="3h"), pid=4242).status,
                         watch.FAILED)


class Expect(unittest.TestCase):
    """Mode 3 — the run finished, claimed success, and did the wrong thing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "sync.log"
        self.now = dt.datetime(2026, 7, 30, 12, 0, 0)
        self.net = watch.Net(offline=True, use_cache=False)
        self.addCleanup(self.tmp.cleanup)

    def run_check(self, j):
        j["check"]["path"] = str(self.log)
        return watch.check_log_block(j, self.net, self.now)

    def test_a_run_that_produced_the_result_is_ok(self):
        write_log(self.log, [(self.now - dt.timedelta(minutes=10),
                              ["  synced client-b -> C0B"], True)])
        r = self.run_check(job(check={"expect": r"^\s*synced \S+ -> "}))
        self.assertEqual(r.status, watch.OK)

    def test_a_run_that_produced_nothing_is_failed(self):
        """The whole point: exit 0, no error line, and no work done. Every other
        check in this file reads that run as healthy."""
        write_log(self.log, [(self.now - dt.timedelta(minutes=10),
                              ["  nothing to do"], True)])
        r = self.run_check(job(check={"expect": r"^\s*synced \S+ -> ",
                                      "expect_what": "updating a single channel"}))
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("updating a single channel", r.detail)

    def test_fail_if_is_reported_before_expect(self):
        """A run where everything failed also matches no `expect`. The named
        failure is the more useful finding, so it must win."""
        write_log(self.log, [(self.now - dt.timedelta(minutes=10),
                              ["  FAILED client-b -> C0B: boom"], True)])
        r = self.run_check(job(check={"expect": r"^\s*synced \S+ -> "}))
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("client-b", r.detail)

    def test_no_expect_configured_changes_nothing(self):
        write_log(self.log, [(self.now - dt.timedelta(minutes=10), ["  whatever"], True)])
        self.assertEqual(self.run_check(job()).status, watch.OK)


class Sleep(unittest.TestCase):
    """Lateness on a laptop. `launchd` StartInterval only counts awake time, so
    a machine that slept through the night reports healthy jobs as late — and a
    surface that cries wolf every morning is one you stop reading."""

    def setUp(self):
        self.now = dt.datetime(2026, 7, 31, 9, 20, 0)
        self.last = dt.datetime(2026, 7, 31, 0, 49, 0)  # 8h31m of wall clock
        # 00:50 → 09:10 asleep, and the log covers well before `last`.
        watch._sleep_log = ([(dt.datetime(2026, 7, 31, 0, 50),
                              dt.datetime(2026, 7, 31, 9, 10))],
                            dt.datetime(2026, 7, 24, 0, 0))
        self.addCleanup(setattr, watch, "_sleep_log", None)

    def test_a_slept_through_gap_is_not_late(self):
        r = watch.freshness(self.last, job(), self.now, local=True)
        self.assertEqual(r.status, watch.OK)
        self.assertIn("asleep", r.detail)

    def test_the_same_gap_is_late_for_a_job_not_on_this_clock(self):
        """A GitHub or Vercel schedule does not care that this laptop slept."""
        self.assertEqual(watch.freshness(self.last, job(), self.now, local=False).status,
                         watch.LATE)

    def test_a_job_that_actually_stopped_is_still_late(self):
        """The discount must not swallow a real finding."""
        r = watch.freshness(dt.datetime(2026, 7, 28, 9, 0), job(), self.now, local=True)
        self.assertEqual(r.status, watch.LATE)

    def test_a_partial_power_log_says_so(self):
        """`pmset` keeps about a week. Past that we can only under-discount, and
        a finding that admits it beats a confident wrong number."""
        watch._sleep_log = ([(dt.datetime(2026, 7, 31, 0, 50),
                              dt.datetime(2026, 7, 31, 9, 10))],
                            dt.datetime(2026, 7, 31, 0, 0))  # log starts after `last`
        r = watch.freshness(dt.datetime(2026, 7, 20, 9, 0), job(), self.now, local=True)
        self.assertEqual(r.status, watch.LATE)
        self.assertIn("power log", r.detail)

    def test_an_unreadable_power_log_falls_back_to_wall_clock(self):
        watch._sleep_log = ([], None)
        self.assertEqual(watch.freshness(self.last, job(), self.now, local=True).status,
                         watch.LATE)

    def test_parses_a_pmset_log(self):
        """`Wake Requests` is written while *going to* sleep — it lists the wakes
        being scheduled. Counting it as a wake would report 1-second sleeps."""
        sample = (
            "2026-07-31 00:17:47 +0200 Sleep               \tEntering Sleep state\n"
            "2026-07-31 00:17:49 +0200 Wake Requests       \t[process=powerd ...]\n"
            "2026-07-31 00:20:07 +0200 Wake                \tWake from Hibernate\n"
            "2026-07-31 00:20:07 +0200 WakeDetails         \tDriverReason:pwrbtn\n"
        )
        got = watch._parse_sleep_log(sample)
        self.assertEqual(got[0], [(dt.datetime(2026, 7, 31, 0, 17, 47),
                                   dt.datetime(2026, 7, 31, 0, 20, 7))])
        self.assertEqual(got[1], dt.datetime(2026, 7, 31, 0, 17, 47))


class VercelCron(unittest.TestCase):
    """Vercel publishes no cron run history, so these check what it does expose:
    that the cron is scheduled as expected, and that the app it calls answers."""

    def setUp(self):
        self.net = watch.Net(offline=False, use_cache=False)
        self.now = dt.datetime(2026, 7, 31, 12, 0)
        self.crons = {"p": {"disabledAt": None,
                            "definitions": [{"path": "/api/cron/x",
                                             "schedule": "0 3 * * *"}]}}
        watch._vercel_projects = lambda net, team: self.crons
        self.addCleanup(setattr, watch, "_vercel_projects", _real_vercel_projects)
        watch._http_status = lambda net, url: self.probe
        self.addCleanup(setattr, watch, "_http_status", _real_http_status)
        self.probe = {"code": 200}

    def check(self, **over):
        j = job()
        j["check"] = {"kind": "vercel_cron", "team": "t", "project": "p",
                      "path": "/api/cron/x", **over}
        return watch.check_vercel_cron(j, self.net, self.now)

    def test_matching_schedule_is_ok(self):
        self.assertEqual(self.check(schedule="0 3 * * *").status, watch.OK)

    def test_a_changed_schedule_is_failed(self):
        """`0 3 * * *` → `0 3 1 * *` turns a nightly purge monthly, and every
        other check here stays green."""
        r = self.check(schedule="0 3 1 * *")
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("0 3 * * *", r.detail)

    def test_an_app_serving_402_is_failed(self):
        """A lapsed plan takes the whole app down and the cron keeps 'firing'."""
        self.probe = {"code": 402}
        r = self.check(reachable="https://example.test/")
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("402", r.detail)

    def test_an_app_that_answers_is_ok(self):
        self.assertEqual(self.check(reachable="https://example.test/").status, watch.OK)

    def test_a_declared_status_code_is_accepted(self):
        self.probe = {"code": 401}
        self.assertEqual(self.check(reachable="https://example.test/",
                                    reachable_ok=[200, 401]).status, watch.OK)

    def test_an_unreachable_app_is_failed(self):
        self.probe = {"code": None, "error": "URLError: nodename nor servname"}
        self.assertEqual(self.check(reachable="https://example.test/").status,
                         watch.FAILED)


class VercelToken(unittest.TestCase):
    """The CLI token is an 8-hour OAuth grant, not a static key. Reading it
    without checking `expiresAt` degrades every Vercel check to `unknown` once a
    day — the quietest possible failure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Path(self.tmp.name) / "auth.json"
        watch.VERCEL_AUTH = self.auth
        watch._vercel_tok = None
        self.addCleanup(setattr, watch, "VERCEL_AUTH", _real_auth_path)
        self.addCleanup(setattr, watch, "_vercel_tok", None)
        self.addCleanup(self.tmp.cleanup)

    def write(self, token, expires):
        self.auth.write_text(json.dumps({"token": token, "expiresAt": expires}))

    def test_a_live_token_is_used_as_is(self):
        soon = (dt.datetime.now() + dt.timedelta(hours=4)).timestamp()
        self.write("live-token", int(soon))
        self.assertEqual(watch._vercel_token(), "live-token")

    def test_a_token_without_an_expiry_is_trusted(self):
        self.auth.write_text(json.dumps({"token": "old-cli-token"}))
        self.assertEqual(watch._vercel_token(), "old-cli-token")

    def test_an_expired_token_that_cannot_be_refreshed_raises(self):
        """`unknown` is the right answer here — but only once it is honest about
        why, so the `fix` can say `vercel login` instead of nothing."""
        gone = (dt.datetime.now() - dt.timedelta(hours=1)).timestamp()
        self.write("stale-token", int(gone))
        real_which, watch.shutil.which = watch.shutil.which, lambda _, path=None: None
        self.addCleanup(setattr, watch.shutil, "which", real_which)
        with self.assertRaises(RuntimeError) as e:
            watch._vercel_token()
        self.assertIn("expired", str(e.exception))

    def test_the_cli_is_found_under_a_launchd_path(self):
        """launchd runs the brief with PATH=/usr/bin:/bin, where a Homebrew
        `vercel` is invisible; every Vercel check read `unknown` from 2026-09-15's
        brief until the lookup also searched the install dirs."""
        seen = {}
        def which(_, path=None):
            seen["path"] = path
            return None
        real_which, watch.shutil.which = watch.shutil.which, which
        self.addCleanup(setattr, watch.shutil, "which", real_which)
        gone = (dt.datetime.now() - dt.timedelta(hours=1)).timestamp()
        self.write("stale-token", int(gone))
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            with self.assertRaises(RuntimeError):
                watch._vercel_token()
        self.assertIn("/opt/homebrew/bin", seen["path"].split(os.pathsep))

    def test_milliseconds_and_seconds_are_told_apart(self):
        """Vercel writes `expiresAt` in seconds and `disabledAt` in milliseconds."""
        secs = dt.datetime(2026, 7, 31, 17, 33, 17)
        self.assertEqual(watch._epoch(int(secs.timestamp())), secs)
        self.assertEqual(watch._epoch(int(secs.timestamp() * 1000)), secs)


class Mtime(unittest.TestCase):
    def test_missing_artifact_is_unknown(self):
        j = job()
        j["check"] = {"kind": "mtime", "path": "/nope/nothing.json"}
        r = watch.check_mtime(j, watch.Net(offline=True, use_cache=False), dt.datetime.now())
        self.assertEqual(r.status, watch.UNKNOWN)

    def test_fresh_artifact_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "artifact.json"
            p.write_text("{}")
            j = job()
            j["check"] = {"kind": "mtime", "path": str(p)}
            r = watch.check_mtime(j, watch.Net(offline=True, use_cache=False), dt.datetime.now())
            self.assertEqual(r.status, watch.OK)


class Intervals(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(watch.parse_interval("30m"), dt.timedelta(minutes=30))
        self.assertEqual(watch.parse_interval("6h"), dt.timedelta(hours=6))
        self.assertEqual(watch.parse_interval("2w"), dt.timedelta(weeks=2))
        self.assertIsNone(watch.parse_interval(None))

    def test_bad_interval_raises(self):
        with self.assertRaises(ValueError):
            watch.parse_interval("soon")


class Offline(unittest.TestCase):
    """Network checks must degrade to unknown, never raise, never hang a surface."""

    def setUp(self):
        self.net = watch.Net(offline=True, use_cache=False)
        self.now = dt.datetime.now()

    def test_github_offline_is_unknown(self):
        j = job()
        j["check"] = {"kind": "github_workflow", "repo": "~/nope", "workflow": "ci.yml"}
        self.assertEqual(watch.check_github_workflow(j, self.net, self.now).status,
                         watch.UNKNOWN)

    def test_vercel_offline_is_unknown(self):
        j = job()
        j["check"] = {"kind": "vercel_cron", "team": "t", "project": "p", "path": "/x"}
        self.assertEqual(watch.check_vercel_cron(j, self.net, self.now).status,
                         watch.UNKNOWN)


class SelfCheck(unittest.TestCase):
    """Who watches the watcher: a checker that cannot evaluate its own registry
    must say so, because a blind check is silent in exactly the way a healthy
    one is."""

    def test_blind_jobs_make_the_self_check_fail(self):
        results = [("a", watch.Result(watch.OK, "fine")),
                   ("b", watch.Result(watch.UNKNOWN, "log not found: ~/gone.log"))]
        r = watch.check_self(job(), None, dt.datetime.now(), results)
        self.assertEqual(r.status, watch.FAILED)
        self.assertIn("partly blind", r.detail)
        self.assertIn("b", r.detail)

    def test_all_evaluated_is_ok(self):
        results = [("a", watch.Result(watch.OK, "fine"))]
        self.assertEqual(watch.check_self(job(), None, dt.datetime.now(), results).status,
                         watch.OK)


class Runner(unittest.TestCase):
    """examples/run-job.sh and log_block must agree on the trailer convention."""

    RUNNER = Path(__file__).resolve().parent / "examples" / "run-job.sh"

    def _run(self, log, *cmd):
        env = dict(os.environ, SCHED_AWAKE="1")  # skip the caffeinate re-exec
        return subprocess.run([str(self.RUNNER), "demo", str(log), *cmd], env=env).returncode

    def _job(self, log):
        return job(check={
            "path": str(log),
            "start": r"^=== (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) demo start ===",
            "done": r"^=== \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} demo done rc=0 ===",
        })

    def test_a_finished_run_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "demo.log"
            self.assertEqual(self._run(log, "true"), 0)
            r = watch.check_log_block(self._job(log), None, dt.datetime.now())
            self.assertEqual(r.status, watch.OK, r.detail)

    def test_a_failed_run_is_not_ok(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "demo.log"
            self.assertEqual(self._run(log, "false"), 1)
            self.assertIn("done rc=1", log.read_text())
            r = watch.check_log_block(self._job(log), None, dt.datetime.now())
            self.assertNotEqual(r.status, watch.OK, r.detail)


class Registry(unittest.TestCase):
    def test_shipped_registry_validates(self):
        """The shipped example registry must parse, carry every required field,
        and — the one that matters — give every job a `fix`."""
        jobs = watch.load_registry(watch.EXAMPLE_REGISTRY)
        self.assertGreater(len(jobs), 0)
        for j in jobs:
            self.assertTrue(j["fix"].strip(), f"{j['id']} has no fix")
            self.assertTrue(j["notices"].strip(), f"{j['id']} has no owner")

    def test_the_checker_is_in_its_own_registry(self):
        ids = [j["id"] for j in watch.load_registry(watch.EXAMPLE_REGISTRY)]
        self.assertIn("watch-self", ids)

    def test_missing_fix_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "jobs.json"
            bad = job()
            bad["fix"] = ""
            p.write_text(json.dumps({"jobs": [bad]}))
            with self.assertRaises(SystemExit):
                watch.load_registry(p)


class Cli(unittest.TestCase):
    def test_check_is_silent_and_zero_when_everything_is_healthy(self):
        """The contract the whole design rests on: no output means no problem."""
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "sync.log"
            write_log(log, [(dt.datetime.now() - dt.timedelta(minutes=5), ["ok"], True)])
            j = job()
            j["check"]["path"] = str(log)
            reg = Path(d) / "jobs.json"
            reg.write_text(json.dumps({"jobs": [j]}))
            p = subprocess.run([sys.executable, str(Path(watch.__file__)),
                                "--registry", str(reg), "--offline", "--no-cache", "check"],
                               capture_output=True, text=True)
            self.assertEqual(p.stdout, "")
            self.assertEqual(p.returncode, 0)

    def test_check_prints_and_exits_one_when_something_is_wrong(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "sync.log"
            write_log(log, [(dt.datetime.now() - dt.timedelta(days=3), ["ok"], True)])
            j = job()
            j["check"]["path"] = str(log)
            reg = Path(d) / "jobs.json"
            reg.write_text(json.dumps({"jobs": [j]}))
            p = subprocess.run([sys.executable, str(Path(watch.__file__)),
                                "--registry", str(reg), "--offline", "--no-cache", "check"],
                               capture_output=True, text=True)
            self.assertIn("Automation health", p.stdout)
            self.assertIn("fix:", p.stdout)
            self.assertEqual(p.returncode, 1)

    def test_json_output_always_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "sync.log"
            write_log(log, [(dt.datetime.now() - dt.timedelta(days=3), ["ok"], True)])
            j = job()
            j["check"]["path"] = str(log)
            reg = Path(d) / "jobs.json"
            reg.write_text(json.dumps({"jobs": [j]}))
            p = subprocess.run([sys.executable, str(Path(watch.__file__)),
                                "--registry", str(reg), "--offline", "--no-cache",
                                "check", "--json"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0)
            out = json.loads(p.stdout)
            self.assertEqual(len(out["findings"]), 1)
            self.assertTrue(out["findings"][0]["fix"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
