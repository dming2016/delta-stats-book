# Contributor And Agent Guide

## Scope

This repository is the public source distribution of 三角洲战绩本, a Windows-local match review application. Read README.md and docs/architecture.md before changes.

## Compatibility

- Preserve the physical launcher name, installer AppId, registry keys, Mutex and user-data directories when contributing upstream.
- Keep Firebreak and Warfare models separate.
- Only an explicit upstream authentication rejection may persist expired credentials. Network failures and busy responses must not invalidate an account.
- Keep accounts, preferences and caches isolated; remote upload is always opt-in.
- Protocol changes require coordinated tests for the builder, launcher, updater and deployment scripts.

## Verification

- Use Python 3.12 on Windows and Node.js 22.
- Run `python -m unittest -v` before delivery.
- Use `tools/preview_ui.py` for synthetic screenshots; never use a real account for public screenshots.
- Use `apply_patch` or small scoped edits. Do not overwrite unrelated work.
- Building recreates `artifacts/local-package/`. Inspect it before running a build.
- Do not install over an existing user installation for testing.

## Publication

- Never commit credentials, local account data, HAR, dumps, private operational records or extracted game assets.
- Public map thumbnails are original generated placeholders; preserve their provenance.
- The deployment scripts are upstream-specific reference implementations. Do not run them against production, publish packages, or push changes without explicit user authorization.
- Do not control the user's desktop without explicit authorization.
