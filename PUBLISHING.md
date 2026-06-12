# Publishing to PyPI

This document covers the one-time setup and per-release workflow for
publishing `ai-tokens-observability` to PyPI using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC — no
stored API token).

**The actual upload is user-gated.** `.github/workflows/publish.yml` stages
the infrastructure; a human must complete the PyPI registration steps below
before any upload can happen.

---

## One-time setup (user action required)

### 1 — Create the PyPI project via Pending Publisher

For a brand-new project that has never been uploaded before, use PyPI's
**pending publisher** flow to register the Trusted Publisher *before* the
first upload (this avoids the bootstrapping catch-22 of needing an existing
project to register a publisher).

Go to <https://pypi.org/manage/account/publishing/> and add a new pending
publisher with **exactly** these values:

| Field | Value |
|---|---|
| PyPI project name | `ai-tokens-observability` |
| Owner | `supachai-j` |
| Repository | `ai-tokens-observability` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

### 2 — Create the GitHub deployment environment

Go to the repository **Settings → Environments** and create an environment
named **`pypi`**. Add any protection rules you want (e.g. required reviewer).
The `publish` job in `publish.yml` gates on this environment — it will not
run until the environment exists.

### 3 — TestPyPI dry-run (optional)

To do a dry-run against TestPyPI before touching production PyPI:

1. Register the same Trusted Publisher on
   <https://test.pypi.org/manage/account/publishing/> using the same field
   values as above.
2. Trigger the workflow manually from **Actions → Publish to PyPI →
   Run workflow**, then select **`testpypi`** from the target dropdown.

---

## Publishing a release

### Automatic (recommended)

When a new GitHub Release is published, `publish.yml` triggers automatically:
1. The `build` job produces a `py3-none-any` wheel + sdist.
2. The `publish` job waits for the `pypi` environment gate, then uploads via
   OIDC — no password prompt, no stored token.

### Manual trigger (v0.1.0 / re-publish)

Because v0.1.0's GitHub Release existed before this workflow was added, use
the manual dispatch to publish it:

1. Go to **Actions → Publish to PyPI → Run workflow**.
2. Leave target as **`pypi`** (default) and click **Run workflow**.
3. Approve the deployment environment gate when prompted.

---

## Local build verification

Run this before every release to confirm the wheel is correct. A stale pip
(≤21.x, pre-PEP 621) will silently produce a broken `UNKNOWN-0.0.0` wheel
with no `pulse.py` — always upgrade pip first and use the `build` frontend.

```bash
# 1. Build
python3 -m venv /tmp/bt
/tmp/bt/bin/python -m pip install --upgrade pip build
/tmp/bt/bin/python -m build
# → dist/ai_tokens_observability-0.1.0-py3-none-any.whl
# → dist/ai_tokens_observability-0.1.0.tar.gz

# 2. Install and smoke-test
python3 -m venv /tmp/it
/tmp/it/bin/python -m pip install dist/ai_tokens_observability-0.1.0-py3-none-any.whl
/tmp/it/bin/rtk-pulse --version
# → ai-tokens-observability 0.1.0

# 3. Serve (confirm dashboard loads)
RTK_PULSE_HOME=/tmp/ith /tmp/it/bin/rtk-pulse serve --port 18399 &
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18399
# → 200
kill %1

# 4. (Optional) metadata check — requires twine
/tmp/bt/bin/python -m pip install twine
/tmp/bt/bin/twine check dist/*
# → PASSED
```

**What to verify:** `--version` prints `ai-tokens-observability 0.1.0`;
the wheel name contains `ai_tokens_observability-0.1.0-py3-none-any` (not
`UNKNOWN-0.0.0`); the dashboard serve returns HTTP 200.

---

## Supply-chain notes

- `pypa/gh-action-pypi-publish@release/v1` is pinned to the maintainer's
  rolling release tag. For stronger supply-chain guarantees, pin to a
  specific commit SHA instead (check the
  [releases page](https://github.com/pypa/gh-action-pypi-publish/releases)
  for the latest) and update it with each new release of the action.
- No PyPI API token is stored anywhere in this repository. Authentication
  is entirely via OIDC; the token is minted ephemerally by GitHub Actions.
