import { setupServer } from "msw/node";

import { handlers } from "./handlers";

/** One deterministic Mock Service Worker instance for the whole suite. */
export const server = setupServer(...handlers);
