"use client";

/**
 * The knowledge base: the customer's own material, and what the agent can quote from it.
 *
 * The gap this fills was the largest one in the product. Every piece of the knowledge
 * base shipped — the pdf/docx extractors, the pgvector store, the agentic retrieval loop
 * with its grades and its fallback decision — and `ingest_document` was called from
 * twenty tests and nowhere else. There was no route and no screen, so no business could
 * ever hold a single chunk, and "it reads your own material" was true of the test suite
 * and not of the thing anybody could use. `docs/FEATURES.md` §7 lists ingesting
 * documents as step 1 of the customer journey.
 *
 * Two decisions about this screen specifically.
 *
 * **It reports what indexing ACHIEVED, never that an upload succeeded.** A scanned PDF
 * uploads perfectly and yields no text, and "Uploaded ✓" on that file is the exact lie
 * this product cannot afford: the owner would believe their price list was searchable.
 * So every row carries its status, its passage count, and the sentence explaining what
 * to do about it.
 *
 * **The total is the number that matters, and it is shown at the top.** Documents are
 * not what the agent retrieves — passages are — and the total is also what decides
 * whether retrieval is wired into a run at all (see `run_executor._build_retrieve`).
 *
 * A client component, for the usual reason: the session cookie needs an `Origin` header
 * and a server component's `fetch` sends none.
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Pill, SoftButton, SoftCard, SoftWell } from "@/app/components/soft";
import { ApiError } from "@/app/lib/api";
import {
  ACCEPT_ATTRIBUTE,
  ACCEPTED_SUFFIXES,
  MAX_UPLOAD_BYTES,
  type Document,
  type DocumentList,
  type UploadResult,
  deleteDocument,
  fetchDocuments,
  statusExplanation,
  statusTone,
  uploadDocument,
} from "@/app/lib/documents-api";

type ListState =
  | { kind: "loading" }
  | { kind: "ready"; list: DocumentList }
  | { kind: "error"; message: string };

type UploadState =
  | { kind: "idle" }
  | { kind: "uploading"; filename: string }
  | { kind: "done"; result: UploadResult }
  | { kind: "error"; message: string };

export default function DocumentsPage() {
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [upload, setUpload] = useState<UploadState>({ kind: "idle" });
  const input = useRef<HTMLInputElement>(null);

  const reload = useCallback(async () => {
    try {
      setState({ kind: "ready", list: await fetchDocuments() });
    } catch (exc) {
      // A 409 `no_business` is an account that has not finished onboarding, and the
      // API's own message says exactly that. Passed through rather than replaced.
      setState({
        kind: "error",
        message: exc instanceof ApiError ? exc.message : "Could not load your documents.",
      });
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function send(file: File) {
    // Checked here as courtesy only — the API enforces the same ceiling before it reads
    // a byte. Refusing instantly beats a long upload that ends in a bare 413.
    if (file.size > MAX_UPLOAD_BYTES) {
      setUpload({
        kind: "error",
        message: `${file.name} is larger than 25 MB. Please upload a smaller export.`,
      });
      return;
    }

    setUpload({ kind: "uploading", filename: file.name });
    try {
      const result = await uploadDocument(file);
      setUpload({ kind: "done", result });
      await reload();
    } catch (exc) {
      setUpload({
        kind: "error",
        message: exc instanceof ApiError ? exc.message : `Could not upload ${file.name}.`,
      });
    } finally {
      // So the same file can be chosen twice — a fixed export usually has the same name.
      if (input.current) input.current.value = "";
    }
  }

  async function remove(document: Document) {
    try {
      await deleteDocument(document.id);
      await reload();
    } catch (exc) {
      setUpload({
        kind: "error",
        message: exc instanceof ApiError ? exc.message : `Could not delete ${document.filename}.`,
      });
    }
  }

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <p
        className="text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Knowledge base
      </p>
      <h1 className="mt-2 text-[26px] font-semibold tracking-tight">Your own material</h1>
      <p className="mt-3 text-sm" style={{ color: "var(--text-muted)" }}>
        Upload the documents that already contain your answers — a price list, a service
        sheet, a quote explainer. The agent reads them when it decides what to write, when
        it outlines a page, and when it looks for a proof point, so what it publishes says
        what you actually do rather than what a model assumes.
      </p>

      <SoftCard className="mt-8 p-6" size="lg">
        <h2 className="text-sm font-semibold">Add a document</h2>
        <p className="mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
          {ACCEPTED_SUFFIXES.join(", ")} — up to 25 MB. Indexing takes a few seconds and
          happens while you wait, so you find out straight away whether the file could be
          read.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <input
            ref={input}
            type="file"
            accept={ACCEPT_ATTRIBUTE}
            className="text-sm"
            style={{ color: "var(--text-muted)" }}
            disabled={upload.kind === "uploading"}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void send(file);
            }}
            aria-label="Choose a document to upload"
          />
          {upload.kind === "uploading" && (
            <span className="text-sm" style={{ color: "var(--text-muted)" }}>
              Indexing {upload.filename}…
            </span>
          )}
        </div>

        {upload.kind === "error" && (
          <p className="mt-3 text-sm font-medium" style={{ color: "var(--err)" }}>
            {upload.message}
          </p>
        )}
        {upload.kind === "done" && <UploadOutcome result={upload.result} />}
      </SoftCard>

      <section className="mt-10" aria-labelledby="documents-heading">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="documents-heading" className="text-sm font-semibold">
            Indexed documents
          </h2>
          {state.kind === "ready" && (
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              {/* Passages, not documents: passages are what the agent retrieves, and
                  the total is what decides whether retrieval runs at all. */}
              {state.list.totalChunks} passage
              {state.list.totalChunks === 1 ? "" : "s"} available to the agent
            </span>
          )}
        </div>

        <div className="mt-4" aria-live="polite">
          {state.kind === "loading" && (
            <p className="py-3 text-sm" style={{ color: "var(--text-muted)" }}>
              Loading your documents…
            </p>
          )}

          {state.kind === "error" && (
            <SoftWell className="p-4">
              <p className="text-sm font-medium" style={{ color: "var(--err)" }}>
                {state.message}
              </p>
            </SoftWell>
          )}

          {state.kind === "ready" && state.list.documents.length === 0 && (
            <SoftWell className="p-4">
              <p className="text-sm" style={{ color: "var(--text-muted)" }}>
                Nothing yet. Until something is here, a run is written from your website and
                live search only — and it will say so, under &ldquo;written without&rdquo;.
              </p>
            </SoftWell>
          )}

          {state.kind === "ready" && state.list.documents.length > 0 && (
            <ul className="space-y-2.5">
              {state.list.documents.map((document) => (
                <DocumentRow key={document.id} document={document} onDelete={remove} />
              ))}
            </ul>
          )}
        </div>
      </section>

      <p className="mt-10">
        <Link
          href="/"
          className="text-sm font-medium underline"
          style={{ color: "var(--primary)" }}
        >
          Back to the dashboard
        </Link>
      </p>
    </main>
  );
}

