# Verify documentation and retain evidence

Use this guide to verify a documentation change and report what remains unknown.
The procedures below cover the documentation reorganization, all thirteen
[tutorials](../tutorials/README.md), and changed operational examples. They do
not claim that a walkthrough has happened. Start every evidence entry as
**pending** and update it only from retained results.

Before the PR, every row has a repository command or the manual procedure here.
Deterministic checks belong in existing CI. Disposable native Linux/Bash and
Windows/PowerShell execution environments, and the four independent reader
journeys, are supplied upstream after the PR. Keep those results pending until
run; upstream reviewers decide merge. This guide adds no separate approval gate.
Existing contributor and release verification requirements still apply.

## Choose the evidence

| Coverage | Procedure | What it establishes | Initial status |
| --- | --- | --- | --- |
| Local links, published anchors and ADR mappings | Structural commands below | Candidate links and declared mappings resolve, including old public entry points | pending |
| Changed configuration and command examples | Example commands below, then native execution protocol | Parsing/source agreement separately from execution | pending |
| Rendered documentation | Render protocol below | Headings, link names, code and tables remain navigable in the rendered document | pending |
| All 13 lessons | Tutorial protocol and individual matrix below | End-to-end results on one declared OS/browser/real screen-reader combination | pending |
| Changed install, diagnosis, backup, isolated restore and update procedures | Native execution protocol on Linux/Bash and Windows/PowerShell | Actual commands and effects on each named platform | pending |
| Unchanged content moved into 48 destination pages | Preservation protocol below | Source preservation and review, without claiming those procedures ran | pending |
| Learn, Do, Look up, Understand navigation | Independent reader protocol below | An unfamiliar reader can find the intended page and observable answer | pending |
| Listening, physical devices, real services | Named tutorial prerequisites and evidence record | Only the specific equipment, service and listening observation used | pending |

A source test, mock, screenshot, browser DOM assertion or accessibility-tree
snapshot cannot substitute for native execution or real screen-reader speech.
Offline fake-provider results must say **offline fake provider**; they establish
neither real-service compatibility nor synthesis quality. Do not make paid
synthesis requests or broadcast publicly without separate authorization.

## Run deterministic checks

Run from the candidate checkout with contributor dependencies installed. These
are executable repository commands, not sample outputs:

```bash
uv run --frozen --no-sync python scripts/check_architecture_docs.py
uv run --frozen --no-sync python scripts/check_architecture_docs.py --include-untracked
uv run --frozen --no-sync pytest -q --no-cov -p no:cacheprovider tests/unit/test_architecture_docs.py tests/unit/test_documentation_examples.py tests/unit/test_documentation_rendering.py
```

The first command follows tracked files; the second includes new candidate
pages before staging. The pytest files exercise the checkers, renderer and documented
configuration parsing; `--no-cov` avoids applying the full-suite coverage floor
to this focused selection. Neither executes a lesson or fetches external links.
The published-anchor fixture also preserves entry points with no incoming local
links. Inspect the actual CI result at the final revision rather than treating
a local command listing as a result.

For command-example validation, use the repository checker:

```bash
uv run --frozen --no-sync python scripts/check_documentation_examples.py --shell bash
uv run --frozen --no-sync python scripts/check_documentation_examples.py --shell powershell
```

The checker selects the three authored guides. To execute the curated safe
fixture for the selected shell, run its explicit safe mode on that native host:

```bash
uv run --frozen --no-sync python scripts/check_documentation_examples.py --shell bash --run-safe
uv run --frozen --no-sync python scripts/check_documentation_examples.py --shell powershell --run-safe
```

There are two curated fixtures in total, **one per invocation/shell**, not two
per command. Record which examples it ran, skipped or could not check. A PowerShell parser on Linux
is still Linux evidence. Full setup, backup, restore and update use the native
procedure below. Placeholders such as `vX.Y.Z`, sample addresses, paths, voice
IDs and credentials require deliberate substitution; they are not runnable
values or verified results.

When application behavior is relevant, select the real tests by the affected
source and ADR mappings, not by a documentation filename. The established full
Python gate is `uv run python scripts/ci_pytest.py`; frontend gates are
`npm run lint`, `npm test` and `npm run deadcode`. Use the existing CI jobs;
do not start a second heavy run while another is active.

For the running app audits, follow [accessibility testing](../accessibility-testing.md):
install Playwright browsers as documented, start the isolated application and
point `AUTODJ_URL` at its actual address. In Bash, for example:

```bash
AUTODJ_URL=http://127.0.0.1:8080 AUTODJ_BROWSERS=chromium npm run audit:ci
```

