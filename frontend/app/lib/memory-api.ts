/**
 * Typed client for business memory and the rules awaiting approval.
 *
 * Mirrors `backend/app/api/memory.py` and the proposals half of
 * `backend/app/api/feedback.py`. Two things worth knowing before reading callers:
 *
 * - **Every write returns the whole memory.** So the panel repaints from the server's
 *   account of what is in force, never from an optimistic local guess. That is what makes
 *   a de-duplicated add visibly a no-op instead of a phantom success.
 * - **The memory routes take no business id** — it comes from the session. The id is
 *   returned in the response, which is what lets the proposals call (which DOES take one)
 *   be made afterwards.
 */

import { request } from "./api";

export type RememberedPreference = {
  /** Stable, URL-safe id derived from the rule's dedup key. Not the array position. */
  id: string;
  rule: string;
};

export type BusinessMemory = {
  businessId: string;
  tone: string | null;
  audience: string | null;
  bannedClaims: string[];
  preferences: RememberedPreference[];
  rememberedCount: number;
  /** The EXACT lines the next run's system prompt receives. */
  promptLines: string[];
  maxPreferences: number;
  maxRuleLength: number;
  /** Which fields this API accepts writes for. The UI's read-only markers come from here. */
  editableFields: string[];
};

export type Proposal = {
  id: string;
  rule: string;
  /** The reject reasons this rule was distilled from — the evidence for the proposal. */
  derivedFrom: string[];
  status: string;
  createdAt: string;
};

const MEMORY = "/api/v1/memory";

export function fetchMemory(): Promise<BusinessMemory> {
  return request<BusinessMemory>(MEMORY);
}

export function addPreference(rule: string): Promise<BusinessMemory> {
  return request<BusinessMemory>(`${MEMORY}/preferences`, {
    method: "POST",
    body: JSON.stringify({ rule }),
  });
}

export function updatePreference(id: string, rule: string): Promise<BusinessMemory> {
  return request<BusinessMemory>(`${MEMORY}/preferences/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify({ rule }),
  });
}

export function deletePreference(id: string): Promise<BusinessMemory> {
  return request<BusinessMemory>(`${MEMORY}/preferences/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function fetchProposals(businessId: string): Promise<Proposal[]> {
  return request<Proposal[]>(`/api/v1/businesses/${businessId}/proposals`);
}

/** Approving is what puts a proposed rule into memory; nothing else applies it. */
export function approveProposal(proposalId: string): Promise<{ rule: string; applied: boolean }> {
  return request<{ rule: string; applied: boolean }>(
    `/api/v1/proposals/${proposalId}/approve`,
    { method: "POST" },
  );
}