/**
 * What the upload achieved — including the case where it achieved nothing.
 *
 * `chunksDuplicate` gets its own line rather than replacing the main one, and that is a
 * correction from driving the real endpoint: re-uploading the same price list returns
 * `chunksStored: 1, chunksDuplicate: 1`, which looks contradictory and is not. The
 * passage was already EMBEDDED for this business so its vector was reused, and a row was
 * still written to attach it to the new document. Treating the two as mutually exclusive
 * showed the wrong sentence for the most common repeat case there is.
 *
 * The number is worth printing because it is the proof that dedup-by-hash is real rather
 * than described — and because "we did not re-read your file" is what a customer wants to
 * hear when they upload it twice.
 */
function UploadOutcome({ result }: { result: UploadResult }) {
  return (
    <SoftWell className="mt-4 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={statusTone(result.status)}>{result.status.replace(/_/g, " ")}</Pill>
        <span className="text-sm font-medium">{result.document.filename}</span>
      </div>
      <p className="mt-2 text-sm" style={{ color: "var(--text-muted)" }}>
        {statusExplanation(result.document)}
      </p>
      {result.chunksDuplicate > 0 && (
        <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
          {result.chunksDuplicate} passage{result.chunksDuplicate === 1 ? " was" : "s were"}{" "}
          already indexed for this business, so {result.chunksDuplicate === 1 ? "it" : "they"}{" "}
          {result.chunksDuplicate === 1 ? "was" : "were"} not read again.
        </p>
      )}
      {result.note && (
        <p className="mt-1.5 text-xs" style={{ color: "var(--warn)" }}>
          {result.note}
        </p>
      )}
    </SoftWell>
  );
}

function DocumentRow({
  document,
  onDelete,
}: {
  document: Document;
  onDelete: (document: Document) => Promise<void>;
}) {
  return (
    <li>
      {/* `soft-edge` because this row contains an interactive control and a neumorphic
          shadow measures about 1.2:1 — the hairline is what carries SC 1.4.11. */}
      <div
        className="soft-flat soft-edge px-4 py-3"
        style={{ borderRadius: "var(--r-sm)" }}
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={statusTone(document.status)}>
                {document.status.replace(/_/g, " ")}
              </Pill>
              <span className="text-sm font-medium">{document.filename}</span>
            </div>
            <p className="mt-1.5 text-xs" style={{ color: "var(--text-muted)" }}>
              {statusExplanation(document)}
            </p>
          </div>
          <SoftButton
            variant="quiet"
            onClick={() => void onDelete(document)}
            ariaLabel={`Delete ${document.filename} and everything retrievable from it`}
          >
            Delete
          </SoftButton>
        </div>
      </div>
    </li>
  );
}
