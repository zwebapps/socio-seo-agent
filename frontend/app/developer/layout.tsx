/**
 * Chrome shared by every `/developer` screen.
 *
 * Still a server component; the nav inside it is not, and the reason is a bug this
 * file's comment used to justify. It said the nav "needs no state and no client
 * bundle" — but knowing WHICH section you are on is state, and without it all four
 * pills rendered identically on every screen. See `nav.tsx`.
 *
 * The pages themselves are `"use client"` because their API calls MUST run in the
 * browser — the Origin-CSRF middleware refuses a cookie-bearing write with no `Origin`
 * header, and `fetch` from a server component sends none. See the note in
 * `app/lib/admin-api.ts`.
 *
 * The nav is a `<nav>` with an `aria-label`, not a row of styled links: there are two
 * landmark-level link groups on these pages once the error card renders a "sign in"
 * link, and an unlabelled one is announced as just "navigation".
 *
 * No role check here. The gate is server-side on the API — every route under
 * `/api/v1/admin/*` carries `require_admin`, and the pages render the 403 as an
 * explanatory card. Duplicating the check in the frontend would put an authorisation
 * decision in a place that cannot enforce it, and would be a second thing to keep in
 * step with the real one.
 */

import { DeveloperNav } from "./nav";

export default function DeveloperLayout({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <DeveloperNav />
      {children}
    </div>
  );
}
