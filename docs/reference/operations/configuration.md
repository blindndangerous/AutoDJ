# Configuration precedence and repeat settings

Look up shared configuration precedence, station definitions and saved repeat settings.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Configuration precedence

Architecture and verification context: [station ownership ADR](../../adr/0005-a-station-owns-its-playout-chain.md).

Shared settings resolve from defaults, `config.toml`, sibling `config.local.toml`, environment
variables, then explicit CLI flags. Station settings load from each station's
`<index_dir>/stations/<slug>/station.json`, with saved web state overriding the definition.
`config.local.toml` is read only when a `config.toml` was loaded next to it; on its own it is
ignored. Startup migrates legacy root station sections and `[stations.<slug>]` tables once; normal
loading does not fall back to those keys. Omitting `--config` is valid. Explicitly naming a
missing file is an error. Put access tokens only in ignored local configuration or
`AUTODJ_ACCESS_TOKEN`. An unknown shared section or key in either TOML file, such as a setting a
newer AutoDJ removed, produces a warning and is ignored. Invalid values for supported settings
still need fixing.

## Repeat settings

Repeat limits are edited in Settings > Picking > Repeats and stored in
`web_state.json`. Old repeat keys in `config.toml` or `web_state.json` are
ignored with a warning; re-enter any limits that still use those keys in that panel. The related
artists file is `index/libraries/main/related-artists.toml` and is read only when
**Use related artists** is enabled. The related tracks file is
`index/libraries/main/related-tracks.toml` and is read only when **Use related tracks** is enabled.
Each must be at most 1 MiB.

The daypart and quarter-hour rules (both off to start with) keep a song from playing at the same
time of day, day after day. They read the station's own `station.timezone`. See
[Time of day](../../track-selection.md#time-of-day) for how they work.
