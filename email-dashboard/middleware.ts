import { withAuth } from "next-auth/middleware";

// Next.js inlines env from this file into the Edge bundle; `withAuth` alone reads
// process.env inside node_modules, where NEXTAUTH_SECRET is often missing → Configuration / NO_SECRET.
export default withAuth({
  pages: { signIn: "/signin" },
  secret: process.env.NEXTAUTH_SECRET ?? process.env.AUTH_SECRET,
});

export const config = {
  matcher: [
    "/dashboard",
    "/dashboard/:path*",
    "/emails",
    "/emails/:path*",
    "/daily-digest",
    "/threads",
    "/threads/:path*",
    "/queue",
    "/queue/:path*",
    "/settings",
    "/settings/:path*",
    "/departments",
    "/departments/:path*",
    "/escalations",
    "/leads",
    "/retag",
    "/mom",
    "/mom/:path*",
    "/follow-up",
    "/follow-up/:path*",
    "/how-to-use",
    "/how-to-use/:path*",
    "/admin",
    "/admin/:path*",
  ],
};
