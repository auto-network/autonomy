"""dispatch_cmd.py — graph dispatch subcommand: show running/queued/history."""

from __future__ import annotations
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _get_dashboard_url() -> str:
    return os.environ.get("DASHBOARD_URL", "https://localhost:8080").rstrip("/")


def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _api_call(base_url: str, path: str, ctx):
    req = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Accept": "application/json"},
    )
    resp = urllib.request.urlopen(req, context=ctx, timeout=10)
    return json.loads(resp.read())


def _format_duration(secs) -> str:
    if secs is None:
        return "?"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _elapsed_since(started_at: str) -> str:
    """Calculate elapsed time since started_at (SQLite or ISO datetime)."""
    if not started_at:
        return "?"
    try:
        dt = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return "?"
    now = datetime.now(timezone.utc)
    return _format_duration(int((now - dt).total_seconds()))


def cmd_dispatch_default(args):
    """Show running + queued state."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()

    try:
        status_data = _api_call(base, "/api/dispatch/status", ctx)
        approved_data = _api_call(base, "/api/dispatch/approved", ctx)
    except (urllib.error.URLError, OSError):
        print(f"Dashboard not reachable at {base} \u2014 is it running?")
        sys.exit(1)

    running_runs = status_data.get("running_runs", [])
    waiting = approved_data.get("waiting", [])

    if args.json:
        print(json.dumps({"running": running_runs, "queued": waiting}, default=str))
        return

    # Build title lookup from the queued beads
    title_map: dict[str, str] = {}
    for bead in waiting:
        title_map[bead["id"]] = bead.get("title", "")

    # For running beads not in the queue, look up from beads API
    missing_ids = [r["bead_id"] for r in running_runs
                   if r.get("bead_id") and r["bead_id"] not in title_map]
    if missing_ids:
        try:
            all_beads = _api_call(base, "/api/beads/list", ctx)
            for b in all_beads:
                if b.get("id") in missing_ids:
                    title_map[b["id"]] = b.get("title", "")
        except Exception:
            pass  # titles are optional

    print(f"RUNNING ({len(running_runs)})")
    for r in running_runs:
        bead_id = r.get("bead_id") or ""
        elapsed = _elapsed_since(r.get("started_at", ""))
        title = (title_map.get(bead_id, "") or "")[:35]
        image = r.get("image", "")
        cname = r.get("container_name", "")
        container = f"{image}:{cname}" if image and cname else (image or cname or "")
        print(f"  {bead_id:<10}  {title:<35}  {elapsed:>7}  {container}")

    print()

    print(f"QUEUED ({len(waiting)})")
    for bead in waiting:
        bid = bead.get("id", "")
        title = (bead.get("title") or "")[:35]
        priority = f"P{bead.get('priority', '?')}"
        print(f"  {bid:<10}  {title:<35}  {priority}")


def cmd_dispatch_runs(args):
    """Show recent run history."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()

    try:
        runs = _api_call(base, "/api/dispatch/runs", ctx)
    except (urllib.error.URLError, OSError):
        print(f"Dashboard not reachable at {base} \u2014 is it running?")
        sys.exit(1)

    if args.running:
        runs = [r for r in runs if r.get("status") == "RUNNING"]
    elif args.failed:
        runs = [r for r in runs if r.get("status") in ("FAILED", "BLOCKED")]
    elif args.completed:
        runs = [r for r in runs if r.get("status") == "DONE"]

    runs = runs[:args.limit]

    if args.json:
        print(json.dumps(runs, default=str))
        return

    if args.primer:
        _print_primer(runs, args)
        return

    for r in runs:
        status = r.get("status", "?")
        bead_id = r.get("bead_id") or ""
        label = bead_id if bead_id else "(librarian)"
        duration = _format_duration(r.get("duration_secs"))
        decision = r.get("decision") or {}
        reason = (decision.get("reason", "") if isinstance(decision, dict) else "")[:50]

        status_col = f"{status:<7}"
        bead_col = f"{label:<12}"
        dur_col = f"{duration:>6}"
        line = f"{status_col}  {bead_col}  {dur_col}"
        if reason:
            line += f"  {reason}"
        print(line)


