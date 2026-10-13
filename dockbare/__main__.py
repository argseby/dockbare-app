"""Command line entry point: ``python -m dockbare <command>``."""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys


def cmd_serve(args) -> int:
    from waitress import serve

    from .app import create_app

    app = create_app()
    core = app.extensions["dockbare"]
    s = core.settings
    print(f"Dockbare listening on http://{s.host}:{s.port}", flush=True)
    try:
        serve(app, host=s.host, port=s.port, threads=args.threads, channel_timeout=600, max_request_body_size=64 * 1024 ** 3,
              ident="dockbare", clear_untrusted_proxy_headers=not s.behind_proxy)
    finally:
        core.stop()
    return 0


def cmd_hash_password(args) -> int:
    from werkzeug.security import generate_password_hash

    pw = args.password or getpass.getpass("Password: ")
    if not args.password and getpass.getpass("Repeat: ") != pw:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(pw) < 12:
        print("Warning: use at least 12 characters.", file=sys.stderr)
    print("DOCKBARE_ADMIN_PASSWORD_HASH=" + generate_password_hash(pw, method="scrypt"))
    return 0


def cmd_totp_setup(args) -> int:
    import pyotp

    secret = args.secret or pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=args.user, issuer_name="Dockbare")
    print("1. Add this to your .env / stack environment and restart:\n")
    print(f"   DOCKBARE_TOTP_SECRET={secret}\n")
    print("2. Scan this with your authenticator app (or enter the secret manually):\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001
        pass
    print(f"\n   {uri}\n")
    print("3. Log in with user, password and the 6-digit code. The Security tab shows the QR again for a second device.")
    return 0


def cmd_gen_secret(_args) -> int:
    print("DOCKBARE_SECRET_KEY=" + secrets.token_urlsafe(48))
    print("DOCKBARE_ENCRYPTION_KEY=" + secrets.token_urlsafe(32))
    return 0


def cmd_decrypt(args) -> int:
    """Standalone decryption of a .tar.gz.enc file (no app needed)."""
    from . import crypto

    key = os.environ.get("DOCKBARE_ENCRYPTION_KEY") or getpass.getpass("Encryption key: ")
    keyring = crypto.Keyring(key)
    src = args.input
    dst = args.output or (src[:-4] if src.endswith(".enc") else src + ".dec")
    if os.path.exists(dst) and not args.force:
        print(f"{dst} exists (use --force).", file=sys.stderr)
        return 1

    def chunks():
        with open(src, "rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    break
                yield b

    try:
        with open(dst, "wb") as out:
            for pt in crypto.decrypt_stream(chunks(), keyring):
                out.write(pt)
    except crypto.CryptoError as e:
        print(f"Error: {e}", file=sys.stderr)
        try:
            os.remove(dst)
        except OSError:
            pass
        return 1
    print(f"Decrypted to {dst}")
    return 0


def cmd_check(_args) -> int:
    from .config import load_settings
    from .dockerctl import DockerCtl

    s = load_settings()
    ok = True
    print(f"env file:        {s.env_file or '(none found)'}")
    print(f"data dir:        {s.data_dir}")
    print(f"backup dir:      {s.backup_dir}")
    print(f"admin password:  {'configured' if s.auth_configured else 'MISSING (DOCKBARE_ADMIN_PASSWORD)'}")
    print(f"TOTP:            {'enabled' if s.totp_enabled else 'off'}")
    print(f"encryption key:  {'configured' if s.encryption_configured else 'MISSING (backups will be plaintext)'}")
    print(f"secret key:      {'generated (put it into .env)' if s.generated_secret else 'from env'}")
    d = DockerCtl(s.helper_image)
    info = d.info()
    print(f"docker:          {'ok ' + str(info.get('version')) if info.get('ok') else 'FAILED ' + str(info.get('error'))}")
    ok = ok and s.auth_configured and info.get("ok", False)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dockbare", description="Dockbare — Docker volume backup")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", help="run the web server (default)")
    sp.add_argument("--threads", type=int, default=8)
    sp.set_defaults(fn=cmd_serve)
    hp = sub.add_parser("hash-password", help="print a password hash for DOCKBARE_ADMIN_PASSWORD_HASH")
    hp.add_argument("--password", help="password (prompted when omitted)")
    hp.set_defaults(fn=cmd_hash_password)
    tp = sub.add_parser("totp-setup", help="generate a TOTP secret for two-factor login")
    tp.add_argument("--user", default=os.environ.get("DOCKBARE_ADMIN_USER", "admin"))
    tp.add_argument("--secret", help="show the QR for an existing secret instead of generating one")
    tp.set_defaults(fn=cmd_totp_setup)
    sub.add_parser("gen-secret", help="print random values for DOCKBARE_SECRET_KEY / DOCKBARE_ENCRYPTION_KEY").set_defaults(fn=cmd_gen_secret)
    dp = sub.add_parser("decrypt", help="decrypt a .tar.gz.enc backup without the app")
    dp.add_argument("input")
    dp.add_argument("output", nargs="?")
    dp.add_argument("--force", action="store_true")
    dp.set_defaults(fn=cmd_decrypt)
    sub.add_parser("check", help="validate configuration and Docker access").set_defaults(fn=cmd_check)
    args = p.parse_args(argv)
    if not args.cmd:
        args.threads = 8
        return cmd_serve(args)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
