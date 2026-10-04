"""Invitation codes remain secret in storage and tolerant of copy/paste formatting."""
import re

from webui_new.auth.invites import generate_invite_code, invite_digest


def test_generated_invites_are_distinct_and_easy_to_copy():
    codes = {generate_invite_code() for _ in range(100)}
    assert len(codes) == 100
    assert all(re.fullmatch(r"HMY(?:-[A-HJ-NP-Z2-9]{5}){4}", code) for code in codes)


def test_digest_hides_plaintext_and_accepts_grouping_variations():
    code = generate_invite_code()
    digest = invite_digest(code)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert code not in digest
    assert invite_digest(f"  {code.lower().replace('-', ' ')}  ") == digest
    assert invite_digest(generate_invite_code()) != digest