def _is_merged(commit_hash: str) -> bool:
    """Check if a commit is an ancestor of HEAD (i.e., merged)."""
    if not commit_hash:
        return False
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit_hash, "HEAD"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _print_primer(runs: list, args):
    """Rich per-run output for orientation."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()

    # Build title map from beads API
    title_map: dict[str, str] = {}
    try:
        all_beads = _api_call(base, "/api/beads/list", ctx)
        for b in all_beads:
            title_map[b.get("id", "")] = b.get("title", "")
    except Exception:
        pass

    # Filter out librarian rows unless --failed
    if not args.failed:
        runs = [r for r in runs if r.get("bead_id")]

    counts = {"completed": 0, "failed": 0, "running": 0, "unmerged": 0}

    for r in runs:
        status = r.get("status", "?")
        bead_id = r.get("bead_id") or ""
        duration = _format_duration(r.get("duration_secs"))
        commit_hash = r.get("commit_hash") or ""
        branch = r.get("branch") or ""

        # Count by status
        if status == "DONE":
            counts["completed"] += 1
        elif status in ("FAILED", "BLOCKED"):
            counts["failed"] += 1
        elif status == "RUNNING":
            counts["running"] += 1

        # Merge state
        merged = _is_merged(commit_hash) if commit_hash else False
        if status == "DONE" and commit_hash and not merged:
            merge_label = "unmerged"
            counts["unmerged"] += 1
        elif status == "DONE" and merged:
            merge_label = "merged"
        else:
            merge_label = ""

        # Header line
        status_extra = f" ({merge_label})" if merge_label else ""
        label = bead_id if bead_id else "(librarian)"
        print(f"\u2500\u2500 {label} \u2500\u2500 {status}{status_extra} \u2500\u2500 {duration} " + "\u2500" * 30)

        # Title
        title = title_map.get(bead_id, "")
        if title:
            print(f"Title:   {title}")

        # Commit info
        if commit_hash:
            short_hash = commit_hash[:7]
            merge_flag = "" if merged else " !! NOT MERGED"
            print(f"Commit:  {short_hash} ({branch}){merge_flag}")
            commit_msg = r.get("commit_message") or ""
            if commit_msg:
                print(f"         {commit_msg}")

        # Diff stats
        lines_added = r.get("lines_added")
        lines_removed = r.get("lines_removed")
        files_changed = r.get("files_changed")
        if lines_added is not None or lines_removed is not None:
            la = lines_added or 0
            lr = lines_removed or 0
            fc = files_changed or 0
            print(f"Changed: +{la} -{lr} across {fc} file{'s' if fc != 1 else ''}")

        # Scores
        decision = r.get("decision") or {}
        scores = decision.get("scores") if isinstance(decision, dict) else None
        if scores and isinstance(scores, dict):
            parts = []
            for key in ("tooling", "clarity", "confidence"):
                val = scores.get(key)
                if val is not None:
                    parts.append(f"{key}={val}")
            if parts:
                print(f"Scores:  {' '.join(parts)}")

        # Smoke result
        smoke = r.get("smoke_result")
        if smoke and isinstance(smoke, dict):
            passed = smoke.get("pass", False)
            dur_ms = smoke.get("duration_ms", "?")
            label_s = "PASS" if passed else "FAIL"
            print(f"Smoke:   {label_s} ({dur_ms}ms)")

        # Librarian review
        lib_review = r.get("librarian_review")
        if lib_review and isinstance(lib_review, dict):
            lib_status = lib_review.get("status", "?")
            findings = lib_review.get("findings", [])
            skipped = lib_review.get("skipped", 0)
            if findings:
                print(f"Review:  {len(findings)} extracted" + (f" \u00b7 {skipped} skipped" if skipped else ""))
            else:
                print(f"Review:  {lib_status}")

        # Experience report summary
        exp_summary = r.get("experience_summary")
        if exp_summary:
            first_line = exp_summary.split("\n")[0][:80]
            print(f"Report:  {first_line}")

        # Validation
        val = r.get("validation")
        if val and isinstance(val, dict):
            print(f"Valid:   graph://{val['source_id']} — {val.get('title', '')[:60]}")

        # Pitfalls
        pitfalls = r.get("pitfalls", [])
        if pitfalls:
            print(f"Pitfall: {len(pitfalls)} note{'s' if len(pitfalls) != 1 else ''}")
            for p in pitfalls[:3]:
                print(f"         graph://{p['id']} {p['title']}")

        # Reason (for failures)
        if status in ("FAILED", "BLOCKED"):
            reason = decision.get("reason", "") if isinstance(decision, dict) else ""
            if reason:
                print(f"Reason:  {reason[:80]}")

        print()

    # Summary line
    parts = []
    if counts["completed"]:
        parts.append(f"{counts['completed']} completed")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    if counts["running"]:
        parts.append(f"{counts['running']} running")
    if counts["unmerged"]:
        parts.append(f"{counts['unmerged']} unmerged")
    if parts:
        print(", ".join(parts))


def cmd_dispatch_watch(args):
    """Block until the next dispatch run completes (DONE or FAILED)."""
    import time
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()
    timeout = getattr(args, "timeout", 600) or 600
    poll_interval = 3.0
    deadline = time.time() + timeout

    # Snapshot current completed run IDs so we can detect new completions
    try:
        data = _api_call(base, "/api/dispatch/runs", ctx)
    except Exception as e:
        print(f"Cannot reach dashboard: {e}", file=sys.stderr)
        return

    runs = data if isinstance(data, list) else data.get("runs", data.get("active", []))
    seen_completed = set()
    for r in runs:
        if r.get("completed_at") or r.get("status") in ("DONE", "FAILED", "done", "failed"):
            seen_completed.add(r.get("bead_id") or r.get("id"))

    running_ids = set()
    for r in runs:
        if not r.get("completed_at") and r.get("status") not in ("DONE", "FAILED", "done", "failed"):
            running_ids.add(r.get("bead_id") or r.get("id"))

    if not running_ids:
        print("No running dispatch — nothing to watch")
        return

    print(f"Watching {len(running_ids)} running dispatch(es): {', '.join(running_ids)}")

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            data = _api_call(base, "/api/dispatch/runs", ctx)
        except Exception:
            continue
        runs = data if isinstance(data, list) else data.get("runs", data.get("active", []))
        for r in runs:
            rid = r.get("bead_id") or r.get("id")
            is_done = r.get("completed_at") or r.get("status") in ("DONE", "FAILED", "done", "failed")
            if is_done and rid not in seen_completed:
                status = r.get("status", "DONE")
                exit_code = r.get("exit_code", "?")
                duration = r.get("duration_secs") or r.get("duration", "?")
                print(f"\n✓ Dispatch completed: {rid}")
                print(f"  Status: {status}  Exit: {exit_code}  Duration: {duration}s")
                if r.get("commit_sha"):
                    print(f"  Commit: {r['commit_sha'][:12]}")
                return

    print(f"Timeout after {timeout}s — dispatch still running", file=sys.stderr)


def cmd_dispatch_approve(args):
    """Set readiness=approved on one or more beads, releasing them for dispatch."""
    import subprocess
    failures = 0
    for bead_id in args.bead_ids:
        result = subprocess.run(
            ["bd", "set-state", bead_id, "readiness=approved"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"✓ {bead_id} approved for dispatch")
        else:
            print(f"✗ {bead_id}: {result.stderr.strip()}", file=sys.stderr)
            failures += 1
    if failures:
        sys.exit(1)


def cmd_dispatch_reset(args):
    """Reset the circuit breaker for a bead by inserting a synthetic DONE record."""
    import subprocess

    # Import dispatch_db — it lives in agents/ which may not be on sys.path
    import importlib
    import pathlib
    agents_dir = str(pathlib.Path(__file__).resolve().parent.parent.parent / "agents")
    if agents_dir not in sys.path:
        sys.path.insert(0, agents_dir)
    import dispatch_db

    bead_id = args.bead_id

    # Check current failure count
    agent_fails, merge_fails = dispatch_db.get_consecutive_failures(bead_id)
    if agent_fails == 0 and merge_fails == 0:
        print(f"  {bead_id}: no consecutive failures — circuit breaker not tripped")
        return

    print(f"  {bead_id}: {agent_fails} agent failures, {merge_fails} merge failures")

    # Insert synthetic DONE record
    run_id = dispatch_db.reset_circuit_breaker(bead_id)
    print(f"  ✓ Inserted synthetic DONE record: {run_id}")

    # Verify reset
    agent_fails_after, merge_fails_after = dispatch_db.get_consecutive_failures(bead_id)
    print(f"  ✓ Failure count now: {agent_fails_after} agent, {merge_fails_after} merge")

    # Reset readiness to ready and re-approve
    result = subprocess.run(
        ["bd", "set-state", bead_id, "readiness=approved"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  ✓ {bead_id} re-approved for dispatch")
    else:
        print(f"  ✗ Failed to re-approve: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def cmd_dispatch_nag(args):
    """Enable or disable dispatch completion nag for the current session."""
    bd_actor = os.environ.get("BD_ACTOR")
    if not bd_actor or ":" not in bd_actor:
        print("Error: $BD_ACTOR not set. Cannot identify current session.", file=sys.stderr)
        print("This command must be run inside a dashboard-managed session.", file=sys.stderr)
        sys.exit(1)
    tmux_name = bd_actor.split(":", 1)[1]

    enabled = not getattr(args, "disable", False)
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()
    import urllib.parse
    url = f"{base}/api/session/{urllib.parse.quote(tmux_name)}/dispatch-nag"
    data = json.dumps({"enabled": enabled}).encode()
    req = urllib.request.Request(url, data=data, method="PUT",
                                headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        if resp.status == 200:
            state = "enabled" if enabled else "disabled"
            print(f"  \u2713 Dispatch nag {state} for {tmux_name}")
        else:
            print(f"  \u2717 Failed: HTTP {resp.status}", file=sys.stderr)
            sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"  \u2717 Failed: HTTP {e.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"  \u2717 Dashboard not reachable: {e}", file=sys.stderr)
        sys.exit(1)


def _parse_since(since_str: str) -> float:
    """Parse duration string like '7d', '30d', '1w' to seconds."""
    import re
    m = re.match(r'^(\d+)\s*([smhdw])$', since_str.strip())
    if not m:
        print(f"Invalid duration: {since_str!r}. Use e.g. 7d, 30d, 1w", file=sys.stderr)
        sys.exit(1)
    val, unit = int(m.group(1)), m.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return val * multipliers[unit]


def _fetch_runs_for_stats() -> list[dict]:
    """Fetch dispatch runs from dashboard API."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()
    try:
        return _api_call(base, "/api/dispatch/runs", ctx)
    except (urllib.error.URLError, OSError):
        print(f"Dashboard not reachable at {base} — is it running?")
        sys.exit(1)


