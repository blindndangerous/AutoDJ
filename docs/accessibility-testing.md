# Accessibility testing

Speech studio checks must include keyboard-only connection setup, adding and
comparing voices, one failed provider beside a playable sample, explicit retry,
saving versus assigning a liner, station switching during generation and preview,
and reviewing/activating generated time checks. Opening or refreshing the studio
must never submit synthesis. Preview audio must remain browser-local and stop on
station change or authentication expiry. Preserve focus and typed names while
results update; announce outcomes once. Functional browser tests are separate from
manual screen-reader and listening evidence; record both and their limits.

The [Proposed accessibility ADR](adr/0011-verify-accessibility-with-browser-and-speech-evidence.md)
connects these existing contracts to implementation and tests. Its acceptance
is pending; the detailed interaction and release-sampling rules remain here.

AutoDJ uses automated checks and requires a screen-reader sample before release to catch accessibility regressions. The results apply only to the paths that the checks exercise. They do not certify the whole application.

Each modal dialog starts with an H1 title referenced by its `aria-labelledby`.
Verify level-one heading navigation inside the dialog as well as its initial field focus.

## Disposable SSO browser audit

`npm run audit:sso` is a separate manual audit, outside `audit:ci`. It checks
automatic browser registration, names, ownership housekeeping and local logout
cleanup. It does not test an identity provider or establish screen-reader speech.

Use an unused checkout with Python and Node dependencies and Caddy installed.
Keep playback stopped. Create `local/sso-audit/config.toml` with:

```toml
[index]
index_dir = "local/sso-audit/index"
model_dir = "local/sso-audit/models"

[server]
host = "127.0.0.1"
port = 2892
auth_mode = "trusted_proxy"
allowed_hosts = ["127.0.0.1"]
allowed_origins = ["http://127.0.0.1:2891"]
sso_logout_url = "/sso-audit-logged-out"
```

Create `local/sso-audit/Caddyfile` with the following disposable test gateway.
Its fixed account admits every local caller. Keep it on loopback, never deploy
it as authentication, and use the [Authentik tutorial](tutorials/sso-authentik-caddy.md)
for real authorization and browser HTTPS.

```caddyfile
http://127.0.0.1:2891 {
    route {
        respond /sso-audit-logged-out "Audit signed out" 200
        request_header -X-AutoDJ-User-ID
        request_header -X-AutoDJ-User-Name
        request_header -X-AutoDJ-Proxy-Auth
        reverse_proxy 127.0.0.1:2892 {
            header_up X-AutoDJ-User-ID audit-user
            header_up X-AutoDJ-User-Name "Audit user"
            header_up X-AutoDJ-Proxy-Auth {env.AUTODJ_TRUSTED_PROXY_SECRET}
        }
    }
}
```

From the checkout in Bash, disable shell tracing and start both processes with
the same random secret. These commands create only disposable state under `local/`:

```bash
set +x
export AUTODJ_TRUSTED_PROXY_SECRET="$(openssl rand -hex 32)"
uv run --frozen --no-sync autodj --config local/sso-audit/config.toml serve &
autodj_audit_pid=$!
caddy run --config local/sso-audit/Caddyfile --adapter caddyfile &
caddy_audit_pid=$!
```

