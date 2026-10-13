# Coding guidelines

- Python 3.13+. Format with `ruff format`, lint with `ruff check` (the rule set is `ALL` minus the
  documented ignores in `pyproject.toml`), type-check with `ty check src/pepper_cli/`.
- Do not add new lint ignores to make a change pass. Fix the code, or justify a narrowly scoped
  ignore in the PR description.
- Bearer tokens and PATs are wrapped in `SecretStr` (`secret.py`) at the boundary and are never
  logged, printed, or put in an exception message.
- Wire models mirror the server's `/api/v1` response shapes. Each mirrored model carries a
  `# MIRROR:` comment naming the server model it follows. Keep models tolerant: ignore unknown
  fields and unknown stream frame types rather than failing.
- Output goes through `render.py`; commands do not `print` ad hoc (the `T201` ignore is for the
  rendering layer only).
- Keep the CLI dependency-light. A new runtime dependency needs a reason in the PR.
