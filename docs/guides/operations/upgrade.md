# Upgrade or return to an older release

Retain a backup, move a clean source checkout to the desired release, validate migrations, and review downgrade compatibility.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Upgrade checklist

These steps apply to a native installation from a source checkout. A checkout started with
`./autodj serve` or `.\autodj.cmd serve` can run them from the page instead: **Update AutoDJ** in
**Settings**, **Global**, **Update and restart** makes the backup, refuses a changed folder, switches, runs setup and
Doctor, and goes back when the new version needs a migration or does not start (see
[Update from the page](../backups-and-updates.md#update-from-the-page)). Use the steps below by hand
for any other checkout, or to finish what the page could not; after a failed update the page
lists the commands for that failure. For Docker or Podman, see
[Update the container](containers-and-windows.md#update-the-container); its backup, release-note
and Doctor advice is the same.

1. Stop AutoDJ, then create and retain a backup before changing the checkout or its dependencies.
2. Run `git status --short`. If it prints any paths, stop. Commit the changes on a branch or copy
   them outside the checkout. Continue only after `git status --short` prints nothing.
3. Replace `vX.Y.Z` below with the release tag you intend to run. Fetch the tags, then check out
   that release:

   ```bash
   git fetch --tags origin
   git switch --detach vX.Y.Z
   ```

4. Verify or adopt the fetched release's runtime with `./autodj setup` on Linux or macOS,
   or `.\autodj.cmd setup` in Windows PowerShell. Use the launcher so an existing GPU runtime
   is not synced against the CPU development lock. The [core operations guide](../core-operations.md#update-and-keep-a-rollback-path)
   gives both shell paths and rollback precautions; [setup](../../setup.md#runtime-storage-and-development)
   explains runtime adoption and development alternatives. For AMD driver and hardware details,
   see [AMD GPU support](../../windows-amd.md).
5. Read the "Removed" and "Changed" sections of every release since yours in
   [CHANGELOG.md](../../../CHANGELOG.md). Unknown shared configuration keys produce warnings and are
   ignored; delete or rename removed keys and fix invalid supported values before serving.
6. Run `./autodj doctor --no-fix` (Windows: `.\autodj.cmd doctor --no-fix`). If it reports
   library migration needed, stop AutoDJ and preview `./autodj migrate-index` (Windows:
   `.\autodj.cmd migrate-index`). Review the plan, then repeat that migration command with
   `--apply` and run Doctor again through the same launcher.
   `index --force` does not bypass a required legacy migration.
7. Start loopback-only and check that the version in the page footer is the release you checked
   out before enabling LAN access.

## Going back to an older release

Neither the page nor the launcher moves to an older release by itself. When **Update AutoDJ**
moved a release to Development, **Go back to Stable now** can go back to that release with the
backup made just before (see
[Go back to the release you came from](../backups-and-updates.md#go-back-to-the-release-you-came-from)).
Otherwise, restore the backup you made before the upgrade. A newer AutoDJ writes some settings that an older
one reads differently:

- AutoDJ 0.22.0 moves the index folder into `libraries/`, `stations/`, `shared/` and `logs/`,
  copies station settings into each `station.json` and moves play history into each station's
  `station.db`. AutoDJ 0.21.0 and older do not read that layout. To go back, restore a backup
  that the older release made.

- An AutoDJ from before History could show liners reads the liners and other non-music items
  logged since as songs: it lists them in History, the play log and SoundExchange reports, and
  ends a song's `duration_played` at the next one. To go back, restore a backup that the older
  release made.
- An every-N-minutes schedule event that keeps to chosen days: AutoDJ 0.20.0 and older ignore its
  days and run it every day. Edit the event, or delete it, before going back.
- A schedule event with a date range, a category with days, hours or dates, a clockwheel step with
  days or hours, or a cart with play dates:
  an older AutoDJ that does not know these fields refuses the whole file. It sets
  `schedule.json`, `categories.json` (a category with dates included) or `carts.json` aside as
  damaged and starts without it, and leaves such a clockwheel off. Files saved without
  these settings load in either version. Liner play dates are in a file of their own,
  `liner_dates.json`, which an older AutoDJ does not read.
- Voice pack play dates are kept beside the time check switches in `time_checks.json`; an older
  AutoDJ ignores them and lets every ticked pack speak.
- Shows (#106): an AutoDJ without shows does not have the **Play a show** action. It sets
  `schedule.json` aside as `schedule.json.bad-<id>`, starts without those events, and drops them
  for good the next time it saves the schedule. Delete or disable the show events, or keep a copy
  of `schedule.json`, before going back. `shows.json` and `show_on_air.json` are not read by an
  older AutoDJ. From this release on, AutoDJ keeps an event whose action it does not have exactly
  as it was, with every save, so going back after a later release adds actions loses nothing.
