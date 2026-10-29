# Record and split the broadcast

Configure a recording folder, start recording, and check split, retention and disk-failure behavior.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Recording the broadcast

Recording needs stream output. It saves the encoded MP3 that listeners receive, including
liners and output processing. Starting a recording keeps the station playing without listeners.
Set `recording.folder` in the station's `station.json` definition. The browser and the API can
neither change nor see the folder.
The default is `recordings`. Files use the station time zone and names such as
`AutoDJ-2026-10-11_13-00-00.mp3`. The station name is reduced to safe filename characters.

In Settings, General settings, Broadcasting, Broadcast recording, the **Record broadcast** toggle button starts and
stops recording (screen readers say whether it is pressed), and **Split recording now** starts
a new file. Under **Start a new file**, **Every clock hour** (`split = "hour"`) splits at
each clock hour, and **For each show** (`split = "show"`) splits when the operator presses
Split, when a scheduled Start or Split recording event runs, or when a profile is applied.
The scheduler also has Stop recording. A new encoder, such as after a bitrate change, starts
a new file.
The running choice survives a server restart; a new dated file begins when audio resumes.

`recording.retention_days = 0` keeps files forever. A positive value deletes only dated
MP3 files in the configured folder whose modification time is older than that many days.
Cleanup runs in the recorder worker about once an hour. Set `free_space_mb` to the minimum
free space that must remain on the recording volume. The defaults are 0 days and 100 MB.
The station definition supplies startup values until you save recording settings in the web
page. From then on the saved split, retention and free space, and whether recording was running,
live in the station's `recording.json` and win over the definition. Backups include `recording.json` but not
the recorded MP3 files; archive the recordings folder yourself. A damaged `recording.json` is
renamed with a `.bad` suffix and recording starts stopped with the station definition settings.

If disk space drops below the floor, a write fails, or the recorder queue fills, recording
pauses and reports the error in Settings and the server log. The stream keeps playing.
The writer tries a new file when space and audio return; audio missed during the failure is
not backfilled. A failed file may be incomplete.

For command-line automation, use the paired HTTP API against the running server:
`GET /api/recording` reports status, while `POST /api/recording/start`,
`POST /api/recording/stop` and `POST /api/recording/split` control it. The requests require
the same paired session cookie as the web UI when LAN authentication is enabled. There is
no separate `autodj record` CLI command because the existing CLI has no authenticated
client for a running server.
