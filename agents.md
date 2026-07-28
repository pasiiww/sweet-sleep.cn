# Sweet Sleep Project Notes

This repository contains the static website for `sweet-sleep.cn`.

## Project Locations

- Local repository: `/Users/bytedance/Documents/Codex/2026-07-03/ni/work/github_sweet_sleep`
- Local staging mirror used during earlier menu work: `/Users/bytedance/Documents/Codex/2026-07-03/ni/work/menu`
- Production web root: `/www/wwwroot/myweb`
- Domain: `sweet-sleep.cn`
- GitHub repository: `pasiiww/sweet-sleep.cn`

## Site Structure

- `/index.html`: home page, gallery, ICP/public-security filing link.
- `/video.html`: Bilibili video page. It embeds the video first and falls back to opening Bilibili if embedding is unavailable.
- `/ar/`: MindAR + Three.js AR page. The recognition target and display assets are under `/ar/assets/`.
- `/menu/`: interactive character menu page. It is hand-built HTML/CSS and uses compressed/watermarked character assets under `/menu/assets/`.

## Assets

- AR target: `/ar/assets/target.png`
- AR compiled target data: `/ar/assets/targets.mind`
- AR rendered image: `/ar/assets/kei.png`
- Menu reference/main visual: `/menu/assets/menu-main.jpg`
- Menu character images: `/menu/assets/kazusa.jpg`, `/menu/assets/reisa.jpg`, `/menu/assets/alice.jpg`, `/menu/assets/kei.jpg`, `/menu/assets/hina.jpg`, `/menu/assets/hoshino.jpg`
- Public-security filing icon: `/beian-gongan.jpg`

## Deployment

This is a static site. Deploy by copying changed files from the local repository to the production web root on the server.

Typical flow:

1. Edit files in the local repository.
2. Preview locally when layout or interaction changes matter.
3. Copy changed files to `/www/wwwroot/myweb`.
4. Verify the live page on `https://sweet-sleep.cn/`.
5. Commit and push changes to `main`.

For `/menu/` changes, also keep the local staging mirror in sync when it is useful for visual QA.

## Implementation Notes

- Keep the site dependency-light. Most pages are plain HTML, CSS, and vanilla JavaScript.
- For `/menu/`, avoid CSS transform-based crop math for the default character strips; iOS Safari handled those inconsistently. Use fixed absolute image crop variables instead.
- The `/menu/` character strips should have no visible gaps in the collapsed state. Expanded characters should open in place toward the right and slightly downward without scaling the existing strip.
- Menu character names should stay clean and unobtrusive, without heavy color blocks or card-like backgrounds.
- For AR, preserve the current mobile-first interaction: user starts the camera, detected content animates in, and the capture button resembles the iOS camera shutter.
- Do not commit private certificates, SSH keys, or local-only generated scratch files.

## Gacha Notes

- The /gacha/ page is a static HTML, CSS, and JavaScript simulator.
- It compares the new 100/200 pull pity system with the old 200/400 pull exchange system side by side.
- The active pickup can switch between Ibuki (Summer) and Iroha (Summer), while both mechanisms keep shared progress across the switch.
- FES simulation uses a 6% three-star rate and a 0.7% active UP rate.
- Switching the active pickup keeps the shared new/old mechanism progress; it only changes the current UP artwork and name.
- Gacha portraits under `/gacha/assets/` are locally downloaded GameKee WebP assets, not reused menu assets.
- Gacha result borders use a multicolor frame for three-star, gold for two-star, and blue for one-star.
- `/gacha/students-source.json` records the current GameKee roster: 165 standard/history-FES three-stars, 24 two-stars, and 37 one-stars, plus the two active swimsuit UPs.
- Run `node scripts/download-gacha-assets.mjs` to refresh `/gacha/assets/students/` and regenerate `/gacha/students.json`; downloads send the GameKee roster page as the referrer because the CDN blocks unreferenced hotlinks.
- The comparison controls above both panels perform the same single pull or ten-pull on the new and old systems together. Panel reset buttons and the old exchange button remain mechanism-specific.
- The current gacha comparison exposes only a shared ten-pull button. New-mechanism event rewards follow the supplied 10-390 pull table, auto-record ordinary items, and show claim/use buttons for choice boxes and limited ten-pull tickets; the same table repeats after the first cycle.
- Limited ten-pull rewards add ten pulls only to the new-mechanism statistics when used. All reset buttons stay disabled until the corresponding progress reaches 400 pulls, and the shared reset requires both sides to reach 400.
- The new 100-pull soft pity is checked only on the 100th pull after the last UP; a miss returns to the normal 0.7% rate until the 200-pull guarantee.
