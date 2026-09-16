# -*- coding: utf-8 -*-
"""Small, secret-free helpers used by the Windows build script.

Signing is opt-in.  The helper reads certificate selectors from environment
variables, never writes them to build metadata, and verifies the resulting EXE
before the build is reported as successful.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from app_meta import APP_CHANNEL, APP_VERSION, BUILD_DATE, EXE_BASENAME


class BuildConfigError(RuntimeError):
    pass


def signing_configuration(env=None):
    env = os.environ if env is None else env
    thumbprint = str(env.get("PBOOT_SIGN_CERT_SHA1", "") or "").replace(" ", "")
    pfx = str(env.get("PBOOT_SIGN_PFX", "") or "").strip()
    if thumbprint and pfx:
        raise BuildConfigError(
            "PBOOT_SIGN_CERT_SHA1 与 PBOOT_SIGN_PFX 只能选择一种")
    if thumbprint and not re.fullmatch(r"[0-9A-Fa-f]{40}", thumbprint):
        raise BuildConfigError("PBOOT_SIGN_CERT_SHA1 必须是 40 位 SHA-1 指纹")
    timestamp = str(env.get("PBOOT_SIGN_TIMESTAMP_URL", "") or "").strip()
    if timestamp:
        parsed = urlparse(timestamp)
        if (parsed.scheme.lower() not in ("http", "https") or
                not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.fragment):
            raise BuildConfigError("PBOOT_SIGN_TIMESTAMP_URL 不是安全的 HTTP(S) 地址")
    if thumbprint:
        method = "certificate-store"
    elif pfx:
        method = "pfx"
    else:
        method = "unsigned"
    return {
        "requested": method != "unsigned",
        "method": method,
        "thumbprint": thumbprint,
        "pfx": pfx,
        "pfx_password": str(env.get("PBOOT_SIGN_PFX_PASSWORD", "") or ""),
        "timestamp_url": timestamp,
        "signtool": str(env.get("PBOOT_SIGNTOOL", "") or "").strip(),
    }


def write_build_metadata(output, env=None):
    env = os.environ if env is None else env
    config = signing_configuration(env)
    manifest_url = str(
        env.get("PBOOT_PUBLISHER_UPDATE_MANIFEST_URL", "") or "").strip()
    if manifest_url:
        parsed = urlparse(manifest_url)
        if (parsed.scheme.lower() != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.fragment):
            raise BuildConfigError(
                "PBOOT_PUBLISHER_UPDATE_MANIFEST_URL 必须是安全的 HTTPS 地址")
    payload = {
        "schema_version": 1,
        "version": APP_VERSION,
        "channel": APP_CHANNEL,
        "build_date": BUILD_DATE,
        # Certificate paths, thumbprints, passwords and timestamps are
        # intentionally omitted.  Runtime performs the authoritative check.
        "signing": {
            "requested": config["requested"],
            "method": config["method"],
        },
        "update": {
            "manifest_url": manifest_url,
            "check_is_manual": True,
            "automatic_download": False,
            "automatic_install": False,
        },
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def _find_signtool(config):
    explicit = config.get("signtool")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise BuildConfigError(f"找不到 PBOOT_SIGNTOOL: {path}")
    found = shutil.which("signtool.exe") or shutil.which("signtool")
    if found:
        return found
    kits_root = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) \
        / "Windows Kits" / "10" / "bin"
    candidates = sorted(kits_root.glob("*/x64/signtool.exe"), reverse=True)
    if candidates:
        return str(candidates[0])
    raise BuildConfigError(
        "未找到 signtool.exe；请安装 Windows SDK 或设置 PBOOT_SIGNTOOL")


def _clean_tool_output(value, secret=""):
    text = str(value or "")
    if secret:
        text = text.replace(secret, "***")
    return text.strip()[-4000:]


def sign_executable(executable, env=None):
    config = signing_configuration(env)
    path = Path(executable).resolve()
    if not path.is_file():
        raise BuildConfigError(f"待签名 EXE 不存在: {path}")
    if not config["requested"]:
        return {"signed": False, "method": "unsigned", "path": str(path)}
    tool = _find_signtool(config)
    command = [tool, "sign", "/fd", "SHA256"]
    if config["method"] == "certificate-store":
        command.extend(["/sha1", config["thumbprint"]])
    else:
        pfx = Path(config["pfx"]).expanduser().resolve()
        if not pfx.is_file():
            raise BuildConfigError(f"签名证书不存在: {pfx}")
        command.extend(["/f", str(pfx), "/p", config["pfx_password"]])
    if config["timestamp_url"]:
        command.extend(["/tr", config["timestamp_url"], "/td", "SHA256"])
    command.append(str(path))
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=180, check=False)
    if completed.returncode:
        output = _clean_tool_output(
            (completed.stdout or "") + "\n" + (completed.stderr or ""),
            config["pfx_password"])
        raise BuildConfigError(f"signtool 签名失败 ({completed.returncode}): {output}")
    verify = subprocess.run(
        [tool, "verify", "/pa", "/all", str(path)],
        capture_output=True, text=True, timeout=60, check=False)
    if verify.returncode:
        output = _clean_tool_output((verify.stdout or "") + "\n" +
                                    (verify.stderr or ""))
        raise BuildConfigError(f"signtool 验证失败 ({verify.returncode}): {output}")
    return {"signed": True, "method": config["method"], "path": str(path)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("exe-basename")
    metadata = sub.add_parser("metadata")
    metadata.add_argument("--output", required=True)
    sign = sub.add_parser("sign")
    sign.add_argument("--exe", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "exe-basename":
            print(EXE_BASENAME)
        elif args.command == "metadata":
            path = write_build_metadata(args.output)
            print(f"[BUILD] metadata: {path}")
        elif args.command == "sign":
            result = sign_executable(args.exe)
            if result["signed"]:
                print(f"[SIGN] Authenticode verified ({result['method']})")
            else:
                print("[SIGN] unsigned build (no certificate configured)")
        return 0
    except (BuildConfigError, OSError, subprocess.SubprocessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
