"""Synthetic stopped station, native edition APIs/rendering and production UI modules.

Reuses the review audit's deterministic local audio/catalog and disabled lifespan.
The small shell wires tab visibility and protected-context clearing; this is not
evidence for normal application startup, microphones or platform speech output.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from programme_review_fixture import build


def application(root: Path, port: int):
    from fastapi import Body
    from fastapi.responses import HTMLResponse, Response

    app = build(root, port)
    station = app.state.stations.editable("default")
    static = Path(__file__).resolve().parents[2] / "src/autodj/static"
    html = re.sub(
        r"<script\b[^>]*>.*?</script>", "", (static / "index.html").read_text(), flags=re.S
    ).replace("</body>", '<script type="module" src="/edition-audit.js"></script></body>')

    @app.get("/edition-audit", response_class=HTMLResponse)
    async def page():
        return html

    @app.get("/edition-audit.js")
    async def script():
        return Response(
            """import { installShowsPanel } from "/modules/shows-panel.js";
import { installProgrammeReview } from "/modules/programme-review.js";
import { invalidateAuthenticatedRequestEpoch } from "/modules/api-client.js";
function tab(name) {
  for (const value of ["now", "library"]) {
    document.getElementById(`panel-${value}`).hidden = name !== value;
  }
}
for (const name of ["now", "library"]) {
  document.getElementById(`tab-${name}`).addEventListener("click", () => tab(name));
}
tab("library");
const shows = installShowsPanel(), review = installProgrammeReview();
window.editionAudit = {shows, review, clear() {
  invalidateAuthenticatedRequestEpoch(); shows.clearProtectedData(); review.clear();
}};
""",
            media_type="text/javascript",
        )

    @app.post("/edition-audit-control")
    async def control(action: str = Body(embed=True)):
        if action != "cue":
            raise ValueError("Unknown audit action")
        original = station.bridge.library.catalog.points
        first = next(iter(station.shows.all().values())).songs[0]
        station.bridge.library.catalog.points = lambda path: (
            {**original(path), "start": 0.1} if path == first else original(path)
        )
        return {"ok": True}

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8895)
    args = parser.parse_args()
    uvicorn.run(
        application(args.root, args.port), host="127.0.0.1", port=args.port, log_level="warning"
    )