Wait for both servers to report readiness. Verify that a direct forged identity
returns 401, then run the browser audit in the same shell:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'X-AutoDJ-User-ID: forged-user' http://127.0.0.1:2892/api/status
AUTODJ_URL=http://127.0.0.1:2891 AUTODJ_BROWSERS=chromium npm run audit:sso
kill "$caddy_audit_pid" "$autodj_audit_pid"
wait "$caddy_audit_pid" "$autodj_audit_pid"
unset AUTODJ_TRUSTED_PROXY_SECRET
```

Record browser versions, results and `sso_audit.json` separately from provider
login and manual keyboard/speech evidence. The generated index and browser
records remain under `local/sso-audit/`; remove that directory when finished.

## Published evidence

For 0.21.0, the release sample is recorded in [accessibility-samples/0.21.0.md](accessibility-samples/0.21.0.md).
It was taken on 2026-10-14 on the release branch at b895fdef with the fix for its one blocker
applied, on Windows 11 with NVDA 2026.2 and Google Chrome 155.0.8059.79 (all the flows listed
below), Microsoft Edge 155.0.4283.45 and Mozilla Firefox 157.0.1 (a subset of flows), over HTTPS
with a self-signed certificate. Narrator was not sampled. No person listened: an automated agent
drove the browsers and read NVDA's speech through the screen-reader-testing bridge, with keys
sent to the page in Chrome and Edge and through NVDA in Firefox. The record starts with a summary
of what 0.21.0 ships with, lists the flows with a pass or fail per browser, eighteen defects (D1,
a blocker in the listener request page, fixed before release; D2 to D18 open) and the limits of
the method. It covers only those pairings and flows.

For 0.19.0, the release sample is recorded in [accessibility-samples/0.19.0.md](accessibility-samples/0.19.0.md).
It was taken on 2026-09-29 on the release commit b437a2c, on Windows 11 with NVDA 2026.2 and Google
Chrome 154.0.8037.58 (all thirteen flows listed below), Mozilla Firefox 156.0.1 and Microsoft Edge
154.0.4258.37 (a subset of flows), each in a temporary profile, over HTTPS with a self-signed
certificate. Narrator was not sampled. No person listened: an automated agent sent real key presses
and read what NVDA spoke from NVDA's own log. The record starts with a summary of what 0.19.0 ships
with, lists the flows with a pass or fail per browser, the checks of playback made by reading the
page's audio state behind a zero-gain stage, five new defects (D1 to D5, none a blocker) and the
limits of the method. It covers only those pairings and flows.

For 0.18.0, the release sample is recorded in [accessibility-samples/0.18.0.md](accessibility-samples/0.18.0.md).
It was taken on 2026-09-27 and 2026-09-28 on Windows 11 with NVDA 2026.2 and Google Chrome
153.0.8010.53 (the twelve flows listed at the time; Settings, Browser access was added later), Mozilla Firefox 156.0.1 and Microsoft Edge 154.0.4258.37
(a subset of flows). Narrator was not sampled. No person listened: an automated agent sent keys to
the page and captured NVDA's speech through the screen-reader-testing MCP server, in place of
sampling by hand. The record lists the flows sampled with a pass or fail per browser, eighteen
defects with their status after four later runs on 2026-09-28 that checked the fixes (the last
on master cfb3ac7), and the limits of that method. The step-by-step speech transcripts are in the
git history of that file. It starts with a summary of what 0.18.0 ships
with: D4 (the pairing dialog's name read twice) is accepted, and D14 (NVDA reads a stray line
from the top of the page when the page's confirmation dialog closes) is accepted as a known NVDA and Chrome quirk.
It covers only those pairings and flows. This repository has no sampling record for earlier
releases.

## Programme review rehearsal

Programme review requires a separate Windows/Google Chrome/NVDA rehearsal and
actual listening evidence before its feature issue closes. The operator will run
this from the PR instructions. The steps below are a procedure, not an executed
result; the published release samples above do not verify this new workflow.

Use the exact PR candidate commit and a test station with a saved Show containing
known songs, a repeated song, saved segue cues and a short automatic voice clip.
Prepare a copied non-song interview clip longer than five minutes when checking
long-clip shortening. Configure the existing default TTS command for the
placeholder cases; no live provider is required if a local command is used.
Keep a previously completed review for failure/recovery comparisons. Start a
second station playing when checking isolation and use a distinguishable private
preview device when testing output routing.

Perform every essential action with keyboard and NVDA; record speech and focus
observations, not just whether the control responded. Follow
[Programme review and export](guides/operations/programme-review.md#programme-review-and-export) for the
exact controls.

| Step | Expected observation |
| --- | --- |
| With the target station stopped, open Programme review; refresh, choose Saved show and generate. | Start/range hints are associated with their inputs; Keyboard focus moves from Generate to Cancel before Generate is disabled. Completion returns focus to Generate only when focus remains on Cancel; focus elsewhere is preserved. The station stays stopped; one completion announcement and no automatic playback. |
| Select a saved result and press Load review; play it and tab through item buttons, Previous item and Next item. | Loading moves focus to Review result; labels identify items and both time axes; keyboard seeks retain usable focus; short clips and the actual joins/ducking sound complete. |
| Review a range that cuts a join or short clip. | Effective range explains expansion; no clipped speech or repeated overlapping section. |
| Change opening/ending and tone duration; generate again, then review a long interview. | Song/long-clip middles are omitted, short clips stay complete, skip tones are distinctive and follow the chosen duration. |
| Download MP3 and WAV and play both outside the page. | Both contain the same ordered sections, joins, skip tones and placeholders as the reviewed result; audible codec quality is acceptable. |
| Generate Current queue and Forecast reviews with automatic clips/time checks eligible at the intended start. | Automatic content follows full programme time; current station playback and the other station remain unaffected. |
| Change a test source cue; press Refresh reviews, reload the old result, then generate again. | Out of date is available in text; old audio stays unchanged; explicit regeneration creates a separate result. |
| Temporarily rename a copied test source file; generate with configured TTS, then restore the source. | Incomplete result names the omission and speaks a placeholder; unknown downstream programme times are identified honestly. |
| Repeat the missing-input case with default TTS unavailable; cancel another generation. | Actionable failure/cancellation is announced once; prior review remains available; retry works after repair. |
| Switch stations during a request and while preview audio is playing. | Old audio stops and delayed results do not populate the new station; download addresses follow the selected station. |
| Use a separate preview output, then exercise an unavailable-device fallback. | Sound reaches the chosen private device; actual routing and fallback announcements match the settings and hearing observations. |
| Restart AutoDJ, load a completed review, cancel its deletion, then delete an unwanted review. | Completed audio/exports survive restart without starting broadcast; confirmation identifies the selected date and warns that reviews are excluded from backups; focus starts on Cancel; cancellation keeps the result; confirmed deletion removes only that result and leaves no replacement selected. |

Record the evidence in the PR using this form. Fill in the candidate commit at
execution; do not reuse a prior revision's result after a relevant change.

```text
Candidate commit:
Date/time and tester:
Windows version:
Google Chrome version:
NVDA version and relevant settings:
Playback output mode and devices:
Private preview device and fallback tested:
Test station/show and automatic-content setup:
Rows completed, expected/actual results:
NVDA speech/focus observations:
Listening findings (joins, ducking, tone, placeholder, MP3/WAV):
Failures, omitted steps and remaining limitations:
Evidence attachments or recordings:
```

Browser automation and synthetic audio checks are separate evidence. They do not
prove NVDA speech, actual device routing or audible quality. Missing required
manual/listening evidence keeps `autodj-ovc` open; publishing implementation for
the operator to rehearse does not waive that closure gate.

For the focused Linux/WSL browser journey, with development dependencies and
Playwright Chromium installed, run `npm run audit:reviews`. Set
`PLAYWRIGHT_BROWSERS_PATH` when the browser lives outside its default cache;
`AUTODJ_PYTHON` can select the development Python interpreter. The report is
`.coord/programme_review_audit.json` by default (`AUTODJ_REVIEW_AUDIT_REPORT`
overrides it). This starts a local synthetic station fixture with native review
routes, source capture, jobs and rendering. It checks real controls and downloads,
with deterministic local TTS audio and injected cancellation delay. It bypasses
normal model/hardware startup and does not establish normal-startup, physical
output-routing, speech or Windows/NVDA evidence.

## Continuous integration

Add/Edit forms for categories, intro rules, scheduled events, ad spots and ad
breaks use `editor-dialog.js`. Keep feature validation and API operations in
their panel; pass the save callback the controller's current-request guard and
check it after every await before changing the UI. Close the controller when
station or authentication state is cleared. Save closes onto the saved row's
Edit control; Cancel and Escape return to the opener, asking before discarding
changed fields. A pending save prevents another submission or dismissal.

Use the dialog's own status region while it is open. `showEditorError` places
the error beside its field and links and focuses it; it announces separately
only when focus cannot convey the error. Each editor owns its dialog and draft,
so opening a second editor preserves the first. Test this with nested dialogs,
including Escape, discarded edits, missing return controls and stale replies.
Nested Add category entry points are not currently provided.

Condition rows use the definitions and named controls in `condition-editor.js`.
Category conditions are enabled only for music clockwheel steps and intro rules.
Use `renumberConditionRows` after removal so changed group numbers replace the
legend node. Shared condition help is browsable text, not a repeated field
description. Date and time inputs retain each feature's existing validation and
saved-data semantics.

CI includes selected static accessibility contracts and JavaScript behavior checks. These checks cover live-region markup, durable descriptions, keyboard interactions, color boundaries, reduced-motion styles, and forced-colors styles. Passing CI does not establish WCAG conformance and does not prove what a screen reader will speak.

The visual audit (`npm run audit:visual`, part of `audit:ci`) loads every view and every Settings subtab the server shows, with every card opened, in Chromium, Firefox and WebKit (the engine of Safari). CI runs it in Chromium and Firefox only, so run WebKit locally before merging web UI changes: install it once with `npx playwright install webkit`, start AutoDJ on an empty library, and run `AUTODJ_URL=http://127.0.0.1:<port> AUTODJ_BROWSERS=webkit npm run audit:visual` (leaving `AUTODJ_BROWSERS` unset runs all three). It does so in four theme modes: Light and Dark chosen in Settings (each against the opposite system preference, so the choice has to win) and System under a light and under a dark system preference. In each mode it checks that the stored theme is on the page before any module script runs, runs axe-core's WCAG 2.2 A and AA rules at 1280 px (text contrast, target size, label in name and the rest), and measures non-text contrast (1.4.11) itself: every enabled control's border or fill against what is behind it, the focus rings (every stop in the Dark mode, the first stops in the others), the progress fill against its rail, a tick of every cue kind against the bar, and the Camelot wheel lit for 8A (each wedge edge, the compatible outline and the current wedge 3:1 against their neighbours, the numbers 4.5:1, and the edges heavier from idle to compatible to current). The status message is shown, as a confirmation and as an error, and axe-core runs again with it and its Dismiss button on screen. On Now Playing, when the server knows its commit, the footer's link to it must be underlined, its text 4.5:1 against the footer, 24 px tall, and reached by keyboard with a 2 px focus ring 3:1. Once per engine it also fails if the page scrolls sideways at 320 px wide or with 200 % text at 1280 px, requires every checkbox and radio button to be at least 24 by 24 px, tabs through each view requiring a solid focus outline at least 2 px wide, 3:1 against what is behind it, on a control scrolled fully into the window (so every focus ring is measured in both themes), switches the Theme setting to Dark and back (at once, with no live-region text, kept over a reload), and checks the status message stays while the pointer is over it, goes after the pointer leaves, passes a click on its frame (outside the message and the button) through to a control underneath, and is closed by Dismiss with the mouse (focus staying on the control that had it, which WebKit, not focusing a clicked button, used to drop to the page body) and with Enter reached by Tab from the stop before it, focus going back to that stop. Every measurement waits for running CSS transitions to end, because under reduced motion each change still takes one 0.01 ms transition, and on a busy runner a focus ring was otherwise read mid-change in the control's text colour. That includes the states opened by a click: WebKit was still running the Add step button's pressed-to-hover change when its refused form was measured. After each Tab it also waits for the browser to scroll the focused control into the window, which WebKit does at its next rendering update rather than on the key press. `AUTODJ_CPU_THROTTLE=<rate>` slows Chromium's CPU to imitate a loaded runner. `AUTODJ_SCREENSHOTS=<folder>` also saves a full-page screenshot of every view and subtab in every mode for review by eye. Now Playing's Hour preview is opened with the server's answer stood in for, so its table, with fixed and predicted rows and a long unbroken title, is measured too. On a server in stream mode, Settings, Stream's Listener statistics is shown with a stood-in answer too (every format, a long run of days, large numbers), so its summary, both tables and the session lengths are measured filled; the More formats address list is measured as the server has it. Track info is opened on a stood-in song, so its Song structure list (a long unbroken cue name included; Right bracket must move the cue point preview to the next landmark, say it once in the page status as "Preview: ..." and leave focus where it was), its Tags form, a refused save with its errors, the Save or Discard dialog, and its Matched intros (a song intro skipped now, an artist intro with a long unbroken label, the note naming an intro the file skips, and Upload intro refused with its error linked to the file field) are measured too. It also audits the public status pages: `/status`, and each station's `/stations/<slug>/status` that `/api/stations` lists, including a stopped station's "off air" page. In each of the four theme modes it runs axe-core's WCAG 2.2 A and AA rules, checks that the chosen theme is applied and the background is light or dark to match, and tabs through every stop requiring a 2 px focus outline 3:1 against what is behind it; it also fails if a status page scrolls sideways at 320 px, with 200 % text, or with both at once. Where the server has them turned on (`[player_page]`), it audits each of those stations' player page and embeddable player the same way, and also measures every control's boundary against what is behind it (1.4.11). The embed is expected to follow the system theme rather than the web UI's stored choice, and is visited again with `?theme=light` or `?theme=dark` pinning the other one. Once per player page it checks that nothing loads or plays on its own, that the browser's own audio controls have been replaced by a Play button and a Volume slider with a visible label (or, for a station that does not stream, that the page says so), that the song line is the only live region and the playback line the only status region, and that two Left arrows on the slider lower it by ten, read as a percentage, without moving focus or announcing anything. CI starts its server with the player page and embed turned on for the default station, which does not stream there, so the Play button and slider are audited only on a server in stream mode, such as a local run. Where the server has it on (`[requests]`), each listed station's listener request page is audited the same way, as first shown, after a search that finds nothing and with a search refused, and every control's boundary is measured too. CI turns requests on for its server, so the operator's Listener requests card in Queue & Search is audited there as well. It runs on the empty-library server, so lists that only fill with music, listeners or saved items are not covered, and Stream is covered only when the server runs in stream mode. mode, so CI starts a second empty-library server with `--stream` and runs the audit over it again in Chromium. Settings > Time checks is shown with two stood-in voice packs, one outside its play dates, and visited again with a backwards date range refused, its error on the page and focus on the field. Settings > Song picking is visited again with daypart start hours that give an hour twice refused, the error under the field and linked to it, and focus on the field. Library tools is visited again with a category's backwards season refused and with a new clockwheel's half-filled step hours refused, each error on the page and focus on its field, and its Shows card is shown with a stood-in show on the air, one show's list open with a search result, and Save show refused with its error linked to Name. Voice tracks is shown with a stood-in open song change and booked list and with the microphone and recorder stubbed in the page, so no device or permission prompt is needed: idle, recording, with a take, and with the microphone refused, its error on the page and linked to Record. Once per engine it also records by keyboard: Enter on Record must keep focus there and put "Recording" in the status region once, Alt+R from the Placement select must stop the recording without moving focus and stop the stubbed microphone, a refused booking must show its error linked to Book voice track with focus still there, and leaving the tab while recording must stop the recording, stop the microphone and say so once in the page-wide status region. Card, fieldset and divider edges (`--border`) are decoration, not control boundaries, and are not held to 3:1. It does not check forced colours, which the static contracts cover; `tests/jsmodules/a11y-contract.test.js` also checks every theme token pair in both themes.

