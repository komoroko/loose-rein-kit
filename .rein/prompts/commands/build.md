# /build — Implementation phase (autonomous loop consumption)

(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.rein/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `approval-presentation` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Prerequisite check (always first)
Read `.rein/state.yaml` and confirm `gates.mandate == approved`. If it is pending, do not work: say
what the mandate still needs (`rein next` names it) and stop. This is the one gate implementation
waits on — and what it authorizes is the `scope` and the claims, not an order of work. Inside it the
loop decomposes, reorders and re-runs as it needs to.

## Running it — `rein build`
**Your part of this phase is to run `rein build`, wait for it, and act on its exit code.** The
orchestrator does everything else in code: it picks the tasks, launches the implementers and
reviewers headless in the OCI sandbox, runs the quality gate (`test` → `check` → `review` → `smoke`
by default, defined once in `.rein/config.yaml`) and its negative control, merges, and ends with the
grounded review of the whole change.

It launches the CLI that `rein agent <cli> [--role <role>]` set (default `claude`), so that CLI must
be installed and authenticated. Without it, `rein build` refuses with exit `2` and names what is
missing; install one and point the roles at it with `rein agent <cli>`.

```
rein build              # run
rein build --dry-run   # check just the control flow without calling the agent CLI/git
```

It refuses to start (exit `2`) when a document it would send an agent to read has moved since
the mandate gate froze it — commit it, or roll back with `rein revise --to mandate` if the approval no longer
covers it.

It also refuses on **any** uncommitted change in the working tree: every task forks from the last
commit on the work branch, so an uncommitted edit is in no task's tree and no gate ran over it.
Commit or stash first.

Before the first launch it reports, all at once and each with the command that repairs it, anything
about the machine the run cannot finish without: no container runtime, an unbuilt pinned image, an
agent CLI not on PATH, a `required:` step with no `command:`.

**`rein build` is one command, not an iteration** — it runs the whole algorithm to completion
and its exit is the signal. The exit code says what to do next, and is meant for an unattended
supervisor as much as a human:

| code | meaning | what to do |
|---|---|---|
| `0` | every task is done | the steps under "When all tasks complete", then `/verify` |
| `1` | a task could not pass the gate, or the frontier is empty with work left | a human reads the escalation |
| `2` | it refused to start, or the machine failed in a way waiting cannot fix (the mandate gate unapproved, plan not frozen, the agent CLI not on PATH, an unpinned sandbox image) | repair what it names |
| `3` | the machine failed in a way time fixes — agent capacity exhausted, a signal, another run holding the lock. **No task was marked and no retry budget was spent** | re-run later; it continues from the preserved work |

A **session/usage limit is a normal event**, not an incident. The loop exits `3` immediately
rather than sleeping on it, so the waiting belongs to whatever re-runs the command. `rein build
--supervise` carries that recipe in-process: only `3` is retried, each attempt a fresh run, and it
sleeps until the reset time the refusal named, or the interval when it named none:

```sh
rein build --supervise   # [--supervise-interval-sec N], default 900

# if something outside `rein` should own the interval/backoff instead (a fixed one here):
while :; do
  rein build && break
  rc=$?; [ "$rc" -eq 3 ] || exit "$rc"   # anything else needs a human
  sleep 900
done
```

A run stopped that way leaves each unfinished task `todo` with its worktree in place, and the next
run continues that work rather than restarting it. `rein start` and `rein doctor` both say so.

**Relay review questions while the run goes on.** A task review's `question` finding is on the board
(`rein next`) from the moment the task lands. Pass it to the human then: one about what the plan
should say costs a `/revise` now and a reopened task at acceptance.

### When the run outlasts your host's command timeout

Every agent host caps how long one tool call may run, and a real build outlasts that cap.
**"Never poll" is not the same as "never wait"**: an agent whose command was cut off starts checking
on the run, and each check is a launch, a context, and a share of the session limit spent on
learning that the build is still building.

**Wait for it. Your capability mapping says how** — every host has one of these, and they are in
this order:

1. **The host re-enters you when it exits.** Start `rein build --supervise` through that mechanism
   and get on with something else; its exit brings you back on its own. No timer, no check, no
   launch spent.
2. **The tool call itself waits.** Run it in the **foreground** with the longest wait your shell
   tool allows — a timeout parameter you can raise, or a cap that counts *silence* rather than
   runtime. The build prints a `[waiting]` line every minute while a launch or a gate step is in
   flight, so that a wait like that can hold.

**Detach only when neither can hold it** — when the run has to outlive this session, or your host
kills a foreground command by the clock no matter what it prints:

```sh
nohup rein build --supervise > .rein/build.log 2>&1 &   # returns immediately; the run owns the lock
```

Then **end your turn** and let the human bring you back. When you come back, read the run's own
record: `rein start` (what changed and what waits on a human; `--full` adds the board, `rein ui`
serves it in a browser) and `tail -n 40 .rein/build.log`.

A second run cannot start while the first holds the build lock: it exits `3`, which is
indistinguishable from a capacity stop, and a supervisor reading that will sleep on a build that is
running perfectly well. Re-run it only after the first has exited, and only for the exit code it
actually returned.

## When all tasks complete

0. **If this cycle ships as a stack, publish it now — as drafts.** `rein pr-stack` cuts the work
   branch into one pull request per task and prints the `gh pr create --draft` lines; `--push`
   does it, after a confirmation typed at a terminal. Skip this step for a cycle that ships as a
   single pull request.

   From here, **a fix for a review finding is committed onto the slice that introduced the code, not
   onto the work branch**, then carried up by merging. **Never rebase a stack**: it strands every
   `completed_commit` and gate receipt on commits that no longer exist. `rein pr-stack --restack`
   carries up a fix you commit onto a slice yourself.

1. **Answer any open change requests first.** Run `rein changes list --gate acceptance --json`. Each anchors a place (`docs/...#R-3`, `T-004`, `C-001`) and says what is wrong. Then `rein changes address <id> --note <what you changed>`; the note is what the human reads beside the digests before deciding, so "done" is not an answer. An open request holds the acceptance gate shut, and approving is what closes the addressed ones.
2. **The grounded review is the run's own.** `rein build` ends by reading the whole change,
   repairing the findings a task's scope owns, and reading it again from cold. What reaches the
   human is what a machine cannot decide, as cards at acceptance. Run `rein review generate
   --supervise` yourself only when the run says the reading could not be taken, or when
   `review_policy.repair_rounds` is 0, and wait for it the way you wait for `rein build`. When
   `rein build` or `rein doctor` says a reading is over `max_diff_bytes`, tell the human: the answer
   is a narrower task at the mandate gate, never a larger budget.
3. **(Only with GitHub integration)** Run `rein issue-sync` to reflect each task's latest status
   (done → close, etc.) to Issues. Best-effort; do not stop if it fails (auto-skips if
   `github.enabled: false` / gh/remote absent). It stays outside the deterministic loop —
   networking does not belong there.
4. Point to "next is `/verify`": it adds the test plan and the dependency audit, and it is the one
   procedure that presents the acceptance gate. Commit what this phase wrote, and suggest
   `session-compaction` (pre-compact check: `.rein/prompts/rules/gate-workflow.md` "Context budget").
