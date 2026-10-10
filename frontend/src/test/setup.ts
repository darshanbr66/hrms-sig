import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach, expect } from "vitest";
import * as axeMatchers from "vitest-axe/matchers";

expect.extend(axeMatchers);

// Pages load on demand (React.lazy). When test files run in parallel the first import of a page
// can take a few seconds, so waits allow for that instead of failing on load time.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => {
  cleanup();
});
