#!/usr/bin/env python3
"""Claude Code <-> Codex ping-pong orchestrator.

Runs the two coding agents turn-by-turn on the same repository. Each turn one
agent works, then hands off by writing .pingpong/HANDOFF.md in the target
project; the orchestrator feeds that handoff to the other agent. The run ends
when one agent declares the task DONE and the other agent verifies and
confirms, or when --max-rounds is reached.

Usage:
    python3 pingpong.py "Build a REST API for todos with tests" --dir ~/code/myproject

Requires the `claude` and `codex` CLIs installed and logged in.
"""

import argparse
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HANDOFF_REL = Path(".pingpong") / "HANDOFF.md"

FULL_PROMPT = """\
You are {agent}, collaborating turn-by-turn with {other} on the repository in the current directory.

# Task
{task}

# How this collaboration works
- You and {other} alternate turns. This is turn {turn} of at most {max_rounds}.
- Do a focused chunk of real work this turn: implement, review, test or fix. Prefer small verifiable steps over big rewrites.
- Critically review what {other} did last turn before building on it. If something is wrong, fix it or push back in your handoff message.
- Do not `git commit` or `git push` unless the task explicitly asks for it.
- Do not create or modify anything under .pingpong/ except the handoff file described below.

# Message from {other}
{message}
{done_note}
# End of your turn (required)
When your work for this turn is finished, overwrite the file .pingpong/HANDOFF.md with exactly this structure:

STATUS: CONTINUE

## What I did
- ...

## Message to {other}
- What you expect {other} to do next, or your review verdict.

Use `STATUS: DONE` instead of `STATUS: CONTINUE` only if you believe the whole task is complete and verified.
Anything not written to .pingpong/HANDOFF.md will NOT be seen by {other}.
"""

SHORT_PROMPT = """\
Turn {turn} of at most {max_rounds}. Same task and same rules as before.

# Message from {other}
{message}
{done_note}
Do your turn's work now. When finished, overwrite .pingpong/HANDOFF.md (STATUS: CONTINUE or DONE, ## What I did, ## Message to {other}). Anything not in that file will NOT be seen by {other}.
"""

DONE_NOTE = """
# IMPORTANT - completion proposed
{other} believes the task is fully complete. This turn, verify that claim yourself: re-read the task requirements and actually run the code/tests.
- If you agree: write STATUS: DONE with a short verification summary. The run will stop.
- If you disagree: write STATUS: CONTINUE and list precisely what is still missing or broken.
"""


def log_print(msg=""):
    print(msg, flush=True)


def banner(text):
    log_print()
    log_print("=" * 70)
    log_print(text)
    log_print("=" * 70)


def stream_run(cmd, cwd, timeout, log_path, echo):
    """Run a command, capture combined stdout/stderr, optionally echo live."""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        fd = proc.stdout.fileno()
        chunks = []
        start = time.time()
        try:
            while True:
                if time.time() - start > timeout:
                    proc.kill()
                    proc.wait()
                    raise TimeoutError(f"turn exceeded {timeout}s")
                ready, _, _ = select.select([fd], [], [], 5)
                if ready:
                    data = os.read(fd, 65536)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        chunks.append(text)
                        log.write(text)
                        log.flush()
                        if echo:
                            sys.stdout.write(text)
                            sys.stdout.flush()
                        continue
                if proc.poll() is not None:
                    break
        except KeyboardInterrupt:
            proc.kill()
            proc.wait()
            raise
    return "".join(chunks), proc.returncode


def run_claude(prompt, cwd, session, args, log_path):
    cmd = ["claude", "-p", "--output-format", "json"]
    if args.yolo:
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--permission-mode", "acceptEdits"]
    if session:
        cmd += ["--resume", session]
    cmd.append(prompt)
    log_print("  claude is working (output shown when the turn ends)...")
    out, rc = stream_run(cmd, cwd, args.timeout, log_path, echo=False)
    result_text, new_session = out, session
    try:
        payload = json.loads(out.strip().splitlines()[-1])
        result_text = payload.get("result", out)
        new_session = payload.get("session_id", session)
    except (json.JSONDecodeError, IndexError):
        pass
    log_print(result_text.strip()[-3000:])
    return result_text, new_session, rc


def run_codex(prompt, cwd, session, args, log_path):
    cmd = ["codex", "exec"]
    if session:
        cmd += ["resume", session if session != "--last" else "--last"]
    if args.yolo:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.append("--full-auto")
    cmd += ["--skip-git-repo-check", prompt]
    out, rc = stream_run(cmd, cwd, args.timeout, log_path, echo=True)
    new_session = session
    m = re.search(r"(?:session id|session_id|thread id)[:\s]+([0-9a-fA-F][0-9a-fA-F-]{7,})", out)
    if m:
        new_session = m.group(1)
    elif not session:
        new_session = "--last"  # fallback: resume most recent codex session
    return out, new_session, rc


RUNNERS = {"claude": run_claude, "codex": run_codex}


def parse_handoff(project_dir):
    path = project_dir / HANDOFF_REL
    if not path.exists():
        return None, None
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*STATUS\s*:\s*(DONE|CONTINUE)", text, re.MULTILINE | re.IGNORECASE)
    status = m.group(1).upper() if m else "CONTINUE"
    return status, text


