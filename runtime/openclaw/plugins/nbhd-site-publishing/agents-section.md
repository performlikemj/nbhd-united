## Publishing to your website

You can add images to the user's portfolio website with the `publish_portfolio_image` tool.

When the user sends a photo and asks to add it to their portfolio, website, or gallery
(e.g. "add this to my site", "put this in my portfolio", "publish this photo"):

1. Make sure you have the image file they just sent.
2. If they haven't given a **title**, ask for a short one. A description/caption and tags
   are optional.
3. Call `publish_portfolio_image` with the image path, the title, and any description/tags.
4. On success, let them know it's published and will appear on their site within a minute.

Guardrails:
- The image goes live on the site immediately — there's no separate "publish" step to undo it,
  so only publish an image the user has **explicitly** asked you to publish. Never publish a
  photo they sent for some other reason.
- If the tool says publishing isn't configured for this account, don't retry — just tell them.
