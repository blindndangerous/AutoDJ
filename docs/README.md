# AutoDJ documentation

Choose a starting point for what you need now. Tutorials guide a learning
experience; task guides help you do a job; reference answers precise questions;
explanations build a mental model. You do not need to read every section.

## Learn

Start with [Build your first station](tutorials/first-station.md), then choose
from the [tutorials](tutorials/README.md): rotation, shows, mixing, station voices,
requests, streaming, live work, recovery and multiple stations. Use an unused
practice setup and read each lesson's prerequisites before changing settings.

## Do a task

For setup and everyday work:

- [Install AutoDJ](guides/install.md) or use the
  [native PowerShell and Bash core procedures](guides/core-operations.md).
- [Add or maintain music](guides/add-your-music.md).
- [Build your sound](guides/build-your-sound.md) and [plan a schedule](guides/schedule.md).
- [Go live and take requests](guides/going-live.md).
- [Stream and relay](guides/streaming-and-relays.md).
- [Run several stations](guides/several-stations.md).
- [Back up and update](guides/backups-and-updates.md).
- [Use a keyboard and screen reader](guides/keyboard-and-screen-reader.md).

For administration and diagnosis:

- [Manage access and credentials](guides/manage-access.md).
- [Prepare a station for regular use](guides/prepare-station.md).
- [Troubleshoot a symptom](guides/troubleshooting.md) or
  [collect diagnostics and report a problem](guides/collect-diagnostics.md).
- [Detailed operational procedures](guides/operations/README.md) cover networking,
  analysis workers, inserted audio, recovery and other independent tasks.

## Look something up

- [Settings reference](settings-reference.md): values, defaults, locations and UI labels.
- [Feature availability](reference/feature-availability.md): playback modes,
  browser requirements and dependencies, with verification limits.
- [Operational reference](reference/operations/README.md): API and storage contracts,
  output formats and detailed behavior.
- [Glossary](glossary.md): recurring station and audio terms, with links for detail.
- [Speech service contract](speech-endpoints.md): the interface a configured
  synthesis endpoint must implement.
- [Hardware setup notes](windows-amd.md): AMD detection and dependency limits.

Use `autodj --help` and `autodj <command> --help` for CLI syntax and flags, through
your normal launcher. Use `./autodj` on Linux/macOS or `.\autodj.cmd` in PowerShell.
The task guides give contextual command examples rather than another flag catalog.

## Understand AutoDJ

- [Music selection](explanation/selection.md): queues, categories, analysis and picking rules.
- [Playback](explanation/playback.md): who mixes audio and what a listening control affects.
- [Stations and shared data](explanation/stations.md): ownership and isolation.
- [Scheduling](explanation/scheduling.md): time, transitions and forecasts.
- [Further operational explanations](explanation/operations/README.md): focused
  background on specialized workflows.

## Contribute or review the system

Start with [Contributing](../CONTRIBUTING.md) and the
[architecture decisions](adr/README.md). For implementation detail, read
[track-selection internals](contributor/track-selection.md) and
[model-selection reasoning](contributor/model-selection.md).

[Accessibility testing](accessibility-testing.md) distinguishes automated checks
from browser, listening and screen-reader evidence.
[Security policy](../SECURITY.md) describes reporting and supported scope;
[the threat model](../THREAT_MODEL.md) records trust boundaries and mitigations.
[Release notes](../CHANGELOG.md) retain version history. These records keep their
specialized formats and are not introductory lessons.

## Existing operations links

The former [operations manual](operations.md) is a compatibility navigation page.
Its published heading links lead to the focused material above. Old links remain
useful; use this documentation home to find new material by purpose.

AutoDJ and this page were developed with AI assistance.