Here `8080` must match the actual server. In native PowerShell, set the two
process variables with `$env:AUTODJ_URL` and `$env:AUTODJ_BROWSERS` before
`npm run audit:ci`. These audits include stand-ins and empty-library paths;
record their coverage separately from populated tutorial and speech evidence.

## Review the rendered document

The repository renderer previews its plan as JSON without writing. By default it
selects tracked and untracked Markdown. Choose a previously absent output
folder outside the checkout; substitute your own new folder in this example:

```bash
uv run --frozen --no-sync python scripts/render_documentation.py
uv run --frozen --no-sync python scripts/render_documentation.py --output /tmp/autodj-doc-review-unique --write
node tests/playwright/documentation_audit.mjs /tmp/autodj-doc-review-unique
```

The audit emits JSON to stdout; its optional `--write` persists `report.json` in
the rendered folder. Record the exact invocation, selected pages, renderer and
browser versions and retained results. The renderer's optional `--base REF`
selects changes relative to a recorded revision; use the default all-page scope
for the full tutorial/navigation review. These checks inspect documents, not a
running app or actual speech. On Windows substitute an absolute new Windows
output path. For the manual review, open the rendered pages, or the upstream
GitHub PR's rendered view at the recorded revision and URL.

The existing CI browser job is configured to render documentation after installing
its pinned browsers and before starting the application. It runs
`python scripts/render_documentation.py --output "$RUNNER_TEMP/documentation-render" --write`,
then `node tests/playwright/documentation_audit.mjs "$RUNNER_TEMP/documentation-render" --write`
with `AUTODJ_BROWSERS=chromium,firefox`. The `playwright-audit-reports` artifact
retains the render manifest and audit report for inspection; the Python checker
and renderer tests run through the existing Python CI suite. This describes the
configured lane, not a completed CI result. Its rendered-document checks do not
execute tutorials, native operational procedures or real screen-reader speech.

For every lesson and changed navigation/operation page:

1. Traverse headings with the declared screen reader's heading commands. Check
   that the title and nested steps form a useful outline without skipped levels.
2. Traverse links and follow the prerequisite, task, reference, explanation and
   return links relevant to the page. Verify their destination and heading.
3. Read each code block and table in reading order. Check punctuation, shell
   boundaries, column headers and long path wrapping; ensure required recovery
   advice is in the normal reading flow, not accessible only through hover.
4. At narrow width and increased zoom, verify steps and code remain readable and
   no required information disappears. Capture viewport/zoom and defects.
5. Retain page URL, keyboard actions and real speech output. Record visual
   inspection, automated rendering checks and speech sampling as separate methods.

## Prepare native disposable environments

Use a fresh Linux VM with Bash and a fresh Windows VM with native PowerShell,
not WSL, Wine or a translated shell for the Windows result. Record OS build,
shell version, CPU/GPU, free space, Git, uv, FFmpeg, Python and dependency
versions. Snapshot each VM before setup. Use a throwaway account with no
production mounts, credentials, live station or public relay. Allow downloads
needed for setup; decline optional test import unless that import is the named
check. Do not equate CPU evidence with GPU support.

Use separate checkout, configuration, index, models, archives and recovery
folders inside the VM. Review locally all inherited `AUTODJ_*` values and
`config.local.toml`; remove production overrides from the test process without
printing secret values. Follow [core operations](../guides/core-operations.md)
for both native shell blocks and its TOML quoting constraints. Before any write,
verify effective index/model paths with Doctor and inspect restored station
settings for external paths, local commands, audio devices, schedules and relay
connections. If isolation cannot be established, stop and record the blocker.

Create a small recognizable practice library with permission to use its files.
Record a sentinel: Practice category count, active wheel and step settings,
queued title, and History entries. Retain a source snapshot or hashes of the
station data and original audio before restore. Keep a separate sentinel file
outside the recovery destination and record its hash. Nothing in the recovery
exercise may change that sentinel or write into the source installation.

## Execute changed native procedures

First inventory the candidate against its recorded source revision using
`git diff BASE -- docs CONTRIBUTING.md README.md` (replace `BASE` with the actual
revision). List each changed command block and materially changed procedure by
page/heading and map it to a native run record, tutorial record, or explicit
pending entry. Inspect untracked new pages separately with `git status --short`;
a diff alone omits them. Reconcile that inventory at the final candidate so the
five core operations below cannot silently stand in for all changed procedures.
Unchanged moves receive the preservation review instead.