Checkboxes and radio buttons are a deliberate exception to the authored control-boundary rule. In the default color mode they keep the browser and operating system's native appearance; AutoDJ does not replace that appearance or draw a custom border. The forced-colors rules may apply system colors such as `CanvasText` and `Highlight`, but they do not disable the native appearance. Test both checked and unchecked states during release sampling when a sampled flow contains these controls.

Settings > Stations is audited again with a refused New station request from a stood-in server.
The error must appear beside Station ID, link to that field, and move focus there. Each browser
must keep a refused create silent apart from the focused field. A stood-in successful create
must announce once in the panel's status region and move focus to Switch. The audit never
creates a station; the stand-in answers every create request.

## Announcement contract

Every message the interface reports follows three rules. They exist because a live region that is
rewritten with text a screen reader has already heard is the single most common cause of the
interface repeating itself.

- A live region is written only when its message actually changes. `announceStatus()` in
  `src/autodj/static/modules/live-region.js` is the shared writer for that; by default it returns
  false and touches nothing when the region already carries the text. The one exception is
  `force: true`, for the result of something the user just did (pressing Move up twice, the same
  search twice): the region is cleared and the same text written again, so it is heard again.
- A value that ticks -- an elapsed-seconds counter, a playback position -- never lives inside a
  region that speaks. It goes in a sibling marked `aria-live="off"`, or on an attribute that is
  refreshed only while the control is not focused. `#lib-job-elapsed` next to `#lib-job-status` is
  the reference example.
