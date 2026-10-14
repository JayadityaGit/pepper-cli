# Invariants

1. **Tokens stay secret.** A PAT or session token is never logged, echoed, or included in an error
   message or in rendered output. The credentials file is written with owner-only permissions.
2. **The CLI follows the server contract, not the reverse.** The server's `/api/v1` is additive-only
   for supported clients. The CLI must keep working against a server that adds fields, endpoints, or
   stream frame types it does not know about.
3. **`# MIRROR:` comments stay accurate.** When a mirrored model changes, update the comment and the
   model together.
4. **Exit codes and stdout are an interface.** Scripts and agents parse the output; changing the
   shape of `--json` output or an exit code is a breaking change and needs a deliberate PR.
5. **The bundled agent skill matches the CLI.** `src/pepper_cli/data/agent-skill/SKILL.md`
   documents the commands; change both together.
