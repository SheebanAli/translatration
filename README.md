# HashiCorp Vault & Urdu Transliteration API Setup Guide

This document provides a complete, step-by-step initialization and setup guide for **HashiCorp Vault** to work seamlessly with the Vault-JWT Urdu Transliteration API (`app.py`).

---

## 1. Architecture Overview

```
                      +------------------------------------------+
                      |               HashiCorp Vault            |
                      |  - AppRole Authentication                |
                      |  - OIDC Provider (Identity Secrets Engine)|
                      |    * Key: translation-key                |
                      |    * Roles: translation-service,         |
                      |             mobile-app                   |
                      +--------------------+---------------------+
                                           ^
                          1. AppRole Login | 2. Issue OIDC JWT
                                           | 3. Introspect JWT
                                           v
+------------------+         +-------------+------------+         +---------------------+
|                  | HTTP    |                          | HTTP    |                     |
|  Client / Web UI +-------->+   FastAPI Server         +-------->+  Translation Engine |
|                  | Request |   (app.py :8000)         |         |  - OpenAI gpt-4o-mini|
+------------------+         +-------------+------------+         |  - Local Qalb LLM   |
                                           ^                      +---------------------+
                                           | Outbound Tunnel
                             +-------------+------------+
                             |  OpenAI MCP Tunnel Client|
                             |  (tunnel-client daemon)  |
                             +--------------------------+
```

1. **Authentication**: `app.py` authenticates to Vault using **AppRole** (`VAULT_ROLE_ID` and `VAULT_SECRET_ID`).
2. **JWT Issuance**: `app.py` calls `GET /v1/identity/oidc/token/<aud>` where `aud` is either `translation-service` or `mobile-app`.
3. **JWT Verification**: `app.py` validates the token via `POST /v1/identity/oidc/introspect` to ensure the token is active, unexpired, and matches the expected audience.

---

## 2. Prerequisites & Environment

Set your Vault server connection parameters in your shell:

```bash
# Vault server address (adjust IP/port if different)
export VAULT_ADDR="https://172.24.71.152:8200"

# Path to Vault CA certificate (required if using self-signed TLS)
export VAULT_CACERT="/etc/pki/vault-ca.crt"

# If testing locally on plaintext HTTP:
# export VAULT_ADDR="http://127.0.0.1:8200"
```

---

## 3. Fresh Vault Initialization & Unsealing

