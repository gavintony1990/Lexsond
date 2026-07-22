# ADR-010: Authentication, server sessions, and workspace tenancy

Status: accepted for staged implementation after 0.8.0.
Date: 2026-07-22

## Context

Lexsond currently has a PostgreSQL-backed control plane but no authenticated
principal or tenant boundary. A resource identifier is therefore sufficient to
read or mutate targets, suites, runs, monitoring state, and Agent memory. Adding
only a login screen would leave that object-level authorization defect intact.

API credentials, OAuth credentials, passwords, Session values, and reset links
are separate secret classes. None may enter probe evidence, SSE, Temporal
History, LangChain checkpoints, ordinary JSON columns, browser storage, or
logs. The native HTTP/SSE probe remains the measurement owner; authentication
must not add provider retries or infer model identity.

## Decision

### Runtime modes

`LEXSOND_AUTH_MODE` accepts exactly `required` or `local-single-user` and
defaults to `required`.

- `required` resolves every protected request from a server-side Session and an
  active workspace membership.
- `local-single-user` creates or resolves a dedicated local user and Personal
  Workspace without a login screen. It is valid only when the HTTP listener is
  bound to a numeric loopback address. A wildcard, hostname, LAN, Unix-forwarded
  public listener, or `0.0.0.0` fails startup rather than weakening auth.
- The UI and bootstrap response display `本地单用户模式` while this exception
  is active. Local mode is a deployment choice, not a request header or query
  option.

PostgreSQL remains the only structured persistent-memory backend in both modes.
There is no SQLite or in-process persistence fallback.

### Users, workspaces, and authorization

Every registered user receives a Personal Workspace and an `OWNER` membership
in the same transaction. Workspace roles are `OWNER`, `ADMIN`, `MEMBER`, and
`VIEWER`; platform administration is the separate `users.system_role=ADMIN`.

Repository and Service methods receive an explicit `workspace_id`. Resource
queries include that value in SQL; code must not load an object globally and
then hide it in React. Composite foreign keys bind targets, suite revisions,
runs, monitoring policies, and Agent sessions inside a workspace. Route guards
are UX only; FastAPI dependencies and the repository are the authority.

Migration `0007_auth_workspaces.sql` assigns existing mutable resources to a
fixed, memberless `Legacy Workspace`. Registration never grants that workspace
to the first user. A later `lexsond-admin bootstrap --email` command may create
a one-use `claim_legacy_workspace` action token. The raw token is displayed or
delivered once; only its hash is durable.

System suites will use `scope=SYSTEM` and be read-only when suite authorization
is migrated. Existing suites enter the Legacy Workspace as `scope=WORKSPACE`.

Write authorization is enforced before request-model execution: `OWNER` and
`ADMIN` may manage workspace resources; `MEMBER` may launch/cancel bounded
probes and use the diagnostic assistant but cannot manage credentials, channels,
suites, monitoring, partners, or workspace membership; `VIEWER` is read-only.
Authentication actions remain user-scoped and do not derive authority from a
workspace role. Platform `system_role=ADMIN` never substitutes for membership.

### Passwords and action tokens

Passwords are hashed with Argon2id through pinned `argon2-cffi 25.1.0` (MIT).
The password field accepts at least 12 characters and long password-manager
values; request body limits provide the abuse ceiling. Authentication performs
a dummy Argon2 verification for unknown emails, returns the same public failure
for an unknown email and a wrong password, and rehashes after successful login
when parameters change.

Email verification, password reset, email change, and Legacy Workspace claim
tokens use at least 256 random bits. PostgreSQL stores only SHA-256 token hashes,
purpose, expiry, and consumption time. Tokens are single-use and invalidated in
the same transaction as their action.

Email links carry the one-use value in a URL fragment, never a query string.
React copies it into short-lived component memory and replaces the current
history entry before submitting it in a CSRF-protected POST body. Registration,
login, verification, resend, forgot-password, and reset-password consume
PostgreSQL-backed limits keyed by one-way hashes of coarse IP/email scopes.

### Web Session and CSRF