- One message has exactly one automatic announcement path. A visible mirror of a transient status
  carries `aria-hidden="true"` and no role, so it shows without speaking. The page-level
  `#status-toast` is that mirror; `#cue-legend` and the stale-playback note follow the same rule.
  Static result text that users may browse stays exposed with `aria-live="off"`; announce its
  completed result through `announceStatus()` so updates stay quiet while the value remains
  available in the reading order. The mirror's Dismiss button sits beside it in
  `#status-toast-box`, outside the mirror and every live region, so it adds nothing to the one
  announcement, and showing the message never moves focus to it.

When adding a status message, write it through `announceStatus()` rather than assigning
`textContent`, and check the region it lands in is not also inside another live region.

## UI wording

Use the same concise numeric text on screen and in accessible names, values and status
messages. Use `HH:MM` or `HH:MM:SS` for clock times and `m:ss` or `h:mm:ss` for durations,
with the existing locale, time zone and AM/PM context. Reuse `fmtTime` for browser durations.
Do not hide `3:45` from assistive technology and substitute “3 minutes 45 seconds”, or spell
out numbers solely on the assumption that NVDA needs it. Actual broadcast speech is separate.

Keep native labels short. Put help outside labels and make it available through normal
browsing rather than repeating it with `aria-describedby` on every focus. Format examples,
ranges and explanations of restrictions can stay in that help; they do not need to be linked
to every field. Expandable help is available after opening its named `details` control.
Keep units and station context where needed to identify a value, and associate validation
errors with the affected field. Add a focus description only when it supplies essential
meaning that the control and its surrounding context do not provide.
Check that hidden settings hide their help too, and that errors still set `aria-invalid`
and receive focus when appropriate.