> **Note:** If your Vault instance is already initialized and unsealed, skip to [Section 4](#4-configure-oidc-identity-provider).

### Step 3.1: Initialize the Vault Operator

```bash
vault operator init -key-shares=1 -key-threshold=1
```

> **IMPORTANT:** Vault outputs the **Unseal Key** and the **Initial Root Token**. Save both immediately in a secure location.

### Step 3.2: Unseal Vault

```bash
vault operator unseal <YOUR_UNSEAL_KEY>
```

### Step 3.3: Log in as Root

```bash
vault login <YOUR_INITIAL_ROOT_TOKEN>
```

---

## 4. Configure OIDC Identity Provider

The application issues and verifies OpenID Connect (OIDC) JWT tokens through Vault's Identity secrets engine.

### Step 4.1: Create the Named Signing Key

```bash
vault write identity/oidc/key/translation-key \
    algorithm="RS256" \
    allowed_client_ids="*" \
    verification_ttl="24h" \
    rotation_period="24h"
```

### Step 4.2: Create Role for `translation-service`

```bash
vault write identity/oidc/role/translation-service \
    key="translation-key" \
    client_id="translation-service" \
    ttl="1h"
```

### Step 4.3: Create Role for `mobile-app`

```bash
vault write identity/oidc/role/mobile-app \
    key="translation-key" \
    client_id="mobile-app" \
    ttl="1h"
```

---

## 5. Create the Access Policy (`translation-policy`)

Create the ACL policy that grants `app.py` permission to:
- Generate tokens for `translation-service` and `mobile-app` (`read` on `identity/oidc/token/*`)
- Introspect/validate tokens (`update` on `identity/oidc/introspect`)

```bash
cat << 'EOF' > /tmp/translation-policy.hcl
# Allow issuing OIDC JWT tokens for configured audiences
path "identity/oidc/token/translation-service" {
  capabilities = ["read"]
}

path "identity/oidc/token/mobile-app" {
  capabilities = ["read"]
}

# Allow introspecting and validating tokens
path "identity/oidc/introspect" {
  capabilities = ["update"]
}
EOF

# Write policy into Vault
vault policy write translation-policy /tmp/translation-policy.hcl

# Clean up temporary file
rm -f /tmp/translation-policy.hcl
```

---

## 6. Enable & Configure AppRole Authentication

### Step 6.1: Enable the AppRole Auth Method

```bash
vault auth enable approle
```
*(If already enabled, Vault will output `path is already in use at approle/` which is safe to ignore).*

### Step 6.2: Create the Application Role

```bash
vault write auth/approle/role/translation-role \
    token_policies="translation-policy" \
    token_ttl=24h \
    token_max_ttl=72h \
    secret_id_ttl=0 \
    token_num_uses=0
```

### Step 6.3: Set Role ID and Secret ID

Choose **Option A** to reuse the exact IDs currently configured in `.env`, or **Option B** to generate fresh random credentials:

#### Option A: Match Existing `.env` Credentials (Recommended)

Set the exact IDs already configured in your `.env` file:

```bash
# 1. Set custom Role ID
vault write auth/approle/role/translation-role/role-id \
    role_id="d94a4b18-8117-3da2-c3dc-58fcb2828451"

# 2. Set custom Secret ID
vault write auth/approle/role/translation-role/custom-secret-id \
    secret_id="f8090830-efac-6f36-4e00-e31186c423c2"
```

#### Option B: Generate New Credentials

If you want Vault to generate new random credentials:

```bash
# Read generated Role ID
NEW_ROLE_ID=$(vault read -field=role_id auth/approle/role/translation-role/role-id)

# Generate new Secret ID
NEW_SECRET_ID=$(vault write -f -field=secret_id auth/approle/role/translation-role/secret-id)

echo "VAULT_ROLE_ID=$NEW_ROLE_ID"
echo "VAULT_SECRET_ID=$NEW_SECRET_ID"
```
*(Then update `VAULT_ROLE_ID` and `VAULT_SECRET_ID` in `.env` with these printed values).*

---

## 7. Generate Service Token for `VAULT_TOKEN` (Optional)

`app.py` accepts an initial `VAULT_TOKEN` in `.env` for direct calls, and automatically uses AppRole to re-login if the token expires:

```bash
vault token create \
    -policy="translation-policy" \
    -ttl="720h" \
    -field="token"
```

Copy the generated token and paste it as `VAULT_TOKEN` in your `.env`.

---

## 8. Verification & Sanity Checks

Run these commands to verify that Vault is fully functional and ready for `app.py`:

### Test 1: Test AppRole Authentication
```bash
vault write auth/approle/login \
    role_id="d94a4b18-8117-3da2-c3dc-58fcb2828451" \
    secret_id="f8090830-efac-6f36-4e00-e31186c423c2"
```
*Expected: Returns a client token with policy `translation-policy`.*

### Test 2: Test OIDC Token Generation
```bash
TEST_TOKEN=$(vault read -field=token identity/oidc/token/translation-service)
echo "JWT Token: $TEST_TOKEN"
```
*Expected: Prints a valid base64-encoded JWT token (`eyJ...`).*

### Test 3: Test OIDC Token Introspection
```bash
vault write identity/oidc/introspect \
    token="$TEST_TOKEN" \
    client_id="translation-service"
```
*Expected output contains:*
```json
{
  "active": true,
  "client_id": "translation-service",
  "token_type": "Bearer"
}
```

---

## 9. Environment File Reference (`.env`)

Ensure your `/usr/newapi(1)/.env` file matches your configuration:

```ini
# OpenAI API Key (used for OpenAI translation backend and MCP tunnel control plane)
OPENAI_API_KEY=sk-proj-...

# HashiCorp Vault Connection Settings
VAULT_ADDR=https://172.24.71.152:8200
VAULT_CACERT=/etc/pki/vault-ca.crt
VAULT_TOKEN=hvs....
VAULT_ROLE_ID=d94a4b18-8117-3da2-c3dc-58fcb2828451
VAULT_SECRET_ID=f8090830-efac-6f36-4e00-e31186c423c2

# OpenAI Secure Tunnel Configuration
OPENAI_TUNNEL_ID="tunnel_6ad8adbeb0608191944746b4cac4cd63"
CONTROL_PLANE_TUNNEL_ID="tunnel_6ad8adbeb0608191944746b4cac4cd63"

# Optional Server Settings
DEBUG_PRINT=1
IAT_SKEW_SECONDS=30
TRANSLATION_BACKEND=openai
```

---

## 10. Running the Application

Once Vault is configured and unsealed, launch the service:

```bash
# 1. Start the FastAPI application
python3 app.py

# 2. Or run via Uvicorn directly
uvicorn app:app --host 0.0.0.0 --port 8000
```

- **Interactive Web Interface**: [`http://localhost:8000/`](http://localhost:8000/)
- **API Swagger Documentation**: [`http://localhost:8000/docs`](http://localhost:8000/docs)
- **Health Check**: [`http://localhost:8000/health`](http://localhost:8000/health)
- **OpenAI Tunnel UI**: [`http://localhost:8080/ui`](http://localhost:8080/ui)

