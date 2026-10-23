# 16. Check for updates and restart from the web page

Date: 2026-10-23
Status: Proposed
Decision authority: Project maintainer. The owner chose the two update channels and the
approach in [#552](https://github.com/blindndangerous/AutoDJ/issues/552); acceptance of this
record is pending.

## Context and scope

A user asked to update AutoDJ from the web page. The owner also wants a Restart button and a
Doctor check for updates. Today an operator updates a source checkout by hand with the
[upgrade checklist](../guides/operations/upgrade.md): stop, back up, check `git status`, fetch,
switch to the release tag, run the launcher's setup, run Doctor's migration check, start again.
A container is rebuilt with Compose. Nothing in AutoDJ can stop and start itself, and nothing says
when a newer version exists.

[#552](https://github.com/blindndangerous/AutoDJ/issues/552) follows other self-hosted programs:
the program reports that an update exists, and whatever installed it does the update. This record
covers the first two steps there: the update check and notice (Doctor, a CLI command and
Settings, Updates), and restarting from the web page. It changes how the source checkout's
launcher runs `serve` (an amendment to [ADR 12](0012-separate-runtime-setup-from-model-execution.md))
and adds browser routes inside the trust boundary of
[ADR 7](0007-separate-browser-worker-and-listener-access.md).

AutoDJ does not update itself under this record. Whether a source checkout started with the
launcher may update from the web page is a later decision in #552. A container gets the notice and
the exact commands; nothing gets the Docker socket. There are no user roles, no automatic updates
and no Windows service.

## Decision

### Channels and the check

- Two channels. **Stable** follows the release tags `vX.Y.Z`; **development** follows `master`.
- An installation infers its channel: a checkout of a release tag is stable, a checkout of
  `master` is development, and anything else is stable. A container reads the branch from the
  checkout its Compose file mounts. A setting saved from Settings, Updates
  (`<index_dir>/shared/updates.json`) can name a channel instead.
- The setting can turn the check off. Then nothing contacts the network: not the page, not
  Doctor, not `autodj check-update`. `doctor --offline` skips the network for one run.
- A source checkout asks its `origin` remote with `git ls-remote`, which reads the same
  references `git fetch` would without changing the checkout, so Doctor still writes nothing.
  An installation without git asks GitHub's REST API, at an address fixed in the code. git runs
  without a terminal prompt, a credential window or an ssh prompt (the checkout's own
  `core.sshCommand` is kept, with `-o BatchMode=yes` added), with a 15-second timeout that kills
  its whole process tree: its own session on POSIX, its own job object on Windows. git's
  transport keeps the pipes open after git itself is killed, so after a timeout nothing waits for
  them. The API call has a 10-second timeout. Only one check runs at a time; page loads while
  it runs wait for it.
- A failure or no network is reported ("could not check"), never an error, and Doctor passes
  it: only an available update is a Doctor warning. The message names no remote address and
  repeats no git output, because a remote address can hold a password. Setup's and restore's
  Doctor runs do not check.
- The result says when the installation is ahead of the latest release, or on a branch other
  than `master`, which no channel follows. The web page caches a result for 6 hours, a failure
  for 10 minutes; Check now asks again, at most once in 10 seconds.

### The notice

- While an update is available, the page, `check-update` and Doctor say so. The page and
  `check-update` link its release notes, because the upgrade checklist asks operators to read the
  Removed and Changed sections: a release's GitHub page for a tag, the changelog on `master` for
  the development channel.
- They also list the commands that update this installation: the checklist's git and launcher
  commands for a checkout, the git and `docker compose` or `podman compose` commands for a
  container, and `uv pip install` of the release wheel for a package. AutoDJ never runs them.

### Restart

- The server exits with status 75 (`EX_TEMPFAIL`), the restart status. Something must start it
  again, and the page offers Restart only when something will:
  - the source checkout's launcher (`autodj`, `autodj.cmd`) now runs `serve` as a child process
    and starts it again when it exits with 75, on POSIX and on Windows. It passes SIGTERM on to
    the child, keeps waiting through Ctrl+C, and on Windows ties the child to the launcher with a
    job object. Five restarts within a minute stop the loop;
  - a systemd service whose `Restart=` is `always` or `on-failure` and whose main process is
    AutoDJ (or AutoDJ's own launcher, which marks its child), read with `systemctl show` for the
    unit in `/proc/self/cgroup`. A wrapper such as `uv run` may not pass on status 75;
  - a container that declares its restart policy in `AUTODJ_RESTART_POLICY`, as `compose.yaml`
    does beside `restart: unless-stopped`. A container cannot see its own policy, so a container
    without the variable gets no button.
- A stop signal always wins over a pending restart, in the server and in the launcher. On POSIX
  the server stays in the launcher's process group and the terminal's foreground, so Ctrl+C, a
  closed terminal and Ctrl+\\ reach it directly and it can still ask a question on the terminal.
  The launcher passes SIGINT, SIGHUP and SIGQUIT on only when they came to it alone (no terminal,
  or not in the foreground), since a second SIGINT makes uvicorn skip its graceful shutdown. It
  always passes SIGTERM on: uvicorn 0.54 treats a second SIGTERM like the first (only a repeated
  SIGINT forces an exit). Its handlers are in place before the server starts. After the server
  exits, the launcher ends with the same signal, so systemd sees a clean stop. A requested
  restart exits 75 without the failed-start hint or the Doctor offer. Shutting down stops a running
  Library tools job, so a restart does not leave one beside the next server.
- Anything else, including a Windows service, which AutoDJ does not support, gets no button and
  a sentence saying to restart AutoDJ the way it was started.
- The installed `autodj` command of a wheel does not loop; it is restarted only by a systemd
  service or a container.

### Who may do it

- These are browser routes like every settings change
  ([ADR 7](0007-separate-browser-worker-and-listener-access.md)): a paired browser session when
  pairing is on, an allowed Origin for writes, and no machine token. There are no user roles, so
  every paired browser may restart AutoDJ.
- Check and restart are audited under their own action names (`update-check`, `server-restart`)
  as well as by the request middleware.
- Restart is limited to one a minute and six an hour, counted in
  `<index_dir>/logs/restart-requests.json`, so a restart does not reset the count. The limit
  fails closed: when the file is there but cannot be read or parsed, or cannot be written, the
  restart is refused with a message that names the file.
- git and `systemctl` run from argument lists, never a shell string, and nothing from a request
  reaches their arguments. See the [threat model](../../THREAT_MODEL.md#updates-and-restart-from-the-web).

## Alternatives

- **Update from the web page.** Deferred to a later decision in #552.
- **`git fetch` for the check.** Gives a commit count, but writes to the checkout, which Doctor
  promises not to do.
- **Restart by sending the server a signal.** The server can stop itself through uvicorn's own
  shutdown; a signal reaches a launcher or a service manager differently on each platform.
- **Automatic updates.** Not asked for; a station should go off the air only when someone
  chooses it.

## Consequences

- A `serve` started through the checkout's launcher is now two processes: the launcher and the
  server. Stopping the launcher stops the server (SIGTERM is passed on; a Windows job object ends
  it with the launcher). A launcher killed with SIGKILL on POSIX leaves the server running.
- Every paired browser can take the station off the air for a moment with Restart. Pairing
  already grants playback control, which is at least as disruptive.

Revisit when AutoDJ gains user roles, a Windows service, or the later #552 decision on updating
from the web page.

## Implementation and operational references

- [Update check, notice and restart detection](../../src/autodj/updates.py),
  [web routes](../../src/autodj/updates_api.py), [launcher restart loop](../../src/autodj/launcher.py),
  [Doctor's check](../../src/autodj/doctor.py) and the `check-update` command in
  [cli.py](../../src/autodj/cli.py).
- [Settings, Updates](../../src/autodj/static/modules/updates.js) in the
  [page](../../src/autodj/static/index.html).
- [Back up and update AutoDJ](../guides/backups-and-updates.md#check-for-updates-and-restart)
  and the [upgrade checklist](../guides/operations/upgrade.md).

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression test is mapped;
structural validation does not execute it or accept this ADR.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0016-01` | A release-tag checkout follows stable, `master` follows development, a container follows its mounted checkout, a package follows stable; a saved channel overrides. | automated | `tests/unit/test_updates.py::test_a_tag_checkout_follows_stable_and_master_follows_development`; `tests/unit/test_updates.py::test_a_saved_channel_overrides_the_inferred_one`; `tests/unit/test_updates.py::test_detect_installation_reads_branch_and_release_tag_from_git`; `tests/unit/test_updates.py::test_a_container_follows_the_branch_of_its_mounted_checkout`; `tests/unit/test_updates.py::test_a_package_without_git_follows_stable` |
| `ADR-0016-02` | With the check off in the settings, `doctor --offline` or `AUTODJ_UPDATE_CHECK=0`, nothing contacts git's remote or GitHub; setup's and restore's Doctor do not check; settings are validated and saved. | automated | `tests/unit/test_updates.py::test_settings_round_trip_and_damaged_files_give_defaults`; `tests/unit/test_updates.py::test_parse_settings_refuses_anything_else`; `tests/unit/test_updates.py::test_a_check_turned_off_contacts_nothing`; `tests/unit/test_updates.py::test_an_offline_check_contacts_nothing`; `tests/unit/test_doctor.py::test_doctor_offline_skips_the_update_check_network`; `tests/integration/test_updates_api.py::test_settings_are_saved_and_validated`; `tests/unit/test_doctor.py::test_restore_runs_doctor_offline`; `tests/unit/test_setup_flow.py::test_setup_runs_doctor_offline_with_a_time_limit` |
| `ADR-0016-03` | The check compares with the newest release tag or the head of `master`, says ahead and other-branch states, times out, kills git's whole process tree on timeout, reports a failure without the remote's address, runs one check at a time, and is cached. | automated | `tests/unit/test_updates.py::test_an_unreachable_remote_is_a_warning_without_its_address`; `tests/unit/test_updates.py::test_a_git_timeout_is_a_warning`; `tests/unit/test_updates.py::test_an_offline_package_check_is_a_warning`; `tests/unit/test_updates.py::test_stable_compares_with_the_newest_release_tag`; `tests/unit/test_updates.py::test_master_after_a_release_is_ahead_of_it_on_the_stable_channel`; `tests/unit/test_updates.py::test_development_compares_with_the_head_of_master`; `tests/unit/test_updates.py::test_a_branch_other_than_master_is_named_and_not_compared`; `tests/unit/test_updates.py::test_a_package_asks_githubs_latest_release`; `tests/unit/test_updates.py::test_a_container_compares_its_build_stamp_with_master`; `tests/unit/test_updates.py::test_the_api_address_cannot_leave_the_repository`; `tests/unit/test_updates.py::test_the_checker_reuses_a_result_until_forced`; `tests/unit/test_doctor.py::test_doctor_passes_an_unreachable_update_check_with_its_message`; `tests/unit/test_updates.py::test_a_timeout_ends_the_whole_process_tree_without_waiting_on_its_pipes`; `tests/unit/test_updates.py::test_git_never_asks_for_a_password`; `tests/unit/test_updates.py::test_the_checkouts_own_ssh_command_is_kept_and_told_not_to_ask`; `tests/unit/test_updates.py::test_the_operators_git_ssh_command_wins`; `tests/unit/test_updates.py::test_a_timeout_does_not_wait_for_a_pipe_holder_that_left_the_tree`; `tests/unit/test_updates.py::test_closing_a_checks_job_ends_exactly_the_processes_in_it`; `tests/unit/test_updates.py::test_callers_during_a_slow_check_wait_for_it_instead_of_starting_more`; `tests/integration/test_updates_api.py::test_page_loads_during_a_slow_check_share_it`; `tests/unit/test_doctor.py::test_check_update_cli_prints_the_result` |
| `ADR-0016-04` | Restart is offered only under the launcher loop, a systemd service that restarts on failure with AutoDJ as its main process, or a container that declares its restart policy; the checkout launcher restarts `serve` on status 75, never loops twice, and stops a server that keeps asking. | automated | `tests/unit/test_updates.py::test_restart_detection_prefers_the_launcher_loop`; `tests/unit/test_updates.py::test_restart_detection_trusts_a_container_only_with_its_restart_policy`; `tests/unit/test_updates.py::test_systemd_restart_needs_autodj_to_be_the_units_main_process`; `tests/unit/test_updates.py::test_systemd_counts_the_parent_only_when_it_is_autodjs_launcher`; `tests/unit/test_updates.py::test_restart_detection_reads_the_systemd_restart_policy`; `tests/unit/test_updates.py::test_restart_detection_without_a_supervisor_offers_nothing`; `tests/unit/test_updates.py::test_systemd_policy_comes_from_the_unit_in_the_cgroup`; `tests/unit/test_launcher.py::test_runs_serve_finds_the_command_after_global_options`; `tests/unit/test_launcher.py::test_the_restart_loop_starts_serve_again_on_the_restart_code`; `tests/unit/test_launcher.py::test_the_restart_loop_stops_a_server_that_keeps_asking`; `tests/unit/test_launcher.py::test_the_checkout_launcher_supervises_serve`; `tests/unit/test_launcher.py::test_serve_is_not_supervised_twice_or_outside_the_checkout_launcher`; `tests/unit/test_launcher.py::test_a_real_child_reports_its_exit_status`; `tests/integration/test_updates_api.py::test_restart_stops_the_server_and_is_audited`; `tests/integration/test_updates_api.py::test_restart_is_refused_when_nothing_would_start_autodj_again` |
| `ADR-0016-05` | A server that cannot stop itself offers no Restart. | automated | `tests/integration/test_updates_api.py::test_the_view_names_version_channel_and_restart` |
| `ADR-0016-06` | Only a paired browser with an allowed Origin reaches the routes; check and restart are audited; restart is limited to one a minute and six an hour across restarts, and Check now to one in 10 seconds; a restart is refused when its count cannot be read or saved. | automated | `tests/integration/test_updates_api.py::test_only_a_paired_browser_on_this_site_reaches_the_update_routes`; `tests/integration/test_updates_api.py::test_check_now_is_audited_and_rate_limited`; `tests/integration/test_updates_api.py::test_restart_stops_the_server_and_is_audited`; `tests/integration/test_updates_api.py::test_restart_is_rate_limited_across_a_restart`; `tests/integration/test_updates_api.py::test_the_restart_limiter_allows_six_an_hour`; `tests/integration/test_updates_api.py::test_restart_is_refused_when_its_count_cannot_be_saved`; `tests/integration/test_updates_api.py::test_a_restart_time_ahead_of_the_clock_still_counts`; `tests/integration/test_updates_api.py::test_an_unreadable_restart_count_refuses_and_names_the_file` |
| `ADR-0016-07` | Settings, Updates uses native labelled controls, quiet result lines and one status region; Restart asks first, says the stream goes off the air, keeps focus and says once whether AutoDJ came back. | automated | `tests/jsmodules/updates.test.js::uses native labelled controls, quiet result lines and one polite status region`; `tests/jsmodules/updates.test.js::asks before Restart, says it is going off the air, and says when it is back` |
| `ADR-0016-08` | Settings, Updates meets WCAG 2.2 AA in the browser audit and speaks as intended with a screen reader. | manual | Run the visual audit (it visits every Settings subtab) and sample the panel with NVDA; record browsers, screen reader, flows and limits in the pull request. Sampled on 2026-10-23 with NVDA 2026.2 and Firefox 158 on master e7b6ea5b: control names and states, Check now and its rate limit, saving the channel, the Restart dialog (focus on Cancel, Escape back to Restart) and a real restart through the launcher, ending with "AutoDJ is back". It found one wording defect ("1 seconds"), fixed with this row. No person listened; an agent read NVDA's speech. |
| `ADR-0016-10` | A stop signal wins over a pending restart in the server and the launcher; the server stays in the launcher's process group, the launcher passes SIGTERM on (other stop signals only when they came to it alone), has its handlers before the server starts and ends with the same signal; a requested restart exits 75 quietly; shutting down stops a running library job. | automated | `tests/integration/test_server.py::TestServeFunction::test_a_stop_signal_wins_over_a_pending_restart`; `tests/unit/test_launcher.py::test_a_stop_signal_ends_the_loop_even_when_the_server_asked_to_restart`; `tests/unit/test_launcher.py::test_a_signal_to_the_launcher_reaches_the_server_and_ends_the_launcher_with_it`; `tests/unit/test_launcher.py::test_a_stop_that_comes_while_the_server_starts_still_stops_it`; `tests/unit/test_serve_doctor_offer.py::test_a_requested_restart_exits_quietly_without_offering_doctor`; `tests/unit/test_jobs.py::TestJobShutdown::test_shutdown_stops_a_running_job_and_waits_for_it`; `tests/unit/test_jobs.py::TestJobShutdown::test_shutdown_kills_a_job_that_ignores_stop`; `tests/unit/test_jobs.py::TestJobShutdown::test_shutdown_when_idle_does_nothing`; `tests/integration/test_updates_api.py::test_shutting_down_stops_a_running_library_job` |
| `ADR-0016-09` | While an update is available, the notice links its release notes and lists the commands for this kind of installation, which AutoDJ never runs. | automated | `tests/unit/test_updates.py::test_an_available_release_links_its_release_notes`; `tests/unit/test_updates.py::test_the_notice_gives_the_commands_for_each_installation`; `tests/integration/test_updates_api.py::test_check_now_is_audited_and_rate_limited`; `tests/jsmodules/updates.test.js::links the release notes and lists the update commands while an update is available` |

## Verification limits

Tests stand in for git, GitHub, systemd and the server's exit. They do not show that a real
systemd unit or a container runtime restarts AutoDJ. A restart through the Windows launcher was
run once by hand against a temporary server; record other runs separately when they are made.
No screen reader has sampled Settings, Updates.
