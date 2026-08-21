import type { Metadata } from "next";

import { AppNav } from "@/app/components/app-nav";
import { SessionBar } from "@/app/components/session-bar";
import { SessionProvider } from "@/app/components/session-context";
import "./globals.css";

export const metadata: Metadata = {
  title: "Social Marketing Agent",
  description:
    "Growth agent for small businesses: SEO content, AI-answer visibility, social content, and lead capture.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // `data-theme="light"` pins the light palette. The dark block in globals.css is
    // guarded by `:root:not([data-theme="light"])`, so this also stops a viewer whose OS
    // prefers dark from getting the dark theme -- which is the whole point: the brand is
    // the deep green and the orange on an off-white surface, and the neumorphic dual
    // shadows read as intended on a light ground.
    //
    // Set here rather than removing the dark palette, so `data-theme="dark"` still works
    // and a theme toggle stays a one-line change rather than a re-theming job.
    <html lang="en" data-theme="light">
      <body>
        {/*
          One provider, one `/auth/me` read. `SessionBar` and `AppNav` both need to know
          who is signed in, and two components each fetching it means two requests for
          one fact on every navigation — plus two components that can disagree while one
          is still in flight.
        */}
        <SessionProvider>
          {/*
            In the root layout so every screen has a way out. Renders nothing for a
            visitor who is not signed in, so it costs the login page nothing. See
            `components/session-bar.tsx` for why it is a client component.
          */}
          <SessionBar />
          {/*
            The sidebar sits BESIDE the content rather than above it, and the content
            column keeps its own `Shell` — so all fifteen existing screens inherit the
            nav without being edited. `min-w-0` on the column is load-bearing: without
            it a wide child (a table, a long post body) refuses to shrink and pushes the
            whole layout into a horizontal scroll.
          */}
          <div className="flex">
            <AppNav />
            <div className="min-w-0 flex-1">{children}</div>
          </div>
        </SessionProvider>
      </body>
    </html>
  );
}
