# Matched intro, ad and show APIs and storage

Look up paired station content routes, request limits, durable files and malformed-file recovery for intros, ads and shows.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

For scripts, the routes are `GET` and `POST /api/intros/rules`, and `PUT` and `DELETE
/api/intros/rules/<id>` (`/api/stations/<slug>/intros/rules` for any station), with the browser
session and the page's `Origin`. The body is a JSON object of at most 64 KiB:

```json
{
  "label": "Rock jingles",
  "conditions": [{"field": "genre", "operator": "is", "value": "Rock"}],
  "match": "all",
  "clip_ids": ["<registered liner library ID>"],
  "liner_folder": "D:\\Radio\\Liners\\Rock",
  "priority": "after_artist",
  "chance": 50,
  "cooldown_minutes": 60,
  "not_before": "2026-12-01",
  "not_after": "2026-12-26"
}
```

`clip_ids`, `liner_folder` or both are required; `priority` is `after_artist` (the default),
`before_artist` or `before_song`. A refused rule answers 422 with `detail` and the `field` at fault.
`GET` also lists the liners, liner folders and condition choices the page offers. For pattern
selection, send `filename_pattern`, a `liner_folder`, and an empty `clip_ids` list. Omitting
`filename_pattern` or sending `null` keeps the existing specific-clip/folder selection.
`POST /api/intros/rules/test?path=<indexed song path>` accepts the same unsaved rule body and
returns `expanded_pattern`, `missing_fields`, `matches` and an explanatory `note`. The named
station route is available too, including while the station is stopped.

## Where they are kept

Each station keeps its own: `matched_intros.json` in the station's folder and the audio in its `intros` folder, under names AutoDJ makes up.
Backups include both for the default station. A song intro follows the song's library ID, so a tag
edit or a move the scan recognises keeps it. A library scan that read every folder and found a
song's file gone removes that song's intros and their audio; a share that is not mounted, or a scan
that could not read a folder, removes nothing, and artist intros are never removed by a scan. When
`matched_intros.json` cannot be read just now, the intros last read are used and saves are refused.
An intro in the file that is not valid (edited by hand, say) is skipped and logged, and Track info
says which, under the Matched intros heading; the other intros still play, and every save writes
the skipped one back as it was, so correct it in the file and it plays again. Only a file that is
not a file of intros at all is set aside, as `matched_intros.json.bad-<id>`. The time an intro last aired
is written to the file by the station's once-a-second upkeep, off the audio thread.

For scripts and other stations, the routes are under `/api/intros` and
`/api/stations/<slug>/intros`, with the browser session and the page's `Origin`:

- `GET /api/intros?track_id=<library ID>`: the song's intros (`song`), its artist's (`artist`),
  the song, the limits, and whether the station plays them (`active`, with a `reason`). Without
  `track_id`, every intro of the station (`intros`).
- `POST /api/intros`: `multipart/form-data` with `file`, `track_id`, and optionally `applies_to`
  (`song` or `artist`), `label`, `chance`, `cooldown_minutes`, `not_before` and `not_after`
  (`YYYY-MM-DD`). `201` answers with the intro; a refused field answers with `detail` and `field`.
- `GET /api/intros/<id>/audio`: the clip.
- `DELETE /api/intros/<id>`: remove the intro and its audio.

## Where they are kept

Each station keeps its own: `ads.json` in the station's folder with the spots, the breaks and the rules, the audio in its `ads` folder under
names AutoDJ makes up, and the play log in its `ad_plays` folder, one file a month
(`2026-10.jsonl`). Backups include all three. Every change to `ads.json` is written to a new file
that replaces the old one; a spot or break in it that is not valid (edited by hand, say) is skipped
and logged, the tab says how many, the others still air, and every save writes the skipped one
back as it was. Only a file that is not a file of ads at all is set aside, as
`ads.json.bad-<id>`. The play log is only ever added to: each line is flushed to disk before the
next, a line cut short by a crash costs only itself, and the report says how many lines it could
not read. Nothing on the audio thread writes: the station's once-a-second upkeep books breaks and
writes the log on a worker thread.

For scripts and other stations, the routes are under `/api/ads` and `/api/stations/<slug>/ads`,
with the browser session and the page's `Origin`:

- `GET /api/ads`: spots, breaks, rules, what is waiting, the latest results, the limits, and why
  the station does not air ads (`reason`).
- `POST /api/ads/spots`: `multipart/form-data` with `file`, `title`, `advertiser`, and optionally
  `campaign`, `category`, `start_date`, `end_date` (`YYYY-MM-DD`), `max_per_day`, `max_per_hour`
  and `enabled`. `PUT /api/ads/spots/<id>` changes the same fields as JSON, `DELETE` removes the
  spot, and `GET /api/ads/spots/<id>/audio` serves its audio.
- `POST /api/ads/breaks`, `PUT` and `DELETE /api/ads/breaks/<id>`: JSON with `name`, `minutes`,
  `days` (0 for Monday to 6), `from_hour`, `until_hour`, `start_date`, `end_date`, `max_spots`,
  `max_seconds` and `enabled`. `POST /api/ads/breaks/<id>/run` is Run now.
- `PUT /api/ads/settings`: `separation_minutes`, `late_minutes` and `miss_after_minutes`.
- `GET /api/ads/report?from=YYYY-MM-DD&to=YYYY-MM-DD`, and `GET /api/ads/report/export` with
  `format=affidavit` or `format=summary` for the CSV.

A refused field answers with `detail` and `field`. JSON bodies are at most 16 KB.

## Where they are kept

`shows.json` in the station's folder,
written atomically and included in `autodj backup` for every station (another station's under
`stations/<slug>/index/shows.json`). A show in the file
that is not valid (edited by hand, say) is skipped and logged, and the card says which; every
save writes it back as it was, so correct it in the file and it returns. Only a file that is
not a file of shows at all is set aside, as `shows.json.bad-<id>`; if it cannot be set aside,
saves are refused so nothing is written over it. When the file cannot be read just now, the
shows last read are used and saves are refused. Request bodies are capped before they are read:
8 KiB for a show's settings, generating and starting, and 1 MiB for its list of songs.

For scripts and other stations, the routes are under `/api/shows` and
`/api/stations/<slug>/shows`, with the browser session and the page's `Origin`:

- `GET /api/shows`: every show without its songs, the categories and clockwheels a show can
  use, the show on the air (`on_air`) and any shows the file skips (`problem`).
- `GET /api/shows/<id>`: one show with its `songs` (path, title, length, `missing`) and total.
- `POST /api/shows`: `name`, `source` (`category` or `clockwheel`), `category` (its ID) or
  `clockwheel` (its name), and `target_minutes`. `201` answers with the show.
- `PUT /api/shows/<id>`: the same fields; the list stays.
- `PUT /api/shows/<id>/songs`: `songs`, the file paths in order. A song added must be in the
  library.
- `POST /api/shows/<id>/generate`: optional `start` (a station wall time such as
  `2026-12-24T20:00`) and `seed`. `409` while another generation runs.
- `POST /api/shows/<id>/start`, `DELETE /api/shows/on-air` and `DELETE /api/shows/<id>`.

A refused field answers `400` with `detail` and `field`.
