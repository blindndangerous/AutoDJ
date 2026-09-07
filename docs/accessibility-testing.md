# Accessibility testing

AutoDJ uses automated checks and a small manual sample to catch accessibility regressions. The results apply only to the paths that the checks exercise. They do not certify the whole application.

## Continuous integration

CI includes selected static accessibility contracts and JavaScript behavior checks. These checks cover live-region markup, durable descriptions, keyboard interactions, color boundaries, reduced-motion styles, and forced-colors styles. Passing CI does not establish WCAG conformance and does not prove what a screen reader will speak.

Checkboxes and radio buttons are a deliberate exception to the authored control-boundary rule. In the default color mode they keep the browser and operating system's native appearance; AutoDJ does not replace that appearance or draw a custom border. The forced-colors rules may apply system colors such as `CanvasText` and `Highlight`, but they do not disable the native appearance. Test both checked and unchecked states during release sampling when a sampled flow contains these controls.

## Announcement contract

Every message the interface reports follows three rules. They exist because a live region that is
rewritten with text a screen reader has already heard is the single most common cause of the
interface repeating itself.

- A live region is written only when its message actually changes. `announceStatus()` in
  `src/autodj/static/modules/live-region.js` is the shared writer for that; it returns false and
  touches nothing when the region already carries the text.
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

Before every release, a person must manually sample each flow below with either NVDA and Firefox or NVDA and Chrome:

- Authentication, session expiry, and connection-status changes.
- Section-tab and disclosure navigation using the keyboard, including focus placement.
- Playback controls, seeking, volume, and the spoken hotkeys.
- Search, queue additions, reordering, removal, and empty states.
- Now-playing changes, persistent metadata, cue descriptions, and timed lyrics.
- Settings changes and their status or error feedback.
- Library-job status and review of the persistent output log. Leave a job running for several
  minutes and confirm the status is spoken once at the start and once at the end, not repeatedly.

For that release, record the date, exact screen reader and browser versions, flows sampled, results, and defects found. Keep this record with the release evidence so readers can find any limits or unresolved defects.

Do not claim support or verified behavior for NVDA, JAWS, VoiceOver, screen readers generally, or assistive technology generally unless the exact screen-reader/browser pairing and flow named in the claim were tested and recorded. Results from one pairing or flow do not transfer to another.