def main():
    ap = argparse.ArgumentParser(description="Run Claude Code and Codex turn-by-turn on one task.")
    ap.add_argument("task", nargs="?", help="The task description (or use --task-file)")
    ap.add_argument("--task-file", help="Read the task description from a file")
    ap.add_argument("--dir", default=".", help="Target project directory (default: current dir)")
    ap.add_argument("--start", choices=["claude", "codex"], default="claude", help="Which agent goes first")
    ap.add_argument("--max-rounds", type=int, default=16, help="Maximum number of turns (default: 16)")
    ap.add_argument("--timeout", type=int, default=1800, help="Per-turn timeout in seconds (default: 1800)")
    ap.add_argument("--yolo", action="store_true",
                    help="Give both agents full permissions (claude --dangerously-skip-permissions, "
                         "codex --dangerously-bypass-approvals-and-sandbox). Use only on projects you trust.")
    args = ap.parse_args()

    if args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8")
    elif args.task:
        task = args.task
    else:
        ap.error("provide a task description or --task-file")

    for tool in ("claude", "codex"):
        if not shutil.which(tool):
            sys.exit(f"error: `{tool}` CLI not found on PATH. Install it and log in first.")

    project_dir = Path(args.dir).expanduser().resolve()
    if not project_dir.is_dir():
        sys.exit(f"error: {project_dir} is not a directory")

    pingpong_dir = project_dir / ".pingpong"
    pingpong_dir.mkdir(exist_ok=True)
    # keep run artifacts out of the target project's git history
    (pingpong_dir / ".gitignore").write_text("*\n")
    run_dir = pingpong_dir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    transcript = run_dir / "transcript.md"
    transcript.write_text(f"# Ping-pong run\n\nProject: {project_dir}\n\nTask:\n{task}\n\n")

    order = [args.start, "codex" if args.start == "claude" else "claude"]
    sessions = {"claude": None, "codex": None}
    seen_full_prompt = {"claude": False, "codex": False}
    message = "(none - you go first)"
    pending_done_by = None
    outcome = "max-rounds"

    banner(f"PING-PONG: {order[0]} starts | project: {project_dir}\nTask: {task[:200]}")

    try:
        for turn in range(1, args.max_rounds + 1):
            agent = order[(turn - 1) % 2]
            other = order[turn % 2]
            banner(f"TURN {turn}/{args.max_rounds} - {agent.upper()}")

            done_note = DONE_NOTE.format(other=other) if pending_done_by == other else ""
            template = SHORT_PROMPT if seen_full_prompt[agent] and sessions[agent] else FULL_PROMPT
            prompt = template.format(agent=agent, other=other, task=task, turn=turn,
                                     max_rounds=args.max_rounds, message=message, done_note=done_note)

            handoff_path = project_dir / HANDOFF_REL
            if handoff_path.exists():
                handoff_path.unlink()

            log_path = run_dir / f"turn-{turn:02d}-{agent}.log"
            out, new_session, rc = RUNNERS[agent](prompt, project_dir, sessions[agent], args, log_path)

            if rc != 0 and sessions[agent]:
                log_print(f"  [warn] {agent} failed on resume (exit {rc}); retrying with a fresh session")
                sessions[agent] = None
                seen_full_prompt[agent] = False
                prompt = FULL_PROMPT.format(agent=agent, other=other, task=task, turn=turn,
                                            max_rounds=args.max_rounds, message=message, done_note=done_note)
                out, new_session, rc = RUNNERS[agent](prompt, project_dir, None, args, log_path)
            if rc != 0:
                log_print(f"  [error] {agent} exited with code {rc}; see {log_path}")
                outcome = "agent-error"
                break

            sessions[agent] = new_session
            seen_full_prompt[agent] = True

            status, handoff_text = parse_handoff(project_dir)
            if handoff_text is None:
                log_print(f"  [warn] {agent} did not write {HANDOFF_REL}; passing its raw output instead")
                status = "CONTINUE"
                handoff_text = f"({agent} forgot the handoff file; raw output below)\n\n{out.strip()[-4000:]}"

            with open(transcript, "a", encoding="utf-8") as t:
                t.write(f"\n---\n\n## Turn {turn} - {agent} (STATUS: {status})\n\n{handoff_text}\n")

            log_print(f"\n  -> STATUS: {status}")
            if status == "DONE":
                if pending_done_by == other:
                    outcome = "done"
                    break
                pending_done_by = agent
            else:
                pending_done_by = None
            message = handoff_text
    except KeyboardInterrupt:
        outcome = "interrupted"
        log_print("\n[interrupted by user]")
    except TimeoutError as e:
        outcome = "timeout"
        log_print(f"\n[error] {e}")

    banner(f"RUN FINISHED: {outcome.upper()}\nTranscript and logs: {run_dir}")
    if outcome == "done":
        log_print("Both agents agree the task is complete. Review the changes before committing.")
    elif outcome == "max-rounds":
        log_print("Hit the round limit before both agents agreed it was done. "
                  "Check the transcript, then re-run with a follow-up task if needed.")
    sys.exit(0 if outcome == "done" else 1)


if __name__ == "__main__":
    main()
