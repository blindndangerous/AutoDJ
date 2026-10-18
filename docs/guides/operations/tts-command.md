# Configure a station text-to-speech command

Set the operator-owned argv command, test its output and understand its use in legacy checks and the speech studio.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Text to speech

The speech studio also offers **Station TTS command** when this station has
`time_checks.tts_command` configured. Select it alongside HTTP connections to compare
samples, save liners, or save a preset and generate a reviewed time-check set.
Voice, model and delivery are fixed by the command; those studio controls are disabled.
Preflight checks configuration without running the command. Commands and arguments
remain file-only settings and are never accepted from the browser or included in
speech manifests. A running/stopped station uses its loaded configuration; restart
AutoDJ after editing its command file.

Studio command generation uses the same stdin/placeholder contract and 20-second
process timeout below. It additionally validates audio against the studio's size,
duration and channel limits. Cancel stops local command work; a command that calls a
remote service can still incur charges. Studio caches are separate from the legacy
time-check cache. Changing the command invalidates studio request identity, and an
old failed comparison must be recreated after a configuration change. Replacing
models or voices behind unchanged command arguments requires **Generate fresh**;
saved liners and activated sets retain their existing audio.


The speech says "It's 3:05", or "It's 15:05" in 24-hour mode. On the hour it says "It's 3
o'clock" in 12-hour mode and "It's 15 hundred" in 24-hour mode, and in both modes "It's midnight"
at 00:00 and "It's noon" at 12:00.

The `tts_command` is an argument array in the station's `station.json`. AutoDJ passes the spoken text on stdin,
replaces `{out}` with a temporary WAV path, replaces `{text}` with the text as one argument,
allows 20 seconds, and caches the result for each command and text. It runs the program
directly, never through a shell, and on a worker thread. A command that fails, times out or
writes a WAV with no sound is skipped with a warning. Examples:

```json
{"time_checks":{"tts_command":["piper","--model","/voices/en_US-lessac-medium.onnx","--output_file","{out}"]}}
```

```json
{"time_checks":{"tts_command":["espeak-ng","-w","{out}","{text}"]}}
```

```json
{"time_checks":{"tts_command":["powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File","C:/AutoDJ/scripts/time_check_sapi.ps1","{out}","-Voice","Microsoft Zira Desktop"]}}
```

The SAPI script speaks the text as plain text and never runs it. Name a voice with `-Voice`:
the Windows default voice can be a screen reader's SAPI bridge, such as NVDA's, which speaks
through the screen reader and writes no sound to the file. To list the installed voices, run
`powershell -NoProfile -Command "Add-Type -AssemblyName System.Speech;
[System.Speech.Synthesis.SpeechSynthesizer]::new().GetInstalledVoices().VoiceInfo.Name"`.

Choose one command, and test it with **Play a time check now**. `intro_clip`, `outro_clip`,
`clips_folder`, `voice_packs` and `tts_command` are local file settings only; the browser
cannot change or read them. The enabled switch, hour format, rule, ticked packs and pack order
live in versioned `time_checks.json` in that station's folder after you change them in Settings;
a key the file lacks takes the station definition value, and a wrong version stops startup with
an error. Backups include `time_checks.json` and the recorded clips; preserve the station
definition separately if you use custom paths or a TTS command.


## Related tasks and contracts

- [Configure recorded time checks and voice packs](time-checks.md)
- [Compare speech and prepare generated time checks](speech-studio.md)
