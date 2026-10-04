"""Create local development credentials without changing global Java settings."""

from pathlib import Path
import os
import secrets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import dotenv_values

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
        "Development database credentials and key pair are ready; existing files were preserved."
    )
    configuration = {**dotenv_values(environment), **os.environ}
    missing = []
    if not str(configuration.get("HOMMEY_API_KEY") or "").strip():
        missing.append("HOMMEY_API_KEY")
    if configuration.get(
        "HOMMEY_RAG_EMBEDDING_BACKEND", "siliconflow"
    ) != "local" and not (
        configuration.get("HOMMEY_EMBEDDING_API_KEY")
        or configuration.get("SILICONFLOW_API_KEY")
    ):
        missing.append("HOMMEY_EMBEDDING_API_KEY")
    if missing:
        print("AI configuration still requires: " + ", ".join(missing))
    print("After starting the stack, build the knowledge index and check AI /readyz.")


if __name__ == "__main__":
    main()
