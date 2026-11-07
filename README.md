# Shopify API starter project

## Files
- app.py: Flask API
- requirements.txt: Python dependencies
- render.yaml: Render deployment configuration

## Render environment variables
Set these in Render Dashboard > Environment:
- SHOPIFY_STORE: your-store.myshopify.com
- SHOPIFY_ACCESS_TOKEN: Shopify Admin API access token
- API_SECRET: a long random secret

## Important
This is a starter integration, not a complete production payment gateway.
Only call `/api/payments/success` after verifying a genuine payment result
server-to-server with your payment provider. Never trust a browser redirect
or a client-supplied success flag. Use a persistent database for production;
Render's local filesystem may be ephemeral.

Protected endpoints require the `X-API-Key` header.
