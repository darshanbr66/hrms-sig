import "vitest";
import type { AxeMatchers } from "vitest-axe/matchers";

declare module "vitest" {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type -- interface merging adds the matchers
  interface Assertion<T = unknown> extends AxeMatchers {} // eslint-disable-line @typescript-eslint/no-unused-vars -- must match Vitest's declaration
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type -- interface merging adds the matchers
  interface AsymmetricMatchersContaining extends AxeMatchers {}
}
