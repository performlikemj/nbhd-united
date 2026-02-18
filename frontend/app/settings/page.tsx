"use client";

import { useEffect, useMemo, useState } from "react";

import { PersonaSelector } from "@/components/persona-selector";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useMeQuery,
  usePersonasQuery,
  usePreferencesQuery,
  useRefreshConfigMutation,
  useRefreshConfigStatusQuery,
  useUpdateProfileMutation,
  useUpdatePreferencesMutation,
} from "@/lib/queries";

type LanguageOption = {
  label: string;
  value: string;
};

const LANGUAGE_OPTIONS: LanguageOption[] = [
  { label: "English", value: "en" },
  { label: "日本語 (Japanese)", value: "ja" },
  { label: "Español (Spanish)", value: "es" },
  { label: "Français (French)", value: "fr" },
  { label: "Deutsch (German)", value: "de" },
  { label: "한국어 (Korean)", value: "ko" },
  { label: "中文 (Chinese)", value: "zh" },
  { label: "Português (Portuguese)", value: "pt" },
  { label: "العربية (Arabic)", value: "ar" },
  { label: "हिन्दी (Hindi)", value: "hi" },
  { label: "Italiano (Italian)", value: "it" },
  { label: "Русский (Russian)", value: "ru" },
  { label: "Türkçe (Turkish)", value: "tr" },
  { label: "Tiếng Việt (Vietnamese)", value: "vi" },
  { label: "ไทย (Thai)", value: "th" },
  { label: "Bahasa Indonesia (Indonesian)", value: "id" },
];

const TIMEZONE_GROUPS: Array<{ region: string; zones: string[] }> = [
  {
    region: "Popular",
    zones: [
      "Asia/Tokyo",
      "America/New_York",
      "America/Los_Angeles",
      "Europe/London",
      "Asia/Singapore",
      "Asia/Shanghai",
      "Europe/Paris",
      "Australia/Sydney",
      "Europe/Berlin",
    ],
  },
  {
    region: "North America",
    zones: [
      "America/Anchorage",
      "America/Chicago",
      "America/Denver",
      "America/Halifax",
      "America/Havana",
      "America/Mexico_City",
      "America/Phoenix",
      "America/Puerto_Rico",
      "America/Santiago",
      "America/Toronto",
      "America/Vancouver",
      "America/Winnipeg",
      "Canada/Atlantic",
      "Canada/Central",
      "Canada/Eastern",
      "Canada/Mountain",
      "Canada/Pacific",
      "Canada/Newfoundland",
      "Mexico/BajaSur",
      "Mexico/General",
    ],
  },
  {
    region: "Europe",
    zones: [
      "Europe/Amsterdam",
      "Europe/Andorra",
      "Europe/Athens",
      "Europe/Budapest",
      "Europe/Dublin",
      "Europe/Helsinki",
      "Europe/Istanbul",
      "Europe/Kiev",
      "Europe/Lisbon",
      "Europe/Madrid",
      "Europe/Moscow",
      "Europe/Oslo",
      "Europe/Prague",
      "Europe/Rome",
      "Europe/Stockholm",
      "Europe/Zurich",
      "Europe/Belgrade",
      "Europe/Brussels",
      "Europe/Minsk",
      "Europe/Zurich",
      "Europe/Madrid",
    ],
  },
  {
    region: "Asia",
    zones: [
      "Asia/Bangkok",
      "Asia/Calcutta",
      "Asia/Dubai",
      "Asia/Hong_Kong",
      "Asia/Jerusalem",
      "Asia/Karachi",
      "Asia/Kolkata",
      "Asia/Krasnoyarsk",
      "Asia/Manila",
      "Asia/Seoul",
      "Asia/Shanghai",
      "Asia/Singapore",
      "Asia/Taipei",
      "Asia/Tashkent",
      "Asia/Tbilisi",
      "Asia/Tehran",
      "Asia/Tomsk",
      "Asia/Yekaterinburg",
      "Asia/Ho_Chi_Minh",
      "Asia/Jakarta",
      "Asia/Almaty",
      "Asia/Baghdad",
      "Asia/Novosibirsk",
      "Asia/Vladivostok",
    ],
  },
  {
    region: "South America",
    zones: [
      "America/Asuncion",
      "America/Araguaina",
      "America/Argentina/Buenos_Aires",
      "America/Argentina/Catamarca",
      "America/Argentina/Cordoba",
      "America/Bahia",
      "America/Belem",
      "America/Bogota",
      "America/Campo_Grande",
      "America/Caracas",
      "America/Cayenne",
      "America/Cuiaba",
      "America/Fortaleza",
      "America/Montevideo",
      "America/Paramaribo",
      "America/Recife",
      "America/Sao_Paulo",
    ],
  },
  {
    region: "Africa",
    zones: [
      "Africa/Abidjan",
      "Africa/Addis_Ababa",
      "Africa/Cairo",
      "Africa/Casablanca",
      "Africa/Dar_es_Salaam",
      "Africa/Johannesburg",
      "Africa/Khartoum",
      "Africa/Lagos",
      "Africa/Lusaka",
      "Africa/Mogadishu",
      "Africa/Nairobi",
      "Africa/Tripoli",
      "Africa/Tunis",
      "Africa/Windhoek",
      "Africa/Algiers",
      "Africa/Brazzaville",
      "Africa/Ceuta",
      "Africa/Cairo",
      "Africa/Juba",
    ],
  },
];

