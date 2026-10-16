"""Read-only integrity checks for the generated, bundled shared library."""
import hashlib
import json
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "087af94a7078de0ae9701fb057840b5492f51f66"
VERSION = "0.1.0.dev4"
# SHA-256 of the canonical full package + MIT-license path/digest inventory
# inspected at COMMIT. Independent of generated adoption records and wheel ZIP
# timestamps; changing source content requires an explicit reviewed pin update.
APPROVED_CONTENT_SHA256 = "5cf26be0ab354c0565ade6d5f04971ff3689a6188b648d70edcffd6474b5c1f2"
PACKAGE = "bin/triad_dispatch_lib"
LICENSE = "vendor/triad-dispatch-lib.LICENSE"
RECORD = "contracts/library-adoption.json"
MANIFESTS = {"receipt": "data/receipt-bundle/source-manifest.json", "verdict": "data/source-manifest.json", "roster": "data/roster-bundle/source-manifest.json", "prompt": "data/prompt-bundle/source-manifest.json"}
MODULES = {"receipt", "__init__", "agreement", "bundle_manifest", "contracts", "dispatch", "errors", "json_input", "output", "prompts", "roster", "verdict"}


def verify_approved_content(digests):
    canonical = json.dumps(digests, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != APPROVED_CONTENT_SHA256:
        raise ValueError("package or license differs from approved content")


def regular_bytes(root, name):
    """Refuse links and special files at every component before reading."""
    path = root
    for part in Path(name).parts:
        path /= part
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"unsafe managed path: {name}")
    if not stat.S_ISREG(mode):
        raise ValueError(f"not a regular file: {name}")
    return path.read_bytes()


def bundle_ids(files):
    expected = {f"{PACKAGE}/{module}.py" for module in MODULES} | {LICENSE}
    result = {}
    for kind, name in MANIFESTS.items():
        expected.add(f"{PACKAGE}/{name}")
        manifest = json.loads(files[f"{PACKAGE}/{name}"])
        digests = manifest["sha256"]
        identity = hashlib.sha256(b"triad-dispatch-lib:bundle:v1\n" + json.dumps(digests, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        if manifest["contract_id"] != identity:
            raise ValueError("bundle identity mismatch")
        for relative, digest in digests.items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("unsafe bundle path")
            data = files[f"{PACKAGE}/{Path(name).parent}/{relative}"]
            expected.add(f"{PACKAGE}/{Path(name).parent}/{relative}")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("bundle resource mismatch")
        result[kind] = identity
    if set(files) != expected:
        raise ValueError("incomplete or unexpected pinned package inventory")
    return result


def verify_library(root=ROOT):
    """Verify the complete generated inventory and return adoption metadata.

    CPython bytecode and its numeric-suffix atomic-publication files are runtime
    cache inputs. Their disappearance is allowed; links and special files fail.
    """
    root = Path(root)
    try:
        metadata = json.loads(regular_bytes(root, RECORD))
        if (type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1
                or metadata["name"] != "triad-dispatch-lib" or metadata["version"] != VERSION
                or metadata["source_commit"] != COMMIT
                or not re.fullmatch(r"[0-9a-f]{64}", metadata["wheel_sha256"])):
            raise ValueError("unexpected library adoption identity")
        expected = metadata["sha256"]
        if not isinstance(expected, dict) or LICENSE not in expected:
            raise ValueError("invalid managed inventory")
        for name, digest in expected.items():
            if (not (name.startswith(PACKAGE + "/") or name == LICENSE)
                    or ".." in Path(name).parts or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("unsafe managed inventory")
        verify_approved_content(expected)
        regular_bytes(root, f"{PACKAGE}/__init__.py")
        actual = {LICENSE}
        for path in (root / PACKAGE).rglob("*"):
            cache_file = (
                (path.parent.name == "__pycache__" and path.suffix == ".pyc")
                or (path.parent == root / PACKAGE / "__pycache__"
                    and path.name.split(".", 1)[0] in MODULES
                    and re.fullmatch(r"[^.]+\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc\.[0-9]+", path.name))
            )
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                if cache_file:
                    continue
                raise
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError("unsafe generated file")
            relative = path.relative_to(root).as_posix()
            if stat.S_ISREG(mode):
                if cache_file:
                    continue
                actual.add(relative)
            elif path.name != "__pycache__" and not any(name.startswith(relative + "/") for name in expected):
                raise ValueError("unexpected generated directory: " + json.dumps(relative, ensure_ascii=True))
        if actual != set(expected):
            raise ValueError("generated inventory mismatch: " + json.dumps({
                "missing": sorted(set(expected) - actual),
                "unexpected": sorted(actual - set(expected)),
            }, ensure_ascii=True))
        files = {name: regular_bytes(root, name) for name in expected}
        if any(hashlib.sha256(data).hexdigest() != expected[name] for name, data in files.items()):
            raise ValueError("generated hash mismatch")
        if bundle_ids(files) != metadata["bundle_ids"]:
            raise ValueError("adoption bundle identity mismatch")
        return metadata
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid library adoption: {exc}") from exc
