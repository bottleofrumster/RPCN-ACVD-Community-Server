"""Compile retained RPCS3 lifecycle methods with an isolated transport test double.

This is a unit boundary check, not a complete RPCS3 build or native gameplay
qualification. No emulator, account, network transport or savedata is opened.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

METHODS = (
    "remove_sig_ctx", "clear_sig_ctx", "signal_sig_callback", "signal_ext_sig_callback",
    "stop_sig", "disconnect_sig2_users", "init_sig1", "update_si_status",
)


def methods(source: str) -> str:
    """Extract complete top-level methods; refuse ambiguous/missing definitions."""
    chunks = []
    for name in METHODS:
        starts = [
            start for start in (f"void signaling_handler::{name}(", f"u32 signaling_handler::{name}(")
            if source.count(start) == 1
        ]
        if len(starts) != 1:
            raise ValueError(f"Expected exactly one method: {name}")
        start = source.index(starts[0])
        # The retained upstream formatting puts only the method's closing
        # brace at column zero. Nested scopes are indented.
        end = source.index("\n}", start) + 2
        chunks.append(source[start:end])
    return "\n\n".join(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    source = args.checkout / "rpcs3/Emu/NP/signaling_handler.cpp"
    implementation = methods(source.read_text())
    fixture = Path(__file__).with_name("rpcs3_signaling_fixture.cpp.in").read_text()
    assert fixture.count("// ACTUAL_UPSTREAM_METHODS") == 1
    fixture = fixture.replace("// ACTUAL_UPSTREAM_METHODS", implementation)
    # The baseline overload adapters preserve its unscoped behavior and let
    # exactly the same tests demonstrate the pre-patch failure.
    if "init_sig1(u32 ctx_id," not in implementation:
        fixture = fixture.replace(
            "void signal_sig_callback(u32, s32, s32, std::optional<u32> = {});",
            "void signal_sig_callback(u32, s32, s32);",
        ).replace(
            "void signal_ext_sig_callback(u32, s32, s32, std::optional<u32> = {}) const;",
            "void signal_ext_sig_callback(u32, s32, s32) const;",
        )
        fixture += "\nu32 signaling_handler::init_sig1(u32, const SceNpId& id) { return init_sig1(id); }\n"
        fixture += "void signaling_handler::stop_sig(u32, u32 id, bool force) { stop_sig(id, force); }\n"
    with tempfile.TemporaryDirectory(prefix="acvd-signaling-unit-") as work:
        cpp, exe = Path(work) / "check.cpp", Path(work) / "check"
        cpp.write_text(fixture)
        subprocess.run(
            ["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(exe)],
            check=True,
        )
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
