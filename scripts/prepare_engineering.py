"""Create local development credentials without changing global Java settings."""

from pathlib import Path
import os
import secrets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / ".secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    private_path, public_path = folder / "jwt-private.pem", folder / "jwt-public.pem"
    if private_path.exists() != public_path.exists():
        raise SystemExit(
            "Incomplete key pair; restore the missing key before continuing."
        )
    if not private_path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        public_path.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        # The protected parent directory restricts host access. Container secrets
        # must remain readable by the backend's unprivileged runtime user.
        os.chmod(private_path, 0o644)
    environment = ROOT / ".env.engineering"
    if not environment.exists():
        template = (ROOT / ".env.engineering.example").read_text(encoding="utf-8")
        for placeholder in (
            "replace-with-a-new-random-password",
            "replace-with-a-different-random-password",
            "replace-with-a-new-random-secret",
        ):
            template = template.replace(placeholder, secrets.token_hex(32), 1)
        environment.write_text(template, encoding="utf-8")
        os.chmod(environment, 0o600)
    print(
        "Development key pair and .env.engineering are ready; existing files were preserved."
    )


if __name__ == "__main__":
    main()
