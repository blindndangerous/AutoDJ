# Collect diagnostics for a problem

Use this guide to collect a small, redacted report before changing an installation.
You need an installed runtime, access to its terminal and the same configuration
used to start AutoDJ. First-run setup is a separate action: complete
[installation](install.md) before collecting a report.

## Run Doctor without repairs

1. From the checkout, run the command for your shell. If you normally pass
   `--config`, put it before `doctor`, for example
   `./autodj --config /srv/radio/config.toml doctor --no-fix`.

   ```bash
   ./autodj doctor --no-fix
   ./autodj doctor --json > doctor.json
   ```

   ```powershell
   .\autodj.cmd doctor --no-fix
   .\autodj.cmd doctor --json | Set-Content -Encoding utf8 doctor.json
   ```

2. Record the exit code immediately after the command: `echo "$?"` in Bash,
   or `$LASTEXITCODE` in PowerShell. A required failed check gives exit 1;
   a warning alone does not. Save the failure text even if JSON could not be produced.
3. For a station-specific problem, repeat with `doctor --no-fix --station SLUG`.
   Keep the server-wide report too when the failure concerns shared storage or access.

`--no-fix` disables repair offers; `--json` is also read-only and cannot be
combined with `--fix`. Doctor takes the index lock during its check and may probe
configured network endpoints. It does not prove continuous playback, a working
speaker, or provider authentication; speech checks do not call providers.

In [no-model mode](../setup.md#no-model-installation), the `model-cache` warning
that local model analysis is unavailable is expected. That warning alone does
not make Doctor fail or mean basic import, browser playback or streaming is
broken. Keep this mode if those are the features you need; follow the setup
guide to enable local model analysis. The warning does not certify that the
server has the compatible cache required for remote analysis workers.

Do not use `--fix` to collect evidence: it also approves supported recovery
commands, including expensive analysis or indexing. See
[Doctor details](../operations.md#diagnose-before-serving) before a repair.

## Collect only relevant logs

1. Note the failure time and time zone, station slug, version shown in the page
   footer, operating system,
   browser and output mode.
2. Copy the few lines around the failure from the serving terminal or
   `<index_dir>/logs/autodj.log`. Serving rotates this file at midnight and
   keeps seven rotated files. Check the matching dated file if necessary.
   When serving cannot open the log file, use the terminal output instead.
3. Read your copy before sharing. Replace private music paths, names, hostnames,
   IP addresses, usernames and private endpoint URLs with consistent placeholders.
   Remove tokens, pairing codes, cookies, authorization headers, passwords and keys.
   Doctor redacts server and Hugging Face tokens, but its output still needs review.
   Keep check names, status, exception type and event order useful for diagnosis.

Do not attach configuration, secret files, environment dumps, full browser network
captures or a station backup to a public issue. Logs can contain personal data
and locally supplied error text even when credential fields are protected.

## Submit a report and retain recovery evidence

Use the [bug report form](https://github.com/blindndangerous/AutoDJ/issues/new?template=bug_report.yml).
Include expected and actual behavior, the smallest reproduction steps, version,
CLI/web surface, environment, output mode and relevant redacted checks/log lines.
Say what you already tried and whether music files are reachable. For a browser
failure, add the exact control name and relevant redacted console error.

If the report could reveal a vulnerability or credential, use the
[private security reporting procedure](../../SECURITY.md#reporting-a-vulnerability)
instead. Keep the unredacted originals locally with restricted access until the
problem is resolved. If collection fails, report that error and the last working
step; preserve the installation and use [troubleshooting](troubleshooting.md)
before accepting repairs.

AutoDJ and this page were developed with AI assistance.
