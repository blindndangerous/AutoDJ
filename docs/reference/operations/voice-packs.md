# Voice-pack filenames and audio assembly

Look up recognized filenames, 12- and 24-hour coverage, ignored-file cases, trimming, gain and joins.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

AutoDJ reads only the files directly in the folder, with the extension `.wav`, `.mp3`, `.flac`
or `.ogg`, and compares letters without regard to case. It recognises these names:

- Hour file: letters, spaces, `_` or `-` that include `h` and no `m`, then a two-digit hour
  from 00 to 23. Examples: `hr_05.mp3`, `H05.wav`, `hour-17.ogg`.
- Exact-hour file: an hour file name with `o` after the digits, either straight after them or
  after one space, `_` or `-`, such as `hr_05o.mp3` or `hrs05_o.mp3`. It plays alone at
  minute 00.
- Minute file: letters, spaces, `_` or `-` that include `m` and no `h`, then a two-digit minute
  from 00 to 59. Examples: `m_56.mp3`, `min 07.wav`.
- `intro` and `outro` (such as `intro.wav`): said before and after the time, joined in the same
  way. They are part of the pack's voice; `intro_clip` and `outro_clip` still wrap the whole
  check.
- `am` and `pm`: in 12-hour mode, said after the time when the pack has both.

At minute 00 AutoDJ uses the exact-hour file; without one it uses the hour file and `m_00`.
Other minutes use the hour file and the minute file. Any other audio file is listed as not
used, and AutoDJ does not guess: a name with both or neither of `h` and `m` before the number
(`hm_05.mp3`), a number out of range (`hr_24.mp3`), a number that is not two digits
(`hr_5.mp3`), a minute with `o`, an `am` file without a `pm` file, or two files for the same
piece (`hr_05.mp3` and `H05.wav`: neither is used, so hour 05 counts as missing). Rename the
files and the next check picks them up.

Hour files are named with the 24-hour clock. In 24-hour mode a pack needs all 24. In 12-hour
mode, when the file for hour 13 to 23 is missing, the file for 1 to 11 is used, and the file
for 12 stands in for 00; so a 12-hour pack with `hr_01` to `hr_12` covers the day, and its
`am` and `pm` files tell the halves apart. A 24-hour mode check never uses a 12-hour file,
which would say the wrong hour.

Joining makes the pieces sound like one sentence:

- Leading and trailing silence is cut from each piece. Silence is anything 45 dB under the
  piece's own peak, measured in 5 ms steps, so soft consonants stay; 10 ms is kept before the
  first sound and 30 ms after the last for the word's natural decay. Only the ends are cut: a
  pause inside a recording stays.
- Each piece is brought to the pieces' average speech loudness, moving no piece more than 12 dB,
  and all of them come down together if one would peak above -0.2 dBFS.
- Neighbouring pieces overlap by 40 ms with an equal-power crossfade. The overlap is exactly the
  10 ms lead plus the 30 ms decay that trimming kept, so only those quiet margins are blended:
  the first word is at full level until its last sound and the next word reaches full level at
  its first sound, with no gap and no click. The joined voice fades in and out over 3 ms.

AutoDJ keeps about 32 MB of joined checks in memory, per pack and minute, and joins again when a
pack file is replaced, added or removed. The check then plays through the voice liner path like
any other, with the same duck and talk-up timing.
