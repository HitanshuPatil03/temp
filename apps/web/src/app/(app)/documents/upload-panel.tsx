"use client";

/**
 * The upload panel.
 *
 * Posts straight to `/api/documents` through the proxy, which attaches the
 * session token server-side — so this component never touches a credential.
 *
 * Two things it does that an upload form usually does not:
 *
 * **It shows the refusal.** The boundary rejects an encrypted PDF, a zip bomb or
 * an oversized file with a *reason* (`mrip.ingest.intake`), and that sentence is
 * rendered verbatim rather than replaced with "upload failed". The officer's
 * next action — ask the subsidiary for an unprotected copy — depends on it.
 *
 * **It says when nothing happened.** Re-uploading identical bytes is a no-op by
 * design, and the response says so. Silence there would look like a failure and
 * produce a second attempt.
 */

import { useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, Upload } from "lucide-react";
import { useSWRConfig } from "swr";

import { Button } from "@/components/ui/button";
import { Card, CardBody, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { keys } from "@/lib/api";

interface Accepted {
  document_id: string;
  filename: string;
  doc_class: string;
  page_count: number | null;
  state: string;
  created: boolean;
  notes: string[];
}

interface Refusal {
  error: string;
  message: string;
  context?: Record<string, unknown>;
}

export function UploadPanel() {
  const { mutate } = useSWRConfig();
  const form = useRef<HTMLFormElement>(null);
  const [pending, setPending] = useState(false);
  const [accepted, setAccepted] = useState<Accepted | null>(null);
  const [refused, setRefused] = useState<Refusal | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const element = event.currentTarget;
    const data = new FormData(element);
    if (!(data.get("file") as File)?.size) {
      setRefused({ error: "no_file", message: "Choose a file to upload." });
      return;
    }

    setPending(true);
    setAccepted(null);
    setRefused(null);
    try {
      const response = await fetch("/api/documents", {
        method: "POST",
        body: data,
      });
      const body = await response.json();

      if (!response.ok) {
        setRefused(
          typeof body?.detail === "object" && body.detail !== null
            ? (body.detail as Refusal)
            : {
                error: "rejected",
                message: String(body?.detail ?? "The upload was refused."),
              },
        );
        return;
      }

      setAccepted(body as Accepted);
      element.reset();
      // The registry and the dashboard counters both changed.
      await mutate(keys.documents());
      await mutate(keys.summary());
    } catch {
      setRefused({
        error: "unreachable",
        message:
          "The API did not respond. Check that the backend is running, then try again.",
      });
    } finally {
      setPending(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Add a document</CardTitle>
        <CardDescription>
          PDF, Excel, Word or an image of a page. The file is checked before it is
          stored — encrypted PDFs, archives that expand beyond their stated size and
          active content are refused or stripped, and identical bytes are never
          ingested twice.
        </CardDescription>
      </CardHeader>
      <CardBody>
        <form ref={form} onSubmit={submit} className="space-y-4">
          <Field label="File">
            <Input
              name="file"
              type="file"
              required
              accept=".pdf,.xlsx,.xls,.docx,.png,.jpg,.jpeg,.tif,.tiff"
              className="file:mr-3 file:rounded file:border-0 file:bg-plane file:px-2 file:py-1 file:text-xs file:text-ink-2"
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Publisher" hint="Canonical entity id, e.g. secl">
              <Input name="publisher_entity_id" placeholder="optional" />
            </Field>
            <Field label="Fiscal year" hint="Only if the document states one">
              <Input name="fiscal_year" placeholder="FY2024-25" />
            </Field>
            <Field label="Sensitivity" hint="Defaults to internal">
              <Select name="sensitivity" defaultValue="internal">
                <option value="public">Public</option>
                <option value="internal">Internal</option>
                <option value="restricted">Restricted</option>
              </Select>
            </Field>
          </div>

          <Field label="Title" hint="Shown instead of the filename, if given">
            <Input name="title" placeholder="optional" />
          </Field>

          <div className="flex items-center gap-3">
            <Button type="submit" variant="primary" pending={pending}>
              <Upload className="size-3.5" aria-hidden />
              Upload
            </Button>
            <span className="text-xs text-ink-3">
              Ingestion starts immediately and runs in the background.
            </span>
          </div>
        </form>

        {refused ? (
          <div
            role="alert"
            className="mt-4 flex gap-2 rounded-md border border-critical/30 bg-critical/5 px-3 py-2.5"
          >
            <AlertTriangle
              className="mt-0.5 size-4 shrink-0 text-critical"
              aria-hidden
            />
            <div className="min-w-0">
              <p className="text-xs font-medium text-critical">
                Refused — {refused.error.replaceAll("_", " ")}
              </p>
              <p className="mt-0.5 text-xs leading-relaxed text-critical">
                {refused.message}
              </p>
            </div>
          </div>
        ) : null}

        {accepted ? (
          <div className="mt-4 flex gap-2 rounded-md border border-good/30 bg-good/5 px-3 py-2.5">
            {accepted.created ? (
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-good" aria-hidden />
            ) : (
              <Info className="mt-0.5 size-4 shrink-0 text-ink-2" aria-hidden />
            )}
            <div className="min-w-0 text-xs leading-relaxed text-ink-2">
              <p className="font-medium text-ink">
                {accepted.created
                  ? `Accepted — ${accepted.filename}`
                  : "Already in the corpus"}
              </p>
              <p className="mt-0.5">
                {accepted.doc_class.replaceAll("_", " ")}
                {accepted.page_count ? `, ${accepted.page_count} pages` : ""} ·{" "}
                <span className="font-mono">{accepted.document_id}</span> ·{" "}
                {accepted.state}
              </p>
              {accepted.notes.map((note) => (
                <p key={note} className="mt-1 text-ink-3">
                  {note}
                </p>
              ))}
            </div>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
