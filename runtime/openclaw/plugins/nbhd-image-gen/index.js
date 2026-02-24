/**
 * NBHD Image Generation Plugin
 *
 * Generates images via OpenAI Images API (gpt-image-1).
 * Rate-limited per tier to control costs.
 *
 * Tier limits (per 24h rolling window):
 *   starter: 3 images/day
 *   premium: 10 images/day
 *   byok:    25 images/day
 */

const fs = require("fs");
const path = require("path");
const https = require("https");

// Rate limits per tier (images per 24h)
const TIER_LIMITS = {
  starter: 3,
  premium: 10,
  byok: 25,
};

const VALID_SIZES = ["1024x1024", "1536x1024", "1024x1536"];
const DEFAULT_SIZE = "1024x1024";
const DEFAULT_MODEL = "gpt-image-1";
const RATE_LIMIT_FILE = "/tmp/nbhd-image-gen-usage.json";

// --- Rate limiting (file-based, survives tool calls but not container restarts) ---

function loadUsage() {
  try {
    if (fs.existsSync(RATE_LIMIT_FILE)) {
      return JSON.parse(fs.readFileSync(RATE_LIMIT_FILE, "utf8"));
    }
  } catch { /* ignore corrupt file */ }
  return { timestamps: [] };
}

function saveUsage(usage) {
  try {
    fs.writeFileSync(RATE_LIMIT_FILE, JSON.stringify(usage), "utf8");
  } catch { /* non-critical */ }
}

function countRecentImages() {
  const usage = loadUsage();
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  // Prune old entries
  usage.timestamps = (usage.timestamps || []).filter((t) => t > cutoff);
  saveUsage(usage);
  return usage.timestamps.length;
}

function recordImageGenerated() {
  const usage = loadUsage();
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  usage.timestamps = (usage.timestamps || []).filter((t) => t > cutoff);
  usage.timestamps.push(Date.now());
  saveUsage(usage);
}

// --- OpenAI API call ---

function callOpenAIImagesAPI(apiKey, prompt, size, model) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      model,
      prompt,
      n: 1,
      size,
    });

    const req = https.request(
      {
        hostname: "api.openai.com",
        path: "/v1/images/generations",
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
        timeout: 60000,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          if (res.statusCode >= 400) {
            try {
              const err = JSON.parse(data);
              reject(new Error(err.error?.message || `HTTP ${res.statusCode}`));
            } catch {
              reject(new Error(`HTTP ${res.statusCode}: ${data.slice(0, 200)}`));
            }
            return;
          }
          try {
            resolve(JSON.parse(data));
          } catch (e) {
            reject(new Error("Invalid JSON response from OpenAI"));
          }
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("Request timed out (60s)"));
    });
    req.write(body);
    req.end();
  });
}

// --- Save image to workspace ---

function saveBase64Image(b64Data, outputDir) {
  const timestamp = Date.now();
  const filename = `generated-${timestamp}.png`;
  const filepath = path.join(outputDir, filename);

  // Ensure directory exists
  fs.mkdirSync(outputDir, { recursive: true });
  fs.writeFileSync(filepath, Buffer.from(b64Data, "base64"));

  return filepath;
}

// --- Plugin entry point ---

module.exports = function register(api) {
  const pluginConfig = (api.pluginConfig && typeof api.pluginConfig === "object") ? api.pluginConfig : {};
  const tier = (pluginConfig.tier || process.env.NBHD_TIER || "starter").toLowerCase();
  const limit = TIER_LIMITS[tier] || TIER_LIMITS.starter;

  api.registerTool({
    name: "nbhd_generate_image",
    description: `Generate an image from a text prompt using AI (OpenAI gpt-image-1). Rate limited to ${limit} images per day. The generated image will be automatically sent to the user in Telegram.`,
    parameters: {
      type: "object",
      required: ["prompt"],
      properties: {
        prompt: {
          type: "string",
          description: "Detailed description of the image to generate. Be specific about style, composition, colors, etc.",
        },
        size: {
          type: "string",
          enum: VALID_SIZES,
          description: "Image size. 1024x1024 (square), 1536x1024 (landscape), 1024x1536 (portrait). Default: 1024x1024.",
        },
      },
    },
    async execute({ prompt, size }) {
      // Validate inputs
      if (!prompt || typeof prompt !== "string" || !prompt.trim()) {
        return { content: [{ type: "text", text: "Error: prompt is required." }] };
      }

      const resolvedSize = VALID_SIZES.includes(size) ? size : DEFAULT_SIZE;

      // Check rate limit
      const used = countRecentImages();
      if (used >= limit) {
        const remaining = "less than 24 hours";
        return {
          content: [{
            type: "text",
            text: `Rate limit reached: you've used ${used}/${limit} image generations today. Try again in ${remaining}.`,
          }],
        };
      }

      // Get API key
      const apiKey = (process.env.OPENAI_API_KEY || "").trim();
      if (!apiKey) {
        return { content: [{ type: "text", text: "Image generation is temporarily unavailable." }] };
      }

      try {
        const result = await callOpenAIImagesAPI(apiKey, prompt.trim(), resolvedSize, DEFAULT_MODEL);

        if (!result.data || !result.data[0]) {
          return { content: [{ type: "text", text: "Image generation returned no results. Try a different prompt." }] };
        }

        const imageData = result.data[0];

        // gpt-image-1 returns b64_json by default
        if (imageData.b64_json) {
          const outputDir = path.join(
            process.env.HOME || "/home/node",
            ".openclaw",
            "workspace",
            "media",
            "generated",
          );
          const filepath = saveBase64Image(imageData.b64_json, outputDir);
          recordImageGenerated();

          return {
            content: [{
              type: "text",
              text: `Image generated successfully!\nMEDIA:${filepath}`,
            }],
          };
        }

        // Fallback: URL-based response (dall-e-3 style)
        if (imageData.url) {
          recordImageGenerated();
          return {
            content: [{
              type: "text",
              text: `Image generated successfully!\nMEDIA:${imageData.url}`,
            }],
          };
        }

        return { content: [{ type: "text", text: "Unexpected response format from image API." }] };

      } catch (err) {
        const msg = err.message || "Unknown error";
        if (msg.includes("content_policy") || msg.includes("safety")) {
          return { content: [{ type: "text", text: "That prompt was rejected by the safety filter. Try rephrasing." }] };
        }
        return { content: [{ type: "text", text: `Image generation failed: ${msg}` }] };
      }
    },
  });
};
