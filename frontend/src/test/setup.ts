import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach, expect } from "vitest";
import * as axeMatchers from "vitest-axe/matchers";

import { backendReadiness } from "../lib/readiness";

expect.extend(axeMatchers);

// Pages load on demand (React.lazy). When test files run in parallel the first import of a page
// can take a few seconds, so waits allow for that instead of failing on load time.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => {
  cleanup();
  // The readiness check is one per tab; each test starts with none.
  backendReadiness.reset();
  // React Aria's live announcer lives on document.body for the whole file and keeps its last
  // announcements, which point at labels of pages already unmounted. Emptying it (not removing
  // it: React Aria keeps a reference) stops one test's announcements leaking into the next.
  for (const region of document.querySelectorAll('body > div [role="log"]')) {
    region.replaceChildren();
  }
});
