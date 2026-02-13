#!/bin/zsh
# Set missing production env vars (Stripe + Composio) on Azure Container App
# Usage: ./scripts/set-prod-env-vars.sh

set -euo pipefail

echo "=== Stripe Configuration ==="
echo -n "STRIPE_TEST_SECRET_KEY (sk_test_...): "; read -rs STRIPE_TEST_SECRET_KEY; echo
echo -n "DJSTRIPE_WEBHOOK_SECRET (whsec_...): "; read -rs DJSTRIPE_WEBHOOK_SECRET; echo
echo -n "STRIPE_PRICE_BASIC (price_...): "; read -r STRIPE_PRICE_BASIC
echo -n "STRIPE_PRICE_PLUS (price_...): "; read -r STRIPE_PRICE_PLUS

echo ""
echo "=== Preview Gate ==="
echo -n "PREVIEW_ACCESS_KEY (UUID, leave empty to disable): "; read -r PREVIEW_ACCESS_KEY

echo ""
echo "=== Composio Configuration ==="
echo -n "COMPOSIO_API_KEY: "; read -rs COMPOSIO_API_KEY; echo
echo -n "COMPOSIO_GMAIL_AUTH_CONFIG_ID (ac_...): "; read -r COMPOSIO_GMAIL_AUTH_CONFIG_ID
echo -n "COMPOSIO_GCAL_AUTH_CONFIG_ID (ac_...): "; read -r COMPOSIO_GCAL_AUTH_CONFIG_ID

echo ""
echo "Setting env vars on nbhd-django-westus2..."
az containerapp update \
  --name nbhd-django-westus2 \
  --resource-group rg-nbhd-prod \
  --set-env-vars \
    FRONTEND_URL=https://neighborhoodunited.org \
    PREVIEW_ACCESS_KEY="$PREVIEW_ACCESS_KEY" \
    STRIPE_TEST_SECRET_KEY="$STRIPE_TEST_SECRET_KEY" \
    STRIPE_LIVE_MODE=False \
    DJSTRIPE_WEBHOOK_SECRET="$DJSTRIPE_WEBHOOK_SECRET" \
    STRIPE_PRICE_BASIC="$STRIPE_PRICE_BASIC" \
    STRIPE_PRICE_PLUS="$STRIPE_PRICE_PLUS" \
    COMPOSIO_API_KEY="$COMPOSIO_API_KEY" \
    COMPOSIO_GMAIL_AUTH_CONFIG_ID="$COMPOSIO_GMAIL_AUTH_CONFIG_ID" \
    COMPOSIO_GCAL_AUTH_CONFIG_ID="$COMPOSIO_GCAL_AUTH_CONFIG_ID"

echo "Done. Container will restart with new config."
