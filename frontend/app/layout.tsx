import type { Metadata } from "next";
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
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
