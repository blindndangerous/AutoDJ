# Prepare a station for regular unattended use

Use this guide after a first-play tutorial, before leaving a station running for
an ordinary session. You need a populated library, a chosen output and a recoverable
installation. This is an operator rehearsal, not an uptime guarantee. Keep a way
to stop playback and regain local terminal access.

## Replace tutorial shortcuts with station settings

1. Confirm the selected station by name. Add the real music folders and check
   [import results](add-your-music.md); sample tracks and tiny categories do not
   establish enough eligible music for a full day.
2. Revisit any tutorial step that turned repeat limits off. In **Settings,
   Song picking**, set the intended song/artist/related rules. Check category sizes,
   clockwheel steps, explicit-content rules and date/hour eligibility. There is
   no universal repeat interval that makes every library sufficient.
3. Set the station time zone and check the host clock. Review schedules,
   clockwheels, shows and date ranges for the coming session using the
   [scheduling guide](schedule.md). Confirm what happens around midnight and
   daylight-saving changes if those affect the session.

The expected result is a usable pool for each scheduled period. If a category
is empty or rules repeatedly relax, enlarge the pool or adjust the intended rule
before relying on it. See [selection](../explanation/selection.md) for why a
fallback is possible.

## Test the rotation, then rehearse the output

1. In **Settings, Profile, Song picking**, open **Test rotation**, choose a span covering
   the intended session and relevant schedule boundaries, and run the test.
   Inspect rule relaxations, empty/skipped clock steps, never-played songs,
   fallback picks and dead-air findings. Record the seed and settings when
   comparing changes. See [rotation testing](../operations.md#testing-the-rotation).
2. Treat simulation as a selection check. It uses private history/clock state
   and does not change on-air playback. It approximates durations and scheduled
   timing; automatic liners, time checks, carts, queue/shows and advance booking
   of announcer breaks are not fully modeled. It does not decode the full set,
   exercise hardware, test network continuity or guarantee legal compliance.
3. Rehearse on the actual **Browser**, **Server sound card** or **Radio stream**
   output with the intended devices. Check textual now-playing/history updates
   and playback progress as well as listening for transitions and level problems.
   Verify liners, time checks and shows you rely on through their real procedures.
   See [feature requirements](../reference/feature-availability.md).
4. For a stream, connect and disconnect the real listener/player. Check whether
   the station should continue without listeners and that its continuous/idle
   settings and enabled relays match that choice. Closing the control page is
   not a stop command. Test listener authentication and the address from the
   intended network device; a localhost address works only on the server.
5. Watch CPU, memory, free disk and logs during representative transitions and
   background work. For several stations, use the documented
   [soak procedure](../operations.md#soak-test). Record the actual machine,
   duration, load and observed failures; a short clean test proves only that run.

If a check fails, retain the last working settings, stop the affected output or
integration and [collect diagnostics](collect-diagnostics.md). Fix and repeat
the failed part before resuming the rehearsal.

## Check restart and recovery readiness

1. Save a [backup](backups-and-updates.md) and retain configuration, music,
   external assets and credentials separately. Rehearse a same-version restore
   in an [isolated destination](core-operations.md#rehearse-recovery-into-a-new-destination).
   A ZIP existing is not evidence that the station can be recovered.
2. Check [access recovery](manage-access.md): local terminal access, browser
   re-pairing, replacement machine tokens, listener login, DJ passwords and any
   required provider/relay credentials. Keep them private and reachable when
   the primary browser or computer is unavailable.
3. Stop AutoDJ deliberately, wait for exit and restart through your normal
   launcher or existing service manager. Confirm the selected station, saved
   settings, schedule/time zone and output. Check what actually resumes and
   whether a browser user action or listener connection is required. AutoDJ
   does not install a system service by default; closing a native terminal or
   allowing the host to sleep can end service.
4. Run [Doctor without repairs](collect-diagnostics.md) and check actual player
   access after restart. Review disk needs for recordings and long-track spill
   files, and decide who will check errors and when to retain another backup.

Keep the rehearsal notes with your operational records: release, configuration,
output, devices, session length, results and unresolved limits. Leave unattended
use pending while a required output, restore or access check is unresolved.

AutoDJ and this page were developed with AI assistance.
