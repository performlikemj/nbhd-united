"use client";

import clsx from "clsx";

function getStrength(password: string): number {
  let score = 0;
  if (password.length >= 8) score++;
  if (/[A-Z]/.test(password)) score++;
  if (/[0-9]/.test(password)) score++;
  if (/[^A-Za-z0-9]/.test(password)) score++;
  return score;
}

const LABELS = ["", "Weak", "Fair", "Good", "Strong"] as const;
const COLORS = ["", "bg-rose-500", "bg-amber-400", "bg-teal-400", "bg-teal-400"] as const;
const LABEL_COLORS = ["", "text-rose-400", "text-amber-400", "text-teal-400", "text-teal-400"] as const;

export function PasswordStrengthMeter({ password }: { password: string }) {
  const strength = getStrength(password);

  if (!password) return null;

  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex gap-1">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className={clsx(
              "h-1 flex-1 rounded-full transition-colors duration-300",
              i <= strength ? COLORS[strength] : "bg-white/10",
            )}
          />
        ))}
      </div>
      {strength > 0 && (
        <p className={clsx("text-xs font-medium", LABEL_COLORS[strength])}>
          {LABELS[strength]}
        </p>
      )}
    </div>
  );
}