Run each changed command example and materially changed procedure from its page,
one command at a time, on **both** native environments. Record substitutions,
stdout/stderr, exit status and resulting state. Do not use only the safe checker
as evidence for these rows. The existing guides supply executable shell blocks;
the observations below make the verification repeatable without copying them.

| Operation and source | Actions and expected evidence | Failure/recovery and cleanup |
| --- | --- | --- |
| [Install](../guides/install.md), [setup](../setup.md), [native install](../guides/core-operations.md#install-and-diagnose) | In a fresh checkout run the appropriate launcher, review the plan, complete CPU setup, decline optional import; retain verified tensor/import checks, saved runtime selection and printed loopback URL. Open the page and verify an empty library. | Capture a refused/missing prerequisite and its instructions before correcting it; do not report a dry-run as installation. Stop the process and retain setup logs/snapshot. |
| [Diagnosis](../guides/operations/diagnose.md), [collect diagnostics](../guides/collect-diagnostics.md) | Run the documented no-fix and JSON forms with the same configuration/environment. Retain exit codes and required findings, including expected empty-library findings; compare source state before/after read-only checks. | Separate no-fix preflight from repair offers. Apply a repair only to the disposable copy after recording the proposed action and expected effect; rerun Doctor and retain both reports. Redact secrets before sharing. |
| [Backup](../guides/operations/backup.md), [native archive](../guides/core-operations.md#capture-diagnostics-and-create-a-backup) | With the sentinel state present, create a new archive; inspect ZIP members and integrity as in the recovery lesson. Confirm declared inclusions and exclusions; retain archive hash, manifest and source version. | Repeat the same destination to observe no-clobber refusal and verify its hash unchanged. Never add `--force`. Retain the archive and separately protected excluded assets. |
| [Restore](../guides/operations/restore.md), [native rehearsal](../guides/core-operations.md#rehearse-recovery-into-a-new-destination) | Stop serving and jobs. Restore the same archive/version into a new isolated destination; retain effective paths, restored-file count, Doctor findings and recovered sentinel state. Run the recovery lesson's simulation against that copy; verify source and outside sentinel unchanged. | Try the documented existing-destination refusal in the disposable copy and confirm no overwrite. Keep failures/journals for diagnosis; use documented rollback advice rather than removing a journal. Retain the recovered copy and archive. |
| [Update](../guides/operations/upgrade.md), [native update](../guides/core-operations.md#update-and-keep-a-rollback-path) | Record old and target revision/version, stop jobs, take final backup and preserve config/assets; check clean status, fetch/select the named target, run launcher setup and no-fix Doctor, then loopback playback and footer-version checks. | Retain and review migration previews before any `--apply`; do not repair production. Rehearse rollback into another isolated destination using the matching old-version archive, not newer data. Stop the test server and retain both environments. |
| [Station output, restart and time zone](../guides/several-stations.md), [station setup](../guides/prepare-station.md), [schedule](../guides/schedule.md) | On each native platform record selected station and initial settings; save output, stop/start that station and verify the running mode with its relevant controls and real audio path. Save a test station time zone, restart the server as instructed, and verify Next events against that zone without changing the system clock. | Inspect startup errors before retrying; distinguish station restart from server restart. Restore output/time zone, disable test events, restart as required and retain final state. Never restart a server serving other listeners. |
| [Access creation, revocation and recovery](../guides/manage-access.md) | On each native platform use the documented launcher/configuration for device list/pair/revoke and token list/create/revoke with disposable IDs. Pair a second test browser, verify revocation ends its access, and check a replacement token's intended scope using only the private local test client. After isolated restore verify credentials excluded from backup, reset a rehearsal DJ password, and pair again as documented. | Retain redacted outcomes, never raw tokens/passwords; use only throwaway access. Review reset-all consequences before any reset. Restore separate private credentials only in the disposable copy; external provider/relay authentication remains pending without separately authorized services. Revoke every rehearsal credential and stop test clients. |


Use a prior released version and the candidate as the declared update pair when
verifying candidate behavior. The guide's `vX.Y.Z` remains a placeholder; record
the actual substituted revisions and any difference from its release procedure.
If a platform is unavailable, its rows stay pending with an upstream owner.

## Walk through every tutorial

Choose and record **one complete OS/browser/real screen-reader combination** for
all thirteen lessons. Additional combinations can add evidence but do not fill
a missing lesson on that declared combination. Use actual keyboard navigation
and capture what the real screen reader speaks in the document and the app.
An agent may send keys and capture NVDA speech, as described in
[release sampling](../accessibility-testing.md#release-sampling). State whether
a person listened; captured speech is not a human listening judgment.

For each matrix row, follow the linked lesson from its stated starting state
through every worked step, checkpoint, recovery instruction, prediction exercise
and stop/cleanup section. Do not jump directly to its expected final state.
Record each checkpoint's actual title/count/status and speech, not just “page
opened.” For recovery advice, safely reproduce a recoverable condition in the
disposable copy (for example an empty queue or a wrong practice category), then
follow the written recovery. Where inducing a failure would spend money, lose
data or require unavailable hardware, record a source review and leave that
recovery execution pending. Never invent a passing recovery result.

Shared setup can be reused only when candidate bytes, configuration, environment,
options and exact starting state match. Record the reused evidence ID and compare
state before continuing. A previously saved Practice wheel alone does not prove
Browser output, empty queue, disabled schedule or completed analysis. Follow the
lesson's reset instructions after branches; do not silently replace live steps
with mocked responses or simulated playback.

Each row inherits the full protocol above. The distinctive checks below are
waypoints, not replacements for lesson instructions. **All rows initially pending.**

| Lesson | Distinctive checkpoint and exercise | Recovery to inspect/run; final state to record |
| --- | --- | --- |
| [First station](../tutorials/first-station.md) | Import finishes; category count at least five; saved active one-step wheel and Clockwheel mode; empty-queue automatic titles/History; queued song wins then rotation resumes. | Correct unreadable folder/zero count or unsaved activation using lesson advice; leave Browser paused with Practice saved. |
| [Two categories](../tutorials/two-categories.md) | Anchor count 1 plus other count equals Practice; observe four automatic picks and wheel wrap; three-step exercise changes song-step share, not guaranteed airtime. | Correct Match any to Match all or wrong category; leave paused and record which wheel is active. |
| [Scheduled show](../tutorials/scheduled-show.md) | Two-song list; future event in station time zone; Recent runs, soft handoff, ordered titles and return to rotation; disabled event's Run now still acts. | Reschedule past time or fix empty list without changing system clock; leave event disabled, show stopped and Browser paused. |
| [Similarity picking](../tutorials/similarity-picking.md) | Each practice track's Advanced analysis Complete; empty queue/Similarity mode and no fallback; automatic follow-up then queued override and resumed picking. | Inspect failed/skipped analysis or problem message; leave paused, jobs finished/stopped, record restored Clockwheel mode if returning to Practice. |
| [Voice tracking](../tutorials/voice-tracking.md) | Real microphone take is not booked; private preview; explicit booking reaches aired; discarded second take creates no booking. | Recover microphone refusal/insecure context or stale pair; stop capture/previews, discard draft, stop server audio and restore Browser/break/transition settings. |
| [Beatmatched set](../tutorials/beatmatched-set.md) | Real A/B BPM/structure; natural A-to-B plain and matched transitions, ratio when applicable; zero stretch comparison for unequal tempos. | Fix missing structure or choose suitable tracks/encoder; retain actual listening judgment separately from plan text; restore recorded settings and Browser paused. |
| [Recorded liners](../tutorials/recorded-liners.md) | Upload and local audition precede placement; queued liner and ducking comparison; explicit queue still acts with automatic liners disabled. | Resolve filename collision without overwrite or refused Test now; stop previews and retain asset with lesson's final automatic-liner state. |
| [Speech liner](../tutorials/speech-liner.md) | Check two explicit candidates before synthesis; independent sample results, explicit retry, saved audio separate from assignment; assignment with automatic playback off. | Use actual failed-result advice without automatic resubmission; cancel/wait and stop previews; retain selected liner and record enable state. Without authorized provider access this row stays pending, including listening. |
| [Stream to second device](../tutorials/stream-second-device.md) | Actual second device plays copied LAN stream address; controller versus listener behavior; last listener leaves and set ends after grace period with continuous playback off. | Correct localhost address/firewall only in trusted test network; stop VLC/Listen here and restore Browser paused. |
| [Listener requests](../tutorials/listener-requests.md) | Listener submits, operator approves, request reaches playback; turning off requests leaves waiting request unapproved, then reject and verify no queue entry. | Handle Already on the way using a different song; restore request/rotation/output settings and pause Practice. |
| [Live handoff](../tutorials/live-handoff.md) | Real BUTT source takes over, Live line identifies source; disconnect returns to automation; second segment tests disconnect rather than pausing a song. | Follow failed connection/login guidance with correct mount/port/account; stop BUTT, remove rehearsal credential and restore practice output. |
| [Backup recovery](../tutorials/backup-recovery.md) | New ZIP passes inspection; explicit isolated config/paths; recovered Practice and six seeded picks; seed change never alters original History. | Correct source config or choose fresh destination on refusal, no force; retain archive/copy and verify original station intact. |
| [Two stations](../tutorials/two-stations.md) | Off-air creation, shared music without reimport; separate queue marker/history/wheel; page switch versus stopping runtime. | Stop on existing ID/folder, inspect station selection/FFmpeg; leave Practice Two off air with autostart off, remove original marker, Browser paused. |

Physical microphone capture, server sound card playback, second-device listening,
source-client handoff, model execution and actual musical/speech quality need the
named resources. A simulated result does not close these entries. Record any
unavailable prerequisite and owner; keep the lesson pending rather than quietly
reducing its outcome to a UI check.

## Check moved content preservation

For the 48 operation destinations, retain the source revision and a mapping from
each old section/anchor to its destination. Review the old source and new page
side by side, including warnings, defaults, restrictions, examples and recovery
advice. Mark separately any intentionally changed instruction and route it
through the changed-procedure protocol. Review migration stubs and return links.

For this migration, inspect the pre-move source with
`git show 9c4db940^:docs/operations.md` and compare each section with its recorded
destination; record the actual baseline and candidate commit. This command only
reads Git history.

Run the structural checker above against the published-anchor fixture at
`tests/fixtures/documentation/public-anchors.json`. Use `git diff` against the
recorded pre-move revision to review the content, not only the filenames or link
counts. Record every destination reviewed, its source sections, reviewer and
result; investigate omitted or changed text. Source preservation establishes
continuity of unchanged instructions, not that 48 procedures have been executed.

## Test four independent reader journeys upstream

After the PR is available, give unfamiliar readers the candidate entry page and
only the task below. Do not supply the expected link or coach navigation. Record
the links/headings they choose, wrong turns, help requests, completion and their
words at a failure. The expected paths are for the observer. Perform these
journeys independently of the author/agent's source review.

| Reader purpose and task prompt | Observer's expected observable path/result |
| --- | --- |
| Learn: “Build a small automatic station, then learn one further skill of your choice.” | Find the tutorial index, complete first-station and one later lesson from its prerequisites; explain its prediction exercise, recover from encountered problems and stop safely. |
| Do: “Make a backup and rehearse recovery without replacing this station.” | Find backup/update or core-operations guidance; inspect archive and restore to isolated paths, identify exclusions and retain the source station intact. |
| Look up: “Find what this output mode supports and the default/value range of a setting it uses.” | Reach feature availability and the named settings reference; report the exact value/restriction with its heading/link, without treating a tutorial example as the default. |
| Understand: “Explain why changing the control page's station does not stop another station and what they share.” | Reach station explanation and, if needed, station operations; distinguish station-owned queue/playout/settings from shared library/server, citing the relevant text. |

For the Learn journey, one uncoached unfamiliar reader must complete
first-station and one later lesson, not just find their links. Record where
instructions fail, fix the documentation, and repeat the affected steps with
retained before/after evidence. Author-led demonstrations do not fill that row.
Do not claim these reader journeys have occurred before the upstream runs.

## Retain an evidence record

Put results in the PR or linked issue, with durable artifact links and an owner
for pending items. Keep private diagnostics/credentials out of public artifacts.
Use one record per distinct method/environment and link reused setup records.
This is a record template; values below are placeholders, not results:

```text
Evidence ID; matrix row/lesson and exact headings/steps:
Candidate commit and documentation/runtime bytes (dirty diff if any):
Date; operator/agent; upstream owner:
Status: pending | passed | failed | blocked | source-reviewed only
OS/build; native shell/version; hardware/devices:
Browser/version/profile; real screen reader/version/configuration:
Configuration/effective paths; starting state; dependencies; actual options:
Setup evidence reused and why inputs/state match:
Actions/commands with substitutions; exit codes:
Expected checkpoint; actual observable result and speech capture:
Method/provenance: source review | static | rendered | native execution | real AT
Provider: none | offline fake provider | named authorized real service
Person listened: yes/no; who/what audio; listening judgment and limits:
Recovery and prediction exercise result; cleanup/final retained state:
Artifacts/logs/archive hashes; retained result location:
Defect link; fix revision; affected retest and result:
Untested paths/platforms; remaining evidence; owner and next procedure:
```

Report structural validation, runtime execution, screen-reader capture, human
listening and independent-reader findings separately. A repaired defect needs
an affected-step retest at the fixed revision. Keep other pending entries open;
a green deterministic gate or a new evidence file does not close them.