def _extract_run_fields(run: dict) -> dict:
    """Normalize a run dict, extracting nested decision fields."""
    decision = run.get("decision") or {}
    if not isinstance(decision, dict):
        decision = {}
    scores = decision.get("scores") or {}
    time_breakdown = decision.get("time_breakdown") or {}
    return {
        "bead_id": run.get("bead_id") or "",
        "status": run.get("status") or "",
        "duration_secs": run.get("duration_secs"),
        "timestamp": run.get("timestamp") or "",
        "score_tooling": scores.get("tooling"),
        "workaround_pct": time_breakdown.get("tooling_workaround_pct"),
        "failure_category": decision.get("failure_category") or "",
        # dir field encodes image info for some runs: image-bead-timestamp
        "dir": run.get("dir") or "",
    }


def _filter_runs(runs: list[dict], args) -> list[dict]:
    """Filter out librarian runs, RUNNING runs, and apply --since."""
    # Exclude librarian runs (empty bead_id) and in-progress runs
    filtered = [r for r in runs if r["bead_id"] and r["status"] != "RUNNING"]

    if args.since:
        from datetime import timedelta
        secs = _parse_since(args.since)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=secs)
        cutoff_str = cutoff.strftime("%Y%m%d")
        result = []
        for r in filtered:
            ts = r["timestamp"].replace("-", "")[:8]
            if ts and ts >= cutoff_str:
                result.append(r)
        filtered = result

    return filtered


