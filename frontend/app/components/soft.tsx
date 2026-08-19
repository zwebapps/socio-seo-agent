"use client";

/**
 * Soft-UI primitives.
 *
 * Every interactive control here carries `soft-edge` as well as its shadow. That is
 * not decoration: a neumorphic shadow measures roughly 1.2:1 against its background,
 * and WCAG 1.4.11 asks for 3:1 on a component boundary — so shadow alone leaves a
 * control invisible to a low-vision user. The hairline supplies the contrast; the
 * shadow supplies the character.
 */

import type { ReactNode } from "react";

const R = {
  sm: "var(--r-sm)",
  md: "var(--r-md)",
  lg: "var(--r-lg)",
  pill: "var(--r-pill)",
} as const;

export function SoftCard({
  children,
  className = "",
  size = "lg",
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  size?: keyof typeof R;
  as?: "section" | "div" | "article";
}) {
  return (
    <Tag
      className={`soft-raised-lg ${className}`}
      style={{ borderRadius: R[size] }}
    >
      {children}
    </Tag>
  );
}

export function SoftWell({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`soft-sunken ${className}`} style={{ borderRadius: R.md }}>
      {children}
    </div>
  );
}

export function SoftButton({
  children,
  onClick,
  type = "button",
  variant = "plain",
  disabled = false,
  className = "",
  ariaLabel,
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  variant?: "plain" | "primary" | "quiet";
  disabled?: boolean;
  className?: string;
  /**
   * An accessible name that replaces the visible label. Needed wherever the same words
   * appear on several buttons — five "Copy" buttons on one screen are five identically
   * named controls to a screen-reader user unless each says what it copies.
   */
  ariaLabel?: string;
}) {
  const primary = variant === "primary";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      className={`soft-press ${primary ? "" : "soft-raised soft-edge"} px-4 py-2 text-sm font-medium disabled:opacity-45 ${className}`}
      style={{
        borderRadius: R.pill,
        ...(primary
          ? {
              background: "var(--primary)",
              color: "var(--primary-ink)",
              boxShadow: "-4px -4px 10px var(--shadow-light), 5px 5px 14px var(--shadow-dark)",
            }
          : { color: variant === "quiet" ? "var(--text-muted)" : "var(--text)" }),
      }}
    >
      {children}
    </button>
  );
}

/** The pill switch from the reference: recessed track, raised knob. */
export function SoftToggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="soft-sunken soft-edge relative inline-flex h-9 w-[74px] shrink-0 items-center disabled:opacity-45"
      style={{ borderRadius: "var(--r-pill)" }}
    >
      <span
        aria-hidden
        className="absolute text-[10px] font-semibold uppercase tracking-wider"
        style={{
          left: checked ? 14 : "auto",
          right: checked ? "auto" : 12,
          color: checked ? "var(--primary)" : "var(--text-faint)",
        }}
      >
        {checked ? "on" : "off"}
      </span>
      <span
        className="absolute h-7 w-7 transition-all duration-200"
        style={{
          borderRadius: "var(--r-pill)",
          left: checked ? 42 : 4,
          background: checked ? "var(--primary)" : "var(--surface-raised)",
          boxShadow: "-2px -2px 6px var(--shadow-light), 3px 3px 8px var(--shadow-dark)",
          border: checked ? "none" : "1px solid var(--edge)",
        }}
      />
    </button>
  );
}

export function SoftSelect({
  value,
  onChange,
  options,
  label,
  className = "",
}: {
  value: string;
  onChange: (next: string) => void;
  options: { value: string; label: string; disabled?: boolean }[];
  label: string;
  className?: string;
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`soft-sunken soft-edge appearance-none px-3 py-2 text-sm ${className}`}
      style={{ borderRadius: R.sm, color: "var(--text)", background: "var(--surface-sunken)" }}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value} disabled={o.disabled}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function SoftInput({
  value,
  onChange,
  label,
  placeholder,
  className = "",
  autoFocus = false,
  describedBy,
}: {
  value: string;
  onChange: (next: string) => void;
  label: string;
  placeholder?: string;
  className?: string;
  /**
   * For a field that APPEARS in response to a click — an inline edit, say. Focus has to
   * follow the thing the user just asked for, or a keyboard user is left at the button
   * that opened a field they cannot reach without hunting for it.
   */
  autoFocus?: boolean;
  /** Id of the element describing this field, e.g. a live character counter. */
  describedBy?: string;
}) {
  return (
    <input
      aria-label={label}
      aria-describedby={describedBy}
      value={value}
      placeholder={placeholder}
      // eslint-disable-next-line jsx-a11y/no-autofocus -- see the prop's docstring
      autoFocus={autoFocus}
      onChange={(e) => onChange(e.target.value)}
      className={`soft-sunken soft-edge px-3 py-2 text-sm ${className}`}
      style={{ borderRadius: R.sm, color: "var(--text)" }}
    />
  );
}

/**
 * Status pill. `tone` maps to a token, never to a raw colour — and it always shows
 * TEXT as well as colour, because colour alone is not an accessible signal.
 */
export function Pill({
  tone = "muted",
  children,
}: {
  tone?: "ok" | "warn" | "err" | "muted" | "accent";
  children: ReactNode;
}) {
  const colour = {
    ok: "var(--ok)",
    warn: "var(--warn)",
    err: "var(--err)",
    accent: "var(--accent)",
    muted: "var(--text-muted)",
  }[tone];

  return (
    <span
      className="soft-flat inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider"
      style={{ borderRadius: "var(--r-pill)", color: colour }}
    >
      <span
        aria-hidden
        className="h-1.5 w-1.5 shrink-0"
        style={{ borderRadius: "50%", background: colour }}
      />
      {children}
    </span>
  );
}

/** The rounded icon tile from the reference — used for provider tiles. */
export function SoftTile({
  children,
  active = false,
  className = "",
}: {
  children: ReactNode;
  active?: boolean;
  className?: string;
}) {
  return (
    <div
      className={`${active ? "soft-raised" : "soft-sunken"} flex items-center justify-center ${className}`}
      style={{
        borderRadius: R.md,
        color: active ? "var(--primary)" : "var(--text-faint)",
      }}
    >
      {children}
    </div>
  );
}
