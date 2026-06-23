# Putting a Price on an Endpoint: x402 on FastAPI, in Detail

*The engineering companion to ["The 402 Moment."](./the-402-moment-agentic-commerce.md) This one is for the people who actually have to ship it.*

---

## What we're building

We're going to take an ordinary FastAPI route and make it **payable by a machine**: an HTTP client (likely an AI agent) calls it, gets told it costs money, pays in USDC, and gets the content — all inside the normal request/response cycle, with no account, no API key, and no human.

The whole thing rests on three primitives:

1. **`402 Payment Required`** — the HTTP status code that carries the price.
2. **EIP-3009 `transferWithAuthorization`** — a signed, gasless payment authorization the client produces.
3. **A facilitator** — a service that verifies and settles that authorization on-chain so your server never touches a private key.

Let's walk the protocol, then the implementation, then the sharp edges.

---

## The protocol, request by request

### Step 1 — The unpaid request

The client makes a perfectly normal request:

```http
GET /api/v1/items/premium/sample HTTP/1.1
Host: api.example.com
```

### Step 2 — The 402 challenge

Because there's no payment attached, the server responds with `402` and a JSON body describing exactly what payment it will accept. This is the heart of the protocol — a machine-readable price tag:

```json
{
  "x402Version": 1,
  "error": "X-PAYMENT header is required",
  "accepts": [
    {
      "scheme": "exact",
      "network": "base",
      "maxAmountRequired": "10000",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "payTo": "0xYourReceivingWallet",
      "resource": "https://api.example.com/api/v1/items/premium/sample",
      "description": "",
      "mimeType": "application/json",
      "maxTimeoutSeconds": 300,
      "extra": { "name": "USDC", "version": "2" }
    }
  ]
}
```

Field by field, because each one matters:

- **`scheme: "exact"`** — pay an exact amount (as opposed to, say, a metered or upto scheme). This is the one in wide use.
- **`network`** — `base` is mainnet; `base-sepolia` is the testnet. These map to EVM chain IDs under the hood.
- **`maxAmountRequired`** — the price in **atomic units, as a string**. USDC has 6 decimals, so `"10000"` is $0.01. Strings, not numbers, because these are token amounts and you never want a float near money.
- **`asset`** — the ERC-20 contract address of the token. The value above is USDC on Base mainnet.
- **`payTo`** — your receiving wallet. This is the only piece of "crypto" your business actually needs to own.
- **`resource`** — the canonical URL being paid for. It's signed over, so it binds the payment to this exact endpoint.
- **`maxTimeoutSeconds`** — how long the client has to deliver payment before the quote goes stale.
- **`extra`** — scheme-specific data. For the EVM `exact` scheme it carries the EIP-712 domain (`name`, `version`) of the token, which the client needs to construct a valid signature.

`accepts` is an **array** on purpose: a server can offer the same resource on multiple networks or in multiple tokens and let the client pick.

### Step 3 — The client pays

The client picks one entry from `accepts` and constructs a payment. For the `exact` EVM scheme, "constructing a payment" means signing an **EIP-3009 `transferWithAuthorization`** message — an EIP-712 typed-data signature that authorizes a transfer of an exact amount, without the signer needing to send a transaction or pay gas themselves:

```json
{
  "x402Version": 1,
  "scheme": "exact",
  "network": "base",
  "payload": {
    "signature": "0x2d6a7588...1c",
    "authorization": {
      "from": "0xClientWallet",
      "to": "0xYourReceivingWallet",
      "value": "10000",
      "validAfter": "1718900000",
      "validBefore": "1718900300",
      "nonce": "0xf3746613c2d920b5fdabc0856f2aeb2d4f88ee6037b8cc5d04a71a4462f13480"
    }
  }
}
```

The `authorization` object is the economically meaningful part:

- **`from` / `to` / `value`** — who pays whom, how much (atomic units again).
- **`validAfter` / `validBefore`** — a time window. Outside it, the authorization is worthless. This bounds replay risk.
- **`nonce`** — a random 32-byte value that makes each authorization single-use.
- **`signature`** — the ECDSA signature over the EIP-712 digest of all of the above, plus the token's domain.

Crucially, this is **gasless from the payer's perspective**: they sign, they don't transact. Someone else submits it on-chain. That "someone else" is the facilitator.

The client base64-encodes that JSON and retries the original request with it attached:

```http
GET /api/v1/items/premium/sample HTTP/1.1
Host: api.example.com
X-PAYMENT: eyJ4NDAyVmVyc2lvbiI6MSwic2NoZW1lIjoiZXhhY3QiLC4uLn0=
```

### Step 4 — The server verifies and settles

Your server doesn't validate signatures or talk to a blockchain. It forwards the decoded payment plus the requirements it issued to a **facilitator**, which exposes two endpoints.

**`POST /verify`** — a read-only check: is this signature valid, does the payer have the funds, is the window open?