def _week_from_timestamp(ts: str) -> str:
    """Extract ISO week label from timestamp like '20260327-171826' or '20260327--171826'."""
    digits = ts.replace("-", "")[:8]
    if len(digits) < 8:
        return "?"
    try:
        dt = datetime.strptime(digits, "%Y%m%d")
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    except ValueError:
        return "?"


def _image_from_dir(dir_name: str) -> str:
    """Infer image name from the run dir.

    Dir format is typically: <bead>-<date>-<time> or <image>-<bead>-<date>-<time>.
    If the dir starts with a known image prefix, extract it.
    """
    # dir looks like: auto-0ncj-20260327-131825 or librarian-review_report-xxx
    # For image breakdown, we parse the prefix before the bead_id pattern
    # Simple heuristic: if it doesn't start with 'auto-' or 'librarian-', it has an image prefix
    if not dir_name:
        return "(unknown)"
    parts = dir_name.split("-")
    # Standard pattern: bead-date-time → no image prefix
    # Image pattern: image-bead-date-time
    if len(parts) >= 4 and parts[0] not in ("auto", "librarian"):
        # Reconstruct image from prefix parts before the bead ID
        # Find where the bead_id starts (auto-XXXX)
        for i, p in enumerate(parts):
            if p == "auto" and i + 1 < len(parts):
                return "-".join(parts[:i])
        return parts[0]
    return "(default)"


