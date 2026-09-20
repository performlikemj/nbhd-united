"use client";

import { useId, useState } from "react";

import {
  useAchieveGoalMutation,
  useCompleteTaskMutation,
  useCreateGoalTaskMutation,
  useReopenTaskMutation,
  useUpdateGoalNotesMutation,
} from "@/lib/queries";
import type { HorizonsGoal, HorizonsGoalTask } from "@/lib/types";

const quietControl =
  "min-h-[44px] rounded px-2 text-xs text-ink-muted hover:text-accent focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2 disabled:opacity-50 disabled:cursor-not-allowed";
const quietInput =
  "w-full rounded border border-border bg-transparent px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2 disabled:opacity-50";

function GoalStep({ task, disabled }: { task: HorizonsGoalTask; disabled: boolean }) {
  const complete = useCompleteTaskMutation();
  const reopen = useReopenTaskMutation();
  const done = task.status === "done";
  const pending = complete.isPending || reopen.isPending;

  return (
    <li>
      <label className="flex min-h-[44px] cursor-pointer items-start gap-3 py-2 text-sm leading-relaxed text-ink">
        <input
          type="checkbox"
          checked={done}
          disabled={disabled || pending}
          onChange={() => {
            complete.reset();
            reopen.reset();
            (done ? reopen : complete).mutate(task.id);
          }}
          className="mt-1 h-4 w-4 shrink-0 accent-accent focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2 disabled:opacity-50"
        />
        <span className={`min-w-0 break-words ${done ? "text-ink-faint line-through" : ""}`}>
          {task.title}
        </span>
        {pending ? <span role="status" className="ml-auto text-xs text-ink-faint">Saving…</span> : null}
      </label>
      {complete.isError || reopen.isError ? (
        <p role="alert" className="text-xs text-ink-muted">Couldn’t update this step. Please try again.</p>
      ) : null}
    </li>
  );
}

export function GoalWorkCard({
  goal,
  displayTitle,
  updatedLabel,
}: {
  goal: HorizonsGoal;
  displayTitle: string;
  updatedLabel: string;
}) {
  const id = useId();
  const [stepTitle, setStepTitle] = useState("");
  // Null follows fresh server notes; a draft survives unrelated query refetches.
  const [notesDraft, setNotesDraft] = useState<string | null>(null);
  const notes = notesDraft ?? goal.markdown ?? "";
  const notesChanged = notes !== (goal.markdown ?? "");
  const addStep = useCreateGoalTaskMutation();
  const saveNotes = useUpdateGoalNotesMutation();
  const achieve = useAchieveGoalMutation();

  return (
    <article className="group block glass-card-horizons min-w-0 border-l-2 border-l-accent p-5 md:p-6" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="font-headline font-semibold text-lg leading-tight text-ink">
        {displayTitle}
      </h3>

      {(goal.tasks ?? []).length > 0 ? (
        <ul className="mt-3" aria-label={`Steps for ${displayTitle}`}>
          {goal.tasks?.map((task) => <GoalStep key={task.id} task={task} disabled={achieve.isPending} />)}
        </ul>
      ) : null}

      <form
        className="mt-3 flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const title = stepTitle.trim();
          if (!title || addStep.isPending || achieve.isPending) return;
          addStep.mutate({ title, parent_goal_id: goal.id }, { onSuccess: () => setStepTitle("") });
        }}
      >
        <label htmlFor={`${id}-step`} className="sr-only">Add a step to {displayTitle}</label>
        <input
          id={`${id}-step`}
          type="text"
          value={stepTitle}
          onChange={(event) => setStepTitle(event.target.value)}
          maxLength={256}
          placeholder="Add a step…"
          disabled={addStep.isPending || achieve.isPending}
          className={`${quietInput} min-w-0`}
        />
        <button type="submit" className={quietControl} disabled={!stepTitle.trim() || addStep.isPending || achieve.isPending}>
          {addStep.isPending ? "Adding…" : "Add"}
        </button>
      </form>
      {addStep.isError ? <p role="alert" className="mt-1 text-xs text-ink-muted">Couldn’t add this step. Please try again.</p> : null}

      <form
        className="mt-5"
        onSubmit={(event) => {
          event.preventDefault();
          if (!notesChanged || saveNotes.isPending || achieve.isPending) return;
          saveNotes.mutate({ id: goal.id, description: notes }, { onSuccess: () => setNotesDraft(null) });
        }}
      >
        <label htmlFor={`${id}-notes`} className="mb-2 block text-xs text-ink-muted">Notes</label>
        <textarea
          id={`${id}-notes`}
          value={notes}
          onChange={(event) => setNotesDraft(event.target.value)}
          rows={3}
          placeholder="A little context, a next thought…"
          disabled={saveNotes.isPending || achieve.isPending}
          className={`${quietInput} resize-y leading-relaxed`}
        />
        <div className="flex items-center justify-between gap-2">
          <span role="status" className="text-xs text-ink-faint">
            {saveNotes.isPending ? "Saving…" : notesChanged ? "Unsaved notes" : saveNotes.isSuccess ? "Notes saved" : ""}
          </span>
          <button type="submit" className={quietControl} disabled={!notesChanged || saveNotes.isPending || achieve.isPending}>Save notes</button>
        </div>
        {saveNotes.isError ? <p role="alert" className="text-xs text-ink-muted">Couldn’t save your notes. Your draft is still here; please try again.</p> : null}
      </form>

      <div className="mt-4 flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">{updatedLabel}</span>
        <button
          type="button"
          className={quietControl}
          disabled={achieve.isPending || saveNotes.isPending || addStep.isPending || notesChanged || !!stepTitle.trim()}
          onClick={() => achieve.mutate(goal.id)}
          aria-label={`Mark ${displayTitle} done`}
        >
          {achieve.isPending ? "Finishing…" : "Mark done"}
        </button>
      </div>
      {achieve.isError ? <p role="alert" className="text-xs text-ink-muted">Couldn’t mark this goal done. Please try again.</p> : null}
    </article>
  );
}