```json
// request
{ "x402Version": 1, "paymentPayload": { /* decoded X-PAYMENT */ },
  "paymentRequirements": { /* the accepts[] entry the client chose */ } }

// response
{ "isValid": true, "payer": "0xClientWallet" }
```

**`POST /settle`** — the state-changing call: submit the `transferWithAuthorization` on-chain and wait for confirmation.

```json
// response
{ "success": true, "payer": "0xClientWallet",
  "transaction": "0x1234...", "network": "base" }
```

If verification fails, the server returns `402` again. If settlement succeeds, it returns the content with a receipt header:

```http
HTTP/1.1 200 OK
X-PAYMENT-RESPONSE: eyJzdWNjZXNzIjp0cnVlLCJ0cmFuc2FjdGlvbiI6IjB4MTIzNC4uLiJ9

{ "id": "...", "title": "Premium sample item", ... }
```

That `X-PAYMENT-RESPONSE` header is the cryptographic receipt — a base64 settlement object with the on-chain transaction hash. The client can verify it independently.

The full handshake:

```
Client                         Resource Server                  Facilitator
  | --- GET /resource --------->  |                                 |
  | <-- 402 + accepts[] --------  |                                 |
  |                               |                                 |
  | --- GET + X-PAYMENT -------->  |                                |
  |                               | --- POST /verify -------------> |
  |                               | <-- isValid: true ------------- |
  |                               | --- POST /settle -------------> |
  |                               | <-- success + tx hash --------- |
  | <-- 200 + X-PAYMENT-RESPONSE  |                                 |
```

Note what the resource server never does: hold a key, sign anything, run a node, or touch the chain. That separation is the whole design.

---

## The implementation on FastAPI

