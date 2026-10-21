"""TIGARSEC's PUBLIC licence-signing keys. CourseCraft uses these to CHECK licences and console
sign-ins; they can't be used to make either (that needs the private key, which only TIGARSEC
holds, in the separate tigarsec-licensing repo - never here).

Fill this in once, from TIGARSEC's own Mac:
    python ccl.py init          (in the tigarsec-licensing folder)
copies a line like  "k1": "AbC...=",  - paste it below, commit, deploy.

When TIGARSEC ever replaces its signing key, ADD the new one ("k2": ...) and keep the old one,
so licences already issued keep working.
"""
TRUSTED_PUBLIC_KEYS = {
}
