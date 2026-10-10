import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "QuantSwarm | Browse Strategies",
  description: "Autonomous strategy agents on deterministic, fault-tolerant trading infrastructure (paper trading only).",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
