# Release Checklist — v0.1.0

Steps for **TEAM-LEAD** to perform when publishing the first public release.
All commands assume you are on branch `cycle-20-release` and the branch has
been pushed to `origin`.

---

## Pre-flight (verify locally)

```bash
# 1. Full test suite must be green
python3 -m unittest discover -s . -p "test*.py"

# 2. Version consistency check
python3 -c "import pulse; print(pulse.__version__)"
# must print: 0.1.0

python3 pulse.py --version
# must print: pulse.py 0.1.0

grep '^version' pyproject.toml
# must print: version = "0.1.0"
```

---

## Merge cycle-20-release → main

```bash
git checkout main
git merge --no-ff cycle-20-release -m "Merge cycle-20-release: v0.1.0 public release prep"
git log --oneline -5   # sanity check
```

---

## Tag v0.1.0

```bash
git tag -a v0.1.0 -m "v0.1.0 — first public release

Feature-complete single-machine and multi-machine token observability
for Claude Code, Codex CLI, and Gemini CLI. Zero dependencies (Python
3.9+ stdlib only). 270 unit tests."
```

---

## Push branch + tag

```bash
git push origin main
git push origin v0.1.0
```

---

## Create GitHub Release

```bash
gh release create v0.1.0 \
  --title "v0.1.0 — First public release" \
  --notes-file CHANGELOG.md \
  --latest
```

The `CHANGELOG.md` already contains the full v0.1.0 release notes.

To verify the release was created:

```bash
gh release view v0.1.0
```

---

## Flip repo to public

```bash
# This is irreversible — do it last, after verifying the release page
gh repo edit supachai-j/ai-tokens-observability --visibility public --accept-visibility-change-consequences
```

Verify:

```bash
gh repo view supachai-j/ai-tokens-observability --json visibility -q '.visibility'
# must print: PUBLIC
```

---

## Post-release smoke test (optional but recommended)

```bash
# Install from the now-public repo via pipx
pipx install "git+https://github.com/supachai-j/ai-tokens-observability"
rtk-pulse --version       # should print: rtk-pulse 0.1.0
rtk-pulse serve --open    # open dashboard, verify it loads
pipx uninstall ai-tokens-observability
```

---

## Notes

- The repo must remain **private** until the `gh repo edit --visibility public`
  step above. Do not share the URL publicly before that.
- The `contrib/seed_demo.py` script generates synthetic data for screenshots
  and demos — it never touches real user data (it writes to a temp dir; the
  demo server is started with a separate `HOME` override so real Claude
  transcripts are unreachable).
- All file writes in `pulse.py` are atomic (write to `.tmp` + rename), so a
  crash during `save`/`scan` cannot corrupt the index.
