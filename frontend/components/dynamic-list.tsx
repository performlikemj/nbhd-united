const MAX_LIST_ITEMS = 10;

export function DynamicList({
  label,
  items,
  onChange,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
}) {
  return (
    <div>
      <p className="text-sm text-ink/70">{label}</p>
      <div className="mt-1 space-y-2">
        {items.map((item, index) => (
          <div key={index} className="flex gap-2">
            <input
              className="flex-1 rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={item}
              placeholder={`${label.slice(0, -1)}...`}
              onChange={(e) => {
                const next = [...items];
                next[index] = e.target.value;
                onChange(next);
              }}
            />
            {items.length > 1 ? (
              <button
                type="button"
                onClick={() => onChange(items.filter((_, i) => i !== index))}
                className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500"
              >
                Remove
              </button>
            ) : null}
          </div>
        ))}
      </div>
      {items.length < MAX_LIST_ITEMS ? (
        <button
          type="button"
          onClick={() => onChange([...items, ""])}
          className="mt-2 rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
        >
          Add {label.slice(0, -1).toLowerCase()}
        </button>
      ) : null}
    </div>
  );
}
