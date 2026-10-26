# Architecture diagrams

State shown: **implemented as Terraform code, not yet deployed in AWS** (26 Oct 2026).

| Diagram | Editable source | Image |
|---|---|---|
| Platform architecture | [ec2-to-ecs-overview.drawio](ec2-to-ecs-overview.drawio) | [ec2-to-ecs-overview.png](ec2-to-ecs-overview.png) |
| Migration and rollback | [migration-traffic-flow.drawio](migration-traffic-flow.drawio) | [migration-traffic-flow.png](migration-traffic-flow.png) |

The `.drawio` files are plain (uncompressed) diagrams.net XML using the AWS 2021
(`mxgraph.aws4`) shapes. Open them in diagrams.net (app.diagrams.net or the desktop
app) or the VS Code "Draw.io Integration" extension.

The PNGs were exported from exactly these sources with the draw.io desktop exporter
in Docker (`rlespinasse/drawio-desktop-headless`, `minimal-v1.75.0` pinned by digest).
SVG exports embed fonts and icons (about 1.5 MB each, above the repository's 500 KB
file limit), so only PNG is kept.

Re-export after an edit (PowerShell, from this folder; Docker Desktop running):

```powershell
$img = "rlespinasse/drawio-desktop-headless@sha256:a5b0f87dd8cec0793ec5dba397dc95862c5212ed2798d1ae311171046289c67f"
foreach ($f in "ec2-to-ecs-overview", "migration-traffic-flow") {
  docker run --rm --shm-size=2g -w /data -v "${PWD}:/data" $img -x -f png --scale 1 --border 20 -o "/data/$f.png" "/data/$f.drawio"
}
```

Without Docker: open the file in diagrams.net → File → Export as → PNG
(zoom 100 %, border 20, transparent background off).
