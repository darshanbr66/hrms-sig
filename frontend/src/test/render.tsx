import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";

import { createQueryClient, routes } from "../app/App";
import type { Me } from "../app/session";
import { json, type FakeApi } from "./fakeApi";

/** The whole app at a path, as a signed-in person with `me`. */
export function renderSignedIn(api: FakeApi, me: Me, path: string) {
  api.on("GET /api/v1/me", json(me)).on("GET /api/v1/me/elevation", json({ elevation: null }));
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const view = render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, ...view };
}
