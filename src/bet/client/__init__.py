"""The BET API client: HTTP calls, error mapping and the token store (ADR 0002 section 3).

What the CLI's remote mode is built on. No Typer and no Rich: rendering stays
in ``bet.cli``, which turns these results into the same output local mode
gives.
"""
