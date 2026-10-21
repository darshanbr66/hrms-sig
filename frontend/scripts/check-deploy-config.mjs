// Runs before the Vercel build (vercel.json buildCommand) and refuses to build a deployment that
// would not work or would leak (docs/deployment.md §8):
// - the /api rewrite must name the real Render API host over https, not the placeholder;
// - no VITE_ variable may look like a secret: every VITE_ value is compiled into the public
//   bundle, and the SPA needs none (it calls the API on its own origin).
// The one exception is the frontend-first deployment (§8): with HRMS_FRONTEND_FIRST=true in the
// Vercel project, the placeholder is accepted, and only the placeholder. Such a build serves the
// SPA while every /api request fails (the placeholder is not a resolvable host). Once vercel.json
// names a real host, HRMS_FRONTEND_FIRST must be removed again, or the build is refused.
import { readFileSync } from "node:fs";

const PLACEHOLDER = "https://__RENDER_API_HOST__/api/:path*";

const config = JSON.parse(readFileSync(new URL("../vercel.json", import.meta.url), "utf8"));
const problems = [];
const frontendFirstValue = process.env.HRMS_FRONTEND_FIRST;
const frontendFirst = frontendFirstValue === "true";
if (frontendFirstValue !== undefined && frontendFirstValue !== "" && !frontendFirst) {
  problems.push("HRMS_FRONTEND_FIRST must be exactly true, or not set.");
}

const api = (config.rewrites ?? []).find((rewrite) => rewrite.source === "/api/:path*");
if (!api) {
  problems.push("vercel.json has no /api/:path* rewrite to the API.");
} else if (api.destination === PLACEHOLDER && frontendFirst) {
  console.warn(
    "Deployment configuration: frontend-first build (HRMS_FRONTEND_FIRST=true). /api requests will " +
      "fail until __RENDER_API_HOST__ is replaced and HRMS_FRONTEND_FIRST is removed (docs/deployment.md §8).",
  );
} else {
  let destination = null;
  try {
    destination = new URL(api.destination.replace(":path*", "path"));
  } catch {
    problems.push(`The /api rewrite destination is not a URL: ${api.destination}`);
  }
  if (destination) {
    if (destination.protocol !== "https:") problems.push("The /api rewrite must use https.");
    if (destination.hostname.includes("__")) {
      problems.push(
        "Replace __RENDER_API_HOST__ in vercel.json with the Render API host (docs/deployment.md §8).",
      );
    } else if (frontendFirst) {
      problems.push(
        "vercel.json names an API host: remove HRMS_FRONTEND_FIRST from the Vercel project (docs/deployment.md §8).",
      );
    }
    if (!api.destination.endsWith("/api/:path*")) {
      problems.push("The /api rewrite must keep the /api prefix: https://<host>/api/:path*");
    }
  }
}

const secretLike = Object.keys(process.env).filter(
  (name) => name.startsWith("VITE_") && /SECRET|PASSWORD|TOKEN|KEY|CREDENTIAL|DATABASE|PRIVATE/i.test(name),
);
for (const name of secretLike) {
  problems.push(`${name} would be compiled into the public bundle. Remove it from the Vercel project.`);
}

if (problems.length > 0) {
  for (const problem of problems) console.error(`Deployment configuration: ${problem}`);
  process.exit(1);
}
console.log("Deployment configuration check passed.");
