autodj_recovery=$(mktemp -d "${TMPDIR:-/tmp}/autodj-recovery.XXXXXX")
cat > "$autodj_recovery/recovery.toml" <<EOFCONFIG
[index]
index_dir = '$autodj_recovery/index'
model_dir = '$autodj_recovery/models'
EOFCONFIG
