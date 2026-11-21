# Highway Closures

Live TxDOT highway closures, lane closures and road work around San Antonio and across Texas, on a street map.

- **Data:** TxDOT DriveTexas road-work feed (WZDx), pulled about every 15 minutes by the GitHub Actions workflow in `.github/workflows/refresh.yml` using `tools/refresh.py`. The API key lives in the `DRIVETEXAS_KEY` repository secret and never reaches the site.
- **Map:** MapLibre GL JS (bundled in `lib/`) with OpenFreeMap tiles; map data © OpenStreetMap contributors.
- **Hosting:** GitHub Pages, deployed by the same workflow.

## Setup

1. Settings > Secrets and variables > Actions > New repository secret: name `DRIVETEXAS_KEY`, value = your DriveTexas API key.
2. Settings > Pages > Build and deployment > Source: **GitHub Actions**.
3. Actions > "Refresh closures and publish site" > Run workflow.

This site is independent and not run by TxDOT.
