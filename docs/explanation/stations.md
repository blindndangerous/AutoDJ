# What belongs to a station

A **station** is an independent programme on one AutoDJ server. Selecting a
station changes the programme you control; it does not give you a separate copy
of every song or a separate account on the server.

## Independent programmes, shared music

Each station owns its picking and playback settings, queue, history, categories,
clockwheels, schedule, shows, carts and stream. Stations can choose different
outputs and have different scheduling time zones. A song played on Practice
does not become a play in another station's history just because both use it.

The music library is shared. Music metadata, analysis and song cue points belong
to that library. Editing a shared song's cue points can therefore affect more
than the selected station. Registered library folders and analysis work should
be understood as server-wide resources, even when reached while operating one
station. Related-artist and related-track data are shared too.

For example, Practice can choose quiet songs while a second station uses a
faster clockwheel. Both can use the same file and its analysis without maintaining
two music copies. Their repeat histories and categories can differ. Correcting
the file's shared title or cue points is a library change, not a private
Practice adjustment.

## Selection, stopping and copying mean different things

Selecting a station chooses the target of station controls. Stopping it ends its
runtime; it retains editable saved settings. Stopping a server-output station is
different from muting a browser that is listening to it. Starting another station
does not mean the first has stopped. Check each station's state explicitly.

Copying a station copies selected settings and allowed assets, not its complete
identity or live activity. Queues, history and destination credentials are among
the things a copy omits. Review the result before starting it; a copy is not a
backup or an assurance that a broadcast destination has been configured.

## Access belongs to the server

Browser pairing, credentials and network security are shared server concerns.
The station picker is not an access-control boundary, and stations do not have
independent operator roles. A Practice station on a production server is still
using production resources and shared library data.

Use [prepare a station](../guides/prepare-station.md) for lifecycle tasks,
[manage access](../guides/manage-access.md) for connection controls, and
[operations](../operations.md#several-stations) for detailed ownership and copy
procedures. The current amendment in [ADR 5](../adr/0005-a-station-owns-its-playout-chain.md)
records the accepted ownership boundary; its original storage and timezone
sections are historical.
