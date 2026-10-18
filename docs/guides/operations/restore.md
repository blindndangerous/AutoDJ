# Restore an archive and recover interrupted restores

Stop writers, restore a compatible archive, resolve any interrupted-restore journal with Doctor, and validate before serving.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Restore and validate

Backup archives are still restricted to the AutoDJ major and minor version that made them.
For an archive made by 0.19, restore it with 0.19 first, then upgrade and convert the index if
needed. Version 0.20 can open an existing schema-4 index directly; the application version bump
does not change that index format. Make a new backup after upgrading to include the station catalog.

Stop AutoDJ, and any `autodj index` or `autodj analyse` run, before you restore: a running AutoDJ
would keep using the files a restore replaces. `autodj serve` holds a lock file,
`.autodj-serve.lock`, in the index folder it serves for as long as it runs, and restore refuses
while it is held ("AutoDJ is serving ...; stop it before restoring"). The operating system drops
the lock when the server ends, however it ends. Restore cannot see an `autodj index` or
`autodj analyse` run, so stop those yourself.

```bash
# Linux/WSL2 Bash
docker compose down
uv run autodj restore --force backups/autodj-2026-08-02.zip
docker compose up
```

For a native Windows process, press Ctrl+C in the terminal running `uv run autodj serve`, then wait
for the process to exit. If a service manager runs AutoDJ, stop that service and wait for it to
report that the process has stopped. Then run:

```powershell
uv run autodj restore --force "backups\autodj-2026-09-12.zip"
```

Restore only accepts an archive made by the same major and minor version of AutoDJ: a 0.19.x
backup restores on any 0.19.x release and on no other. To roll back to an earlier release, check
out that release's tag as in the [upgrade checklist](upgrade.md#upgrade-checklist), then restore a backup made
by that release. Restore also refuses an archive whose file names are absolute or contain `..`,
files the manifest does not list, and an archive without a current catalog. It also refuses file
names Windows cannot hold, such as `CON`, `nul.mp3` or `COM1`, or a name ending in a dot or a
space.

Backup refuses a link that leaves its folder: a file link pointing outside the liner, cart or
other folder it sits in, a state file that is a link to somewhere else, and any linked subfolder
(a symbolic link or a Windows junction). This keeps unrelated files out of an archive. A file
link to another file in the same folder is saved as an ordinary file. Uploaded Stereo Tool
preset links are still left out. Configure a folder directly if it is meant to be backed up.
Hard-linked files are refused because another name may be outside the backup folder.
Restore refuses links stored in an archive and destinations that a link takes outside their
folder.

Without `--force`, restore refuses when anything it would replace already exists. With it:

- The catalog in the archive replaces `library.db`, including its shared records and vectors.
  Each archived `station.db` replaces that station's history, last-played values, and clock state.
  Archives containing old index generations are refused; migrate them with an older release first.
- `dj_meta.db` and `web_state.json` are replaced when the archive has them.
- The liners folder and `index/profiles` are replaced whole: files that are not in the backup are
  deleted. Parts the archive does not hold are left alone.
- Each station's files and folders go back to that station's folder; a station the archive does
  not hold is left alone. A station absent from the destination configuration can be restored
  from a valid archived `station.json`. A removed station's ledger-only `station.db` is also
  preserved without creating a station definition. Other unknown-station contents are refused
  before anything is replaced.

Restore first checks that the archive fits: it refuses one that unpacks to more than 256 GiB,
or to more than the free space beside its destinations. It unpacks every file into a temporary
folder beside its destination and checks the catalog before it replaces anything, so a damaged
archive changes nothing. It then sets every file and folder it replaces aside under a hidden name
(`.<name>.old-<id>`), together with any SQLite journal or WAL files beside a database, puts the
new ones in, and deletes what it set aside only once all are in. If any replacement fails,
everything is put back as it was and restore says nothing was changed. If an old copy cannot be
deleted at the end, the restore still succeeds and names the copy; `autodj doctor` can move it out
of the way.

Each step is written first to a journal, `.autodj-restore.json` in the index folder, which is
deleted when the restore finishes. If AutoDJ, the machine or the power stops part way, the
journal is left behind: `autodj serve` refuses to start while a restore is half done, restore
refuses to start another, and `autodj doctor` reports it and, in a terminal, offers to roll it
back, putting every old file back where it was. A restore that stopped after every new file was
in place only has old copies left, and doctor offers to move them away. Doctor also reports
hidden `.old-` and `.restore-` copies no journal names. It always asks first, even with `--fix`,
and it deletes nothing: whatever it moves goes into `restore-leftovers-<time>` in the index
folder, for you to delete when AutoDJ looks right. Restore then runs `autodj doctor` and says so if doctor
finds a required failure; do not serve until doctor passes. Use the same configuration for backup,
restore, and doctor that the previous `serve` process used. Keep the archive until playback,
profiles and liners look right.


## Related tasks and contracts

- [Back up the installation and its stations](backup.md)
- [Diagnose an installation before serving](diagnose.md)
- [Upgrade or return to an older release](upgrade.md)
