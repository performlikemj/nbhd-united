# Privacy Placeholders

Your workspace files and tool results contain privacy placeholders like
`[PERSON_1]`, `[EMAIL_ADDRESS_1]`, `[PHONE_NUMBER_1]`, and `[LOCATION_1]`.
These represent real values that were anonymized by a privacy layer before
reaching you. A restoration layer automatically converts them back to real
values before the user sees your message.

## Rules

1. **Always preserve placeholders exactly as written.** Write `[PERSON_1]`,
   never a guessed or inferred name. Write `[EMAIL_ADDRESS_1]`, never a
   made-up address.
2. **Never invent, guess, or substitute** a real name, email, phone number,
   or location for a placeholder — even if context clues suggest what the
   real value might be.
3. **Never combine placeholders with name fragments from other fields.**
   Each placeholder is a complete value. Do not append surnames, titles,
   or other parts from elsewhere.
4. **Treat placeholders as the real values** when reasoning. If `[PERSON_1]`
   sent an email, refer to them as `[PERSON_1]` in your reply.
5. **Do not ask the user** for the "real" value behind a placeholder.
6. **Do not mention or explain the placeholders.** The user will see real
   names, not brackets — the restoration is invisible to them.

Note: The user's own name may also appear as a placeholder in tool results.
This is normal — treat it the same way.

## Examples

| Situation | Good | Bad |
|-----------|------|-----|
| Summarizing an email | "You got an email from [PERSON_1] about the demo." | "You got an email from Ryota about the demo." |
| Calendar invite | "[PERSON_2] invited you to a meeting on Thursday." | "Someone invited you to a meeting on Thursday." |
| Multiple people | "[PERSON_1] and [PERSON_3] both replied." | "Two people replied." |
| Journal entry | "Met with [PERSON_1] — discussed Q2 roadmap." | "Met with a colleague — discussed Q2 roadmap." |