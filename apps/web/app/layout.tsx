import "./globals.css";
import type { ReactNode } from "react";
import { AccountGate } from "./account-gate";

export const metadata = {
  title: "QuantSwarm | Browse Strategies",
  description: "Private trading research workspaces and authenticated Kalshi account connections.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body><AccountGate>{children}</AccountGate></body>
    </html>
  );
}
