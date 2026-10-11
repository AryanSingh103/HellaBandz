import "./globals.css";
import "./guide.css";
import "./marketplace.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "HellaBandz | Markets",
  description: "Autonomous strategy agents on deterministic, fault-tolerant trading infrastructure, with simulated execution and verifiable audit trails.",
};

export const viewport = { themeColor: "#0b0d0f", colorScheme: "dark" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        {/* eslint-disable-next-line @next/next/no-page-custom-font */}
        <link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Inter:ital,wght@0,400;0,500;0,600;0,700;0,800;1,800;1,900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
      </head>
      <body>{children}</body>
    </html>
  );
}
