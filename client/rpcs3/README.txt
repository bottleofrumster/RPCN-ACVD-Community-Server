EXPERIMENTAL RPCS3 PATCH

Not a general re-sortie fix. Not included in the client install kit.
Do not replace a player's working emulator with this candidate.

signaling-context-ownership.patch keeps a shared peer connection alive while
another local signaling context still owns it. Real disconnects remain visible.

Base: RPCS3 c96c2099b717bac25dd47ed2fea60b3e202d2108
Source: https://github.com/RPCS3/rpcs3
Upstream GPL terms apply to this source patch.

BUILD
The checker requires g++ with C++20 support.

1. Use a separate checkout of the base commit.
   git apply --check /path/to/signaling-context-ownership.patch
   git apply /path/to/signaling-context-ownership.patch
2. From the ACVD source directory:
   python3 tools/analysis/check_rpcs3_signaling.py /path/to/rpcs3
3. Build using the upstream RPCS3 instructions.

STATUS
Linux build and lifecycle tests pass. Two opposing one-player teams completed
three PvP rounds with two re-sortie transitions on different maps.
Same-team NPC re-sortie still has an intermittent handoff failure.
Larger squads, hired mercenaries, Windows, mixed builds and PS3 are unqualified.
Use copied profiles for tests. Do not change player saves.
