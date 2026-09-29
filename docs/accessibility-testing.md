# Accessibility testing

AutoDJ uses automated checks and requires a screen-reader sample before release to catch accessibility regressions. The results apply only to the paths that the checks exercise. They do not certify the whole application.

## Published evidence

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

## Continuous integration

CI includes selected static accessibility contracts and JavaScript behavior checks. These checks cover live-region markup, durable descriptions, keyboard interactions, color boundaries, reduced-motion styles, and forced-colors styles. Passing CI does not establish WCAG conformance and does not prove what a screen reader will speak.

Checkboxes and radio buttons are a deliberate exception to the authored control-boundary rule. In the default color mode they keep the browser and operating system's native appearance; AutoDJ does not replace that appearance or draw a custom border. The forced-colors rules may apply system colors such as `CanvasText` and `Highlight`, but they do not disable the native appearance. Test both checked and unchecked states during release sampling when a sampled flow contains these controls.

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
- One message has exactly one announcement path. Where a message also needs to be visible, the
  visible copy carries `aria-hidden="true"` and no role, so it shows without speaking. The
  page-level `#status-toast` is that mirror; `#cue-legend` and the stale-playback note follow the
  same rule.

When adding a status message, write it through `announceStatus()` rather than assigning
`textContent`, and check the region it lands in is not also inside another live region.

## Release sampling

Before every release, each flow below must be sampled with either NVDA and Firefox or NVDA and Chrome, by a person or by an agent that drives the page and captures NVDA's speech; the record says which:

- Authentication, session expiry, and connection-status changes.
- Section-tab and disclosure navigation using the keyboard, including focus placement.
- Playback controls, seeking, volume, and the spoken hotkeys.
- Search, queue additions, reordering, removal, and empty states.
- Now-playing changes, persistent metadata, cue descriptions, and timed lyrics.
- Settings changes and their status or error feedback.
- Settings, Browser access: renaming a device (focus back on its Rename, the new name said
  once), and Show pairing code (the code said once, the countdown silent, a device that pairs
  meanwhile said once).
- Library-job status and review of the persistent output log. Leave a job running for several
  minutes and confirm the status is spoken once at the start and once at the end, not repeatedly.
- Stream mode (`serve --stream`): the Settings, Stream section, Copy address, the
  quality choice and listener count, and Listen here starting and stopping the stream in the page.
- Voice liners: uploading a liner, Test now, and each rotation mode, including the spoken
  result when an upload or test fails.
- EQ: each band's slider, its spoken value, and resetting the bands.
- History: the History tab's table read with table navigation, its pagination, and the empty state.
- Why this track: reading the reasons for the current pick, and confirming they are not read
  again on their own when the track changes.

For that release, record the date, exact screen reader and browser versions, flows sampled, results, and defects found. Keep this record with the release evidence so readers can find any limits or unresolved defects.

Do not claim support or verified behavior for NVDA, JAWS, VoiceOver, screen readers generally, or assistive technology generally unless the exact screen-reader/browser pairing and flow named in the claim were tested and recorded. Results from one pairing or flow do not transfer to another.
