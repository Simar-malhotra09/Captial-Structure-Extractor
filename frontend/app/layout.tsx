import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Capital Structure Extractor",
  description: "Extract capital structure from SEC filings",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
