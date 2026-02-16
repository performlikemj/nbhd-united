"use client";

import { FormEvent, useEffect, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import type { NoteTemplate, NoteTemplateSection } from "@/lib/types";
import {
  useCreateNoteTemplateMutation,
  useDeleteNoteTemplateMutation,
  useNoteTemplatesQuery,
  useUpdateNoteTemplateMutation,
} from "@/lib/queries";

type TemplateFormState = {
  name: string;
  slug: string;
  is_default: boolean;
  sections: NoteTemplateSection[];
};

const emptySection: NoteTemplateSection = {
  slug: "",
  title: "",
  content: "",
  source: "shared",
};

const sectionSeed: NoteTemplateSection[] = [
  {
    slug: "morning-report",
    title: "Morning Report",
    content: "",
    source: "agent",
  },
  {
    slug: "weather",
    title: "Weather",
    content: "",
    source: "agent",
  },
  {
    slug: "news",
    title: "News",
    content: "",
    source: "agent",
  },
  {
    slug: "focus",
    title: "Focus",
    content: "",
    source: "agent",
  },
  {
    slug: "evening-check-in",
    title: "Evening Check-in",
    content: "",
    source: "human",
  },
];

function cloneSections(sections: NoteTemplateSection[]): NoteTemplateSection[] {
  return sections.map((section) => ({ ...section }));
}

function normalizeSlug(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, "-");
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Request failed.";
}

