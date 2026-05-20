import "./globals.css";

export const metadata = {
  title: "Voice Layer Control Plane",
  description: "Minimal black-and-white control plane for realtime voice dispatch.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

