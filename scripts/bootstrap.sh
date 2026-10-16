#!/usr/bin/env bash
# Constructed-launcher trust invariant: the launcher, its pinned Python runtime,
# and the checkout wrapper it executes must remain outside the mutable project
# worktree. Bootstrap installs no persistent global permission policy.
# Host is Google Antigravity (agy).
set -u

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh --install
       scripts/bootstrap.sh --remove

--install checks local prerequisites for agy-triad-dispatch, verifies the bundled
triad-dispatch-lib 0.1.0.dev3 adoption, and installs the 5 managed provider and
selector launchers (claude_wrapper.py, codex_wrapper.py, gemini_wrapper.py,
antigravity_wrapper.py, review_round.py).

--remove uninstalls all 5 managed provider and selector launchers and exact
legacy plugin-owned artifacts. Learned classifier patches are preserved.

Install targets must resolve outside the project worktree. Run TRIAD from the
same authenticated login terminal and worktree used for development.
EOF
}

MODE=""
if [ "$#" -ne 1 ]; then
  usage >&2
  exit 2
fi
case "${1:-}" in
  --install)
    MODE="install"
    ;;
  --remove)
    MODE="remove"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RAW_REPO_ROOT="${TRIAD_BOOTSTRAP_REPO_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"
RAW_LAUNCHER_DIR="${TRIAD_BOOTSTRAP_BIN_DIR:-$HOME/.local/bin}"
LAUNCHER_DIR="$RAW_LAUNCHER_DIR"
RAW_BOOTSTRAP_CODEX_ROOT="${TRIAD_BOOTSTRAP_CODEX_ROOT:-${CODEX_HOME:-$HOME/.codex}}"
BOOTSTRAP_CODEX_ROOT="$RAW_BOOTSTRAP_CODEX_ROOT"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
RAW_CLASSIFIER_PATH="${TRIAD_CLASSIFIER_EXTENSION:-$CONFIG_HOME/agy-triad-dispatch/classifier-patches.json}"
REPO_ROOT="$RAW_REPO_ROOT"
CLASSIFIER_PATH="$RAW_CLASSIFIER_PATH"
CODEX_PROFILE_NAME="${TRIAD_CODEX_PROFILE_NAME:-triad-codex-dispatch}"
CODEX_RULES_NAME="${TRIAD_CODEX_RULES_NAME:-triad-codex-dispatch.rules}"
REPAIR_ANALYZER_NAME="triad-repair-analyzer"
APPLY_REPAIR_LAUNCHER="triad-apply-repair"
MANAGED_LAUNCHERS="claude_wrapper.py codex_wrapper.py gemini_wrapper.py antigravity_wrapper.py review_round.py"
SHELL_RC="${TRIAD_BOOTSTRAP_SHELL_RC:-}"
if [ -z "$SHELL_RC" ]; then
  case "${SHELL:-}" in
    */zsh) SHELL_RC="$HOME/.zshrc" ;;
    *) SHELL_RC="$HOME/.bashrc" ;;
  esac
fi

errors=0
SEC2_FLAGGED_SELECTORS=""

add_flagged_selector() {
  if [ -z "$SEC2_FLAGGED_SELECTORS" ]; then
    SEC2_FLAGGED_SELECTORS="$1"
  else
    SEC2_FLAGGED_SELECTORS="$SEC2_FLAGGED_SELECTORS
$1"
  fi
}

flagged_selector_matches() {
  target="$1"
  while IFS= read -r flagged_name; do
    [ -z "$flagged_name" ] && continue
    [ "$flagged_name" = "$target" ] && return 0
  done <<EOF
$SEC2_FLAGGED_SELECTORS
EOF
  return 1
}

ok() {
  printf '[ok] %s\n' "$1"
}

warn() {
  printf '[warn] %s\n' "$1"
}

fail() {
  printf '[error] %s\n' "$1" >&2
  errors=$((errors + 1))
}

validate_codex_profile_name() {
  case "$CODEX_PROFILE_NAME" in
    ""|[!0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz]*|*[!0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._-]*)
      fail "invalid TRIAD_CODEX_PROFILE_NAME: must match [A-Za-z0-9][A-Za-z0-9._-]*"
      return 1
      ;;
  esac
  return 0
}

