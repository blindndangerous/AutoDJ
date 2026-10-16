# Connecting a GPU speech service

AutoDJ's speech studio can reuse the station's configured `time_checks.tts_command`
without an HTTP adapter; see [text to speech](operations.md#text-to-speech). The
following contract applies when connecting an HTTP speech endpoint. The service can
run on a workstation, a rented GPU, or another server. AutoDJ does not install
models, start GPU processes, provision infrastructure, or upload reference voices.
See [speech operations](operations.md#configure-speech-connections) and
[ADR 13](adr/0013-produce-speech-outside-playout.md) for the client boundary.

## Which side needs an adapter?

An existing service that implements the contract below needs no additional AutoDJ
server on the GPU. Select its engine, enter its complete synthesis URL and use its
explicit voice IDs. A native engine with a different API needs a small translation
adapter in front of it, owned and released with that service. Keep model libraries,
CUDA dependencies and service supervision in that deployment's repository.

The Fish, Chatterbox and GPT-SoVITS choices currently implement the same minimal
HTTP shape, with separate engine/version identities in AutoDJ's cache. These are
endpoint profiles, not universal native-engine clients. In particular, a native
GPT-SoVITS `/tts` endpoint or a Fish service expecting another request encoding
cannot be connected merely by selecting that engine name.

During integration, the inspected deployment code's Chatterbox voice probe sent
`input` and `voice` to `/v1/audio/speech`, checked for WAV output, and required an
unknown voice to return 404. That is a matching contract. Its Fish and GPT-SoVITS
runbooks described pending services; they did not establish deployed compatibility.
No GPU service was contacted to establish this documentation. The portable
contract below is authoritative for AutoDJ; no sibling checkout is required.

## Minimal service contract, version 1

Configure the full endpoint path, conventionally `/v1/audio/speech`. AutoDJ sends
one synchronous POST per distinct sample:

```http
POST /v1/audio/speech
Content-Type: application/json
Accept: audio/wav, application/json
Authorization: Bearer <configured-service-key>

{"input":"This is the station ID.","voice":"station-main"}
```

The Authorization header is present only when a key is configured. Authenticate
it at the service or reverse proxy. AutoDJ's browser pairing credentials are not
GPU service credentials. Use HTTPS or an explicitly trusted local connection
through a tunnel; keep an unprotected model listener private.

| Field or response | Required behavior |
| --- | --- |
| `input` | Plain supplied text, at most 5000 characters. Do not interpret it as a command, file path or URL. |
| `voice` | Exact stable service voice ID. Resolve it to a server-controlled voice mapping. Reject unknown IDs; do not substitute a default or random voice. |
| Model and delivery | The service owns its model, language, reference audio, transcript and inference defaults. This profile sends no `model`, speed or instructions; AutoDJ rejects those overrides. |
| Success | HTTP 200 with complete audio bytes. Return PCM WAV for predictable compatibility. No JSON job ID, base64 envelope, download URL, redirect or queued-job polling. |
| Audio bounds | At most 32 MiB received and 32 MiB decoded float allocation, 120 seconds, one or two channels, 8–192 kHz, finite non-silent samples. AutoDJ converts accepted audio to PCM16 WAV. |
| Timing | Complete DNS, connection, synthesis, transfer and decoding inside AutoDJ's 300-second wall deadline. Short socket timeouts also apply; this is not a service latency guarantee. |
| Failure | A non-200 status, preferably 400 for invalid input, 401/403 for authentication, 404 for an unknown voice and 503 for an unavailable model. AutoDJ does not display the upstream body or automatically retry. |

A disconnected client does not prove that GPU work stopped or billing was undone.
Bound concurrent generation and cancellation in the service itself. AutoDJ admits
one speech batch per server, which does not limit other clients of that GPU.

A service adapter should only translate these fields and convert the result to
WAV. It must not accept browser-supplied reference paths, fetch arbitrary reference
URLs, run shell commands from text, or silently try another model/voice. If native
voice selection changes global model state, serialize that operation with synthesis
so simultaneous clients cannot receive each other's voice.

## Voice and model identity

Keep a service-side mapping from each voice ID to the required model weights,
reference WAV and transcript/language. AutoDJ needs the ID, not the GPU's paths.
Changing that mapping or replacing weights under the same URL and voice ID changes
the effective generator. Publish a new connection URL/voice ID, or save the AutoDJ
connection again to invalidate its request cache. Rotate credentials through the
private connection store or environment. Existing saved audio and active time-check
sets remain immutable local assets.

## Verify before connecting a station

1. Pin and record the model, service adapter and voice mapping revisions. Use the
   GPU service's own tools to check model readiness and GPU memory; a listening
   port or a successful health response alone does not prove synthesis works.
2. Configure a dedicated AutoDJ connection and an explicit known voice. **Check
   comparison** validates local configuration only and sends no synthesis request.
3. Explicitly generate a short sample. This is a real provider request and may
   incur charges. Confirm playable WAV, the intended voice, duration, completion
   time, and GPU allocation during that request. Listen to it before production use.
4. Verify unknown-voice refusal at the service boundary using an explicit test
   request. Check that an unavailable model gives a failure rather than a voice
   substitution. Keep any service-side logs free of credentials.
5. Test provider loss and recovery with a comparison: another provider's successful
   sample must remain playable, and an explicit retry should target the failed
   sample. A timeout has unknown billing status; check service records before retry.
6. Only then generate a full time-check inventory, review it, generate the set,
   audition representative minutes and explicitly activate it. Confirm that the
   saved set still plays when the GPU service is unavailable.

The local test
`tests/unit/test_speech_providers.py::TestSpeechProviders::test_owned_worker_posts_audio_without_following_redirects`
exercises the actual AutoDJ child process against a synthetic HTTP service. It
checks the client transport and audio handling, not a particular GPU runtime or
its audible quality. OpenAI and Speechify have separate direct-provider protocols;
do not put their requests through this minimal service profile.