Keep text that supplies meaning a symbol alone lacks, such as missing-value states, key
names and the sign of a negative value. A screen-reader-specific wording exception needs a
reproduced problem and a recorded browser/screen-reader sample; automated tests should not
require expanded speech merely because an earlier implementation used it.

## Release sampling

Before every release, each flow below must be sampled with either NVDA and Firefox or NVDA and Chrome, by a person or by an agent that drives the page and captures NVDA's speech; the record says which:

- Authentication, session expiry, and connection-status changes. A dropped connection says
  "Disconnected, retrying" once and "Live" once when it returns, and playback (Listen here and
  browser playback) keeps playing through it; a session that has ended stops playback and opens
  the pairing dialog. When AutoDJ stays out of reach, the page says once that a reload may be
  needed to sign in again, and nothing more while it retries.
- Section-tab and disclosure navigation using the keyboard, including focus placement.  Moving
  between tabs says the tab's name and "selected" once, not "selected selected".
- Settings subtabs: the arrow keys, Home and End move between them and skip Stream outside
  stream mode; clicking one moves focus to its panel's heading; the page reopens on the last
  one used; and Profiles and access loads its lists when shown.  Settings that have no
  effect stay hidden, for example the DJ-mix options with saved segue points.
- Playback controls, seeking, volume, and the spoken hotkeys.
- Search, queue additions, reordering, removal, and empty states.
- Now-playing changes, persistent metadata, cue descriptions, and timed lyrics.
- Track landmarks (#60): Track info's Song structure read as a list of plain text ("Drop, 1:35,
  bar 49"); Right and Left bracket in Track info
  moving the cue point preview and saying each landmark once ("Preview: ..."), with focus staying
  put; on Now Playing saying the landmark of the song on air ("On air: ..., in 0:12")
  without moving it, and moving it only with Bracket keys seek the song on air checked (that
  checkbox read with its label, unchecked by default, and its help); Shift+U saying what is coming
  next once ("On air: Drop in 0:12, 6 bars"); and none of them firing with Enable keyboard
  shortcuts cleared.
- Settings changes and their status or error feedback.  Settings, Display and keys, Theme: the
  radio group read with its name, its help available through browsing, a choice said as the checked radio button and
  nothing more, and the visible status message's Dismiss button reachable without it being
  announced along with the message.
- Settings, Profiles and access: renaming a device (focus back on its Rename, the new name said
  once), and Show pairing code (the code said once, the countdown silent, a device that pairs
  meanwhile said once, and the used code hidden with focus on Show pairing code).
- Library-job status and review of the persistent output log. Leave a job running for several
  minutes and confirm the status is spoken once at the start and once at the end, not repeatedly.
- Stream mode (`serve --stream`): the Settings, Stream subtab, the station name, Copy address,
  the quality choice and listener count, and Listen here starting and stopping the stream in the page,
  and still playing, with nothing said by the stream, after the page's connection drops and returns.
- Stream mode, Settings, Stream, Now-playing feed (a card closed by default: open it first, with
  Enter or Space on its heading, and confirm it is read as collapsed, then expanded): Copy
  now-playing link said once per press,
  Show the next song publicly saved and its result said once, the webhook list read as plain text
  (or the config.local.toml note when there are none), and each Send test naming its host and
  saying "Sending" and then the result once, with focus staying on the button.
- Stream mode, Settings, Stream, More formats and Listener statistics (#50), each a card closed
  by default (open each first, with Enter or Space on its heading): AAC quality and
  Opus quality read with their help, a choice saved once it settles and said once ("Opus stream
  on at 64 kbps."), the address list read as plain text with a named Copy button for each
  format; Listener statistics read by heading, the summary as one paragraph, both tables with
  their captions and column headers (table navigation reads each cell with its column and row),
  the session lengths as a list, Refresh statistics, Show and Days to keep each said once, and
  nothing said while the tables are being read.
- DMCA mode (on in Settings > Song picking): the "DMCA mode is on" line under the feed links read as
  plain text; Show the next song publicly read as unavailable with its reason, pressing it saying
  the reason once and leaving it unticked; and queueing a song over a limit from Search or
  Library opening the confirm dialog with the limit in words and focus on Cancel, Queue anyway
  (or Play anyway) saying the song was queued over the limit, Cancel saying it was not, and
  focus back on the button pressed either way.
- The public status page (`/status`, and `/stations/<slug>/status` for another station): read
  with headings and the definition list, with and without JavaScript; the song line said once,
  politely, when the song changes, and nothing said on a poll that finds the same song; the next
  song shown only while it is allowed. With a station stopped, its page says it is off air, and
  an open page says so once when the station stops and says the song once when it is back.
- Several stations (#70), with two configured: the Station button read as "Station: AutoDJ",
  menu button, collapsed; Enter opening the menu on the checked station, read with its state
  ("AutoDJ, on air, 2 listeners") and checked; the arrows, Home, End and a letter moving without
  switching; Enter switching, with focus staying on the button (now named after the new
  station) and the switch said once ("Now controlling ..."), the connection status saying
  nothing more; Escape closing without switching; Skip, Play or Pause and the queue buttons
  read with "On <station>" after their names; `c` and Shift+C; Listen here still playing the
  station it started on, with its "Playing ..." line; and a stopped station's "Off air" said
  once, with Start reachable on Now Playing.
- Settings, Stations (#233): browse the station list and read New station fields with their short
  labels. Station ID includes its format hint. Choosing a copy reveals Also copy as a group of
  three checkboxes; choosing AutoDJ defaults hides that group and its help. A refused ID or name
  is read once with focus on its field. Create station says "Created <name>, off air." once and
  focuses Switch; Switch focuses the Station button, now named after the new station.
- The listener request page (`/requests`), without JavaScript:
  - The search field is read with its label. A refused search is read with its error, and the
    page title says "Error".
  - The results are read as a list of links named by their songs.
  - The confirmation is read with the song and the optional name field, with its hint.
  - Each answer page's title and first heading say what happened.
- Queue & Search, Listener requests: the card is read with its summary.
  - Each waiting request is read with who sent it, when, and where it would fit. Approve,
    Play next and Turn down are each read with the song's name.
  - A button's result is said once, and focus moves to the next request, or to the card's heading
    when none is left. A refused Approve says why.
  - A refresh that finds a new request says nothing.
- The player page (`/player`) and the embeddable player (`/embed`, also inside an `<iframe>` with
  a title on another page): Play read as a button, pressing it saying "Connecting to the stream."
  and "Playing." once each and the button then read as Stop; Stop saying "Stopped." with focus
  staying on the button; the Volume slider read with its label and a percentage, moved with the
  arrow keys without anything else being said; the song line said once, politely, when the song
  changes; and a station that is stopped saying it is off air, with Play then explaining that
  the stream could not be played.
- Voice liners: uploading a liner, Test now, and each rotation mode, including the spoken
  result when an upload or test fails.
- Library tools, folder table: arrow onto each checkbox and hear its name and checked state;
  toggle it once, keep focus while saving and after the next poll, and confirm only one save.
  After a failed save, check that the saved value is restored and focus stays on the checkbox.
  Search and History action cells focus their buttons and announce the button role without
  repeating the track name; returning from Track info leaves arrow navigation working.
- Queue & Search, browsing: Tab to Type, Folder, Search text and Search; one result grid with
  arrows, Home and End, and Enter or Space activating each action; the track count said once per
  search and page, with focus staying on Previous or Next (or moving to the other when a page ends);
  and Track info returning focus to its result. Under Settings > Voice liners, check Delete and
  Play dates with focus moving to the next liner or the list.
- Track info, Tags: each field read with its label and, for Year, BPM, Key and ISRC, its help;
  Save tags with a refused field marking it invalid, focusing the first one and reading its
  error; a save said once with focus staying on Save tags; and switching tracks with unsaved tags
  opening the Save, Discard or Keep editing dialog, its explanation read after its name, and Keep
  editing (like Escape) keeping the edits and putting focus back on the control that was pressed.
- Library tools, Categories: Add category and Edit landing on Name; the source radio buttons
  showing the query or the playlist field; Check saying the count once while the count line
  stays silent; a missing name or a refused query said once, marked invalid and focused; Save
  saying the count once and landing on the category's Edit button; Cancel returning to the
  button that opened the form; Delete naming the clockwheels that use the category and landing
  on the next Edit button or Add category; and the list never spoken on its 15-second refresh.
  In Clockwheels, a Music condition with Field Category showing the named Category choice;
  multiple categories read with the chosen Match all/any; a legacy category step opening as
  Music with its category selected; a missing category named as unavailable; Category absent
  from voice liner conditions.
- Library tools, Clockwheels: the conditions read as a group named Conditions, each with its
  numbered legend; wildcard and MM:SS help available through browsing without repeating on Value and Upper value; Add condition
  focusing the new condition and saying "Condition 2 added"; Remove condition 2 focusing the
  condition that took its place (or the one before it), saying how many are left, and Add
  condition once none are left; an invalid value or duration marked invalid and focused; Match
  all/Match any and every condition read in the step summary; and saved values and keyboard focus
  preserved when folder, genre or Beets choices refresh, with an open genre list left alone.
- Library tools, Clockwheels, the step list: Tab into the list turning on NVDA's focus mode on
  the first step name (a grid cell, not a button); Up and Down moving between names and Left,
  Right, Home and End along a row without leaving focus mode; Enter and Space pressing the focused
  cell's button; Alt+Up and Alt+Down moving the step and saying its new position once; and the
  Delete key behaving like the Delete button; Delete and Undo disabled while a step is being
  edited, with its unsaved fields preserved; Undo step change and Ctrl+Z restoring the step and
  its focus, including after deleting the last step; and Ctrl+Z still undoing text in inputs.
- Library tools, Shows: Add show and Edit landing on Name; a refused field marked invalid, its
  error read with it and focused; Open list moving focus to the list heading and saying the total
  once; each song's Move up, Move down and Remove buttons named with the song, focus staying on
  the moved song's button with its new number said once, Move up on the first song read as
  unavailable and saying why; Remove moving focus to the next song, or the previous one after the
  last (the search box when none is left); the list buttons unavailable while it is generated; a
  song listed twice read with its warning; Search
  results with their Add buttons, the added song said once; Generate list saying the result once
  with focus kept on the button; Stop show leaving focus on Add show; and nothing said by the
  15-second refresh or a song change, and a failing refresh said once.
- EQ: each band's slider, its spoken value, and resetting the bands.
- History: the History tab's table read with table navigation, its pagination, the empty state,
  and a new song added to the top of page 1 without the reading position moving or anything said.
- Why this track: reading the reasons for the current pick, and confirming they are not read
  again on their own when the track changes.
- Now Playing, Hour preview: the table read with table navigation (column headers Starts, What,
  Length and Status), both time columns exposing their displayed numeric text without hidden
  word-expanded alternatives, Fixed or Predicted said in the
  Status column; Refresh preview saying the summary once with focus staying on it; nothing said
  by the 30-second refresh or a song change, with the reading position kept; and Shift+H saying
  the summary from another tab; a new span under Show said once with its summary; and a failed
  automatic refresh said once, not on every retry.

For that release, record the date, exact screen reader and browser versions, flows sampled, results, and defects found. Keep this record with the release evidence so readers can find any limits or unresolved defects.

Do not claim support or verified behavior for NVDA, JAWS, VoiceOver, screen readers generally, or assistive technology generally unless the exact screen-reader/browser pairing and flow named in the claim were tested and recorded. Results from one pairing or flow do not transfer to another.
