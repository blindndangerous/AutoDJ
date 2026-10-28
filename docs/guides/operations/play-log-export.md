# Export play logs and webcasting reports

Export a date range from History or the CLI, review missing reporting fields, and check licensing requirements independently.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Play log export

AutoDJ keeps every completed play in the selected station's `station.db`, with no expiry. Export the plays between two
dates, both included, for royalty reports and station logs:

```bash
uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --out september.csv
```

Without `--out` the file goes to standard output. A one-line summary goes to standard error: how
many plays were exported, how many songs lack an ISRC, label or other reporting field, and a
warning when some songs have neither an ISRC nor both an album and a label, because SoundExchange
may not be able to match those. Add `--missing` to get the songs that lack a field instead of the
plays, one row each, with the fields they lack, how often they played, and a `matchable` column
that says `no` for the songs SoundExchange may not be able to match. Dates are in the server's time zone; an ISO
8601 date and time also works, for example `--from 2026-09-01T06:00`. Each station keeps its own
play log: `--station NAME` exports another station's plays, and without it you get the default
station's, which are all the plays logged before AutoDJ ran more than one station.

In the web page, open History, go to "Export play log", choose the dates and the format, and press
Export. Check **Include liners** to add the liners, matched intros, time checks and other non-music
items that aired; the SoundExchange format ignores it. AutoDJ says "Exporting play log" and the
Export button is unavailable until the file is ready. Then the browser saves the file and AutoDJ says how many plays it holds, with the same
warnings as the summary, or why it failed.

## Reporting for webcasting licenses

This is general information, not legal advice. Check the current rules for your licence with the
licensing body or a lawyer.

A US webcaster that streams sound recordings under the statutory license in section 114 of the
Copyright Act reports its plays to SoundExchange in a Report of Use. AutoDJ can write one:

```bash
uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --format soundexchange --service "My Station" --out MyStation20260901-20260930.txt
```

or choose "SoundExchange Report of Use" as the format in the History tab, then enter the name of
the service and the transmission category.

- `--service` is the name of the service, and of the filing entity if that is different.
- `--category` is the transmission category code from SoundExchange's template. The default `A`
  covers webcasts that are not simulcasts of a broadcast station; `B` is a simulcast of a
  broadcast station. The command also accepts C, H, I, J, K and L.
- `--channel` fills CHANNEL_OR_PROGRAM_NAME; it is optional.

The report has the 11 columns of SoundExchange's Report of Use templates for commercial and
noncommercial webcasters, in this order: NAME_OF_SERVICE, TRANSMISSION_CATEGORY, FEATURED_ARTIST,
SOUND_RECORDING_TITLE, ISRC, ALBUM_TITLE, MARKETING_LABEL, ACTUAL_TOTAL_PERFORMANCES,
AGGREGATE_TUNING_HOURS, CHANNEL_OR_PROGRAM_NAME and PLAY_FREQUENCY.

- Only songs are reported. Liners, station IDs and ads are not sound recordings.
- There is one line per recording: plays whose artist, title, ISRC, album and label are the same
  once written for the report. `Björk` and `Bjork` are one recording, and so are ISRCs written
  with and without hyphens.
- ACTUAL_TOTAL_PERFORMANCES adds up the performances of the recording's plays (see "Listener
  counts" above). It is left empty when any of those plays has no count.
- PLAY_FREQUENCY is the number of plays. AGGREGATE_TUNING_HOURS is left empty.
- SoundExchange needs an ISRC, or else both the album title and the marketing label, to match a
  recording. A song with neither still gets its line, but the summary and the web page warn about
  it. Run `--missing` and look for `no` in the `matchable` column to find the songs to tag before
  you send the report.
- The layout follows the rules in 37 CFR 370.4 for a file without headers: plain ASCII text, `|`
  between fields, `^` around text and nothing around numbers, no header row, and empty fields
  still separated. Accents are dropped (`Björk` becomes `Bjork`), other characters outside ASCII
  become `?`, and `|` or `^` inside a name becomes a space. The file name AutoDJ suggests follows
  the same rules: the service name, then the first and last day as `YYYYMMDD-YYYYMMDD`, then `.txt`.

Sources, checked in October 2026: SoundExchange's
[reporting requirements](https://www.soundexchange.com/service-provider/reporting-requirements/)
(the "per play, per listener" definition of actual total performances) and its Commercial
Webcaster and Noncommercial Webcaster Report of Use templates linked there,
[37 CFR 370.4](https://www.law.cornell.edu/cfr/text/37/370.4), and the definition of a performance
in [37 CFR 380.7](https://www.law.cornell.edu/cfr/text/37/380.7). Most webcasters report every
calendar month, within 45 days of the month's end.

Royalties for the compositions (the songs as written) are separate from SoundExchange. In the US
they are licensed through ASCAP, BMI, SESAC and GMR, each with its own reporting. Outside the US
the rules and the collecting societies are different.


## Related tasks and contracts

- [Play-log fields, exclusions and listener counts](../../reference/operations/play-log.md)
- [Schedule ad breaks and read the ad report](ad-breaks.md)
