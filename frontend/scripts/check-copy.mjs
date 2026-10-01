// Refuses banned words and patterns in UI copy (docs/ui-ux-guidelines.md §9, CLAUDE.md).
// Scans every source file under src/, tests included, so examples cannot drift either.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const BANNED = [
  "seamless",
  "cutting-edge",
  "revolutionize",
  "empower",
  "skyrocket",
  "next-generation",
  "game-changing",
  "delve",
  "leverage",
  "transformative",
  "unlock",
  "supercharge",
  "effortless",
  "magic",
  "journey",
];
const BANNED_PHRASES = ["Oops!", "Uh oh", "Something went wrong"];
const root = new URL("../src/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function* files(directory) {
  for (const name of readdirSync(directory)) {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else if (/\.(tsx?|css|html)$/.test(name) && name !== "api-schema.ts") yield path;
  }
}

const words = new RegExp(`\b(${BANNED.join("|")})\w*\b`, "i");
const problems = [];
for (const path of files(root)) {
  readFileSync(path, "utf8")
    .split("\n")
    .forEach((line, index) => {
      const word = words.exec(line);
      const phrase = BANNED_PHRASES.find((banned) => line.includes(banned));
      if (word || phrase) problems.push(`${relative(root, path)}:${index + 1}: "${word?.[0] ?? phrase}"`);
    });
}
if (problems.length > 0) {
  console.error(`Banned words in UI copy:\n${problems.join("\n")}`);
  process.exit(1);
}
console.log("UI copy check passed.");
