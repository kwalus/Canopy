# Contributing to Canopy

Thank you for your interest in contributing to Canopy! This document explains how to get involved.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [How to Contribute](#how-to-contribute)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Reporting Bugs](#reporting-bugs)
- [Suggesting Features](#suggesting-features)

---

## Code of Conduct

By participating in this project you agree to our [Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

---

## Getting Started

1. **Fork** the repository on GitHub.
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/Canopy.git
   cd Canopy
   ```
3. **Add upstream remote:**
   ```bash
   git remote add upstream https://github.com/kwalus/Canopy.git
   ```

---

## Development Setup

**Requirements:** Python 3.10+

```bash
# Create a virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install core + dev dependencies
pip install -r requirements.txt
pip install -e ".[dev]"

# Optional: install MCP dependencies
pip install -r requirements-mcp.txt

# Copy environment template
cp .env.example .env
```

Start Canopy in development mode:

```bash
python -m canopy
# Open http://localhost:7770
```

---

## How to Contribute

### Reporting Bugs

Before filing a bug, search [existing issues](https://github.com/kwalus/Canopy/issues) to avoid duplicates. When filing a new bug, use the **Bug Report** template and include:

- A clear, descriptive title.
- Steps to reproduce the issue.
- Expected vs. actual behaviour.
- Environment details (OS, Python version, Canopy version).
- Relevant logs (check `/tmp/canopy_web.log` or console output).

### Suggesting Features

Open a **Feature Request** issue and describe:

- The problem you are trying to solve.
- Your proposed solution.
- Alternatives you have considered.

### Submitting Code

1. Open an issue (or comment on an existing one) so we can discuss the change before you invest time.
2. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. Make your changes following the [Coding Standards](#coding-standards).
4. Write or update tests (see [Testing](#testing)).
5. Commit with a clear message:
   ```bash
   git commit -m "feat: add channel message pinning"
   ```
6. Push and open a Pull Request against `main`.

---

## Pull Request Process

1. Fill in the pull request template completely.
2. Ensure all existing tests pass (`pytest`).
3. Add tests for any new functionality.
4. Keep PRs focused: one logical change per PR.
5. Reference the related issue (`Closes #123`).
6. A maintainer will review your PR; please respond to feedback promptly.
7. PRs are merged by a maintainer once approved.

---

## Coding Standards

- **Style:** Follow [PEP 8](https://pep8.org/). Run `black` to auto-format:
  ```bash
  black canopy/
  ```
- **Linting:** Use `flake8` to check for issues:
  ```bash
  flake8 canopy/
  ```
- **Type hints:** Add type annotations for new functions where practical. Run `mypy` to check:
  ```bash
  mypy canopy/
  ```
- **Security:** Never commit real credentials, API keys, or secrets. Use `.env` (gitignored) for local config.
- **Dependencies:** Avoid adding new dependencies unless necessary; discuss in the issue first.

---

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=canopy tests/

# Run a specific test file
pytest tests/test_file_access_hardening.py -v
```

New functionality should be accompanied by tests under `tests/`. Follow the existing test structure and use `pytest-flask` fixtures where applicable.

---

## Security Vulnerabilities

**Please do not open public issues for security vulnerabilities.** See [SECURITY.md](SECURITY.md) for the responsible disclosure process.

---

## License

By contributing to Canopy you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
