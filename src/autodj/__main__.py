"""``python -m autodj`` entry point.

The console script installed from ``[project.scripts]`` is the usual way
in, but the web UI's Library-tools panel does not have it: ``jobs.py``
spawns ``[sys.executable, "-m", "autodj", <subcommand>]`` so the child
always runs under the same interpreter as the server, whatever the PATH
looks like.  Without this module that spawn failed immediately with "No
module named autodj.__main__", which made Index, Enrich, Prune and Stats
dead buttons.

Example:
    $ python -m autodj stats
"""

from __future__ import annotations

from autodj.cli import cli as main

if __name__ == "__main__":
    main()