The browser receives one opaque random Session value in the
`lexsond_session` Cookie. The value contains at least 256 random bits; only its
SHA-256 hash is stored in `auth_sessions`. The Cookie uses `HttpOnly`,
`SameSite=Lax`, and `Path=/`; `Secure` is mandatory for the HTTPS production
configuration and disabled only for explicit loopback HTTP development.

Sessions have a 12-hour idle timeout and a seven-day absolute expiry. Login,
password changes, OAuth binding, and role changes rotate or revoke Sessions.
Users can revoke a device or all Sessions. Last-seen updates are bounded so a
request does not create an unbounded write stream.

Cookie-authenticated POST, PUT, PATCH, and DELETE requests require an
`X-CSRF-Token`. The CSRF value is returned only by the authenticated Session
endpoint, retained in React memory, and compared to a server-side hash using a
constant-time operation. SameSite and same-origin checks are defense in depth,
not the only CSRF control. Session and CSRF values never enter localStorage or
sessionStorage.

Each Session retains at most eight hashed CSRF values so an authenticated page
reload can rotate the current value without invalidating a small number of
already-open tabs. `/auth/session` is always `Cache-Control: no-store`; no raw
CSRF value is durable.

### OAuth boundary

GitHub uses Authorization Code with PKCE and random `state`. Google uses
Authorization Code with PKCE, `state`, and OIDC `nonce`; the callback verifies
signature, issuer, audience, expiry, nonce, and `email_verified`. Callback and
return targets are exact configured allowlists or same-site relative paths.

External identity is keyed by `(provider, provider_subject)`. A matching email
never silently links accounts. The user must authenticate with an existing
method and explicitly bind. Unbinding is rejected when it would remove the last
login method. An OAuth access token is used only to read minimum identity data
and is never persisted after that exchange. Client secrets come only from the
environment or a Secret Manager.

### Audit and secret handling

Authentication audit rows contain a user identifier when known, provider,
allowlisted outcome category, coarse IP prefix, user-agent hash, and time. They
never contain passwords, raw Session/CSRF/action tokens, OAuth codes or tokens,
API keys, Authorization values, or upstream response bodies.

Account deletion immediately revokes Sessions and removes API Key Secret
material at the vault boundary. Non-secret measurement history follows its
existing retention policy and is never reassigned across workspaces.

### API credential vault boundary

Migration `0008_credential_profiles.sql` persists only workspace-scoped
credential metadata. `secret_locator` is an internal random UUID and
`fingerprint` is an application-keyed HMAC digest used only for duplicate
detection. PostgreSQL has no API Key, ciphertext, Authorization, or reversible
secret column. Target bindings use composite workspace foreign keys so a target
cannot bind another workspace's credential.

In explicit loopback `local-single-user` mode, persistent credential material
uses the operating-system credential service through pinned `keyring 25.7.0`
(MIT). An unavailable or locked secure backend disables persistence; it never
falls back to PostgreSQL, a file, Base64, XOR, or process memory. Cloud
`required` mode does not use the web host's keyring for tenant credentials; it
stores only a random locator for an external Secret Manager adapter. Temporary
keys remain request-scoped and are never added to the metadata table.

## Rollout

1. Apply the schema and Legacy Workspace migration.
2. Introduce explicit workspace-scoped repository methods and isolation tests.
3. Enable email authentication, server Sessions, CSRF, and local single-user.
4. Add React authentication and stable protected-route skeletons.
5. Add GitHub and Google adapters only after callback fixtures are frozen.
6. Split CredentialProfile, Channel, ModelVendor, and ModelSource domains, then
   migrate the eight-module navigation as real pages become available.

The default stays `required`; deployments must explicitly select
`local-single-user` for loopback-only operation. A migration stage may ship
before a later runtime stage, but no UI route is presented as usable until its
server authorization and persistence contract exist.

## Consequences

Every control-plane query gains tenant context, and PostgreSQL constraints make
cross-workspace binding harder to express accidentally. Authentication adds a
small number of security-critical dependencies and more writes for Sessions and
audit events. In return, the browser stores no bearer credential, revocation is
immediate, and local users retain a deliberate loopback-only path.
