/**
 * NBHD Site Publishing Plugin
 *
 * Lets a subscriber's assistant publish a portfolio image to the subscriber's
 * own website by writing directly to their Azure Blob Storage + Cosmos DB,
 * authenticating as the tenant's user-assigned managed identity (no stored
 * keys).
 *
 * Per-tenant and inert by default: the tool no-ops unless the tenant's
 * `site_config` (injected via api.pluginConfig by config_generator when
 * `site_publishing_enabled` is set) supplies the target Cosmos/Blob
 * coordinates. config_generator only loads this plugin for flagged tenants,
 * so it never even registers for anyone else.
 *
 * Auth: DefaultAzureCredential bound to the container's AZURE_CLIENT_ID
 * (the user-assigned identity mi-nbhd-<tenant>). Requires data-plane RBAC on
 * the target resources:
 *   - Storage Blob Data Contributor on the storage account
 *   - Cosmos DB Built-in Data Contributor on the Cosmos account
 * Both are additive to (and independent of) any key-based access the site
 * already uses.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const { wrapTool } = require("../../tool-logger.js");
const wrap = (def) => wrapTool(def, { plugin: "nbhd-site-publishing" });

// Lazy-require the Azure SDKs so a missing dependency degrades to a clear
// tool-level error instead of crashing plugin load for the whole agent.
function loadAzure() {
  const { DefaultAzureCredential } = require("@azure/identity");
  const { BlobServiceClient } = require("@azure/storage-blob");
  const { CosmosClient } = require("@azure/cosmos");
  return { DefaultAzureCredential, BlobServiceClient, CosmosClient };
}

const CONTENT_TYPES = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".webp": "image/webp",
  ".gif": "image/gif",
};

module.exports = function register(api) {
  const cfg = (api.pluginConfig && typeof api.pluginConfig === "object") ? api.pluginConfig : {};

  api.registerTool(wrap({
    name: "publish_portfolio_image",
    description:
      "Publish ONE image to the subscriber's own portfolio website. Call this whenever the user " +
      "sends one or more images and asks to add, publish, or update them on their site, portfolio, " +
      "website, or gallery — exactly once per image (N images = N calls), passing `image_path` and " +
      "a `title`. Never tell the user an image is live, added, or published unless this call " +
      "returned success this turn; do not claim a publish you did not actually make.",
    parameters: {
      type: "object",
      required: ["image_path", "title"],
      properties: {
        image_path: {
          type: "string",
          description: "Absolute path to the image file the user provided (e.g. the photo they just sent).",
        },
        title: {
          type: "string",
          description: "Short display title for the portfolio piece.",
        },
        description: {
          type: "string",
          description: "Optional caption / description shown with the image.",
        },
        tags: {
          type: "array",
          items: { type: "string" },
          description: "Optional list of tags.",
        },
        featured: {
          type: "boolean",
          description: "Optional. Set true to feature this image on the homepage.",
        },
      },
    },
    async execute({ image_path, title, description, tags, featured }) {
      // Self-gate: inert unless this tenant has site_config injected.
      if (!cfg.cosmosEndpoint || !cfg.cosmosDatabase || !cfg.cosmosContainer || !cfg.blobAccount || !cfg.blobContainer) {
        return { content: [{ type: "text", text: "Site publishing isn't configured for this account." }] };
      }
      if (!image_path || !fs.existsSync(image_path)) {
        return { content: [{ type: "text", text: `Error: no image file found at ${image_path}.` }] };
      }
      if (!title || !title.trim()) {
        return { content: [{ type: "text", text: "Error: a title is required." }] };
      }

      let Azure;
      try {
        Azure = loadAzure();
      } catch {
        return { content: [{ type: "text", text: "Site publishing is temporarily unavailable (dependencies missing)." }] };
      }
      const { DefaultAzureCredential, BlobServiceClient, CosmosClient } = Azure;

      try {
        const buffer = fs.readFileSync(image_path);
        const ext = (path.extname(image_path) || ".jpg").toLowerCase();
        const contentType = CONTENT_TYPES[ext] || "application/octet-stream";
        const id = crypto.randomUUID();
        const fileName = path.basename(image_path);
        const prefix = String(cfg.blobPathPrefix || "projects/portfolio").replace(/\/+$/, "");
        const blobName = `${prefix}/${id}${ext}`;
        const now = new Date().toISOString();

        // Authenticate as the tenant's user-assigned managed identity.
        const credential = new DefaultAzureCredential({
          managedIdentityClientId: process.env.AZURE_CLIENT_ID,
        });

        // 1. Upload the image to Blob Storage.
        const blobService = new BlobServiceClient(
          `https://${cfg.blobAccount}.blob.core.windows.net`,
          credential,
        );
        await blobService
          .getContainerClient(cfg.blobContainer)
          .getBlockBlobClient(blobName)
          .uploadData(buffer, { blobHTTPHeaders: { blobContentType: contentType } });

        // 2. Write the portfolio metadata doc the site reads. Schema mirrors
        //    api/shared/models.js PortfolioImage — crucially type:'image' and
        //    isActive:true, which the site's getImages()/getFeaturedImages()
        //    filter on. (The site's own uploaders omit these, so their images
        //    only surface on category pages — this writes complete docs.)
        const cosmos = new CosmosClient({ endpoint: cfg.cosmosEndpoint, aadCredentials: credential });
        const doc = {
          id,
          type: "image", // partition key (/type) + the site's primary filter
          title: title.trim(),
          description: (description || "").trim(),
          categoryId: null,
          blobName,
          thumbnailBlobName: blobName, // no separate thumbnail (matches current site behavior)
          fileName,
          contentType,
          size: buffer.length,
          width: 0, // dimensions not computed client-side; site tolerates 0
          height: 0,
          order: 0,
          tags: Array.isArray(tags) ? tags.filter((t) => typeof t === "string") : [],
          isActive: true,
          isFeatured: Boolean(featured),
          createdAt: now,
          updatedAt: now,
        };
        await cosmos.database(cfg.cosmosDatabase).container(cfg.cosmosContainer).items.create(doc);

        return {
          content: [{
            type: "text",
            text: `Published "${doc.title}" to the portfolio (id ${id}). It should appear on the site within a minute.`,
          }],
        };
      } catch (err) {
        const msg = (err && err.message ? err.message : String(err)).replace(/\s+/g, " ").slice(0, 300);
        return { content: [{ type: "text", text: `Couldn't publish the image: ${msg}` }] };
      }
    },
  }));
};
