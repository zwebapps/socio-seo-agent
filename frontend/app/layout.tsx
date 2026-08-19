import type { Metadata } from "next";

import { SessionBar } from "@/app/components/session-bar";
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
          In the root layout so every screen has a way out. Renders nothing for a
          visitor who is not signed in, so it costs the login and landing pages
          nothing. See `components/session-bar.tsx` for why it is a client component.
        */}
        <SessionBar />
        {children}
      </body>
    </html>
  );
}
