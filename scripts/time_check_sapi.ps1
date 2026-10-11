# Speak the text on stdin into a WAV file with a Windows SAPI voice, for
# [time_checks] tts_command.  The text is spoken as plain text (not SSML)
# and never run.  -Voice picks an installed voice by name; the default
# voice can be one that writes no audio, such as a screen reader's bridge.
param(
    [Parameter(Mandatory = $true)][string]$OutputFile,
    [string]$Voice = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    if ($Voice) {
        $speaker.SelectVoice($Voice)
    }
    $speaker.SetOutputToWaveFile($OutputFile)
    $speaker.Speak([Console]::In.ReadToEnd())
} finally {
    $speaker.Dispose()
}
