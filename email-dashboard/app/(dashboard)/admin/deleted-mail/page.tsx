"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";
import { cn } from "@/lib/utils";
import { DEPARTMENT_CATEGORIES, normalizeCategoryQueryParam } from "@/lib/departments";
import { getApi } from "@/lib/api/client";
import type { EmailRecord } from "@/lib/types";
import { EmailsTable } from "@/components/tables/emails-table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Select, SelectItem } from "@/components/ui/select";
import { Search } from "lucide-react";
import { DateRangePair } from "@/components/ui/date-range-pair";

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100, 200];

const PILL_LINKS: { slug: string; label: string }[] = [
  { slug: "all", label: "All" },
  ...DEPARTMENT_CATEGORIES.map((c) => ({ slug: c.toLowerCase(), label: c })),
];

function categoryFromSlug(slug: string): string {
  if (slug === "all") return "";
  const m = DEPARTMENT_CATEGORIES.find((c) => c.toLowerCase() === slug.toLowerCase());
  return m ?? "";
}

function DeletedMailContent() {
  const searchParams = useSearchParams();
  const categoryFilter = useMemo(
    () => normalizeCategoryQueryParam(searchParams.get("category")),
    [searchParams]
  );

  const { data: session, status } = useSession();
  const api = useMemo(
    () => getApi(session?.user?.email ?? null, session?.user?.name ?? null),
    [session?.user?.email, session?.user?.name]
  );

  const [emails, setEmails] = useState<EmailRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [search, setSearch] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    if (status !== "authenticated") return;
    setLoading(true);
    setError(null);
    api
      .getAdminEmails({
        page,
        pageSize,
        search: search || undefined,
        from: from || undefined,
        to: to || undefined,
        category: categoryFilter || undefined,
        deletedOnly: true,
      })
      .then((r) => {
        setEmails(r.emails);
        setTotal(r.total);
      })
      .catch((e: Error) => {
        setError(e.message?.includes("403") ? "Admin access required." : "Failed to load deleted mail.");
      })
      .finally(() => setLoading(false));
  }, [status, api, page, pageSize, search, from, to, categoryFilter]);

  useEffect(() => {
    setPage(1);
  }, [categoryFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const buildPillHref = (slug: string) => {
    const cat = categoryFromSlug(slug);
    const p = new URLSearchParams();
    if (cat) p.set("category", cat);
    const q = p.toString();
    return q ? `/admin/deleted-mail?${q}` : "/admin/deleted-mail";
  };

  return (
    <div className="min-w-0 max-w-full space-y-4 sm:space-y-6">
      <div className="min-w-0">
        <h1 className="text-xl font-bold tracking-tight text-neutral-900 dark:text-neutral-50 sm:text-2xl">
          Deleted mail
        </h1>
      </div>

      <nav
        className="-mx-0.5 flex min-w-0 flex-wrap items-center gap-1.5 gap-y-2 sm:gap-2"
        aria-label="Department filters"
      >
        {PILL_LINKS.map(({ slug, label }) => {
          const active =
            slug === "all" ? !categoryFilter : categoryFilter.toLowerCase() === slug;
          return (
            <Link
              key={slug}
              href={buildPillHref(slug)}
              className={cn(
                "shrink-0 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
                active
                  ? "border-neutral-900 bg-neutral-900 text-white dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900"
                  : "border-border/90 bg-panel/90 text-muted-foreground hover:border-border hover:bg-muted/80 hover:text-foreground"
              )}
            >
              {label}
            </Link>
          );
        })}
      </nav>

      <div className="glass-surface flex flex-col gap-4 rounded-2xl p-3 sm:p-4 md:flex-row md:items-center md:justify-between md:gap-6">
        <div className="flex min-w-0 flex-col gap-3 md:min-w-0 md:flex-1 md:flex-row md:flex-wrap md:items-center">
          <div className="relative min-w-0 w-full md:w-64 md:flex-none">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-400" />
            <Input
              placeholder="Search subject, sender, mailbox…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && load()}
              className="pl-9"
            />
          </div>
          <DateRangePair
            from={from}
            to={to}
            onFromChange={setFrom}
            onToChange={setTo}
            className="w-full min-w-0 md:w-auto"
            fieldClassName="relative min-w-0 flex-1"
          />
          <Button type="button" variant="outline" className="w-full shrink-0 md:w-auto" onClick={load}>
            Apply
          </Button>
        </div>
        <div className="flex min-w-0 shrink-0 flex-col gap-2 border-t border-border/60 pt-3 sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-3 md:border-l md:border-t-0 md:pt-0 md:pl-6">
          <div className="flex min-w-0 items-center gap-2">
            <span className="shrink-0 text-sm text-muted-foreground">Rows per page</span>
            <Select
              value={String(pageSize)}
              onValueChange={(v) => {
                setPageSize(Number(v));
                setPage(1);
              }}
              className="w-[5.75rem] shrink-0 sm:w-24"
            >
              {PAGE_SIZE_OPTIONS.map((n) => (
                <SelectItem key={n} value={String(n)}>
                  {n}
                </SelectItem>
              ))}
            </Select>
          </div>
          <p className="text-sm text-muted-foreground">
            <span className="font-medium tabular-nums text-foreground">{total}</span> deleted
          </p>
        </div>
      </div>

      {error && (
        <div className="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-400">
          {error}
        </div>
      )}

      <EmailsTable
        emails={emails}
        isLoading={loading}
        emptyMessage={categoryFilter ? `No deleted mail in ${categoryFilter}.` : "No deleted mail."}
        readOnly
        showRetag={false}
        showMailbox
      />

      {totalPages > 1 && (
        <div className="grid grid-cols-1 gap-2 sm:flex sm:flex-row sm:items-center sm:justify-between sm:gap-3">
          <Button type="button" variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Previous
          </Button>
          <span className="py-1 text-center text-sm text-muted-foreground sm:flex-1">
            Page {page} of {totalPages}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}

export default function AdminDeletedMailPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted-foreground">Loading…</div>}>
      <DeletedMailContent />
    </Suspense>
  );
}
