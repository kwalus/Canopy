# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.6.x (current public release line) | ✅ Active |
| < 0.6.0 | ⚠️ Best-effort only; fixes may not be backported |

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability in Canopy, please disclose it responsibly:

1. **Open a [GitHub Security Advisory](https://github.com/kwalus/Canopy/security/advisories/new)**. This is the preferred private disclosure path for the public repository.
2. If GitHub Security Advisories are not available to you, contact the maintainers through the repository contact path and clearly label the report as a **security disclosure request**. Do **not** open a public issue with exploit details.

Please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce (proof-of-concept code or detailed instructions).
- Affected versions, if known.
- Any suggested mitigations.

We aim to acknowledge reports within **48 hours** and to provide an initial assessment within **7 days**. We will keep you informed of progress and credit you in the release notes unless you prefer to remain anonymous.

---

## Security Design

Canopy's security is layered by design:

| Layer | Mechanism |
|-------|-----------|
| **Identity** | Ed25519 + X25519 keypairs generated locally on first launch. No central identity broker is required for normal operation. |
| **Transit encryption** | ChaCha20-Poly1305 (AEAD) for P2P transport. Key agreement uses X25519, and peer messages/signals are signed with Ed25519. |
| **At-rest protection** | Sensitive database fields are protected with HKDF-derived keys tied to local peer identity. This is not full-database or full-disk encryption. |
| **Authentication** | Web UI uses session cookies; external clients, scripts, and agents use scoped API keys (`X-API-Key`). |
| **Direct-message security** | Direct messages can use recipient-targeted peer E2E transport when both peers support compatible DM crypto, with explicit fallback markers when they do not. |
| **Private channel security** | Private and confidential channels use member-scoped key distribution so non-members cannot decrypt channel content even if packets are relayed. |
| **Password security** | bcrypt (12 rounds) with per-password salt. Strength validation enforced on registration and password change. |
| **File access** | Files are served only to the owner, instance admin, or users with visibility of referencing content. |
| **Trust & deletion** | EigenTrust-inspired model; delete signals are signed and peer compliance is tracked. |
| **Rate limiting** | Applied across login, registration, selected UI AJAX surfaces, file upload, API, and P2P-facing endpoints to reduce brute-force and abuse pressure. |

For a full security assessment, see [docs/SECURITY_ASSESSMENT.md](docs/SECURITY_ASSESSMENT.md).

---

## Scope

The following are **in scope** for vulnerability reports:

- Authentication and authorisation bypasses.
- Remote code execution or command injection.
- Path traversal or arbitrary file read/write.
- Cryptographic weaknesses in key generation, exchange, or storage.
- SQL injection or other data-layer attacks.
- Cross-site scripting (XSS) or CSRF in the web UI.
- P2P network attacks (identity spoofing, replay attacks, route manipulation).
- API key misuse that bypasses permission boundaries or visibility rules.
- Agent and MCP behaviors that break the same permission or visibility boundaries enforced for human/API clients.

The following are **out of scope**:

- Vulnerabilities in third-party dependencies before a Canopy-specific impact is demonstrated (please report those upstream first when appropriate).
- Theoretical weaknesses without a practical proof of concept.
- Attacks that require physical access to the device running Canopy.
- Social engineering of project maintainers.

---

## Acknowledgements

We are grateful to all researchers who help keep Canopy and its users safe. Confirmed vulnerability reporters will be credited in the [CHANGELOG](CHANGELOG.md) unless they request anonymity.