function offsetLabel(tz: string): string {
  if (!tz) return "UTC+0";

  const map: Record<string, string> = {
    "Asia/Tokyo": "JST, UTC+9",
    "America/New_York": "EST, UTC-5",
    "America/Los_Angeles": "PST, UTC-8",
    "Europe/London": "GMT, UTC+0",
    "Europe/Berlin": "CET, UTC+1",
    "Europe/Paris": "CET, UTC+1",
    "America/Chicago": "CST, UTC-6",
    "America/Denver": "MST, UTC-7",
    "America/Santiago": "CLST, UTC-3",
    "America/Sao_Paulo": "BRT, UTC-3",
    "Asia/Shanghai": "CST, UTC+8",
    "Asia/Seoul": "KST, UTC+9",
    "Asia/Singapore": "SGT, UTC+8",
    "Asia/Kolkata": "IST, UTC+5:30",
    "Asia/Karachi": "PKT, UTC+5",
    "Asia/Jakarta": "WIB, UTC+7",
    "Australia/Sydney": "AEST, UTC+10",
    "Pacific/Auckland": "NZST, UTC+12",
  };

  return map[tz] ?? "UTC";
}

function findTimezoneLabel(tz: string) {
  return `${tz} (${offsetLabel(tz)})`;
}

function timeAgo(dateStr: string): string {
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

export default function SettingsPage() {
  const { data: me, isLoading } = useMeQuery();
  const { data: personas } = usePersonasQuery();
  const { data: prefs } = usePreferencesQuery();
  const { data: refreshConfigStatus, refetch: refetchRefreshConfigStatus } = useRefreshConfigStatusQuery();
  const refreshConfigMutation = useRefreshConfigMutation();
  const updatePrefs = useUpdatePreferencesMutation();
  const updateProfile = useUpdateProfileMutation();

  const [editingPersona, setEditingPersona] = useState(false);
  const [editingDisplayName, setEditingDisplayName] = useState(false);
  const [editingLanguage, setEditingLanguage] = useState(false);
  const [editingTimezone, setEditingTimezone] = useState(false);

  const [selectedPersona, setSelectedPersona] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [language, setLanguage] = useState("en");
  const [timezone, setTimezone] = useState("UTC");
  const [refreshMessage, setRefreshMessage] = useState("");
  const [refreshError, setRefreshError] = useState("");

  const [savingField, setSavingField] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState("");

  const languageLookup = useMemo(() => {
    const map = new Map<string, string>();
    LANGUAGE_OPTIONS.forEach((option) => map.set(option.value, option.label));
    return map;
  }, []);

  const currentPersona = prefs?.agent_persona ?? "neighbor";
  const currentPersonaLabel = personas?.find((p) => p.key === currentPersona);

  const hasPendingConfigUpdate = refreshConfigStatus?.has_pending_update ?? false;
  const canRefreshConfig = refreshConfigStatus?.can_refresh ?? true;
  const cooldownMinutes = refreshConfigStatus ? Math.max(1, Math.ceil(refreshConfigStatus.cooldown_seconds / 60)) : 0;

  useEffect(() => {
    if (!editingDisplayName && me) {
      setDisplayName(me.display_name || "");
    }
    if (!editingLanguage && me) {
      setLanguage(me.language || "en");
    }
    if (!editingTimezone && me) {
      setTimezone(me.timezone || "UTC");
    }
  }, [me, editingDisplayName, editingLanguage, editingTimezone]);

  useEffect(() => {
    if (!editingPersona && currentPersona) {
      setSelectedPersona(currentPersona);
    }
  }, [currentPersona, editingPersona]);

  const clearStatus = () => {
    setSavingField(null);
    setSaveMessage("");
  };

  const clearRefreshStatus = () => {
    setRefreshMessage("");
    setRefreshError("");
  };

  const handleSaveProfileField = async (
    field: "display_name" | "language" | "timezone",
    payload: { display_name?: string; language?: string; timezone?: string },
  ) => {
    setSaveMessage("");
    setSavingField(field);

    const previousTimezone = me?.timezone;

    try {
      await updateProfile.mutateAsync(payload);

      if (field === "display_name") {
        setEditingDisplayName(false);
      }
      if (field === "language") {
        setEditingLanguage(false);
      }
      if (field === "timezone") {
        setEditingTimezone(false);
      }

      if (field === "timezone" && payload.timezone && previousTimezone !== payload.timezone) {
        setSaveMessage("Saved! Agent timezone updated. Changes take effect on next message.");
      } else {
        setSaveMessage("Saved!");
      }
    } catch (error) {
      setSaveMessage(error instanceof Error ? error.message : "Failed to save. Please try again.");
    } finally {
      setSavingField(null);
      if (field === "display_name") {
        setEditingDisplayName(false);
      }
      if (field === "language") {
        setEditingLanguage(false);
      }
      if (field === "timezone") {
        setEditingTimezone(false);
      }
      window.setTimeout(clearStatus, 3000);
    }
  };

  const handleTimezoneSave = async () => {
    if (!timezone || timezone === me?.timezone) {
      setEditingTimezone(false);
      return;
    }
    await handleSaveProfileField("timezone", { timezone });
  };

  const handleLanguageSave = async () => {
    const normalized = language || "en";
    if (!normalized || normalized === me?.language) {
      setEditingLanguage(false);
      return;
    }
    await handleSaveProfileField("language", { language: normalized });
  };

  const handleDisplayNameSave = async () => {
    const next = displayName.trim() || me?.display_name || "";
    if (!next || next === me?.display_name) {
      setEditingDisplayName(false);
      return;
    }
    await handleSaveProfileField("display_name", { display_name: next });
  };

  const handlePersonaSave = async () => {
    if (selectedPersona && selectedPersona !== currentPersona) {
      await updatePrefs.mutateAsync({ agent_persona: selectedPersona });
      setSaveMessage("Saved!");
      window.setTimeout(clearStatus, 3000);
    }
    setEditingPersona(false);
  };

  const handleRefreshConfig = async () => {
    setRefreshMessage("");
    setRefreshError("");
    try {
      await refreshConfigMutation.mutateAsync();
      setRefreshMessage("Configuration refreshed. Your assistant will restart momentarily.");
      window.setTimeout(clearRefreshStatus, 4000);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to refresh configuration.";
      const statusResult = await refetchRefreshConfigStatus().catch(() => undefined);
      const status = statusResult?.data as { can_refresh?: boolean; cooldown_seconds?: number } | undefined;
      const canRefresh = status?.can_refresh ?? canRefreshConfig;
      const cooldown = status?.cooldown_seconds ?? refreshConfigStatus?.cooldown_seconds ?? 0;
      const nextCooldownMinutes = Math.max(1, Math.ceil(Math.max(cooldown, 0) / 60));

      if (!canRefresh || message.includes("429")) {
        setRefreshError(cooldown > 0 ? `Available in ${nextCooldownMinutes} minutes` : "Configuration refresh is on cooldown.");
        return;
      }

      setRefreshError(message);
      window.setTimeout(clearRefreshStatus, 5000);
    }
  };

  const isSaving = savingField !== null || updatePrefs.isPending;
  const isRefreshingConfig = refreshConfigMutation.isPending;

  if (isLoading) {
    return (
      <div className="space-y-4">
        <SectionCardSkeleton lines={4} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <SectionCard title="Account" subtitle="Your profile and authentication details">
        {me ? (
          <dl className="grid min-w-0 gap-3 text-sm sm:grid-cols-2">
            {/* Display Name */}
            <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden">
              <div className="mb-2 flex items-start justify-between gap-2">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Display Name</dt>
                {!editingDisplayName ? (
                  <button
                    type="button"
                    onClick={() => {
                      setDisplayName(me.display_name || "");
                      setEditingDisplayName(true);
                    }}
                    className="rounded-full border border-border px-4 py-1.5 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
                  >
                    Edit
                  </button>
                ) : null}
              </div>
              {!editingDisplayName ? (
                <dd className="mt-1 break-words text-base font-medium text-ink">{me.display_name || "Not set"}</dd>
              ) : (
                <div className="space-y-3">
                  <input
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                    placeholder="Enter display name"
                  />
                  <div className="flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      onClick={handleDisplayNameSave}
                      disabled={savingField === "display_name"}
                      className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
                    >
                      {savingField === "display_name" ? "Saving..." : "Save"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditingDisplayName(false)}
                      className="rounded-full border border-border px-4 py-1.5 text-sm transition hover:border-border-strong"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Email */}
            <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Email</dt>
              <dd className="mt-1 break-words text-base text-ink">{me.email}</dd>
            </div>

            {/* Username */}
            <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Username</dt>
              <dd className="mt-1 break-words text-base text-ink">{me.username}</dd>
            </div>

            {/* Language */}
            <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden">
              <div className="mb-2 flex items-start justify-between gap-2">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Language</dt>
                {!editingLanguage ? (
                  <button
                    type="button"
                    onClick={() => {
                      setLanguage(me.language || "en");
                      setEditingLanguage(true);
                    }}
                    className="rounded-full border border-border px-4 py-1.5 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
                  >
                    Edit
                  </button>
                ) : null}
              </div>
              {!editingLanguage ? (
                <dd className="mt-1 break-words text-base text-ink">{languageLookup.get(me.language || "en") || me.language || "en"}</dd>
              ) : (
                <div className="space-y-3">
                  <label className="block text-sm text-ink-muted">
                    <span className="sr-only">Language</span>
                    <select
                      value={language}
                      onChange={(e) => setLanguage(e.target.value)}
                      className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                    >
                      {LANGUAGE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      onClick={handleLanguageSave}
                      disabled={savingField === "language"}
                      className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
                    >
                      {savingField === "language" ? "Saving..." : "Save"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditingLanguage(false)}
                      className="rounded-full border border-border px-4 py-1.5 text-sm transition hover:border-border-strong"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Timezone */}
            <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden sm:col-span-2">
              <div className="mb-2 flex items-start justify-between gap-2">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Timezone</dt>
                {!editingTimezone ? (
                  <button
                    type="button"
                    onClick={() => {
                      setTimezone(me.timezone || "UTC");
                      setEditingTimezone(true);
                    }}
                    className="rounded-full border border-border px-4 py-1.5 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
                  >
                    Edit
                  </button>
                ) : null}
              </div>
              {!editingTimezone ? (
                <dd className="mt-1 break-words text-base text-ink">{findTimezoneLabel(me.timezone || "UTC")}</dd>
              ) : (
                <div className="space-y-3">
                  <label className="block text-sm text-ink-muted">
                    <span className="sr-only">Timezone</span>
                    <select
                      value={timezone}
                      onChange={(e) => setTimezone(e.target.value)}
                      className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                    >
                      {TIMEZONE_GROUPS.map((group) => (
                        <optgroup key={group.region} label={group.region}>
                          {group.zones.map((tz) => (
                            <option key={`${group.region}-${tz}`} value={tz}>
                              {findTimezoneLabel(tz)}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                  </label>
                  <div className="flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      onClick={handleTimezoneSave}
                      disabled={savingField === "timezone"}
                      className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
                    >
                      {savingField === "timezone" ? "Saving..." : "Save"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditingTimezone(false)}
                      className="rounded-full border border-border px-4 py-1.5 text-sm transition hover:border-border-strong"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Telegram */}
            <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Telegram</dt>
              <dd className="mt-1">
                {me.telegram_username ? (
                  <span className="break-words text-base text-ink">@{me.telegram_username}</span>
                ) : (
                  <StatusPill status="pending" />
                )}
              </dd>
            </div>

            {/* Tenant */}
            <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden">
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Tenant</dt>
              <dd className="mt-1">
                {me.tenant ? (
                  <StatusPill status={me.tenant.status} />
                ) : (
                  <span className="text-sm text-ink-muted">No tenant provisioned</span>
                )}
              </dd>
            </div>
          </dl>
        ) : (
          <p className="text-sm text-ink-muted">Could not load account details.</p>
        )}
      </SectionCard>

      {saveMessage ? (
        <div className="rounded-panel border border-signal/30 bg-signal-faint px-3 py-2 text-sm text-signal">
          {saveMessage}
        </div>
      ) : null}

      <SectionCard title="Agent Persona" subtitle="Your assistant's personality and communication style">
        {!editingPersona ? (
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              {currentPersonaLabel && (
                <>
                  <span className="text-2xl">{currentPersonaLabel.emoji}</span>
                  <div>
                    <p className="font-medium text-ink">{currentPersonaLabel.label}</p>
                    <p className="text-sm text-ink-muted">{currentPersonaLabel.description}</p>
                  </div>
                </>
              )}
              {!currentPersonaLabel && <p className="text-sm text-ink-muted">No persona selected</p>}
            </div>
            <button
              type="button"
              onClick={() => {
                setSelectedPersona(currentPersona);
                setEditingPersona(true);
              }}
              className="rounded-full border border-border px-4 py-1.5 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
            >
              Change
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            {personas && <PersonaSelector personas={personas} selected={selectedPersona} onSelect={setSelectedPersona} />}
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={handlePersonaSave}
                disabled={isSaving}
                className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
              >
                {updatePrefs.isPending ? "Saving..." : "Save"}
              </button>
              <button
                type="button"
                onClick={() => setEditingPersona(false)}
                className="rounded-full border border-border px-4 py-2 text-sm text-ink-muted transition hover:border-border-strong"
              >
                Cancel
              </button>
            </div>
            <p className="text-xs text-ink-faint">Changes take effect on the next container restart or reprovision.</p>
          </div>
        )}
      </SectionCard>

      <SectionCard
        title="Agent Configuration"
        subtitle="Configuration updates are applied automatically when your assistant is idle"
      >
        <div className="rounded-panel border border-border bg-surface p-4 min-w-0 overflow-hidden">
          <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
            Agent Configuration
          </p>
          <p className="mt-1 text-base text-ink">
            {refreshConfigStatus?.last_refreshed
              ? `Last updated: ${timeAgo(refreshConfigStatus.last_refreshed)}`
              : "Last updated: never"}
          </p>
        </div>

        {hasPendingConfigUpdate ? (
          <>
            <div className="mt-3 rounded-panel border border-amber-border/80 bg-amber-bg px-3.5 py-2.5 text-sm text-amber-text">
              🔄 An update is available and will be applied automatically when your assistant is idle.
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={handleRefreshConfig}
                disabled={isRefreshingConfig || !canRefreshConfig}
                className="rounded-full border border-border-strong px-5 py-2 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink disabled:cursor-not-allowed disabled:opacity-55"
              >
                {isRefreshingConfig
                  ? "Applying..."
                  : canRefreshConfig
                    ? "Apply Now"
                    : `Available in ${cooldownMinutes} minutes`}
              </button>
              {(refreshMessage || refreshError) ? (
                <p className={`text-sm ${refreshMessage ? "text-signal" : "text-rose-text"}`}>{refreshMessage || refreshError}</p>
              ) : null}
            </div>
            <p className="mt-2 text-xs text-ink-faint">Or wait — it&apos;ll apply automatically within 15 minutes of inactivity.</p>
          </>
        ) : (
          <p className="mt-3 text-sm text-ink-muted">✓ Your assistant is up to date</p>
        )}
      </SectionCard>
    </div>
  );
}
