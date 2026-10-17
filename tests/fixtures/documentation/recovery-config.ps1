$autodjRecovery = Join-Path ([IO.Path]::GetTempPath()) ('autodj-recovery-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $autodjRecovery -ErrorAction Stop | Out-Null
$autodjRecoveryToml = $autodjRecovery.Replace('\', '/')
$autodjRecoveryConfig = @"
[index]
index_dir = '$autodjRecoveryToml/index'
model_dir = '$autodjRecoveryToml/models'
"@
[IO.File]::WriteAllText((Join-Path $autodjRecovery recovery.toml), $autodjRecoveryConfig, [Text.UTF8Encoding]::new($false))