def cmd_dispatch_stats(args):
    """Aggregate statistics and trends over dispatch runs."""
    raw_runs = _fetch_runs_for_stats()
    runs = [_extract_run_fields(r) for r in raw_runs]
    runs = _filter_runs(runs, args)

    if not runs:
        print("No dispatch runs found.")
        return

    if args.trend:
        _stats_trend(runs, args)
    elif args.by_image:
        _stats_by_image(runs, args)
    else:
        _stats_summary(runs, args)


def _stats_summary(runs, args):
    """Default summary output."""
    from collections import Counter

    total = len(runs)
    status_counts = Counter(r["status"] for r in runs)

    done = status_counts.get("DONE", 0)
    failed = status_counts.get("FAILED", 0)
    merge_failed = status_counts.get("MERGE_FAILED", 0)
    timeout = status_counts.get("TIMEOUT", 0)
    blocked = status_counts.get("BLOCKED", 0)
    success_pct = round(100 * done / total) if total else 0

    # Duration
    durations = sorted(r["duration_secs"] for r in runs if r["duration_secs"] is not None)
    avg_dur = sum(durations) / len(durations) if durations else None
    median_dur = durations[len(durations) // 2] if durations else None

    # Tooling scores
    tooling_scores = [r["score_tooling"] for r in runs if r["score_tooling"] is not None]
    tooling_avg = sum(tooling_scores) / len(tooling_scores) if tooling_scores else None
    tooling_dist = Counter(tooling_scores)

    # Workaround %
    wa_values = [r["workaround_pct"] for r in runs if r["workaround_pct"] is not None]
    wa_avg = sum(wa_values) / len(wa_values) if wa_values else None

    # Top failure categories
    fail_cats = Counter(
        r["failure_category"] for r in runs
        if r["failure_category"]
    )

    if args.json:
        data = {
            "total": total,
            "success_pct": success_pct,
            "status_breakdown": dict(status_counts),
            "duration_avg_secs": round(avg_dur) if avg_dur else None,
            "duration_median_secs": median_dur,
            "tooling_avg": round(tooling_avg, 1) if tooling_avg else None,
            "tooling_distribution": {str(k): v for k, v in sorted(tooling_dist.items(), reverse=True)},
            "workaround_avg_pct": round(wa_avg, 1) if wa_avg else None,
            "top_failures": dict(fail_cats.most_common(5)),
        }
        print(json.dumps(data, default=str))
        return

    since_label = f"since {args.since}" if args.since else "all time"
    print(f"Dispatch Stats ({since_label}, {total} runs)")

    status_parts = []
    for label, count in [("DONE", done), ("FAILED", failed), ("MERGE_FAILED", merge_failed),
                         ("BLOCKED", blocked), ("TIMEOUT", timeout)]:
        if count:
            status_parts.append(f"{count} {label}")
    print(f"  Success: {success_pct}% ({', '.join(status_parts)})")

    avg_s = _format_duration(avg_dur) if avg_dur else "?"
    med_s = _format_duration(median_dur) if median_dur else "?"
    print(f"  Duration: avg {avg_s}, median {med_s}")

    if tooling_avg is not None:
        dist_parts = [f"score {k}: {v}" for k, v in sorted(tooling_dist.items(), reverse=True)]
        print(f"  Tooling: avg {tooling_avg:.1f}/5 ({', '.join(dist_parts)})")

    if wa_avg is not None:
        print(f"  Workaround: avg {wa_avg:.1f}%")

    if fail_cats:
        fail_parts = [f"{cat} ({cnt})" for cat, cnt in fail_cats.most_common(5)]
        print(f"  Top failures: {', '.join(fail_parts)}")


def _stats_trend(runs, args):
    """Weekly trend output."""
    from collections import defaultdict

    buckets = defaultdict(list)
    for r in runs:
        week = _week_from_timestamp(r["timestamp"])
        buckets[week].append(r)

    weeks = sorted(buckets.keys())
    if not weeks:
        print("No dispatch runs found.")
        return

    if args.json:
        data = []
        for w in weeks:
            bucket = buckets[w]
            n = len(bucket)
            done = sum(1 for r in bucket if r["status"] == "DONE")
            tooling = [r["score_tooling"] for r in bucket if r["score_tooling"] is not None]
            wa = [r["workaround_pct"] for r in bucket if r["workaround_pct"] is not None]
            durs = [r["duration_secs"] for r in bucket if r["duration_secs"] is not None]
            data.append({
                "week": w,
                "runs": n,
                "success_pct": round(100 * done / n) if n else 0,
                "tooling_avg": round(sum(tooling) / len(tooling), 1) if tooling else None,
                "workaround_avg_pct": round(sum(wa) / len(wa), 1) if wa else None,
                "duration_avg_secs": round(sum(durs) / len(durs)) if durs else None,
            })
        print(json.dumps(data, default=str))
        return

    print(f"{'Week':<13} {'Runs':>4}  {'Success':>7}  {'Tooling':>7}  {'Workaround':>10}  {'Duration':>8}")

    prev_success = None
    for w in weeks:
        bucket = buckets[w]
        n = len(bucket)
        done = sum(1 for r in bucket if r["status"] == "DONE")
        success = round(100 * done / n) if n else 0

        tooling_vals = [r["score_tooling"] for r in bucket if r["score_tooling"] is not None]
        tooling = f"{sum(tooling_vals)/len(tooling_vals):.1f}" if tooling_vals else "\u2014"

        wa_vals = [r["workaround_pct"] for r in bucket if r["workaround_pct"] is not None]
        wa = f"{sum(wa_vals)/len(wa_vals):.1f}%" if wa_vals else "\u2014"

        dur_vals = [r["duration_secs"] for r in bucket if r["duration_secs"] is not None]
        dur = _format_duration(sum(dur_vals) / len(dur_vals)) if dur_vals else "\u2014"

        indicator = ""
        if prev_success is not None:
            if success > prev_success:
                indicator = " \u2191"
            elif success < prev_success:
                indicator = " \u2193"
        prev_success = success

        print(f"{w:<13} {n:>4}  {success:>6}%  {tooling:>7}  {wa:>10}  {dur:>8}{indicator}")


def _stats_by_image(runs, args):
    """Breakdown by container image."""
    from collections import defaultdict

    buckets = defaultdict(list)
    for r in runs:
        img = _image_from_dir(r["dir"])
        buckets[img].append(r)

    images = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))

    if args.json:
        data = []
        for img in images:
            bucket = buckets[img]
            n = len(bucket)
            done = sum(1 for r in bucket if r["status"] == "DONE")
            tooling = [r["score_tooling"] for r in bucket if r["score_tooling"] is not None]
            wa = [r["workaround_pct"] for r in bucket if r["workaround_pct"] is not None]
            data.append({
                "image": img,
                "runs": n,
                "success_pct": round(100 * done / n) if n else 0,
                "tooling_avg": round(sum(tooling) / len(tooling), 1) if tooling else None,
                "workaround_avg_pct": round(sum(wa) / len(wa), 1) if wa else None,
            })
        print(json.dumps(data, default=str))
        return

    print(f"{'Image':<35} {'Runs':>4}  {'Success':>7}  {'Tooling':>7}  {'Workaround':>10}")
    for img in images:
        bucket = buckets[img]
        n = len(bucket)
        done = sum(1 for r in bucket if r["status"] == "DONE")
        success = round(100 * done / n) if n else 0

        tooling_vals = [r["score_tooling"] for r in bucket if r["score_tooling"] is not None]
        tooling = f"{sum(tooling_vals)/len(tooling_vals):.1f}" if tooling_vals else "\u2014"

        wa_vals = [r["workaround_pct"] for r in bucket if r["workaround_pct"] is not None]
        wa = f"{sum(wa_vals)/len(wa_vals):.1f}%" if wa_vals else "\u2014"

        print(f"{img[:35]:<35} {n:>4}  {success:>6}%  {tooling:>7}  {wa:>10}")


def cmd_dispatch_status(args):
    """Print compact one-liner summary."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()

    try:
        status_data = _api_call(base, "/api/dispatch/status", ctx)
        approved_data = _api_call(base, "/api/dispatch/approved", ctx)
        runs = _api_call(base, "/api/dispatch/runs", ctx)
    except (urllib.error.URLError, OSError):
        print(f"Dashboard not reachable at {base} \u2014 is it running?")
        sys.exit(1)

    n_running = len(status_data.get("running_runs", []))
    n_queued = len(approved_data.get("waiting", []))

    today_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
    done_today = sum(
        1 for r in runs
        if (r.get("timestamp", "") or "").startswith(today_prefix) and r.get("status") == "DONE"
    )
    failed_today = sum(
        1 for r in runs
        if (r.get("timestamp", "") or "").startswith(today_prefix)
        and r.get("status") in ("FAILED", "BLOCKED")
    )

    if args.json:
        print(json.dumps({
            "running": n_running,
            "queued": n_queued,
            "done_today": done_today,
            "failed_today": failed_today,
        }))
        return

    parts = [f"{n_running} running", f"{n_queued} queued", f"{done_today} done today"]
    if failed_today:
        parts.append(f"{failed_today} failed")
    print(", ".join(parts))
