import { Skeleton } from "@/components/ui/states";

/**
 * The fallback shown while a route's server component resolves (the layout
 * awaits the current user on every navigation). A quiet skeleton of the page
 * frame reads as "loading" rather than a blank flash that reads as "broken".
 */
export default function AppLoading() {
  return (
    <main className="min-w-0 flex-1 space-y-6 px-8 py-6" aria-busy>
      <Skeleton className="h-9 w-64" />
      <Skeleton className="h-5 w-96" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
      </div>
      <Skeleton className="h-64" />
    </main>
  );
}
