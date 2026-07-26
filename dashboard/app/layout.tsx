import "./globals.css";
import Link from "next/link";
import type { ReactNode } from "react";

export const metadata = {
  title: "kalshi-bias-engine",
  description: "Bias-exploiting trading engine dashboard.",
};

const nav = [
  { href: "/", label: "overview" },
  { href: "/signals", label: "signals" },
  { href: "/calibration", label: "calibration" },
  { href: "/ledger", label: "ledger" },
  { href: "/heartbeat", label: "heartbeat" },
  { href: "/control", label: "control" },
];

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="min-h-screen">
          <header className="border-b border-neutral-800 px-4 py-3 flex items-center gap-6">
            <div className="font-mono text-neutral-300">kbe</div>
            <nav className="flex gap-4 text-sm">
              {nav.map((n) => (
                <Link
                  key={n.href}
                  href={n.href as any}
                  className="text-neutral-400 hover:text-neutral-100"
                >
                  {n.label}
                </Link>
              ))}
            </nav>
          </header>
          <main className="p-4 max-w-6xl">{children}</main>
        </div>
      </body>
    </html>
  );
}
