# Compare speech and prepare generated time checks

Configure approved speech connections, explicitly compare samples, publish a saved liner, or generate and activate a complete reviewed time-check set.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Speech studio

Open **Settings, Voice liners, Speech studio** to produce speech from supplied
text. The studio compares selected voices and saves audio; it does not write
scripts, research announcements, provision services or start a Showcraft workflow.
[ADR 13](../../adr/0013-produce-speech-outside-playout.md) records the ownership and
generation boundaries.

## Configure speech connections

Expand **Speech connections for all stations**. Supply a connection ID, display
name, engine, full synthesis endpoint URL and any API key. These settings are
server-wide; voice presets, comparisons and saved audio belong to the selected
station. Saved keys are write-only in the browser and excluded from backups.
An environment key named `AUTODJ_SPEECH_<ID>_KEY` overrides the saved key; uppercase
the ID and replace hyphens with underscores, for example
`AUTODJ_SPEECH_STATION_VOICE_KEY` for `station-voice`. Environment-managed keys
cannot be changed through the browser.

Supported protocol profiles:

For the GPU service's responsibilities, exact wire contract and compatibility
checks, see [Connecting a GPU speech service](../../speech-endpoints.md).

| Engine | Full endpoint example | Controls |
| --- | --- | --- |
| [OpenAI](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create) | `https://api.openai.com/v1/audio/speech` | Explicit voice and model; speed; delivery instructions for models that support them |
| [Speechify](https://docs.speechify.ai/build/api-reference/v1/audio/speech) | `https://api.speechify.ai/v1/audio/speech` | Explicit voice and model; speaking rate through escaped speech markup |
| Fish, Chatterbox, GPT-SoVITS | Your configured service's full synthesis URL | The minimal service profile accepts JSON `input` and `voice`, returning audio bytes; native APIs may require a service-side adapter |

Enter voice IDs from the provider or service configuration. Model weights and
reference voices remain service responsibilities. Engine names do not guarantee
compatibility with every server implementing that model. The three service profiles
do not use a RunPod control API, SSH discovery or a sibling client script. Selecting
a connection does not demonstrate that its service is running or its credentials
will authenticate.

Use HTTPS. Explicitly allow trusted local network access only for a service or
tunnel you control; this also permits plain HTTP to that connection. Metadata and
link-local addresses, URL credentials, redirects and provider-supplied result URLs
are refused. Generation requests name configured connections, never arbitrary URLs.

## Compare and save speech

Enter a script, select a connection and voice, and add up to eight comparison
voices. Each sample can have its own supported model and delivery settings.
**Check comparison** reports configuration problems and applied or approximated
controls before synthesis. Unsupported controls are refused rather than silently
ignored. OpenAI permits at most 4096 characters; the Speechify profile permits
2000 spoken characters; AutoDJ's overall script limit is 5000 characters.

**Generate selected samples** explicitly submits the chosen requests. Cost is
reported as unknown when AutoDJ has no reliable estimate; provider charges can
apply. Opening the studio, selecting a preset, checking a comparison and listening
to saved audio do not generate speech. Matching validated audio is reused unless
**Generate fresh audio** is selected. Identical candidates share one outcome within
a batch, including when generation fails.

Results remain independent: listen to successful samples while another sample has
failed, then explicitly retry only failures. A timed-out, cancelled or interrupted
request may already have been billed. There is no automatic retry or provider
failover. One speech batch runs per server; another request waits for the operator
to submit it after the current batch finishes. Cancellation stops remaining work;
it cannot promise to undo a provider charge.

**Save liner** preserves the source text, voice/model/settings, capability report
and audio outside liner rotation. **Assign to liner rotation** publishes it to the
station's normal liner folder. Existing liner switches, dates, clockwheel and queue
rules then apply. Saving a liner does not select the station voice. Save a named
voice preset and separately choose **Use as station voice** to seed future work.
Changing a preset does not modify audio already produced.

Unsaved comparison audio expires after seven days. Temporary samples and cache
share a 1 GiB ceiling; saved liners and complete time-check sets are retained
separately. Remove obsolete comparisons when the 100-comparison station limit is
reached. Each response and decoded allocation is limited to 32 MiB, and a sample
must contain sound, have at most two channels and last at most 120 seconds.
The 300-second wall deadline includes name resolution, response headers and audio
decoding in an owned child process. No speech model or provider SDK is installed
inside AutoDJ by this feature.

## Prepare time checks

Select a voice preset and hour format, then choose **Check time-check set**. The
inventory covers all 1440 local minutes, deduplicates identical phrases and reports
matching cached samples and requests still needed. Review that inventory before
choosing **Generate time-check set**; changing the settings requires another check.
Bulk generation can incur many provider charges. AutoDJ reserves free disk space
for cache, comparison copies and the completed set, and stops remaining requests
if the set exceeds its 512 MiB bound.

A partial or failed set remains inactive. Retry failed time checks explicitly.
If unfinished samples expire, the same action explicitly regenerates those samples;
this may incur new provider charges.
After completion, listen to samples, acknowledge review and choose **Activate
time-check set**. Activation validates full minute coverage and local audio before
switching; a failure keeps the preceding active set. The normal Time checks enabled
switch and scheduling rule still govern when announcements air.

The generated set's wording and voice remain fixed until an explicitly generated
replacement is activated. Changing legacy time-check settings or the default voice
does not rewrite the set. Active generated sets use local audio only. Missing or
corrupt clips are skipped with a warning, never synthesized on demand. **Use
existing recorded time-check sources** returns to the existing voice-pack,
recorded-clip and configured TTS-command fallback behavior.

Backups preserve saved speech, station voice presets and complete generated sets,
including the active selection. They exclude temporary samples, unfinished
generation and provider keys. Existing local audio can play after restore without
provider access; configure connections again before new generation. Doctor inspects
local configuration and saved files without calling providers or proving readiness.

## Evidence for workflow improvements

Functional tests cover explicit generation, partial failure, retry admission,
station isolation and preview separation. These checks do not establish better
usability or audible quality than Showcraft. Compare the same station ID through
external generation/import and the studio, recording completion time, repeated
entry, synthesis calls and recovery from one provider failure. Include keyboard
and screen-reader use and listen to provider output. No comparative usability or
live-provider result is claimed by the implementation tests.

AutoDJ and this page were developed with AI assistance.

## Related tasks and contracts

- [Configure recorded time checks and voice packs](time-checks.md)
- [Configure a station text-to-speech command](tts-command.md)
