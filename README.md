# claude-codex-pingpong

Make **Claude Code** and **Codex** work together on the same task, turn by turn.

One agent works, writes a handoff note, then the other agent picks it up: reviews the work, does its own chunk, and hands back. The loop stops when one agent says the task is **DONE** and the other one independently **verifies and confirms it** (or when the round limit is hit).

```
you ──task──> pingpong.py ──> claude (turn 1) ──HANDOFF.md──> codex (turn 2) ──HANDOFF.md──> claude (turn 3) ── ... ──> DONE + confirmed
```

## Requirements

- `claude` CLI installed and logged in (Claude Code)
- `codex` CLI installed and logged in (OpenAI Codex)
- Python 3.8+ (macOS built-in is fine, no packages needed)

## Install

```bash
git clone <this repo> ~/Documents/Perso/claude-codex-pingpoing
```

Optional but recommended — add an alias to `~/.zshrc` so it works from anywhere:

```bash
alias pingpong='python3 ~/Documents/Perso/claude-codex-pingpoing/pingpong.py'
```

## Usage

You never prompt Claude or Codex yourself — the script launches and prompts both. You only give it the task:

```bash
cd ~/code/my-project
pingpong "Add a /health endpoint to the API and cover it with tests"
```

Or from anywhere, pointing at the project:

```bash
pingpong "Refactor the parser module and make all tests pass" --dir ~/code/my-project
```

Long task? Put it in a file:

```bash
pingpong --task-file task.md --dir ~/code/my-project
```

### Options

| Flag | Default | What it does |
|---|---|---|
| `--dir PATH` | current dir | Project the agents work on |
| `--start claude\|codex` | `claude` | Who takes the first turn |
| `--max-rounds N` | `16` | Hard cap on turns (prevents infinite ping-pong) |
| `--timeout SECONDS` | `1800` | Per-turn time limit |
| `--yolo` | off | Full permissions for both agents (no sandbox / no permission checks). Only on projects you trust — the agents can then run any command. |

Without `--yolo`, Claude runs with `--permission-mode acceptEdits` and Codex with `--full-auto` (workspace-write sandbox). That's enough for editing files and running most builds/tests. If a turn fails because a command was blocked, re-run with `--yolo`.

## How the loop works

1. The orchestrator sends the task + the rules to the starting agent.
2. The agent works, then must overwrite `.pingpong/HANDOFF.md` in the project:

   ```
   STATUS: CONTINUE        (or DONE)

   ## What I did
   - ...

   ## Message to <other agent>
   - ...
   ```

3. The orchestrator reads the handoff and sends it to the other agent as its incoming message.
4. When an agent writes `STATUS: DONE`, the other agent gets one verification turn: it must re-check the requirements and actually run the code/tests. Two agreeing `DONE`s end the run. A disagreement continues the loop.
5. Each agent keeps its own session (`claude --resume` / `codex exec resume`), so they remember their previous turns and only receive the new handoff each time.

## The prompts sent to the agents

This is the exact template each agent (including Codex) receives on its first turn — you don't need to paste it anywhere, the script does it:

```
You are {agent}, collaborating turn-by-turn with {other} on the repository in the current directory.

# Task
{your task}

# How this collaboration works
- You and {other} alternate turns. This is turn {n} of at most {max}.
- Do a focused chunk of real work this turn: implement, review, test or fix. Prefer small verifiable steps over big rewrites.
- Critically review what {other} did last turn before building on it. If something is wrong, fix it or push back in your handoff message.
- Do not `git commit` or `git push` unless the task explicitly asks for it.
- Do not create or modify anything under .pingpong/ except the handoff file described below.

# Message from {other}
{the other agent's handoff}

# End of your turn (required)
Overwrite .pingpong/HANDOFF.md with: STATUS line, ## What I did, ## Message to {other}.
Use STATUS: DONE only if the whole task is complete and verified.
Anything not written to .pingpong/HANDOFF.md will NOT be seen by {other}.
```

Later turns get a short version (just the new handoff + the reminder), since each agent's session already has the context.

## Where things end up

- All work happens in your project's files, as usual.
- `.pingpong/runs/<timestamp>/` inside the target project contains:
  - `transcript.md` — every handoff, in order (read this first)
  - `turn-NN-<agent>.log` — full raw output of each turn
  - `summary.json` — machine-readable run metadata (outcome, per-turn durations, exit codes, errors)
- `.pingpong/` self-ignores via its own `.gitignore`, so it never pollutes the project's git history.
- Nothing is committed: at the end, review `git diff` and commit yourself (or make committing part of the task).

## Tips

- Be explicit in the task about the definition of done: "all tests pass", "works with `npm run build`", etc. That's what the verification turn checks against.
- `--start codex` if you want Codex to design first and Claude to review, or vice versa.
- Exit code is `0` only when both agents agreed on DONE — usable in scripts.