We used [`fastapi-x402`](https://github.com/jordo1138/fastapi-x402), which models the protocol as **ASGI middleware plus a per-route decorator**. The middleware intercepts requests; the decorator marks which routes are paid and at what price.

### Configuration

We surfaced everything through the app's existing `pydantic-settings` config so it's environment-driven and validated at boot ([`backend/app/core/config.py`](../backend/app/core/config.py)):

```python
# x402 payments
X402_ENABLED: bool = False
X402_PAY_TO: str = ""
X402_NETWORK: Literal[
    "base-sepolia", "base", "avalanche-fuji", "avalanche", "iotex"
] = "base-sepolia"
X402_FACILITATOR_URL: str | None = None
X402_PREMIUM_PRICE: str = "$0.01"

@model_validator(mode="after")
def _enforce_x402_mainnet_pay_to(self) -> Self:
    # Guard against settling real funds with no/misconfigured recipient.
    if self.X402_ENABLED and self.X402_NETWORK == "base" and not self.X402_PAY_TO:
        raise ValueError(
            "X402_PAY_TO must be set ... when X402_ENABLED is true and "
            "X402_NETWORK is 'base' (mainnet)."
        )
    return self
```

Two deliberate choices here. First, **`X402_ENABLED` defaults to `False`** — the entire feature is dark until you opt in, so it can't break existing behavior. Second, the **validator refuses to boot on mainnet with an empty pay-to address**. Money-moving config deserves a fail-fast guard; a typo here is a transaction you can't claw back.

### Wiring the middleware

In [`backend/app/main.py`](../backend/app/main.py), the middleware is installed only when enabled:

```python
if settings.X402_ENABLED:
    init_x402(
        app,
        pay_to=settings.X402_PAY_TO,
        network=settings.X402_NETWORK,
        facilitator_url=settings.X402_FACILITATOR_URL,
    )
```

We also extended the existing CORS middleware to expose the receipt header, otherwise browser-based clients can't read it:

```python
app.add_middleware(
    CORSMiddleware,
    ...,
    expose_headers=["X-PAYMENT-RESPONSE"],
)
```

By default, browsers only expose a handful of "simple" response headers to JavaScript. `X-PAYMENT-RESPONSE` isn't one of them, so without `expose_headers` a web client would receive the settlement receipt and be unable to read it. Server-to-server agents don't care, but it's a one-line fix and worth doing.

### Marking a route as paid

The decorator stacks above the route registration ([`backend/app/api/routes/items.py`](../backend/app/api/routes/items.py)):

```python
@router.get("/premium/sample", response_model=ItemPublic)
@pay(settings.X402_PREMIUM_PRICE)  # type: ignore[untyped-decorator]
async def read_premium_item(session: SessionDep) -> Any:
    ...
```

Decorator order is load-bearing. `@router.get(...)` must sit **above** `@pay(...)` so FastAPI registers the payment-wrapped function as the endpoint while still seeing the real signature for dependency injection and `response_model`. The `@pay` decorator preserves the wrapped function's metadata (via `functools.wraps`), which is how the middleware later recognizes the route as paid.

One subtlety worth internalizing: **`@pay` is inert unless the middleware is installed.** It registers price metadata in a module-level table, but nothing enforces it until `init_x402` adds the ASGI middleware that reads that table. That's exactly why our disabled-by-default flag works cleanly — with `X402_ENABLED=False`, the decorated route is just a normal, free route. No conditional logic inside the handler, no branching. The gate is entirely in the middleware layer.

---

## How the gate actually fires

When a request comes in, the middleware iterates the app's routes, matches the request, and checks whether the matched endpoint carries the payment marker:

```python
for route in request.app.routes:
    match, _ = route.matches(request.scope)
    if match and requires_payment(route.endpoint):
        # look up price, read X-PAYMENT, verify + settle, or return 402
```

If the route is paid and there's no `X-PAYMENT` header, the middleware short-circuits and returns the `402` challenge **before your handler runs**. That has a pleasant consequence for correctness and cost: an unpaid request never touches your database, never spends compute on generating the premium content. The paywall is genuinely in front of the work, not bolted on after it.

---

## Testing without a blockchain

The full verify-and-settle path needs a live facilitator and a funded wallet, which doesn't belong in CI. But the **`402` challenge** is pure HTTP — no chain involved — so it's completely testable. From [`backend/tests/api/routes/test_items.py`](../backend/tests/api/routes/test_items.py):

```python
def test_premium_item_requires_payment_when_x402_enabled() -> None:
    pay_to = "0x0000000000000000000000000000000000000001"
    app = FastAPI()
    # load_dotenv_file=False keeps the challenge hermetic; otherwise the project
    # .env (X402_NETWORK) would override the network passed here.
    init_x402(app, pay_to=pay_to, network="base-sepolia", load_dotenv_file=False)
    app.include_router(items.router, prefix=settings.API_V1_STR)

    client = TestClient(app)
    response = client.get(f"{settings.API_V1_STR}/items/premium/sample")
    assert response.status_code == 402
    body = response.json()
    assert body["accepts"][0]["scheme"] == "exact"
    assert body["accepts"][0]["payTo"] == pay_to
```

Two real lessons are baked into that test:

- **`load_dotenv_file=False`.** The library helpfully loads `.env` and lets `X402_NETWORK` override the network argument. Helpful in production, hostile in tests — it makes the assertion depend on ambient environment. Disabling it makes the test hermetic. The general principle: anything that reads the environment implicitly will eventually surprise you in a test runner.
- **Spin up a throwaway app.** Because the middleware is global once installed, the cleanest way to test the *enabled* path without contaminating the rest of the suite (which runs with x402 off) is a fresh `FastAPI()` instance per test, including the same router. The `@pay` registration is module-level, so the route is still recognized as paid.

We also keep a test for the **disabled** path that asserts the route returns `200` for free, which guards the "additive, off by default" promise from regressing.

---

## The sharp edges, collected

If you take one section to your team, take this one.

- **Idempotency and replay.** A `nonce` and a `validBefore` window bound replay at the protocol level, but think about your own side: if `/settle` succeeds and then your handler throws, did the client pay for nothing? Decide your ordering (settle-then-serve vs. serve-then-settle) deliberately and make failures observable.
- **Atomic units are strings.** Never let a float touch a token amount. `"10000"` is $0.01 USDC; `0.01` is a bug waiting to round.
- **Network is identity.** `base` vs `base-sepolia` is the difference between real money and play money, and a stray environment variable can flip it (see the test note above). Treat the network setting like a production credential.
- **The facilitator is a dependency.** Verify/settle is a network hop to a third party. It can be slow or down. Budget a timeout, decide your failure mode, and consider that mainnet typically means the Coinbase CDP facilitator (with credentials) rather than the public testnet one.
- **CORS exposes the receipt, nothing more.** Don't over-grant. `expose_headers=["X-PAYMENT-RESPONSE"]` is the surgical fix; a wildcard is not.
- **Decorator order and untyped decorators.** `@pay` is untyped, so under strict mypy it'll flag the wrapped function — we annotated it with a targeted `# type: ignore[untyped-decorator]` rather than loosening the whole project's strictness.

---

## Where to go from here

What we built is intentionally minimal: one route, one price, flat. The protocol supports more, and the natural extensions are straightforward:

- **Dynamic, per-resource pricing** — compute the price from the resource itself (a per-item `price` column, say) instead of a flat constant.
- **Multiple payment options** — populate `accepts` with several networks or tokens and let the client choose.
- **Usage accounting** — record settlements (you already get a transaction hash and payer address per call) for analytics, reconciliation, and rate-limiting by payer.
- **Combining with auth** — payment and identity are orthogonal. A route can require *both* a JWT and a payment, or payment alone for anonymous machine buyers. Our demo route uses payment as the sole gate precisely to show the account-less case.

None of that changes the shape of what we did. The endpoint that used to be free now answers a machine with `402`, accepts a hundredth of a cent, and returns the goods — and the dormant status code finally has a job.

---

*The complete, runnable integration lives in this repository. Start with [`backend/README.md`](../backend/README.md#x402-payments), then read [`main.py`](../backend/app/main.py), [`items.py`](../backend/app/api/routes/items.py), and [`config.py`](../backend/app/core/config.py) in that order.*
