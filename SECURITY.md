# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.4.x (latest) | ✅ Active |
| < 0.4.0 | ❌ No longer supported |

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability in Canopy, please disclose it responsibly:

1. **Open a [GitHub Security Advisory](https://github.com/kwalus/Canopy/security/advisories/new)** – this creates a private, encrypted channel between you and the maintainers.
2. Alternatively, open a **private issue** on GitHub (visible only to repository collaborators).

Please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce (proof-of-concept code or detailed instructions).
- Affected versions, if known.
- Any suggested mitigations.

We aim to acknowledge reports within **48 hours** and to provide an initial assessment within **7 days**. We will keep you informed of progress and credit you in the release notes (unless you prefer to remain anonymous).

---

## Security Design

Canopy's security is layered by design:

| Layer | Mechanism |
|-------|-----------|
| **Identity** | Ed25519 + X25519 keypairs generated locally on first launch. No central authority. |
| **Transit encryption** | ChaCha20-Poly1305 (AEAD) for all P2P traffic. Key agreement via ECDH (X25519). Messages signed with Ed25519. |
| **At-rest encryption** | Sensitive database fields encrypted with HKDF-derived keys tied to local peer identity. |
| **Authentication** | Web UI uses session cookies; external clients and agents use scoped API keys (`X-API-Key` header). |
| **Password security** | bcrypt (12 rounds) with per-password salt. Strength validation enforced on registration and password change. |
| **File access** | Files are served only to the owner, instance admin, or users with visibility of referencing content. |
| **Trust & deletion** | EigenTrust-inspired model; delete signals are signed and peer compliance is tracked. |
| **Rate limiting** | Applied to login, registration, and API endpoints to prevent brute-force and DoS. |

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

The following are **out of scope**:

- Vulnerabilities in third-party dependencies (please report those upstream).
- Theoretical weaknesses without a practical proof of concept.
- Attacks that require physical access to the device running Canopy.
- Social engineering of project maintainers.

---

## Acknowledgements

We are grateful to all researchers who help keep Canopy and its users safe. Confirmed vulnerability reporters will be credited in the [CHANGELOG](CHANGELOG.md) unless they request anonymity.
