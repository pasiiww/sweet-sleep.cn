import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const projectRoot = join(scriptDir, "..");
const sourcePath = join(projectRoot, "gacha", "students-source.json");
const outputPath = join(projectRoot, "gacha", "students.json");
const assetRoot = join(projectRoot, "gacha", "assets", "students");
const forceDownload = process.argv.includes("--force");
const execFileAsync = promisify(execFile);

const manifest = JSON.parse(await readFile(sourcePath, "utf8"));
const groups = ["standardThree", "two", "one"];
const students = groups.flatMap((group) => manifest[group]);
students.push(manifest.currentUp.ibuki, manifest.currentUp.iroha);

await mkdir(assetRoot, { recursive: true });

let downloaded = 0;
let skipped = 0;
for (const student of students) {
  const destination = join(projectRoot, "gacha", student.image);
  try {
    if (!forceDownload) {
      const existing = await readFile(destination);
      if (existing.length > 0) {
        skipped += 1;
        continue;
      }
    }
  } catch {
    // The asset is missing and will be downloaded below.
  }

  await mkdir(dirname(destination), { recursive: true });
  await execFileAsync("curl", [
    "--fail",
    "--location",
    "--retry",
    "2",
    "--silent",
    "--show-error",
    "--referer",
    manifest.source,
    "--user-agent",
    "sweet-sleep-gacha-asset-sync/1.0",
    "--output",
    destination,
    student.sourceImage,
  ]);
  downloaded += 1;
  console.log(`downloaded ${student.name} -> ${student.image}`);
}

const runtimeData = {
  source: manifest.source,
  generatedAt: manifest.generatedAt,
  counts: manifest.counts,
  currentUp: manifest.currentUp,
  standardThree: manifest.standardThree,
  two: manifest.two,
  one: manifest.one,
};
await writeFile(outputPath, `${JSON.stringify(runtimeData, null, 2)}\n`, "utf8");
console.log(`完成：下载 ${downloaded} 个，跳过 ${skipped} 个，数据写入 gacha/students.json`);
