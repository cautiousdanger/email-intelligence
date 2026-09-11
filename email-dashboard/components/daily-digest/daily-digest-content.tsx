"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSession } from "next-auth/react";
import { CalendarDays, Mail, RefreshCw, Sparkles } from "lucide-react";
import { getApi } from "@/lib/api/client";
import type { DailyBulkSummaryDay } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

function formatDayLabel(dateStr: string) {
  try {
    const d = new Date(`${dateStr}T12:00:00Z`);
    const yesterday = new Date();
    yesterday.setUTCDate(yesterday.getUTCDate() - 1);
    const yesterdayKey = yesterday.toISOString().slice(0, 10);
    const long = d.toLocaleDateString(undefined, {
      weekday: "long",
      month: "long",
      day: "numeric",
      year: "numeric",
      timeZone: "UTC",
    });
    if (dateStr === yesterdayKey) return `Yesterday · ${long}`;
    return long;
  } catch {
    return dateStr;
  }
}

function BulkSummaryBody({ text }: { text: string }) {
  const renderChunk = (chunk: string, keyPrefix: string) => {
    const parts = chunk.split(/(\*\*[^*]+?\*\*)/g);
    return parts.map((part, i) => {
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
        return (
          <strong key={`${keyPrefix}-${i}`} className="font-semibold text-foreground">
            {part.slice(2, -2)}
          </strong>
        );
      }
      return <span key={`${keyPrefix}-${i}`}>{part}</span>;
    });
  };

  return (
    <div className="space-y-3 text-sm leading-relaxed text-foreground">
      {text.split(/\n\n+/).map((block, i) => (
        <div key={i} className="whitespace-pre-wrap">
          {renderChunk(block, String(i))}
        </div>
      ))}
    </div>
  );
}

export function DailyDigestContent() {
  const { data: session } = useSession();
  const api = useMemo(
    () => getApi(session?.user?.email ?? null, session?.user?.name ?? null),
    [session?.user?.email, session?.user?.name]
  );

  const [days, setDays] = useState<DailyBulkSummaryDay[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [generatingDate, setGeneratingDate] = useState<string | null>(null);
  const [generateError, setGenerateError] = useState<string | null>(null);

  const loadDays = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    return api
      .getDailyBulkSummaries()
      .then((r) => {
        setDays(r.days ?? []);
      })
      .catch(() => {
        setLoadError("Could not load daily digests.");
        setDays([]);
      })
      .finally(() => setLoading(false));
  }, [api]);

  useEffect(() => {
    void loadDays();
  }, [loadDays]);

  const generateForDay = useCallback(
    async (date: string) => {
      setGeneratingDate(date);
      setGenerateError(null);
      try {
        const res = await api.generateDailyBulkSummary(date);
        setDays((prev) =>
          prev.map((d) => (d.date === date ? { ...d, bulkSummary: res.bulkSummary } : d))
        );
      } catch (e) {
        const msg = e instanceof Error ? e.message : "Could not generate digest.";
        setGenerateError(
          msg.includes("504")
            ? "Summary generation timed out. Wait a minute, then click Generate or Regenerate again."
            : msg
        );
      } finally {
        setGeneratingDate(null);
      }
    },
    [api]
  );

  return (
    <div className="space-y-4 sm:space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">Daily digest</h1>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="shrink-0"
          disabled={loading || generatingDate != null}
          onClick={() => void loadDays()}
        >
          <RefreshCw className={cn("mr-2 h-4 w-4", loading && "animate-spin")} />
          Refresh
        </Button>
      </div>

      {loadError ? (
        <Card className="border-destructive/40">
          <CardContent className="py-6 text-sm text-destructive">{loadError}</CardContent>
        </Card>
      ) : null}

      {generateError ? (
        <Card className="border-amber-500/40 bg-amber-500/5">
          <CardContent className="py-4 text-sm text-amber-900 dark:text-amber-100">{generateError}</CardContent>
        </Card>
      ) : null}

      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Card key={i}>
              <CardHeader className="pb-2">
                <Skeleton className="h-5 w-48" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-20 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {days.map((day) => {
            const isGenerating = generatingDate === day.date;
            const hasEmails = day.emailCount > 0;
            return (
              <Card key={day.date} className="overflow-hidden">
                <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0 pb-2">
                  <div className="min-w-0">
                    <CardTitle className="flex items-center gap-2 text-base font-semibold">
                      <CalendarDays className="h-4 w-4 shrink-0 text-amber-500" />
                      <span className="truncate">{formatDayLabel(day.date)}</span>
                    </CardTitle>
                    <p className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground">
                      <Mail className="h-3.5 w-3.5" />
                      {day.emailCount === 1 ? "1 email" : `${day.emailCount} emails`}
                    </p>
                  </div>
                  {hasEmails ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="shrink-0"
                      disabled={isGenerating}
                      onClick={() => void generateForDay(day.date)}
                    >
                      <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", isGenerating && "animate-spin")} />
                      {day.bulkSummary ? "Regenerate" : "Generate"}
                    </Button>
                  ) : null}
                </CardHeader>
                <CardContent>
                  {!hasEmails ? (
                    <p className="text-sm text-muted-foreground">No emails received this day.</p>
                  ) : isGenerating ? (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2 text-sm text-muted-foreground">
                        <Sparkles className="h-4 w-4 animate-pulse text-violet-500" />
                        Generating summary for {day.emailCount} emails…
                      </div>
                      <Skeleton className="h-24 w-full" />
                    </div>
                  ) : day.bulkSummary ? (
                    <BulkSummaryBody text={day.bulkSummary} />
                  ) : (
                    <div className="space-y-3">
                      <p className="text-sm text-muted-foreground">No summary yet.</p>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={isGenerating}
                        onClick={() => void generateForDay(day.date)}
                      >
                        <Sparkles className={cn("mr-1.5 h-3.5 w-3.5", isGenerating && "animate-pulse")} />
                        Generate summary
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
