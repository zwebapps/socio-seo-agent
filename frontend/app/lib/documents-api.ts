/**
 * Typed client for the document knowledge base.
 *
 * Mirrors `backend/app/api/documents.py`. Two things about this file are load-bearing.
 *
 * **The upload does NOT go through `request`.** That helper sets
 * `content-type: application/json` on every call, and a multipart body must carry the
 * boundary the browser generates — so setting the header at all is what breaks the
 * upload. `FormData` with no explicit content type is the whole trick.
 *
 * **Every call here runs in the browser.** The session cookie needs an `Origin` header
 * and the API's CSRF guard refuses a cookie-bearing write that arrives without one,
 * which is exactly what `fetch` from a server component sends. Same warning as
 * `runs-api.ts`, and the same reason it is there.
 */

import { ApiError, request } from "./api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

/** One uploaded document, as the screen shows it. */
export type Document = {
  id: string;
  filename: string;
  kind: string;
  /**
   * `indexed` · `no_text` · `failed` · `pending`.
   *
   * Deliberately a `string` and not a union: the server owns this vocabulary, and a
   * union would make the compiler assert something false the day it grows a value.
   */
  status: string;
  chunkCount: number;
  /** Why a document is not searchable, in words a customer can act on. */
  extractionNote: string | null;
  createdAt: string;
};

export type DocumentList = {
  documents: Document[];
  /** Chunks across every document — what the agent can actually retrieve. */
  totalChunks: number;
};

export type UploadResult = {
  document: Document;
  status: string;
  chunksStored: number;
  /** Chunks whose text was already indexed, so nothing was re-embedded. */
  chunksDuplicate: number;
  charsExtracted: number;
  note: string | null;
};

/** What the API will read. Kept in sync with `document_service._SUFFIXES`. */
export const ACCEPTED_SUFFIXES = [".pdf", ".docx", ".md", ".txt", ".html"] as const;

/** The `accept` attribute for the file input, so the picker filters for the customer. */
export const ACCEPT_ATTRIBUTE = ACCEPTED_SUFFIXES.join(",");

/**
 * 25 MiB, matching the body-size ceiling the API enforces for this route ALONE.
 *
 * Checked here as well so an oversized file is refused instantly with a sentence
 * rather than after a long upload that ends in a bare 413 — the server is still the
 * authority, this is only courtesy.
 */
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export function fetchDocuments(): Promise<DocumentList> {
  return request<DocumentList>("/api/v1/documents");
}

export function deleteDocument(id: string): Promise<void> {
  return request<void>(`/api/v1/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/**
 * Upload one file and index it.
 *
 * Hand-rolled rather than routed through `request` for the content-type reason in the
 * module note, which also means the API's two error shapes have to be unpacked here.
 * They are unpacked the same way, so a failure reads as a sentence and never as
 * "[object Object]".
 */
export async function uploadDocument(file: File): Promise<UploadResult> {
  const body = new FormData();
  body.append("file", file);

  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/v1/documents`, {
      method: "POST",
      body,
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    throw new ApiError("network", `Cannot reach the API at ${API_URL}. Is it running?`, 0);
  }

  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const detail = (payload as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === "object" && "code" in detail) {
      const named = detail as { code: string; message: string };
      throw new ApiError(named.code, named.message, response.status);
    }
    if (response.status === 413) {
      throw new ApiError(
        "too_large",
        "That file is larger than 25 MB. Please upload a smaller export.",
        413,
      );
    }
    throw new ApiError("unknown", `Upload failed (${response.status}).`, response.status);
  }

  return payload as UploadResult;
}

/**
 * What a document's status MEANS, in a sentence the owner can act on.
 *
 * The status alone is not enough and the difference matters: `no_text` sends someone to
 * OCR or a different export, while `failed` means try again or tell us. Collapsing them
 * into "didn't work" is what makes a support ticket.
 *
 * `indexed` with a zero chunk count is the case the STATUS cannot carry on its own, which
 * is why the API sends both — its own `DocumentOut` docstring says "`indexed` with zero
 * chunks is a scan". Saying "Searchable — 0 passages the agent can quote" there is a
 * contradiction inside one sentence, and the half a customer reads is "Searchable": they
 * are told the agent can quote a file it holds not one word of, and the thin answers that
 * follow have no visible cause. So the count decides the sentence, not the status.
 */
export function statusExplanation(document: Document): string {
  if (document.extractionNote) return document.extractionNote;
  if (document.status === "indexed") {
    if (document.chunkCount === 0) {
      return (
        "Indexed, but no passages were stored — there is nothing here the agent can " +
        "quote. If it is a scan, it needs OCR before we can use it."
      );
    }
    return `Searchable — ${document.chunkCount} passage${document.chunkCount === 1 ? "" : "s"} the agent can quote.`;
  }
  if (document.status === "no_text") {
    return "No readable text in this file. If it is a scan, it needs OCR before we can use it.";
  }
  if (document.status === "failed") return "This file could not be indexed.";
  return "Still indexing.";
}

export function statusTone(status: string): "ok" | "warn" | "err" | "muted" {
  if (status === "indexed") return "ok";
  if (status === "no_text") return "warn";
  if (status === "failed") return "err";
  return "muted";
}

/**
 * The pill colour for a whole document, which is NOT always its status's colour.
 *
 * `indexed` with zero chunks is a real state — the backend's `DocumentOut` docstring
 * says so, and it is why the API sends `status` and `chunkCount` as two fields. A green
 * pill on that document contradicts the sentence beside it ("nothing here the agent can
 * quote") and the half a customer reads is the colour, so the count has to be able to
 * override the status.
 *
 * `warn`, not `err`: nothing failed. The file was read and held no passages, which is
 * the customer's to fix (usually with OCR) and is exactly what `no_text` means — so it
 * gets `no_text`'s colour, because it is the same fact arrived at down a different path.
 */
export function documentTone(document: Document): "ok" | "warn" | "err" | "muted" {
  if (document.status === "indexed" && document.chunkCount === 0) return "warn";
  return statusTone(document.status);
}
