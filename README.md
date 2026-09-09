# Sweet Sleep

Static website for `sweet-sleep.cn`.

## Pages

- `/index.html` - home page and image gallery
- `/video.html` - Bilibili video embed page
- `/ar/` - MindAR + Three.js AR page
- `/menu/` - interactive character menu page
- `/gacha/` - pure frontend Blue Archive gacha simulator
- `/knowledge/` - authenticated knowledge management and retrieval, with a separate Python backend; see [service documentation](services/knowledge/README.md)
- QQ retrieval bot - official `botpy` service that replies to private messages and group mentions with knowledge excerpts; see [bot documentation](services/qqbot/README.md)

## Deploy

Upload only the static site files to the web root. The knowledge service is deployed separately to `/opt/sweet-knowledge`; never publish its database, environment files, or backend source under the web root.

## Sync

Run the helper from the repository root to commit local changes, upload the
site files to the production server, and push the current Git branch:

```sh
./scripts/sync-site.sh "Describe the change"
```

The script uses `root@101.43.110.147` and `/www/wwwroot/myweb` by default.
Set `REMOTE_HOST`, `REMOTE_ROOT`, or `SITE_URL` before running it to override
those values. SSH may ask for a QR-code login during the upload.