validate_codex_rules_name() {
  case "$CODEX_RULES_NAME" in
    *.rules)
      case "$CODEX_RULES_NAME" in
        */*|*\\*) ;;
        *) return 0 ;;
      esac
      ;;
  esac
  fail "invalid TRIAD_CODEX_RULES_NAME: must be a basename ending in .rules"
  return 1
}

validate_bootstrap_inputs() {
  validation_failed=0
  validate_codex_profile_name || validation_failed=1
  validate_codex_rules_name || validation_failed=1
  return "$validation_failed"
}

path_has_dir() {
  case ":$PATH:" in
    *":$1:"*) return 0 ;;
    *) return 1 ;;
  esac
}

launcher_is_managed() {
  launcher="$1"
  wrapper="$2"
  if [ -L "$launcher" ]; then
    return 1
  fi
  if [ ! -e "$launcher" ]; then
    return 0
  fi
  if [ ! -f "$launcher" ]; then
    return 1
  fi
  python3 "$REPO_ROOT/bin/bootstrap_repair.py" command-owned \
    --kind launcher --name "$wrapper" --path "$launcher" >/dev/null
}

COMMAND_MANIFEST=""

begin_command_group() {
  COMMAND_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/triad-command-group.XXXXXX")" || {
    fail "could not create command transaction manifest"
    return 1
  }
}

queue_command_artifact() {
  name="$1"
  kind="$2"
  target="$3"
  payload="$4"
  if ! python3 - "$name" "$kind" "$target" "$payload" >>"$COMMAND_MANIFEST" <<'PY'
import json
import sys

print(json.dumps({
    "name": sys.argv[1],
    "kind": sys.argv[2],
    "target": sys.argv[3],
    "data_path": sys.argv[4],
    "mode": 0o755,
}, ensure_ascii=False))
PY
  then
    rm -f "$payload"
    fail "could not queue managed command: $target"
    return 1
  fi
}

queue_command_removal() {
  name="$1"
  kind="$2"
  target="$3"
  if ! python3 - "$name" "$kind" "$target" >>"$COMMAND_MANIFEST" <<'PY'
import json
import sys

print(json.dumps({
    "name": sys.argv[1],
    "kind": sys.argv[2],
    "target": sys.argv[3],
    "mode": 0o755,
}, ensure_ascii=False))
PY
  then
    fail "could not queue managed command removal: $target"
    return 1
  fi
}

publish_command_group() {
  [ -n "$COMMAND_MANIFEST" ] || return 0
  if ! python3 "$REPO_ROOT/bin/bootstrap_repair.py" commands-install --manifest "$COMMAND_MANIFEST"; then
    fail "could not install managed command group"
  else
    ok "managed command group installed"
  fi
  python3 - "$COMMAND_MANIFEST" <<'PY'
import json
from pathlib import Path
import os
import stat
import sys

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        payload = Path(json.loads(line)["data_path"])
        payload.unlink(missing_ok=True)
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        pass
PY
  rm -f "$COMMAND_MANIFEST"
  COMMAND_MANIFEST=""
}

remove_command_group() {
  [ -n "$COMMAND_MANIFEST" ] || return 0
  if [ ! -s "$COMMAND_MANIFEST" ]; then
    rm -f "$COMMAND_MANIFEST"
    COMMAND_MANIFEST=""
    return 0
  fi
  if ! python3 "$REPO_ROOT/bin/bootstrap_repair.py" commands-remove \
    --manifest "$COMMAND_MANIFEST" \
    --preserve-foreign \
    --test-fail-at "${TRIAD_BOOTSTRAP_TEST_FAIL_COMMAND_REMOVE_AT:-}"; then
    fail "could not remove managed command group"
    rm -f "$COMMAND_MANIFEST"
    COMMAND_MANIFEST=""
    return 1
  else
    ok "managed command group removed"
  fi
  rm -f "$COMMAND_MANIFEST"
  COMMAND_MANIFEST=""
}

is_expected_wrapper() {
  resolved="$1"
  target="$2"
  launcher="$3"
  if [ -z "$resolved" ] || [ ! -x "$resolved" ]; then
    return 1
  fi
  python3 - "$resolved" "$target" "$launcher" <<'PY'
from pathlib import Path
import shutil
import sys

resolved, target, launcher = (Path(arg).resolve() for arg in sys.argv[1:])
raise SystemExit(0 if resolved in {target, launcher} else 1)
PY
}

run_repair_lifecycle() {
  action="$1"
  python3 "$REPO_ROOT/bin/bootstrap_repair.py" "$action" \
    --config "$BOOTSTRAP_CODEX_ROOT/config.toml" \
    --analyzer "$BOOTSTRAP_CODEX_ROOT/agents/$REPAIR_ANALYZER_NAME.toml" \
    --launcher "$LAUNCHER_DIR/$APPLY_REPAIR_LAUNCHER"
  if [ "$?" -eq 0 ]; then
    ok "repair artifacts $action completed"
    return 0
  fi
  fail "repair artifact $action failed"
  return 1
}

print_owner_apply_argv() {
  python3 - "$REPO_ROOT" "$CLASSIFIER_PATH" <<'PY'
from pathlib import Path
import shlex
import sys

toolkit_root = Path(sys.argv[1])
classifier_path = sys.argv[2]
inner = [
    "python3",
    str(toolkit_root / "bin" / "apply_patch.py"),
    "--cli",
    "<cli>",
    "--classifier-file",
    classifier_path,
    "--proposal-file",
    "<absolute-proposal-path>",
]
owner_argv = ["/bin/zsh", "-lic", shlex.join(inner)]
print("owner apply argv: " + shlex.join(owner_argv))
PY
}

remove_owned_artifact() {
  owned_kind="$1"
  owned_path="$2"
  removed_message="$3"
  unmanaged_message="$4"
  owned_status="$(python3 "$REPO_ROOT/bin/bootstrap_repair.py" managed-remove \
    --kind "$owned_kind" --path "$owned_path")"
  if [ "$?" -ne 0 ]; then
    fail "could not safely remove managed artifact: $owned_path"
    return 1
  fi
  case "$owned_status" in
    absent)
      return 0
      ;;
    unmanaged)
      warn "$unmanaged_message"
      return 0
      ;;
    removed)
      ok "$removed_message"
      return 0
      ;;
    *)
      fail "unexpected managed removal status for $owned_path: $owned_status"
      return 1
      ;;
  esac
}

check_binary() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "found binary: $1"
  else
    fail "missing required binary: $1"
  fi
}

check_optional_binary() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "found optional binary: $1"
  else
    warn "optional binary not found: $1"
  fi
}

GOOGLE_ROUTE=""
check_google_route() {
  if command -v agy >/dev/null 2>&1; then
    GOOGLE_ROUTE="agy"
    ok "found preferred native Google reviewer: agy"
  elif command -v gemini >/dev/null 2>&1; then
    GOOGLE_ROUTE="gemini"
    warn "agy not found; personal Google Sign-In formal review is unavailable"
    ok "Gemini Enterprise OAuth fallback available: gemini"
  else
    fail "missing formal Google reviewer: install agy or gemini"
  fi
}

check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    fail "missing required binary: python3"
    return
  fi
  if python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
  then
    ok "python3 >= 3.12"
  else
    fail "python3 >= 3.12 required"
  fi
}

check_library_adoption() {
  if python3 "$REPO_ROOT/bin/bootstrap_repair.py" verify-library --root "$REPO_ROOT" >/dev/null 2>&1; then
    ok "bundled shared library adoption verified (triad-dispatch-lib 0.1.0.dev3)"
  else
    fail "bundled shared library adoption verification failed (library_adoption.verify_library)"
  fi
}

check_workspace_escape() {
  workspace_guard_output="$(python3 - "$PWD" "$LAUNCHER_DIR" "$BOOTSTRAP_CODEX_ROOT" "$(dirname -- "$CLASSIFIER_PATH")" "$SHELL_RC" "$REPO_ROOT" "${TRIAD_BOOTSTRAP_SOURCE_SOT_REVIEW_ROOT:-}" <<'PY'
from pathlib import Path
import os
import sys

pwd_raw, launcher_raw, codex_home_raw, classifier_dir_raw, shell_rc_raw, repo_root_raw, source_sot_review_raw = sys.argv[1:]
workspace = Path(pwd_raw).resolve()


def _within(target, root):
    target_path = Path(target).expanduser().resolve(strict=False)
    root_path = Path(root).expanduser().resolve()
    for candidate in (target_path, *target_path.parents):
        try:
            if candidate.exists() and os.path.samefile(candidate, root_path):
                return True
        except OSError:
            continue
    return False


targets = (
    ("launcher directory (TRIAD_BOOTSTRAP_BIN_DIR)", Path(launcher_raw)),
    ("Codex root (TRIAD_BOOTSTRAP_CODEX_ROOT / CODEX_HOME)", Path(codex_home_raw)),
    (
        "classifier directory (TRIAD_CLASSIFIER_EXTENSION / XDG_CONFIG_HOME)",
        Path(classifier_dir_raw),
    ),
    ("plugin/checkout root (TRIAD_BOOTSTRAP_REPO_ROOT)", Path(repo_root_raw)),
    ("python3 runtime", Path(sys.executable)),
)
failures = []
for label, target in targets:
    resolved = target.expanduser().resolve()
    if _within(resolved, workspace):
        failures.append(
            f"workspace-escape guard: {label} resolves inside the "
            f"sandbox-writable workspace: {resolved} (workspace root {workspace})"
        )
if source_sot_review_raw:
    review_input = Path(source_sot_review_raw).expanduser()
    if not review_input.is_absolute():
        failures.append(
            "source-SOT stage containment guard: "
            "TRIAD_BOOTSTRAP_SOURCE_SOT_REVIEW_ROOT must be absolute"
        )
    elif not review_input.is_dir():
        failures.append(
            "source-SOT stage containment guard: "
            "TRIAD_BOOTSTRAP_SOURCE_SOT_REVIEW_ROOT must be an existing directory"
        )
    else:
        protected_roots = (
            ("toolkit", Path(repo_root_raw).resolve()),
            ("review-worktree", review_input.resolve()),
        )
        generated_roots = (
            ("bootstrap-cwd", workspace),
            (
                "stage-root/launcher-dir",
                Path(launcher_raw).expanduser().resolve(strict=False),
            ),
            ("codex-home", Path(codex_home_raw).expanduser().resolve(strict=False)),
            (
                "classifier-dir",
                Path(classifier_dir_raw).expanduser().resolve(strict=False),
            ),
            ("shell-rc", Path(shell_rc_raw).expanduser().resolve(strict=False)),
        )
        for generated_label, generated_root in generated_roots:
            for protected_label, protected_root in protected_roots:
                if _within(generated_root, protected_root):
                    failures.append(
                        "source-SOT stage containment guard: "
                        f"{generated_label} resolves inside {protected_label}: "
                        f"{generated_root} (protected root {protected_root})"
                    )
for line in failures:
    print(line)
raise SystemExit(1 if failures else 0)
PY
)"
  if [ "$?" -ne 0 ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && fail "$line"
    done <<EOF
$workspace_guard_output
EOF
    fail "invariant: the constructed-launcher trust boundary requires launchers and everything they exec to live outside all sandbox-writable roots; run bootstrap from a directory that does not contain the install targets, or point the TRIAD_BOOTSTRAP_* / CODEX_HOME overrides outside this workspace"
  fi
}

canonicalize_path_inputs() {
  canonicalize_err="$(mktemp "${TMPDIR:-/tmp}/triad-codex-canonicalize.XXXXXX")" || {
    fail "could not create temporary file for path canonicalization"
    return
  }
  canonicalized="$(python3 - "$RAW_REPO_ROOT" "$RAW_CLASSIFIER_PATH" "$RAW_LAUNCHER_DIR" "$RAW_BOOTSTRAP_CODEX_ROOT" 2>"$canonicalize_err" <<'PY'
from pathlib import Path
import shutil
import sys

repo_raw, classifier_raw, launcher_raw, codex_home_raw = sys.argv[1:]
repo = Path(repo_raw).expanduser()
classifier = Path(classifier_raw).expanduser()
launcher = Path(launcher_raw).expanduser()
codex_home = Path(codex_home_raw).expanduser()
errors = []
if not repo.is_absolute():
    errors.append(f"TRIAD_BOOTSTRAP_REPO_ROOT must be an absolute path: {repo_raw}")
elif not repo.is_dir():
    errors.append(f"TRIAD_BOOTSTRAP_REPO_ROOT is not an existing directory: {repo_raw}")
if not classifier.is_absolute():
    errors.append(f"TRIAD_CLASSIFIER_EXTENSION must be an absolute path: {classifier_raw}")
if not launcher.is_absolute():
    errors.append(f"TRIAD_BOOTSTRAP_BIN_DIR must be an absolute path: {launcher_raw}")
if not codex_home.is_absolute():
    errors.append(f"TRIAD_BOOTSTRAP_CODEX_ROOT / CODEX_HOME must be an absolute path: {codex_home_raw}")
if errors:
    for error in errors:
        print(error)
    raise SystemExit(1)
print(repo.resolve())
print(classifier.parent.resolve(strict=False) / classifier.name)
print(launcher.resolve(strict=False))
print(codex_home.resolve(strict=False))
PY
  )"
  if [ "$?" -ne 0 ]; then
    canonicalize_output="$(
      printf '%s\n' "$canonicalized"
      cat "$canonicalize_err"
    )"
    while IFS= read -r line; do
      [ -n "$line" ] && fail "$line"
    done <<EOF
$canonicalize_output
EOF
    rm -f "$canonicalize_err"
    return
  fi
  if [ -s "$canonicalize_err" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && warn "$line"
    done <"$canonicalize_err"
  fi
  rm -f "$canonicalize_err"
  REPO_ROOT="$(printf '%s\n' "$canonicalized" | sed -n '1p')"
  CLASSIFIER_PATH="$(printf '%s\n' "$canonicalized" | sed -n '2p')"
  LAUNCHER_DIR="$(printf '%s\n' "$canonicalized" | sed -n '3p')"
  BOOTSTRAP_CODEX_ROOT="$(printf '%s\n' "$canonicalized" | sed -n '4p')"
}

resolve_python_runtime() {
  python_exe="$(python3 "$REPO_ROOT/bin/bootstrap_repair.py" runtime-path)" || {
    fail "could not resolve a portable Python runtime for launchers"
    return
  }
  escaped_python="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$python_exe")" || {
    fail "could not quote launcher Python runtime"
    return
  }
  escaped_classifier="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$CLASSIFIER_PATH")" || {
    fail "could not quote classifier path for launchers"
    return
  }
}

check_formal_schema_dependency() {
  if python3 "$REPO_ROOT/bin/bootstrap_repair.py" formal-schema-ready \
    --requirements "$REPO_ROOT/requirements.txt"; then
    ok "Pydantic 2, jsonschema Draft 2020-12 APIs and library adoption verified"
  else
    fail "formal review dependency readiness failed"
  fi
}

preflight_install_command_targets() {
  command_preflight_failed=0
  for wrapper in $MANAGED_LAUNCHERS; do
    launcher="$LAUNCHER_DIR/$wrapper"
    if [ -L "$launcher" ]; then
      fail "refusing to overwrite symlinked launcher: $launcher"
      command_preflight_failed=1
    elif ! launcher_is_managed "$launcher" "$wrapper"; then
      fail "refusing to overwrite unmanaged launcher: $launcher"
      command_preflight_failed=1
    fi
  done
  [ "$command_preflight_failed" -eq 0 ]
}

install_launchers() {
  repo_bin="$REPO_ROOT/bin"
  if [ ! -d "$repo_bin" ]; then
    fail "missing repo bin directory: $repo_bin"
    return
  fi

  if [ -z "${python_exe:-}" ] || [ -z "${escaped_python:-}" ] || [ -z "${escaped_classifier:-}" ]; then
    fail "missing preflighted Python runtime or classifier path for launchers"
    return
  fi

  all_wrappers_ready=1
  for wrapper in $MANAGED_LAUNCHERS; do
    target="$repo_bin/$wrapper"
    resolved="$(command -v "$wrapper" 2>/dev/null || true)"
    if ! is_expected_wrapper "$resolved" "$target" "$target"; then
      all_wrappers_ready=0
    fi
  done
  if [ "$all_wrappers_ready" -eq 1 ]; then
    ok "wrapper commands resolve to this checkout"
  fi

  mkdir -p "$LAUNCHER_DIR" || {
    fail "could not create launcher directory: $LAUNCHER_DIR"
    return
  }

  for wrapper in $MANAGED_LAUNCHERS; do
    target="$repo_bin/$wrapper"
    launcher="$LAUNCHER_DIR/$wrapper"
    if [ ! -f "$target" ]; then
      fail "missing wrapper: $target"
      continue
    fi
    if [ -L "$launcher" ]; then
      fail "refusing to overwrite symlinked launcher: $launcher"
      continue
    fi
    if ! launcher_is_managed "$launcher" "$wrapper"; then
      fail "refusing to overwrite unmanaged launcher: $launcher"
      continue
    fi
    escaped_target="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$target")" || {
      fail "could not quote launcher target: $target"
      continue
    }
    second_vendor_cmd=""
    second_vendor_env=""
    case "$wrapper" in
      claude_wrapper.py)
        vendor_cmd="claude"
        vendor_env="TRIAD_CLAUDE_BIN"
        ;;
      codex_wrapper.py)
        vendor_cmd="codex"
        vendor_env="TRIAD_CODEX_BIN"
        ;;
      antigravity_wrapper.py)
        vendor_cmd="agy"
        vendor_env="TRIAD_AGY_BIN"
        ;;
      gemini_wrapper.py)
        vendor_cmd="gemini"
        vendor_env="TRIAD_GEMINI_BIN"
        ;;
      review_round.py)
        vendor_cmd="agy"
        vendor_env="TRIAD_AGY_BIN"
        second_vendor_cmd="gemini"
        second_vendor_env="TRIAD_GEMINI_BIN"
        ;;
      *)
        fail "unknown wrapper: $wrapper"
        continue
        ;;
    esac
    vendor_path=""
    if command -v "$vendor_cmd" >/dev/null 2>&1; then
      vendor_path="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$(command -v "$vendor_cmd")")" || {
        fail "could not resolve vendor binary for $wrapper: $vendor_cmd"
        continue
      }
    fi
    escaped_vendor_env="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$vendor_env")" || {
      fail "could not quote vendor env for $wrapper"
      continue
    }
    escaped_vendor_path="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$vendor_path")" || {
      fail "could not quote vendor path for $wrapper"
      continue
    }
    second_vendor_path=""
    escaped_second_vendor_env=""
    escaped_second_vendor_path=""
    if [ -n "$second_vendor_cmd" ]; then
      if command -v "$second_vendor_cmd" >/dev/null 2>&1; then
        second_vendor_path="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$(command -v "$second_vendor_cmd")")" || {
          fail "could not resolve second vendor binary for $wrapper: $second_vendor_cmd"
          continue
        }
      fi
      escaped_second_vendor_env="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$second_vendor_env")" || {
        fail "could not quote second vendor env for $wrapper"
        continue
      }
      escaped_second_vendor_path="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$second_vendor_path")" || {
        fail "could not quote second vendor path for $wrapper"
        continue
      }
    fi
    escaped_path="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${PATH:-/usr/bin:/bin}")" || {
      fail "could not quote launcher PATH for $wrapper"
      continue
    }
    temp_launcher="$(mktemp "${TMPDIR:-/tmp}/triad-${wrapper}.XXXXXX")" || {
      fail "could not create temporary launcher: $launcher"
      continue
    }
    {
      printf '#!%s -E\n' "$python_exe"
      printf '# agy-triad-dispatch managed launcher\n'
      printf 'import os\n'
      printf 'import sys\n'
      printf '_SCRUB = (\n'
      printf '    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",\n'
      printf '    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",\n'
      printf '    "NODE_OPTIONS", "NODE_PATH",\n'
      printf '    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",\n'
      printf '    "BASH_ENV", "ENV", "PERL5LIB", "RUBYOPT", "RUBYLIB",\n'
      printf ')\n'
      printf 'env = {k: v for k, v in os.environ.items() if k not in _SCRUB}\n'
      printf 'env["PATH"] = %s\n' "$escaped_path"
      printf 'env["TRIAD_AUDIT_REDACT_PROMPTS"] = "1"\n'
      printf 'env["TRIAD_CLASSIFIER_EXTENSION"] = %s\n' "$escaped_classifier"
      printf 'env["TRIAD_REQUIRE_PINNED_VENDOR"] = "1"\n'
      if [ -n "$vendor_path" ]; then
        printf 'env[%s] = %s\n' "$escaped_vendor_env" "$escaped_vendor_path"
      else
        printf 'env.pop(%s, None)\n' "$escaped_vendor_env"
      fi
      if [ -n "$second_vendor_env" ]; then
        if [ -n "$second_vendor_path" ]; then
          printf 'env[%s] = %s\n' "$escaped_second_vendor_env" "$escaped_second_vendor_path"
        else
          printf 'env.pop(%s, None)\n' "$escaped_second_vendor_env"
        fi
      fi
      printf 'os.execve(%s, [%s, "-E", %s] + sys.argv[1:], env)\n' "$escaped_python" "$escaped_python" "$escaped_target"
    } >"$temp_launcher" || {
      rm -f "$temp_launcher"
      fail "could not write launcher: $launcher"
      continue
    }
    if ! chmod 0755 "$temp_launcher"; then
      rm -f "$temp_launcher"
      fail "could not chmod launcher: $launcher"
      continue
    fi
    queue_command_artifact "$wrapper" launcher "$launcher" "$temp_launcher"
  done

  if ! path_has_dir "$LAUNCHER_DIR"; then
    fail "launcher directory is not on PATH: $LAUNCHER_DIR"
    return
  fi
}

verify_installed_launchers() {
  repo_bin="$REPO_ROOT/bin"
  for wrapper in $MANAGED_LAUNCHERS; do
    target="$repo_bin/$wrapper"
    launcher="$LAUNCHER_DIR/$wrapper"
    resolved="$(command -v "$wrapper" 2>/dev/null || true)"
    if ! is_expected_wrapper "$resolved" "$target" "$launcher"; then
      fail "wrapper command is shadowed or stale on PATH: $wrapper resolves to ${resolved:-<missing>} (expected $launcher or $target)"
    fi
  done

  if [ "$errors" -eq 0 ]; then
    ok "launcher scripts installed and active on PATH: $LAUNCHER_DIR"
  fi
}

ensure_log_dir() {
  dir="$REPO_ROOT/bin/_logs"
  mkdir -p "$dir" || {
    fail "could not create runtime log directory: $dir"
    return
  }
  ok "runtime log directory exists: $dir"
}

remove_codex_config_fragment() {
  if [ "$errors" -ne 0 ]; then
    return
  fi
  config_path="$BOOTSTRAP_CODEX_ROOT/config.toml"
  remove_err="$(mktemp "${TMPDIR:-/tmp}/triad-config-remove-err.XXXXXX")" || {
    fail "could not create temporary file for config fragment removal stderr"
    return 1
  }
  remove_status="$(python3 "$REPO_ROOT/bin/bootstrap_repair.py" config-fragment \
    --action remove --path "$config_path" 2>"$remove_err")"
  remove_rc=$?
  if [ "$remove_rc" -ne 0 ]; then
    fail "config fragment removal helper failed for $config_path"
    while IFS= read -r line; do
      [ -n "$line" ] && printf '[error] %s\n' "$line" >&2
    done <"$remove_err"
    rm -f "$remove_err"
    return 1
  fi
  if [ -s "$remove_err" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && warn "$line"
    done <"$remove_err"
  fi
  rm -f "$remove_err"
  case "$remove_status" in
    removed | removed-file)
      ok "removed managed [shell_environment_policy] fragment from $config_path"
      ;;
    absent | not-managed)
      :
      ;;
    unrecognized-managed | unreadable | writeerror)
      warn "could not remove the managed [shell_environment_policy] fragment from $config_path"
      ;;
    *)
      warn "could not remove the managed [shell_environment_policy] fragment from $config_path"
      ;;
  esac
}

remove_legacy_permission_artifacts() {
  if ! run_repair_lifecycle remove; then
    return 1
  fi

  rules_path="$BOOTSTRAP_CODEX_ROOT/rules/$CODEX_RULES_NAME"
  if ! remove_owned_artifact \
    rules \
    "$rules_path" \
    "removed Codex command rules: $rules_path" \
    "leaving unmanaged Codex rules file in place: $rules_path"; then
    return 1
  fi

  profile_path="$BOOTSTRAP_CODEX_ROOT/$CODEX_PROFILE_NAME.config.toml"
  if ! remove_owned_artifact \
    profile \
    "$profile_path" \
    "removed Codex runtime profile: $profile_path" \
    "leaving unmanaged Codex profile in place: $profile_path"; then
    return 1
  fi

  if ! remove_codex_config_fragment; then
    return 1
  fi

  shell_remove_status="$(python3 "$REPO_ROOT/bin/bootstrap_repair.py" shell-entry \
    --action remove --path "$SHELL_RC")"
  if [ "$?" -ne 0 ]; then
    fail "could not remove managed shell entry from $SHELL_RC"
    return 1
  fi
  case "$shell_remove_status" in
    removed)
      ok "removed managed shell entry from $SHELL_RC"
      ;;
    unmanaged)
      warn "leaving unmanaged shell entry in place: $SHELL_RC"
      ;;
    absent)
      :
      ;;
    *)
      fail "unexpected shell remove status for $SHELL_RC: $shell_remove_status"
      return 1
      ;;
  esac
  return 0
}

run_remove() {
  if ! command -v python3 >/dev/null 2>&1; then
    fail "missing required binary: python3"
    return
  fi
  canonicalize_path_inputs
  if [ "$errors" -ne 0 ]; then
    return
  fi

  if ! run_repair_lifecycle preflight-remove; then
    return
  fi

  if ! remove_legacy_permission_artifacts; then
    return
  fi

  if ! begin_command_group; then
    return
  fi
  for wrapper in $MANAGED_LAUNCHERS; do
    launcher="$LAUNCHER_DIR/$wrapper"
    queue_command_removal "$wrapper" launcher "$launcher"
  done
  for runtime_command in triad-setup triad-doctor; do
    target="$LAUNCHER_DIR/$runtime_command"
    queue_command_removal "$runtime_command" runtime "$target"
  done
  if [ "$errors" -ne 0 ]; then
    rm -f "$COMMAND_MANIFEST"
    COMMAND_MANIFEST=""
    return
  fi
  if ! remove_command_group; then
    return
  fi

  for name in claude-wrapper-repair gemini-wrapper-repair agy-wrapper-repair; do
    agent_file="$BOOTSTRAP_CODEX_ROOT/agents/$name.toml"
    if [ -L "$agent_file" ]; then
      warn "leaving legacy repair-agent symlink in place (never following a symlink target): $agent_file"
      continue
    fi
    remove_owned_artifact \
      legacy-agent \
      "$agent_file" \
      "removed managed repair agent: $agent_file" \
      "preserving unmanaged repair agent: $agent_file"
  done

  if [ -e "$CLASSIFIER_PATH" ]; then
    ok "classifier patches preserved (learned classifications): $CLASSIFIER_PATH — remove manually to reset"
  fi
}

preflight_classifier() {
  classifier_status="$(python3 "$REPO_ROOT/bin/bootstrap_repair.py" classifier \
    --action preflight --path "$CLASSIFIER_PATH")"
  if [ "$?" -ne 0 ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && fail "$line"
    done <<EOF
$classifier_status
EOF
    return 1
  fi
  case "$classifier_status" in
    absent | ready)
      return 0
      ;;
    *)
      fail "unexpected classifier preflight status for $CLASSIFIER_PATH: $classifier_status"
      return 1
      ;;
  esac
}

ensure_classifier() {
  classifier_err="$(mktemp "${TMPDIR:-/tmp}/triad-classifier-err.XXXXXX")" || {
    fail "could not create temporary file for classifier stderr"
    return 1
  }
  classifier_status="$(python3 "$REPO_ROOT/bin/bootstrap_repair.py" classifier \
    --action ensure --path "$CLASSIFIER_PATH" 2>"$classifier_err")"
  classifier_rc=$?
  if [ "$classifier_rc" -ne 0 ]; then
    fail "classifier ensure helper failed for $CLASSIFIER_PATH"
    while IFS= read -r line; do
      [ -n "$line" ] && printf '[error] %s\n' "$line" >&2
    done <"$classifier_err"
    rm -f "$classifier_err"
    return 1
  fi
  if [ -s "$classifier_err" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && warn "$line"
    done <"$classifier_err"
  fi
  rm -f "$classifier_err"
  case "$classifier_status" in
    created | ready)
      ok "classifier file is writable JSON: $CLASSIFIER_PATH"
      ;;
    *)
      fail "unexpected classifier ensure status for $CLASSIFIER_PATH: $classifier_status"
      return 1
      ;;
  esac
  return 0
}

if ! validate_bootstrap_inputs; then
  printf '[error] bootstrap input validation failed with %s issue(s)\n' "$errors" >&2
  exit 1
fi

if [ "$MODE" = "remove" ]; then
  printf 'agy-triad-dispatch bootstrap remove\n'
  run_remove
  if [ "$errors" -eq 0 ]; then
    ok "bootstrap remove completed"
    exit 0
  fi
  printf '[error] bootstrap remove failed with %s issue(s)\n' "$errors" >&2
  exit 1
fi

printf 'agy-triad-dispatch bootstrap install\n'
check_binary claude
check_binary codex
check_google_route
if [ "${TRIAD_BOOTSTRAP_REQUIRE_GEMINI:-0}" = "1" ]; then
  check_binary gemini
elif [ "$GOOGLE_ROUTE" = "agy" ]; then
  check_optional_binary gemini
fi
check_python
check_library_adoption
if [ "$errors" -eq 0 ]; then
  canonicalize_path_inputs
fi
if [ "$errors" -eq 0 ]; then
  check_workspace_escape
fi
if [ "$errors" -eq 0 ]; then
  resolve_python_runtime
fi
if [ "$errors" -eq 0 ]; then
  check_formal_schema_dependency
fi
printf 'repo root: %s\n' "$REPO_ROOT"

if [ "$errors" -ne 0 ]; then
  fail "required prerequisite checks failed; skipping installation steps"
  exit 1
fi

if ! run_repair_lifecycle preflight-remove; then
  printf '[error] legacy repair cleanup preflight failed; skipping installation steps\n' >&2
  exit 1
fi

if ! preflight_install_command_targets; then
  printf '[error] managed command preflight failed; skipping installation steps\n' >&2
  exit 1
fi
if ! preflight_classifier; then
  printf '[error] classifier preflight failed; skipping installation steps\n' >&2
  exit 1
fi
if ! remove_legacy_permission_artifacts; then
  printf '[error] legacy plugin artifact cleanup failed; skipping installation steps\n' >&2
  exit 1
fi

ensure_log_dir
if ! ensure_classifier; then
  printf '[error] classifier installation failed; skipping remaining installation steps\n' >&2
  exit 1
fi

if begin_command_group; then
  install_launchers
  if [ "$errors" -eq 0 ]; then
    publish_command_group
    if [ "$errors" -eq 0 ]; then
      verify_installed_launchers
    fi
  else
    python3 - "$COMMAND_MANIFEST" <<'PY'
import json
from pathlib import Path
import sys

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        Path(json.loads(line)["data_path"]).unlink(missing_ok=True)
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        pass
PY
    rm -f "$COMMAND_MANIFEST"
    COMMAND_MANIFEST=""
  fi
fi

if [ "$errors" -ne 0 ]; then
  fail "managed command installation failed; skipping remaining installation steps"
  exit 1
fi

if [ "$errors" -eq 0 ]; then
  print_owner_apply_argv
  ok "bootstrap install passed"
  exit 0
fi

fail_count="$errors"
printf '[error] bootstrap install failed with %s issue(s)\n' "$fail_count" >&2
exit 1