export default function TemplatesPage() {
  const { data, isLoading, error } = useNoteTemplatesQuery();
  const createMutation = useCreateNoteTemplateMutation();
  const updateMutation = useUpdateNoteTemplateMutation();
  const deleteMutation = useDeleteNoteTemplateMutation();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [formState, setFormState] = useState<TemplateFormState>({
    name: "",
    slug: "",
    is_default: false,
    sections: cloneSections(sectionSeed),
  });

  useEffect(() => {
    if (!data) {
      return;
    }
    const editingTemplate = data.find((template) => template.id === editingId);
    if (!editingTemplate) {
      return;
    }
    setFormState({
      name: editingTemplate.name,
      slug: editingTemplate.slug,
      is_default: editingTemplate.is_default,
      sections: cloneSections(editingTemplate.sections),
    });
  }, [data, editingId]);

  const handleStartCreate = () => {
    setEditingId(null);
    setFormState({
      name: "",
      slug: "",
      is_default: false,
      sections: cloneSections(sectionSeed),
    });
  };

  const handleStartEdit = (template: NoteTemplate) => {
    setEditingId(template.id);
    setFormState({
      name: template.name,
      slug: template.slug,
      is_default: template.is_default,
      sections: cloneSections(template.sections),
    });
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const payload = {
      slug: formState.slug.trim(),
      name: formState.name.trim(),
      is_default: formState.is_default,
      sections: formState.sections
        .map((section) => ({
          slug: section.slug.trim(),
          title: section.title.trim(),
          content: section.content,
          source: section.source || "shared",
        }))
        .filter((section) => section.slug && section.title),
    };
    if (!payload.slug || !payload.name || !payload.sections.length) {
      return;
    }

    if (editingId) {
      await updateMutation.mutateAsync({ id: editingId, data: payload });
      setEditingId(null);
    } else {
      await createMutation.mutateAsync(payload);
      setEditingId(null);
    }
  };

  const handleSectionFieldChange = (index: number, key: keyof NoteTemplateSection, value: string) => {
    setFormState((prev) => ({
      ...prev,
      sections: prev.sections.map((section, sectionIndex) =>
        sectionIndex === index ? { ...section, [key]: value } : section,
      ),
    }));
  };

  const addSectionField = () => {
    setFormState((prev) => ({
      ...prev,
      sections: [...prev.sections, { ...emptySection }],
    }));
  };

  const removeSectionField = (index: number) => {
    setFormState((prev) => ({
      ...prev,
      sections: prev.sections.filter((_, sectionIndex) => sectionIndex !== index),
    }));
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold text-ink">Templates</h2>
        <button
          type="button"
          onClick={handleStartCreate}
          className="rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition hover:bg-accent/85"
        >
          New template
        </button>
      </div>

      <SectionCard title={editingId ? "Edit template" : "Create template"} subtitle="Define sectionized note structure">
        <form className="space-y-3" onSubmit={handleSubmit}>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="text-sm text-ink/70">
              Name
              <input
                className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                value={formState.name}
                onChange={(event) =>
                  setFormState((prev) => ({ ...prev, name: event.target.value }))
                }
                placeholder="Template name"
              />
            </label>
            <label className="text-sm text-ink/70">
              Slug
              <input
                className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                value={formState.slug}
                onChange={(event) =>
                  setFormState((prev) => ({
                    ...prev,
                    slug: normalizeSlug(event.target.value),
                  }))
                }
                placeholder="e.g. workday"
              />
            </label>
          </div>

          <label className="inline-flex items-center gap-2 text-sm text-ink/80">
            <input
              type="checkbox"
              checked={formState.is_default}
              onChange={(event) =>
                setFormState((prev) => ({ ...prev, is_default: event.target.checked }))
              }
            />
            Set as default template
          </label>

          <div className="space-y-3">
            <p className="text-sm text-ink/70">Sections</p>
            {formState.sections.map((section, index) => (
              <div key={`${section.slug}-${index}`} className="rounded-panel border border-ink/12 bg-white p-4">
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="text-sm text-ink/70">
                    Slug
                    <input
                      className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                      value={section.slug}
                      onChange={(event) =>
                        handleSectionFieldChange(index, "slug", normalizeSlug(event.target.value))
                      }
                      placeholder="morning-report"
                    />
                  </label>
                  <label className="text-sm text-ink/70">
                    Title
                    <input
                      className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                      value={section.title}
                      onChange={(event) =>
                        handleSectionFieldChange(index, "title", event.target.value)
                      }
                      placeholder="Morning Report"
                    />
                  </label>
                </div>
                <label className="mt-3 block text-sm text-ink/70">
                  Seed content
                  <textarea
                    className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                    rows={2}
                    value={section.content}
                    onChange={(event) =>
                      handleSectionFieldChange(index, "content", event.target.value)
                    }
                  />
                </label>

                <div className="mt-3">
                  <button
                    type="button"
                    onClick={() => removeSectionField(index)}
                    className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500"
                  >
                    Remove section
                  </button>
                </div>
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={addSectionField}
            className="text-sm text-ink/50 hover:text-ink/70"
          >
            + Add section
          </button>

          <div>
            <button
              type="submit"
              className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85"
              disabled={createMutation.isPending || updateMutation.isPending}
            >
              {editingId
                ? updateMutation.isPending
                  ? "Saving..."
                  : "Save template"
                : createMutation.isPending
                  ? "Creating..."
                  : "Create template"}
            </button>
          </div>
        </form>

        {createMutation.isError ? (
          <p className="mt-3 rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            {getErrorMessage(createMutation.error)}
          </p>
        ) : null}
        {updateMutation.isError ? (
          <p className="mt-3 rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            {getErrorMessage(updateMutation.error)}
          </p>
        ) : null}
      </SectionCard>

      {isLoading ? (
        <SectionCardSkeleton lines={6} />
      ) : error ? (
        <SectionCard title="Templates" subtitle="Failed to load templates">
          <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            Could not load templates.
          </p>
        </SectionCard>
      ) : (
        <SectionCard title="Saved templates" subtitle={`${data?.length ?? 0} templates`}>
          <div className="space-y-3">
            {!data || data.length === 0 ? (
              <p className="text-sm text-ink/70">No templates yet. Create your first template above.</p>
            ) : (
              data.map((template) => (
                <article key={template.id} className="rounded-panel border border-ink/15 bg-white p-4">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium text-ink">{template.name}</p>
                      <p className="mt-1 text-xs text-ink/50">/{template.slug}</p>
                    </div>
                    <div className="flex gap-2">
                      {template.is_default ? <StatusPill status="active" /> : null}
                      <button
                        type="button"
                        onClick={() => handleStartEdit(template)}
                        className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => deleteMutation.mutate(template.id)}
                        disabled={deleteMutation.isPending}
                        className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500 disabled:opacity-45"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                  <p className="mt-2 text-sm text-ink/70">Sections: {template.sections.length}</p>
                </article>
              ))
            )}
          </div>
        </SectionCard>
      )}

      {deleteMutation.isError ? (
        <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          {getErrorMessage(deleteMutation.error)}
        </p>
      ) : null}
    </div>
  );
}
